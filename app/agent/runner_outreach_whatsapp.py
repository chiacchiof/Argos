"""Runner outreach_whatsapp — invio DM WhatsApp con doppio motore.

Agent mode: `outreach_whatsapp`. Pipeline:
  1. Validazioni (ARGOS_SECRET, almeno un engine configurato)
  2. Engine selector PER ogni contatto:
       - whatsapp_consent='opt_in' OR inbound recente (24h) → Motore B (API)
       - default (cold)                                     → Motore A (browser)
       - Override task: engine_preference = 'auto' | 'force_A' | 'force_B'
  3. LLM rephrase del template per ogni contatto (Qwen locale, $0)
  4. Invio:
       - Motore A: OutreachEngine + WhatsAppBrowser via Playwright
       - Motore B: WhatsAppAPI HTTP client (Meta Cloud API)
  5. Log in social_dm_log + update assets.outreach_status='contacted'
  6. Report finale

Caveat ToS WhatsApp documentato in UI + GUIDA: il Motore A viola i ToS, rischio
ban del numero per uso massivo a freddo. Il Motore B è legale ma limitato a
contatti con opt-in / nella 24h-window.
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .. import db
from ..config import RESULTS_DIR
from .llm_providers import resolve_api_key, resolve_base_url
from .runner_control import wait_if_paused_or_stop, RunnerStopped
from .social.crypto_creds import is_configured
from .social.engine import OutreachEngine
from .social.message_generator import MessageRequest, generate_batch
from .social.platform_base import SocialAccount
from .social.whatsapp_api import WhatsAppAPI, can_send_freeform


log = logging.getLogger(__name__)


# ---------- helpers ----------

def _parse_template_variants(task: dict[str, Any]) -> list[str]:
    """Estrae lista esempi-stile (riusa pattern di outreach_social)."""
    import re
    raw = (task.get("message_template_variants") or "").strip()
    if raw:
        chunks = [c.strip() for c in re.split(r"\n\s*-{3,}\s*\n", raw) if c.strip()]
        cleaned = [c for c in (s.strip().strip("-").strip() for s in chunks) if c]
        if cleaned:
            return cleaned[:10]
    single = (task.get("message_template") or "").strip()
    return [single] if single else []


def _select_engine(
    target: dict[str, Any],
    preference: str,
    has_engine_a: bool,
    has_engine_b: bool,
) -> str | None:
    """Decide quale engine usare per il singolo asset destinatario.

    Ritorna 'A' / 'B' / None (=skip per mismatch).

    Logica:
    - preference='force_A' → A se disponibile, else None
    - preference='force_B' → B se disponibile E (opt_in OR 24h-window), else None
    - preference='auto':
        consent='optedout' → None (skip)
        consent='opt_in' OR 24h-window attiva → B se disponibile, else A
        default (cold) → A se disponibile, else None
    """
    consent = (target.get("whatsapp_consent") or "cold").lower()
    last_in = target.get("whatsapp_last_inbound_at")

    if consent == "optedout":
        return None  # skip

    if preference == "force_A":
        return "A" if has_engine_a else None
    if preference == "force_B":
        # B richiede contatto opt-in O nella 24h-window
        if has_engine_b and (consent == "opt_in" or can_send_freeform(last_in)):
            return "B"
        return None

    # auto
    if has_engine_b and (consent == "opt_in" or can_send_freeform(last_in)):
        return "B"
    if has_engine_a:
        return "A"
    return None


# ---------- main runner ----------

async def run_agent(task: dict[str, Any], job_id: int) -> str:
    """Entry-point outreach_whatsapp."""
    db.update_job(job_id, status="running", started_at=db.now_iso())
    db.set_control_signal(job_id, None)

    def jlog(line: str) -> None:
        db.append_job_log(job_id, line)

    jlog(f'Avvio outreach_whatsapp per task #{task["id"]} "{task["name"]}"')

    # ---- 1. Validazioni ----
    if not is_configured():
        msg = "ARGOS_SECRET non settata in .env. Abort."
        jlog(f"❌ {msg}")
        db.update_job(job_id, status="error", error=msg, finished_at=db.now_iso())
        return ""

    intent = (task.get("outreach_intent") or task.get("message_template") or "").strip()
    if not intent and not task.get("message_template_variants"):
        msg = "Nessun intent/template specificato (outreach_intent o message_template)."
        jlog(f"❌ {msg}")
        db.update_job(job_id, status="error", error=msg, finished_at=db.now_iso())
        return ""

    preference = (task.get("whatsapp_engine_preference") or "auto").strip()
    if preference not in ("auto", "force_A", "force_B"):
        jlog(f"⚠️ whatsapp_engine_preference invalido ({preference!r}), uso 'auto'")
        preference = "auto"

    # ---- 2. Carica engines ----
    # Motore A — account WA browser.
    # Se il task ha `whatsapp_account_id` valorizzato → SOLO quell'account
    # (fail-fast se banned/disabled). Altrimenti: pool default = tutti active.
    sender_account_id = task.get("whatsapp_account_id")
    if sender_account_id:
        single = db.get_social_account(int(sender_account_id))
        if not single:
            msg = f"whatsapp_account_id={sender_account_id} non trovato (eliminato?). Abort."
            jlog(f"❌ {msg}")
            db.update_job(job_id, status="error", error=msg, finished_at=db.now_iso())
            return ""
        if single.get("platform") != "whatsapp_browser":
            msg = f"Account #{sender_account_id} non è whatsapp_browser (platform={single.get('platform')!r}). Abort."
            jlog(f"❌ {msg}")
            db.update_job(job_id, status="error", error=msg, finished_at=db.now_iso())
            return ""
        if single.get("status") != "active":
            msg = (
                f"Account #{sender_account_id} ('{single.get('username')}') ha "
                f"status='{single.get('status')}' (non active). Abort (fail-fast, "
                "nessun fallback al pool: l'utente ha scelto esplicitamente questo sender)."
            )
            jlog(f"❌ {msg}")
            db.update_job(job_id, status="error", error=msg, finished_at=db.now_iso())
            return ""
        a_rows = [single]
        jlog(f"Sender Motore A: SOLO #{single['id']} '{single.get('username')}' (single-select)")
    else:
        a_rows = db.list_social_accounts(platform="whatsapp_browser", status="active")
        if a_rows:
            jlog(f"Sender Motore A: pool default ({len(a_rows)} account active)")

    a_accounts: list[SocialAccount] = []
    account_id_by_uuid: dict[str, int] = {}
    for r in a_rows:
        # Per WhatsApp non c'è una password decifrabile (login via QR persistito).
        # Passiamo stringa vuota e WhatsAppBrowser.login ignora il campo.
        # session_dir → Playwright user_data_dir per persistenza sessione WA Web
        # (così non si scansiona QR ad ogni run).
        a_accounts.append(SocialAccount(
            uuid=r["uuid"],
            platform="whatsapp_browser",
            username=r.get("phone_number") or r["username"],
            password="",
            proxy_label=r.get("proxy_label"),
            daily_dm_cap=int(r.get("daily_dm_cap") or 100),
            status=r.get("status") or "active",
            session_dir=r.get("session_dir") or None,
        ))
        account_id_by_uuid[r["uuid"]] = r["id"]
    has_engine_a = len(a_accounts) > 0

    # Motore B — config API.
    # Stesso pattern del Motore A: single-select se task.whatsapp_api_config_id
    # è valorizzato (fail-fast su disabled), altrimenti prima config active.
    sender_api_id = task.get("whatsapp_api_config_id")
    b_api: WhatsAppAPI | None = None
    api_config_id: int | None = None
    if sender_api_id:
        single_cfg = db.get_whatsapp_api_config(int(sender_api_id))
        if not single_cfg:
            msg = f"whatsapp_api_config_id={sender_api_id} non trovato. Abort."
            jlog(f"❌ {msg}")
            db.update_job(job_id, status="error", error=msg, finished_at=db.now_iso())
            return ""
        if single_cfg.get("status") != "active":
            msg = (
                f"Config API #{sender_api_id} ('{single_cfg.get('label')}') ha "
                f"status='{single_cfg.get('status')}' (non active). Abort fail-fast."
            )
            jlog(f"❌ {msg}")
            db.update_job(job_id, status="error", error=msg, finished_at=db.now_iso())
            return ""
        try:
            b_api = WhatsAppAPI(single_cfg)
            api_config_id = single_cfg["id"]
            jlog(f"Sender Motore B: SOLO #{single_cfg['id']} '{single_cfg.get('label')}' (single-select)")
        except Exception as e:
            msg = f"Motore B init fallito per config #{sender_api_id}: {e}"
            jlog(f"❌ {msg}")
            db.update_job(job_id, status="error", error=msg, finished_at=db.now_iso())
            return ""
    else:
        b_configs = db.list_whatsapp_api_config(status="active")
        if b_configs:
            try:
                b_api = WhatsAppAPI(b_configs[0])
                api_config_id = b_configs[0]["id"]
                jlog(f"Sender Motore B: prima config active = #{api_config_id}")
            except Exception as e:
                jlog(f"⚠️ Motore B disponibile ma init fallito: {e}")
                b_api = None
    has_engine_b = b_api is not None

    if not (has_engine_a or has_engine_b):
        msg = (
            "Nessun engine WhatsApp configurato. Vai su /settings/whatsapp e "
            "aggiungi almeno un account browser (Motore A) o una config API (Motore B)."
        )
        jlog(f"❌ {msg}")
        db.update_job(job_id, status="error", error=msg, finished_at=db.now_iso())
        return ""

    jlog(
        f"Engines: A={'OK ('+str(len(a_accounts))+' account)' if has_engine_a else '—'} | "
        f"B={'OK' if has_engine_b else '—'}  preference={preference}"
    )

    # ---- 3. Carica asset target ----
    max_dms_per_run = int(task.get("max_dms_per_run") or 30)

    # Se l'utente ha selezionato target espliciti dalla UI (target_asset_ids /
    # legacy target_contact_ids), usiamo SOLO quelli. Altrimenti fallback agli
    # asset qualified con whatsapp.
    explicit_ids: list[int] = []
    raw_ids = task.get("target_asset_ids") or task.get("target_contact_ids") or []
    for x in raw_ids:
        try:
            i = int(x)
            if i > 0:
                explicit_ids.append(i)
        except (TypeError, ValueError):
            continue

    if explicit_ids:
        jlog(
            f"Selezione esplicita: {len(explicit_ids)} asset scelti nel task "
            "(status filter bypassato per la selezione, ma optedout viene comunque escluso)."
        )
        targets_raw = []
        for aid in explicit_ids:
            a = db.get_asset(aid)
            if not a:
                continue
            if not (a.get("whatsapp") or "").strip():
                continue
            if (a.get("whatsapp_consent") or "").lower() == "optedout":
                continue
            targets_raw.append(a)
    else:
        # Multi-tag filter: AND fra (interests_inferred=fitness, location=Catania, ...)
        tag_filters_raw = task.get("outreach_filter_tags") or []
        asset_tag_filters: list[tuple[str, str]] = []
        for tf in tag_filters_raw:
            if isinstance(tf, dict):
                k, v = (tf.get("key") or "").strip(), (tf.get("value") or "").strip()
                if k and v:
                    asset_tag_filters.append((k, v))
        if asset_tag_filters:
            chips = ", ".join(f"{k}={v}" for k, v in asset_tag_filters)
            jlog(f"Selezione automatica: asset qualified con whatsapp + tag-filter [{chips}].")
        else:
            jlog("Selezione automatica: tutti gli asset qualified con whatsapp.")
        targets_raw = db.list_assets_for_whatsapp_outreach(
            limit=max_dms_per_run * 2,
            asset_tag_filters=asset_tag_filters or None,
        )

    if not targets_raw:
        jlog("WARN Nessun asset da contattare (selezione vuota o filtri esclusi tutti).")
        db.update_job(job_id, status="done", finished_at=db.now_iso())
        return ""

    # Engine selection per asset
    targets: list[dict[str, Any]] = []
    n_skip_optout = 0
    n_skip_mismatch = 0
    for a in targets_raw:
        eng = _select_engine(a, preference, has_engine_a, has_engine_b)
        if eng is None:
            if (a.get("whatsapp_consent") or "").lower() == "optedout":
                n_skip_optout += 1
            else:
                n_skip_mismatch += 1
            continue
        targets.append({"asset": a, "engine": eng})
        if len(targets) >= max_dms_per_run:
            break

    if not targets:
        jlog(
            f"WARN Nessun asset idoneo dopo engine selection "
            f"(opt-out: {n_skip_optout}, no-engine-compatibile: {n_skip_mismatch}). Abort."
        )
        db.update_job(job_id, status="done", finished_at=db.now_iso())
        return ""

    n_a = sum(1 for t in targets if t["engine"] == "A")
    n_b = sum(1 for t in targets if t["engine"] == "B")
    jlog(f"Target: {len(targets)} asset -> A={n_a}, B={n_b}  (skip opt-out: {n_skip_optout})")

    # ---- 4. Genera messaggi personalizzati via LLM ----
    template_variants = _parse_template_variants(task)
    llm_provider = (task.get("llm_provider") or "ollama").strip().lower()
    try:
        llm_base_url = resolve_base_url(llm_provider, task.get("llm_base_url"))
        llm_api_key = resolve_api_key(llm_provider, task.get("llm_api_key"))
    except Exception as e:
        msg = f"LLM config fail: {e}"
        jlog(f"❌ {msg}")
        db.update_job(job_id, status="error", error=msg, finished_at=db.now_iso())
        return ""
    llm_model = task.get("model") or "qwen3-coder:30b"
    jlog(f"Generazione messaggi via {llm_provider}/{llm_model}...")

    reqs: list[MessageRequest] = []
    for t in targets:
        a = t["asset"]
        raw_data: dict[str, Any] = {}
        if isinstance(a.get("raw_json"), str):
            try:
                raw_data = json.loads(a["raw_json"])
            except (json.JSONDecodeError, TypeError):
                pass
        reqs.append(MessageRequest(
            target_display_name=a.get("display_name") or a.get("title") or a.get("whatsapp") or "",
            target_username=a.get("whatsapp") or "",
            target_platform="whatsapp",
            target_profile_url=a.get("source_url") or "",
            target_raw_data=raw_data,
            intent=intent,
            template_variants=template_variants,
            max_chars=600,  # WA tollera più char di IG DM
        ))

    msg_results = await generate_batch(
        reqs,
        llm_base_url=llm_base_url,
        llm_api_key=llm_api_key,
        llm_model=llm_model,
    )

    # Map asset → (message, engine)
    plan: list[dict[str, Any]] = []
    errors_seen: list[str] = []
    for (req, msg, err), t in zip(msg_results, targets):
        if msg:
            plan.append({
                "asset": t["asset"],
                "engine": t["engine"],
                "phone": t["asset"]["whatsapp"],
                "message": msg,
            })
        elif err:
            errors_seen.append(f"{t['asset'].get('whatsapp') or t['asset'].get('id')}: {err}")

    if not plan:
        jlog("⚠️ Nessun messaggio generato. Errori:")
        for e in errors_seen[:5]:
            jlog(f"  ↳ {e}")
        db.update_job(job_id, status="error", error="message generation failed",
                      finished_at=db.now_iso())
        return ""

    jlog(f"Messaggi generati: {len(plan)}/{len(targets)}")

    # ---- 5. Esecuzione invii ----
    n_ok_a = n_fail_a = n_ok_b = n_fail_b = 0
    dry_run = bool(task.get("whatsapp_dry_run"))
    if dry_run:
        jlog("🧪 DRY-RUN attivo: nessun invio reale, solo log simulato.")

    # 5a. Motore A — browser
    plan_a = [p for p in plan if p["engine"] == "A"]
    if plan_a:
        if dry_run:
            for p in plan_a:
                db.insert_social_dm_log({
                    "account_id": next(iter(account_id_by_uuid.values()), None),
                    "job_id": job_id,
                    "target_asset_id": p["asset"]["id"],
                    "target_platform": "whatsapp",
                    "target_username": p["phone"],
                    "message": p["message"],
                    "ok": True,
                    "reason": "dry_run",
                    "engine": "A_browser",
                })
                n_ok_a += 1
        else:
            try:
                n_ok_a, n_fail_a = await _run_engine_a(
                    plan_a, a_accounts, account_id_by_uuid, job_id, task, jlog,
                )
            except RunnerStopped:
                jlog("⏹ Stop richiesto durante invii Motore A.")
            except Exception as e:
                jlog(f"❌ Motore A crashato: {type(e).__name__}: {e}")

    # 5b. Motore B — API
    plan_b = [p for p in plan if p["engine"] == "B"]
    if plan_b and b_api:
        try:
            n_ok_b, n_fail_b = await _run_engine_b(
                plan_b, b_api, api_config_id, job_id, dry_run, jlog,
            )
        except RunnerStopped:
            jlog("⏹ Stop richiesto durante invii Motore B.")
        except Exception as e:
            jlog(f"❌ Motore B crashato: {type(e).__name__}: {e}")

    n_ok = n_ok_a + n_ok_b
    n_fail = n_fail_a + n_fail_b
    jlog(
        f"✅ outreach_whatsapp completato: {n_ok} inviati ({n_ok_a} A + {n_ok_b} B), "
        f"{n_fail} falliti."
    )

    # ---- 6. Report ----
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_dir = RESULTS_DIR / str(task["id"]) / ts
    run_dir.mkdir(parents=True, exist_ok=True)
    report_path = run_dir / "report.md"
    # Audience snapshot vs filtri dinamici
    audience_total = len(explicit_ids) if explicit_ids else len(targets_raw)
    report = (
        f"# Outreach WhatsApp — {task['name']}\n\n"
        f"- **Audience**: {audience_total} asset" + (" (snapshot esplicito)" if explicit_ids else " (filtri dinamici)") + "\n"
        f"- **Target idonei (post engine selection)**: {len(targets)}\n"
        f"- **Inviati**: {n_ok} (Motore A: {n_ok_a} | Motore B: {n_ok_b})\n"
        f"- **Falliti**: {n_fail} (A: {n_fail_a} | B: {n_fail_b})\n"
        f"- **Breakdown skip**:\n"
        f"  - **{n_skip_optout}** opt-out (whatsapp_consent='optedout')\n"
        f"  - **{n_skip_mismatch}** no-engine-compatibile (es. senza WhatsApp o canale non disponibile)\n"
        f"- **Dry-run**: {dry_run}\n"
        f"- **Engine preference**: {preference}\n"
    )
    report_path.write_text(report, encoding="utf-8")

    db.update_job(
        job_id, status="done", finished_at=db.now_iso(),
        result_path=str(report_path),
    )
    return str(report_path)


# ---------- Motore A invocation ----------

async def _run_engine_a(
    plan: list[dict[str, Any]],
    accounts: list[SocialAccount],
    account_id_by_uuid: dict[str, int],
    job_id: int,
    task: dict[str, Any],
    jlog,
) -> tuple[int, int]:
    """Esegue invii via browser usando OutreachEngine + WhatsAppBrowser.

    Ritorna (n_ok, n_fail).
    """
    from .runner_control import wait_if_paused_or_stop

    max_per_session = int(task.get("max_dms_per_session") or 5)
    headed = bool(task.get("headed", 1))
    engine = OutreachEngine(accounts, headed=headed, use_patchright=True)

    n_ok = 0
    n_fail = 0
    queue = list(plan)

    while queue:
        await wait_if_paused_or_stop(job_id, jlog)
        batch = queue[:max_per_session]
        queue = queue[max_per_session:]
        # Format dei target richiesti da OutreachEngine: [(username, message), ...]
        # Per WhatsApp `username` = phone number.
        targets_pairs = [(p["phone"], p["message"]) for p in batch]
        asset_by_phone = {p["phone"]: p["asset"] for p in batch}
        message_by_phone = {p["phone"]: p["message"] for p in batch}
        jlog(f"-> Sessione A: {len(targets_pairs)} DM via WhatsApp Web")
        # Warmup WA modulato dal speed_profile del task.
        # safe: 0.3-0.8 min (anti-ban, comportamento storico).
        # balanced: 0.1-0.3 min. aggressive: 0 (niente warmup).
        # WA non ha feed da scrollare quindi il warmup max è già basso.
        speed_profile = task.get("speed_profile") or None
        if speed_profile == "balanced":
            wu_lo, wu_hi = 0.1, 0.3
        elif speed_profile == "aggressive":
            wu_lo, wu_hi = 0.0, 0.0
        else:
            wu_lo, wu_hi = 0.3, 0.8
        warmup_min = random.uniform(wu_lo, wu_hi)
        results = await engine.run_session(
            platform_name="whatsapp_browser",
            targets=targets_pairs,
            warmup_min=warmup_min,
            max_dms_per_session=max_per_session,
            jlog=jlog,
            gap_min_minutes=task.get("gap_between_dms_min"),
            gap_max_minutes=task.get("gap_between_dms_max"),
            speed_profile=speed_profile,
        )
        for r in results:
            phone = r.target_username or ""
            asset = asset_by_phone.get(phone)
            account_id_db = next(iter(account_id_by_uuid.values()), None)
            try:
                db.insert_social_dm_log({
                    "account_id": account_id_db,
                    "job_id": job_id,
                    "target_asset_id": asset["id"] if asset else None,
                    "target_platform": "whatsapp",
                    "target_username": phone,
                    "message": message_by_phone.get(phone, ""),
                    "ok": r.ok,
                    "reason": r.reason,
                    "engine": "A_browser",
                    "health_post": (
                        r.health.value if hasattr(r.health, "value") else str(r.health)
                    ),
                })
            except Exception as e:
                jlog(f"  WARN insert_social_dm_log fail: {e}")
            if r.ok:
                n_ok += 1
                if asset:
                    try:
                        db.update_asset_outreach_status(asset["id"], "contacted")
                    except Exception:
                        pass
            else:
                n_fail += 1
        if not results:
            jlog("WARN Sessione A vuota (account esauriti o off-hours). Stop loop A.")
            break

    return n_ok, n_fail


# ---------- Motore B invocation ----------

async def _run_engine_b(
    plan: list[dict[str, Any]],
    api: WhatsAppAPI,
    api_config_id: int | None,
    job_id: int,
    dry_run: bool,
    jlog,
) -> tuple[int, int]:
    """Esegue invii via Meta Cloud API. Niente rate-limit aggressivo (Meta gestisce
    throttling lato suo); aggiungiamo solo una piccola pausa tra send per non
    saturare la rete.
    """
    n_ok = 0
    n_fail = 0

    for p in plan:
        await wait_if_paused_or_stop(job_id, jlog)
        asset = p["asset"]
        phone = p["phone"]
        message = p["message"]
        last_in = asset.get("whatsapp_last_inbound_at")

        if dry_run:
            db.insert_social_dm_log({
                "account_id": None,
                "job_id": job_id,
                "target_asset_id": asset["id"],
                "target_platform": "whatsapp",
                "target_username": phone,
                "message": message,
                "ok": True,
                "reason": "dry_run",
                "engine": "B_api",
                "api_config_id": api_config_id,
            })
            n_ok += 1
            continue

        # Decide free-form vs template:
        # - se asset dentro 24h-window → free-form (più naturale, no template)
        # - altrimenti → template (richiesto da Meta fuori finestra)
        try:
            if can_send_freeform(last_in):
                result = await api.send_text(phone, message)
                method = "send_text"
            else:
                if not api.default_template_name:
                    jlog(
                        f"  WARN {phone}: fuori 24h-window e nessun default_template. "
                        "Configura un template per il Motore B."
                    )
                    db.insert_social_dm_log({
                        "account_id": None,
                        "job_id": job_id,
                        "target_asset_id": asset["id"],
                        "target_platform": "whatsapp",
                        "target_username": phone,
                        "message": message,
                        "ok": False,
                        "reason": "missing_template_outside_24h_window",
                        "engine": "B_api",
                        "api_config_id": api_config_id,
                    })
                    n_fail += 1
                    continue
                # body_params: usiamo il display_name come {{1}}.
                # Se il template ha più di 1 placeholder, l'utente deve estendere.
                result = await api.send_template(
                    phone,
                    api.default_template_name,
                    api.default_template_language,
                    body_params=[asset.get("display_name") or asset.get("title") or "amico"],
                )
                method = "send_template"
        except Exception as e:
            jlog(f"  WARN Motore B errore per {phone}: {type(e).__name__}: {e}")
            n_fail += 1
            continue

        db.insert_social_dm_log({
            "account_id": None,
            "job_id": job_id,
            "target_asset_id": asset["id"],
            "target_platform": "whatsapp",
            "target_username": phone,
            "message": message if method == "send_text" else f"[template:{api.default_template_name}]",
            "ok": result.ok,
            "reason": result.error_message if not result.ok else f"sent ({method}, id={result.message_id})",
            "engine": "B_api",
            "api_config_id": api_config_id,
        })
        if result.ok:
            n_ok += 1
            try:
                db.update_asset_outreach_status(asset["id"], "contacted")
            except Exception:
                pass
        else:
            n_fail += 1

        # Pausa breve tra send (Meta gestisce throttling, ma noi siamo gentili)
        await asyncio.sleep(random.uniform(0.5, 1.5))

    return n_ok, n_fail
