"""Runner outreach_social: invio DM via browser automation (Instagram/TikTok).

Agent mode: `outreach_social`. Pipeline:
  1. Verifica AGENTSCRAPER_SECRET (cifratura credenziali)
  2. Carica account social attivi dal DB
  3. Carica contacts qualified con social[platform] popolato
  4. Genera messaggi personalizzati via LLM (Qwen locale, $0)
  5. Engine apre browser headed + stealth, fa DM con humanize
  6. Log in social_dm_log + update contacts.status='contacted' su ok
  7. Report finale

I selettori CSS di IG/TikTok sono FRAGILI per design: il modulo `social/instagram.py`
e `social/tiktok.py` vanno tenuti aggiornati. Aspettati manutenzione ogni 1-2 mesi.

Sicurezza: NON viene mai loggata la password in chiaro. Le sessioni Playwright
sono salvate in data/sessions/<uuid>.json (cookies + storage). Ricaricare
manualmente AGENTSCRAPER_SECRET in .env per cambiare la chiave di cifratura.
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .. import db
from ..config import RESULTS_DIR
from .blocked_domains import is_blocked
from .llm_providers import resolve_api_key, resolve_base_url
from .social.crypto_creds import decrypt, is_configured
from .social.engine import OutreachEngine
from .social.message_generator import MessageRequest, generate_batch
from .social.platform_base import HealthStatus, SocialAccount


log = logging.getLogger(__name__)


def _parse_message_template_variants(task: dict[str, Any]) -> list[str]:
    """Estrae lista di esempi-stile dal campo `message_template_variants`.

    Formato: righe separate da `---` (textarea-friendly). Fallback: campo
    `message_template` legacy come singolo esempio.
    """
    variants_raw = (task.get("message_template_variants") or "").strip()
    if variants_raw:
        chunks = [c.strip() for c in re.split(r"\n\s*-{3,}\s*\n", variants_raw) if c.strip()]
        cleaned = [c.strip().strip("-").strip() for c in chunks]
        cleaned = [c for c in cleaned if c]
        if cleaned:
            return cleaned[:10]
    single = (task.get("message_template") or "").strip()
    if single:
        return [single]
    return []


def _extract_username_from_url(url: str, platform: str) -> str | None:
    """Estrae l'username dalla URL social. None se non parseable."""
    if not url:
        return None
    try:
        p = urlparse(url)
        path = (p.path or "").strip("/")
    except Exception:
        return None
    if not path:
        return None
    if platform == "instagram":
        m = re.match(r"^([A-Za-z0-9._]+)(?:/.*)?$", path)
        return m.group(1) if m else None
    if platform == "tiktok":
        m = re.match(r"^@?([A-Za-z0-9._-]+)(?:/.*)?$", path)
        return m.group(1).lstrip("@") if m else None
    if platform == "facebook":
        # Pattern 1: facebook.com/<username>
        # Pattern 2: facebook.com/profile.php?id=<numeric>
        if path.startswith("profile.php") or path == "profile.php":
            # estraggo id dalla query string
            try:
                from urllib.parse import parse_qs
                q = parse_qs(p.query or "")
                idv = (q.get("id") or [""])[0]
                return idv or None
            except Exception:
                return None
        m = re.match(r"^([A-Za-z0-9.]+)(?:/.*)?$", path)
        return m.group(1) if m else None
    return None


def _load_targets_for_platform(
    task: dict[str, Any], platform: str, *, limit: int
) -> list[dict[str, Any]]:
    """Carica asset target per la piattaforma.

    Strategia:
      - Se `task.target_asset_ids` (o legacy `target_contact_ids`) non vuoto →
        usa SOLO quegli ID (bypassa il filtro per status: l'utente li ha
        scelti esplicitamente).
      - Altrimenti → tutti gli asset qualified (status='qualified') con
        `social_json[platform]` popolato.

    In entrambi i casi si tiene un social URL per la platform e si scartano
    quelli su host bloccati.
    """
    explicit_ids = task.get("target_asset_ids") or task.get("target_contact_ids") or []
    if isinstance(explicit_ids, str):
        try:
            explicit_ids = json.loads(explicit_ids) or []
        except (json.JSONDecodeError, TypeError):
            explicit_ids = []
    explicit_ids = [int(x) for x in explicit_ids if str(x).strip().lstrip("-").isdigit()]

    if explicit_ids:
        # Selezione manuale vince: ignora i filtri del task
        candidates = db.get_assets_by_ids(explicit_ids)
    else:
        # Applica i filtri di task (source_task_id, source_follower_of, tags) se valorizzati
        f_tid_raw = task.get("outreach_filter_source_task_id")
        f_tid = int(f_tid_raw) if str(f_tid_raw or "").strip().isdigit() else None
        f_fof = (task.get("outreach_filter_source_follower_of") or "").strip() or None
        f_tags_raw = task.get("outreach_filter_tags") or []
        if isinstance(f_tags_raw, str):
            try:
                f_tags_raw = json.loads(f_tags_raw) or []
            except Exception:
                f_tags_raw = []
        f_tags = [
            (t.get("key"), t.get("value")) for t in f_tags_raw
            if isinstance(t, dict) and t.get("key") and t.get("value")
        ]
        if f_fof:
            f_tags.append(("source_follower_of", f_fof))
        # list_assets_with_social_platform tiene fuori optedout, qui aggiungiamo
        # filtro qualified + tag/source_task_id via post-filter in Python (rapido
        # perche' candidates e' limitato).
        raw = db.list_assets_with_social_platform(platform=platform, limit=limit * 10)
        candidates = []
        for a in raw:
            if a.get("status") != "qualified":
                continue
            if f_tid is not None and a.get("source_task_id") != f_tid:
                continue
            if (a.get("outreach_status") or "") == "contacted":
                continue
            candidates.append(a)

    out: list[dict[str, Any]] = []
    for a in candidates:
        soc_raw = a.get("social_json")
        if not soc_raw:
            continue
        try:
            socials = json.loads(soc_raw)
        except (json.JSONDecodeError, TypeError):
            continue
        for s in (socials or []):
            if not isinstance(s, dict):
                continue
            if (s.get("platform") or "").lower() != platform:
                continue
            url = s.get("url") or ""
            if not url or is_blocked(url):
                continue
            username = _extract_username_from_url(url, platform)
            if username:
                out.append({"asset": a, "username": username, "social_url": url})
                break
        if len(out) >= limit:
            break
    return out


async def run_agent(task: dict[str, Any], job_id: int) -> str:
    """Entry-point outreach_social. Schema simmetrico agli altri runner."""
    db.update_job(job_id, status="running", started_at=db.now_iso())
    db.set_control_signal(job_id, None)

    def jlog(line: str) -> None:
        db.append_job_log(job_id, line)

    jlog(f'Avvio outreach_social per task #{task["id"]} "{task["name"]}"')

    # ---- 1. Validazioni ----
    if not is_configured():
        msg = (
            "AGENTSCRAPER_SECRET non settata in .env: cifratura credenziali "
            "disattivata, niente outreach. Abort."
        )
        jlog(f"❌ {msg}")
        db.update_job(job_id, status="error", error=msg, finished_at=db.now_iso())
        return ""

    platform = (task.get("social_platform") or "").strip().lower()
    if platform not in ("instagram", "tiktok", "facebook"):
        msg = f"social_platform '{platform}' non supportata. Usa 'instagram', 'tiktok' o 'facebook'."
        jlog(f"❌ {msg}")
        db.update_job(job_id, status="error", error=msg, finished_at=db.now_iso())
        return ""

    intent = (task.get("outreach_intent") or "").strip()
    if not intent:
        msg = "outreach_intent vuoto. Specifica lo scopo del DM nel task."
        jlog(f"❌ {msg}")
        db.update_job(job_id, status="error", error=msg, finished_at=db.now_iso())
        return ""

    # ---- 2. Carica account social attivi ----
    rows = db.list_social_accounts(platform=platform, status="active")
    if not rows:
        msg = (
            f"Nessun account '{platform}' in stato active. "
            f"Vai a /social/accounts per aggiungerli + fare warmup."
        )
        jlog(f"❌ {msg}")
        db.update_job(job_id, status="error", error=msg, finished_at=db.now_iso())
        return ""

    accounts: list[SocialAccount] = []
    account_id_by_uuid: dict[str, int] = {}
    for r in rows:
        try:
            password = decrypt(r["encrypted_password"])
        except Exception as e:
            jlog(f"  ⚠️ decrypt fail per {r['username']}: {e} — skip")
            continue
        accounts.append(SocialAccount(
            uuid=r["uuid"],
            platform=r["platform"],
            username=r["username"],
            password=password,
            proxy_label=r.get("proxy_label"),
            daily_dm_cap=int(r.get("daily_dm_cap") or 10),
            status=r.get("status") or "active",
        ))
        account_id_by_uuid[r["uuid"]] = r["id"]
    if not accounts:
        msg = "Nessun account decifrabile (chiave AGENTSCRAPER_SECRET mismatched?)."
        jlog(f"❌ {msg}")
        db.update_job(job_id, status="error", error=msg, finished_at=db.now_iso())
        return ""
    jlog(f"Caricati {len(accounts)} account {platform} attivi")

    # ---- 3. Target ----
    max_dms_per_run = int(task.get("max_dms_per_run") or 30)
    explicit_ids = task.get("target_asset_ids") or task.get("target_contact_ids") or []
    if explicit_ids:
        jlog(
            f"Selezione esplicita: {len(explicit_ids)} asset scelti nel task "
            f"(status filter bypassato)"
        )
    else:
        jlog("Selezione automatica: tutti gli asset qualified con URL per la platform")
    targets = _load_targets_for_platform(task, platform, limit=max_dms_per_run)
    if not targets:
        if explicit_ids:
            jlog(
                f"WARN Nessuno dei {len(explicit_ids)} asset selezionati ha un URL "
                f"{platform} valido / non bloccato. Niente da fare."
            )
        else:
            jlog(f"WARN Nessun target con {platform} URL fra asset qualified. Niente da fare.")
        db.update_job(job_id, status="done", finished_at=db.now_iso())
        return ""
    jlog(f"Target {platform}: {len(targets)} asset")

    # ---- 4. Genera messaggi personalizzati ----
    template_variants = _parse_message_template_variants(task)
    if template_variants:
        jlog(f"Trovati {len(template_variants)} esempi di stile (message_template_variants)")

    llm_provider = (task.get("llm_provider") or "ollama").strip().lower()
    try:
        llm_base_url = resolve_base_url(llm_provider, task.get("llm_base_url"))
    except Exception as e:
        msg = f"resolve_base_url({llm_provider!r}) fail: {e}"
        jlog(f"❌ {msg}")
        db.update_job(job_id, status="error", error=msg, finished_at=db.now_iso())
        return ""
    try:
        llm_api_key = resolve_api_key(llm_provider, task.get("llm_api_key"))
    except Exception as e:
        msg = f"resolve_api_key({llm_provider!r}) fail: {e}. Imposta llm_api_key nel task o la env var del provider."
        jlog(f"❌ {msg}")
        db.update_job(job_id, status="error", error=msg, finished_at=db.now_iso())
        return ""
    llm_model = task.get("model") or "qwen3-coder:30b"
    jlog(f"Generazione messaggi via {llm_provider}/{llm_model} (base_url={llm_base_url})...")

    reqs: list[MessageRequest] = []
    for t in targets:
        a = t["asset"]
        raw_data: dict[str, Any] = {}
        if isinstance(a.get("raw_json"), str):
            try:
                raw_data = json.loads(a["raw_json"])
            except (json.JSONDecodeError, TypeError):
                pass
        elif isinstance(a.get("raw_json"), dict):
            raw_data = a["raw_json"]
        reqs.append(MessageRequest(
            target_display_name=a.get("display_name") or a.get("title") or t["username"],
            target_username=t["username"],
            target_platform=platform,
            target_profile_url=t["social_url"],
            target_raw_data=raw_data,
            intent=intent,
            template_variants=template_variants,
        ))

    msg_results = await generate_batch(
        reqs,
        llm_base_url=llm_base_url,
        llm_api_key=llm_api_key,
        llm_model=llm_model,
    )

    pairs: list[tuple[str, str]] = []
    asset_id_by_username: dict[str, int] = {}
    errors_seen: list[str] = []
    for (req, msg, err), t in zip(msg_results, targets):
        if msg:
            pairs.append((t["username"], msg))
            asset_id_by_username[t["username"]] = t["asset"]["id"]
        elif err:
            errors_seen.append(f"{t['username']}: {err}")
    jlog(f"Generati {len(pairs)}/{len(targets)} messaggi")
    if not pairs:
        for line in errors_seen[:5]:
            jlog(f"  ↳ {line}")
        jlog("⚠️ Nessun messaggio generato (LLM giù o tutti rifiutati). Abort.")
        db.update_job(job_id, status="error", error="message generation failed", finished_at=db.now_iso())
        return ""

    # ---- 5. Engine sessione ----
    max_per_session = int(task.get("max_dms_per_session") or 5)
    headed = bool(task.get("headed", 1))
    engine = OutreachEngine(accounts, headed=headed, use_patchright=True)

    all_results = []
    while pairs:
        if db.get_control_signal(job_id) == "stop":
            jlog("STOP richiesto durante outreach. Salvo quanto fatto.")
            break
        batch = pairs[:max_per_session]
        pairs = pairs[max_per_session:]
        jlog(f"→ Sessione: {len(batch)} DM via {platform}")
        # Warmup variabile 3-5 min: rende meno prevedibile il pattern temporale
        # session → DM (un fisso 5min dopo login e' un signal facilmente fingerprintabile).
        warmup_min = random.uniform(3.0, 5.0)
        results = await engine.run_session(
            platform_name=platform,
            targets=batch,
            warmup_min=warmup_min,
            max_dms_per_session=max_per_session,
            jlog=jlog,
        )
        all_results.extend(results)
        if not results:
            jlog("⚠️ Sessione vuota (account esauriti o off-hours). Stop loop.")
            break

    # ---- 6. Log su DB ----
    n_ok = 0
    n_fail = 0
    for r in all_results:
        asset_id = asset_id_by_username.get(r.target_username)
        account_id_db = next(iter(account_id_by_uuid.values()), None)
        if account_id_db is None:
            continue
        try:
            msg_text = next(
                (m for u, m in [(u, m) for u, m in [(p[0], p[1]) for p in []]] if u == r.target_username),
                "",
            )
            db.insert_social_dm_log({
                "account_id": account_id_db,
                "job_id": job_id,
                "target_asset_id": asset_id,
                "target_platform": platform,
                "target_username": r.target_username or "",
                "message": msg_text or "(message_text non disponibile)",
                "ok": r.ok,
                "reason": r.reason,
                "health_post": r.health.value if hasattr(r.health, "value") else str(r.health),
            })
        except Exception as e:
            jlog(f"  WARN insert_social_dm_log fail: {e}")
        if r.ok:
            n_ok += 1
            if asset_id:
                try:
                    db.update_asset_outreach_status(asset_id, "contacted")
                except Exception as e:
                    jlog(f"  WARN update_asset_outreach_status({asset_id}) fail: {e}")
        else:
            n_fail += 1

    jlog(f"✅ outreach_social completato: {n_ok} DM inviati, {n_fail} falliti")

    # Report
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_dir = RESULTS_DIR / str(task["id"]) / ts
    run_dir.mkdir(parents=True, exist_ok=True)
    report_path = run_dir / "report.md"
    report = (
        f"# Riepilogo outreach_social {ts}\n\n"
        f"- **Piattaforma**: {platform}\n"
        f"- **DM inviati**: {n_ok}\n"
        f"- **DM falliti**: {n_fail}\n"
        f"- **Account usati**: {len(accounts)}\n"
        f"- **Modello LLM messaggi**: {llm_model}\n"
    )
    report_path.write_text(report, encoding="utf-8")

    db.update_job(job_id, status="done", finished_at=db.now_iso(), result_path=str(report_path))
    return str(report_path)
