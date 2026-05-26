"""Runner audience_discovery — esplorazione FB-loggata per scoperta audience.

Agent mode: `audience_discovery`. v1: solo Facebook.

Architettura pipeline a 5 fasi (NON full-ReAct, più deterministica/debuggabile):

  Fase 1 (1 LLM call): legge il brief NL (`task.objective`) e deduce 3-8
    keyword tematiche da usare per search FB.
  Fase 2: per ogni anchor profile (`task.seed_queries`) → apre /friends e
    raccoglie amici visibili (se la friend list è pubblica).
  Fase 3: per ogni keyword → `search_groups(keyword)` accumula gruppi
    tematici.
  Fase 4: per ogni gruppo → `open_group_and_collect_members` accumula
    autori attivi (= candidati audience).
  Fase 5: per ogni candidato unico:
    - dedup vs DB via `db.has_recent_asset` (refresh_policy_days)
    - apri profilo, `facebook_recon.extract_profile_data`
    - 1 LLM call per scorare il profilo rispetto al brief (0-10 + reason)
    - se score >= recon_score_threshold → salva asset + scrivi profiles.jsonl
    - cap su `recon_max_targets_per_day`

Anti-ban: pause `speed_profile` (safe/balanced/aggressive) tra ogni azione FB.

Output:
- `data/results/<task_id>/<ts>/profiles.jsonl` — UNA riga per profilo salvato
- `data/results/<task_id>/<ts>/recon_audit_log.jsonl` — log azioni (riusa
  il modulo `ReconAudit` di safe_browser, filename ereditato)
- `data/results/<task_id>/<ts>/report.md` — riepilogo
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

import httpx

from .. import db
from ..config import RESULTS_DIR
from .llm_providers import resolve_api_key, resolve_base_url
from .ollama import maybe_add_keep_alive
from .social import facebook_audience, facebook_recon
from .social.crypto_creds import is_configured
from .social.safe_browser import ReconAudit, SafeBrowser, is_recon_disabled


log = logging.getLogger(__name__)


# ============================================================
# Browser helpers (duplicati semplificati da runner_recon_social per FB)
# ============================================================

async def _open_persistent_browser(*, headed: bool, user_data_dir: Path):
    """Apre Chromium con persistent context (sessione loggata persistente)."""
    try:
        from patchright.async_api import async_playwright as _ap
    except ImportError:
        from playwright.async_api import async_playwright as _ap

    Path(user_data_dir).mkdir(parents=True, exist_ok=True)
    p = await _ap().start()
    context = await p.chromium.launch_persistent_context(
        user_data_dir=str(user_data_dir),
        headless=not headed,
        viewport={"width": 1280, "height": 800},
        locale="it-IT",
        timezone_id="Europe/Rome",
        args=[
            "--disable-blink-features=AutomationControlled",
            "--no-default-browser-check",
        ],
    )
    page = context.pages[0] if context.pages else await context.new_page()
    return p, context, page


async def _verify_fb_logged_in(page, jlog) -> bool:
    """Verifica che la sessione FB sia loggata. Heuristica: dopo goto su
    facebook.com, se vediamo il form di login o testo "Accedi" → non loggato."""
    try:
        await page.goto("https://www.facebook.com/", wait_until="domcontentloaded", timeout=30_000)
    except Exception as e:
        jlog(f"⚠️ goto facebook.com fallita: {e}")
        return False
    await asyncio.sleep(2.5)

    # Heuristica login: presenza form email/password o testo "Crea nuovo account"
    not_logged_markers = [
        'input[name="email"][type*="text"]',
        'input[name="pass"]',
        'a:has-text("Crea nuovo account")',
        'button:has-text("Accedi")',
    ]
    for sel in not_logged_markers:
        try:
            if await page.locator(sel).count() > 0:
                jlog(f"❌ Browser FB NON loggato (selettore '{sel}' presente).")
                return False
        except Exception:
            pass
    return True


# ============================================================
# LLM helpers — 2 chiamate: keyword deduction + profile scoring
# ============================================================

_KEYWORDS_SYSTEM_PROMPT = """\
Sei un assistente che traduce un brief NL in keyword di ricerca per Facebook.

Il brief descrive un'audience target (topic + demografia + segnali di interesse).
Il tuo compito: produrre 4-8 keyword/frasi brevi (max 4 parole l'una) che
sarebbero efficaci nella barra di ricerca Facebook per trovare gruppi tematici
e persone attive su quel topic.

Output: SOLO un array JSON di stringhe. Niente prosa, niente <think>, niente
backtick. Esempio:
["vintage anni 80", "moda anni 80", "collezionismo magliette", "nostalgia anni 80"]
"""


async def _llm_deduce_keywords(
    brief: str,
    *,
    llm_base_url: str,
    llm_api_key: str,
    llm_model: str,
    jlog,
) -> list[str]:
    """1 LLM call: dal brief estrae 4-8 keyword di ricerca FB."""
    payload = {
        "model": llm_model,
        "messages": [
            {"role": "system", "content": _KEYWORDS_SYSTEM_PROMPT},
            {"role": "user", "content": f"Brief audience:\n{brief}\n\nProduci l'array JSON di keyword."},
        ],
        "temperature": 0.4,
        "max_tokens": 400,
    }
    maybe_add_keep_alive(payload, llm_base_url)

    try:
        async with httpx.AsyncClient(timeout=120) as client:
            r = await client.post(
                f"{llm_base_url.rstrip('/')}/chat/completions",
                json=payload,
                headers={"Authorization": f"Bearer {llm_api_key}"},
            )
            r.raise_for_status()
            data = r.json()
            txt = (data.get("choices", [{}])[0].get("message", {}).get("content", "") or "").strip()
    except Exception as e:
        jlog(f"⚠️ LLM keyword deduction fail: {e}")
        return []

    # Strip <think> tags + estrai primo array JSON
    txt = re.sub(r"<think>[\s\S]*?</think>", "", txt, flags=re.IGNORECASE).strip()
    # Cerca primo blocco [...] nel testo
    m = re.search(r"\[\s*(?:\".*?\"\s*,?\s*)+\]", txt, flags=re.DOTALL)
    if not m:
        jlog(f"⚠️ LLM non ha prodotto array JSON. Output: {txt[:200]!r}")
        return []
    try:
        arr = json.loads(m.group(0))
        if not isinstance(arr, list):
            return []
        keywords = [str(k).strip() for k in arr if isinstance(k, str) and str(k).strip()]
        return keywords[:8]
    except Exception as e:
        jlog(f"⚠️ JSON parse error: {e}")
        return []


_SCORE_SYSTEM_PROMPT = """\
Sei un assistente che valuta se un profilo Facebook matcha un brief audience.

Riceverai:
- un brief NL che descrive l'audience target
- i dati estratti del profilo (display_name, bio, post visibili, interessi
  inferiti, language, ecc.)

Tuo compito: produrre uno score 0-10 di quanto il profilo matcha il brief,
+ una breve `reason` (1-2 frasi italiane) che spiega il perché.

- score=10: match perfetto (tutti i segnali confermati)
- score=7-9: match forte (la maggior parte dei segnali confermati)
- score=4-6: match parziale (alcuni segnali, ma non determinanti)
- score=0-3: match debole o nullo

Output: SOLO un JSON `{"score": N, "reason": "..."}`. Niente prosa esterna,
niente <think>, niente backtick.
"""


async def _llm_score_profile(
    brief: str,
    profile_data: dict[str, Any],
    *,
    llm_base_url: str,
    llm_api_key: str,
    llm_model: str,
    jlog,
) -> tuple[int, str]:
    """1 LLM call per profilo: ritorna (score 0-10, reason). Score 0 se errore."""
    profile_json = json.dumps(profile_data, ensure_ascii=False, default=str)[:6000]
    payload = {
        "model": llm_model,
        "messages": [
            {"role": "system", "content": _SCORE_SYSTEM_PROMPT},
            {"role": "user", "content": (
                f"Brief audience:\n{brief}\n\n"
                f"Dati profilo:\n{profile_json}\n\n"
                "Produci il JSON {\"score\": N, \"reason\": \"...\"}."
            )},
        ],
        "temperature": 0.2,
        "max_tokens": 400,
    }
    maybe_add_keep_alive(payload, llm_base_url)

    try:
        async with httpx.AsyncClient(timeout=120) as client:
            r = await client.post(
                f"{llm_base_url.rstrip('/')}/chat/completions",
                json=payload,
                headers={"Authorization": f"Bearer {llm_api_key}"},
            )
            r.raise_for_status()
            data = r.json()
            txt = (data.get("choices", [{}])[0].get("message", {}).get("content", "") or "").strip()
    except Exception as e:
        jlog(f"⚠️ LLM score fail: {e}")
        return 0, "llm_error"

    txt = re.sub(r"<think>[\s\S]*?</think>", "", txt, flags=re.IGNORECASE).strip()
    m = re.search(r"\{[^{}]*\"score\"[^{}]*\}", txt, flags=re.DOTALL)
    if not m:
        return 0, "no_json"
    try:
        obj = json.loads(m.group(0))
        score = int(obj.get("score", 0))
        score = max(0, min(10, score))
        reason = str(obj.get("reason", "")).strip()[:500]
        return score, reason
    except Exception:
        return 0, "json_parse_error"


# ============================================================
# Asset save helper
# ============================================================

def _save_match_to_db_and_jsonl(
    *,
    task_id: int,
    job_id: int,
    profile_url: str,
    profile_name: str,
    profile_data: dict[str, Any],
    score: int,
    reason: str,
    asset_type: str,
    profiles_path: Path,
    jlog,
) -> int | None:
    """Salva il match come asset DB (con tag) + append a profiles.jsonl.
    Ritorna asset_id o None se fail.

    Usa la firma `db.upsert_asset(data: dict, tags: dict[str, list[str]])` con
    dedup automatico su source_url_canonical + asset_type."""
    asset_id: int | None = None
    try:
        tags = {
            "audience_match_task": [str(task_id)],
            "audience_score": [str(score)],
            "source_audience_discovery": ["true"],
            "platform": ["facebook"],
        }
        if reason:
            tags["audience_reason"] = [reason[:200]]
        # display_name dal profile_data se disponibile (più completo del cand_name)
        display_name = profile_data.get("display_name") or profile_name or ""
        asset_id = db.upsert_asset(
            {
                "source_url": profile_url,
                "asset_type": asset_type,
                "title": (display_name or "")[:300],
                "display_name": (display_name or None),
                "source_task_id": task_id,
                "source_job_id": job_id,
                "raw_json": json.dumps(profile_data, ensure_ascii=False, default=str),
            },
            tags=tags,
        )
    except Exception as e:
        jlog(f"⚠️ upsert_asset fail per {profile_url}: {e}")
        asset_id = None

    # Append profiles.jsonl (anche se asset save fallisce, il record finisce qui)
    try:
        rec = {
            "task_id": task_id,
            "job_id": job_id,
            "asset_id": asset_id,
            "profile_url": profile_url,
            "display_name": profile_name or profile_data.get("display_name"),
            "score": score,
            "reason": reason,
            "platform": "facebook",
            "source": "audience_discovery",
            "extracted_at": datetime.now(timezone.utc).isoformat(),
            "profile_data": profile_data,
        }
        with profiles_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
    except Exception as e:
        jlog(f"⚠️ profiles.jsonl write fail: {e}")

    return asset_id


# ============================================================
# Main entry-point
# ============================================================

async def run_agent(task: dict[str, Any], job_id: int) -> str:
    """Entry-point audience_discovery (v1: solo FB)."""
    db.update_job(job_id, status="running", started_at=db.now_iso())
    db.set_control_signal(job_id, None)

    def jlog(line: str) -> None:
        db.append_job_log(job_id, line)

    jlog(f'Avvio audience_discovery per task #{task["id"]} "{task["name"]}"')

    # ---- 0. Speed profile (anti-ban) ----
    speed = (task.get("speed_profile") or "safe").lower()
    if speed not in ("safe", "balanced", "aggressive"):
        speed = "safe"
    pause_min, pause_max = {
        "safe": (30, 180),
        "balanced": (15, 60),
        "aggressive": (10, 30),
    }[speed]

    def jitter_sleep() -> float:
        secs = random.uniform(pause_min, pause_max)
        return secs

    async def sleep_with_jitter() -> None:
        secs = jitter_sleep()
        jlog(f"  ⏳ pause anti-ban {secs:.1f}s")
        await asyncio.sleep(secs)

    jlog(f"⚡ Speed profile: '{speed}' — pause {pause_min}-{pause_max}s")

    # ---- 1. Kill-switch + validazioni ----
    if is_recon_disabled():
        msg = "RECON_SOCIAL_DISABLED=1: audience discovery disattivato. Abort."
        jlog(f"⛔ {msg}")
        db.update_job(job_id, status="error", error=msg, finished_at=db.now_iso())
        return ""

    if not is_configured():
        msg = "ARGOS_SECRET non settata: niente decifratura credenziali. Abort."
        jlog(f"❌ {msg}")
        db.update_job(job_id, status="error", error=msg, finished_at=db.now_iso())
        return ""

    platform = (task.get("social_platform") or "facebook").lower()
    if platform != "facebook":
        msg = f"v1 supporta solo Facebook (richiesto: {platform}). Abort."
        jlog(f"❌ {msg}")
        db.update_job(job_id, status="error", error=msg, finished_at=db.now_iso())
        return ""

    # ---- 2. Account FB loggato ----
    sender_id = task.get("recon_social_account_id")
    if not sender_id:
        msg = "recon_social_account_id mancante. Configura un account FB in /social/accounts."
        jlog(f"❌ {msg}")
        db.update_job(job_id, status="error", error=msg, finished_at=db.now_iso())
        return ""

    account = db.get_social_account(int(sender_id))
    if not account:
        msg = f"social_account #{sender_id} non trovato. Abort."
        jlog(f"❌ {msg}")
        db.update_job(job_id, status="error", error=msg, finished_at=db.now_iso())
        return ""
    if (account.get("platform") or "").lower() != "facebook":
        msg = f"social_account #{sender_id} non è Facebook ({account.get('platform')}). Abort."
        jlog(f"❌ {msg}")
        db.update_job(job_id, status="error", error=msg, finished_at=db.now_iso())
        return ""
    if account.get("status") != "active":
        msg = f"social_account #{sender_id} ('{account.get('username')}') status='{account.get('status')}', non active. Abort."
        jlog(f"❌ {msg}")
        db.update_job(job_id, status="error", error=msg, finished_at=db.now_iso())
        return ""

    jlog(f"Account FB: #{account['id']} '{account.get('username')}'")

    # ---- 3. Parametri task ----
    brief = (task.get("objective") or "").strip()
    if not brief:
        msg = "Brief audience (campo 'Obiettivo') mancante. Abort."
        jlog(f"❌ {msg}")
        db.update_job(job_id, status="error", error=msg, finished_at=db.now_iso())
        return ""

    anchor_seeds = task.get("seed_queries") or []
    if isinstance(anchor_seeds, str):
        anchor_seeds = [x.strip() for x in anchor_seeds.splitlines() if x.strip()]
    anchor_urls = [x for x in anchor_seeds if x.startswith(("http://", "https://"))]

    max_targets = int(task.get("recon_max_targets_per_day") or 50)
    score_threshold = int(task.get("recon_score_threshold") or 6)
    refresh_days = int(task.get("refresh_policy_days") or 7)
    output_asset_type = (task.get("output_asset_type") or "social_profile").strip() or "social_profile"
    max_iterations = int(task.get("max_iterations") or 30)  # cap step LLM

    jlog(f"Brief: {brief[:120]!r}")
    jlog(f"Anchor profiles: {len(anchor_urls)}")
    jlog(f"Cap audience: {max_targets}, score≥{score_threshold}, refresh≥{refresh_days}d")

    # ---- 4. LLM config ----
    llm_provider = (task.get("llm_provider") or "ollama").strip().lower()
    try:
        llm_base_url = resolve_base_url(llm_provider, task.get("llm_base_url"))
        llm_api_key = resolve_api_key(llm_provider, task.get("llm_api_key"))
    except Exception as e:
        msg = f"LLM config fail: {e}"
        jlog(f"❌ {msg}")
        db.update_job(job_id, status="error", error=msg, finished_at=db.now_iso())
        return ""
    llm_model = task.get("model") or "llama3.1:8b"
    jlog(f"LLM: {llm_provider} / {llm_model}")

    # ---- 5. Run dir + audit ----
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_dir = RESULTS_DIR / str(task["id"]) / ts
    run_dir.mkdir(parents=True, exist_ok=True)
    profiles_path = run_dir / "profiles.jsonl"
    # audit_path: ReconAudit hardcoda il filename a `recon_audit_log.jsonl`
    # (lo riusiamo perche' l'infrastruttura di audit/screenshot e' la stessa).
    report_path = run_dir / "report.md"
    audit = ReconAudit(run_dir, screenshot_every_n=10, enabled_screenshots=True)
    audit.log_event("RUN_START", job_id=job_id, account_id=account["id"], brief=brief[:200])

    # ---- 6. Browser persistent ----
    sess_dir = account.get("session_dir") or (Path("data/social_sessions") / account["uuid"])
    if not isinstance(sess_dir, Path):
        sess_dir = Path(str(sess_dir))
    headed = bool(int(task.get("headed", 1) or 0))
    jlog(f"Apertura browser (headed={headed}, session_dir={sess_dir})")

    p_handle = None
    context = None
    saved_count = 0
    candidates_pool: list[dict[str, str]] = []
    seen_urls: set[str] = set()
    keywords_used: list[str] = []
    groups_explored: list[str] = []

    try:
        p_handle, context, page = await _open_persistent_browser(headed=headed, user_data_dir=sess_dir)
        # SafeBrowser solo per audit (no enforcement: il runner non clicca su like/follow)
        safe = SafeBrowser(page, audit, strict=False)

        # ---- 6b. Verifica login ----
        if not await _verify_fb_logged_in(page, jlog):
            msg = "Browser FB non loggato. Vai a /social/accounts e fai login per questo account."
            db.update_job(job_id, status="error", error=msg, finished_at=db.now_iso())
            return ""

        # ============================================================
        # Fase 1: deduzione keyword dal brief (1 LLM call)
        # ============================================================
        jlog("─── Fase 1: deduzione keyword dal brief ───")
        keywords = await _llm_deduce_keywords(
            brief, llm_base_url=llm_base_url, llm_api_key=llm_api_key,
            llm_model=llm_model, jlog=jlog,
        )
        if not keywords:
            jlog("⚠️ Nessuna keyword dedotta dall'LLM. Procedo solo con anchor friends-of.")
        else:
            jlog(f"Keyword dedotte ({len(keywords)}): {keywords}")
            keywords_used = keywords
        audit.log_event("KEYWORDS_DEDUCED", keywords=keywords)

        # ============================================================
        # Fase 2: friends-of per ogni anchor profile
        # ============================================================
        if anchor_urls:
            jlog(f"─── Fase 2: friends-of per {len(anchor_urls)} anchor ───")
            for anchor in anchor_urls:
                if saved_count >= max_targets:
                    break
                jlog(f"  anchor: {anchor}")
                try:
                    friends = await facebook_audience.friends_of_profile(
                        page, anchor, limit=30, jlog=jlog,
                    )
                    for f in friends:
                        if f["url"] not in seen_urls:
                            seen_urls.add(f["url"])
                            candidates_pool.append({**f, "source": f"friends_of:{anchor}"})
                    audit.log_event("FRIENDS_OF_DONE", anchor=anchor, n_friends=len(friends))
                except Exception as e:
                    jlog(f"  ⚠️ friends_of_profile error: {e}")
                await sleep_with_jitter()
        else:
            jlog("─── Fase 2: nessun anchor → salto ───")

        # ============================================================
        # Fase 3: search_groups per ogni keyword
        # ============================================================
        groups_pool: list[dict[str, Any]] = []
        if keywords:
            jlog(f"─── Fase 3: search_groups per {len(keywords)} keyword ───")
            for kw in keywords:
                if saved_count >= max_targets:
                    break
                try:
                    found = await facebook_audience.search_groups(
                        page, kw, limit=5, jlog=jlog,
                    )
                    for g in found:
                        if g["url"] not in {x["url"] for x in groups_pool}:
                            groups_pool.append(g)
                    audit.log_event("SEARCH_GROUPS_DONE", keyword=kw, n_groups=len(found))
                except Exception as e:
                    jlog(f"  ⚠️ search_groups error: {e}")
                await sleep_with_jitter()
            jlog(f"Gruppi totali dedup: {len(groups_pool)}")

        # ============================================================
        # Fase 4: per ogni gruppo, raccogli autori post recenti
        # ============================================================
        if groups_pool:
            # Cap a 10 gruppi al max (per non superare anti-ban budget)
            for g in groups_pool[:10]:
                if saved_count >= max_targets or len(candidates_pool) >= max_targets * 3:
                    break
                jlog(f"  open group: {g['name']} ({g['url']})")
                try:
                    members = await facebook_audience.open_group_and_collect_members(
                        page, g["url"], scrolls=8, limit=30, jlog=jlog,
                    )
                    groups_explored.append(g["url"])
                    for m in members:
                        if m["url"] not in seen_urls:
                            seen_urls.add(m["url"])
                            candidates_pool.append({**m, "source": f"group:{g['url']}"})
                    audit.log_event("OPEN_GROUP_DONE", group=g["url"], n_members=len(members))
                except Exception as e:
                    jlog(f"  ⚠️ open_group error: {e}")
                await sleep_with_jitter()

        jlog(f"─── Fase 5: scoring di {len(candidates_pool)} candidati ───")

        # ============================================================
        # Fase 5: dedup vs DB + apri profilo + LLM score + save_match
        # ============================================================
        for i, cand in enumerate(candidates_pool):
            if saved_count >= max_targets:
                jlog(f"Cap audience raggiunto ({max_targets}), stop scoring.")
                break
            if i >= max_iterations:
                jlog(f"Cap max_iterations raggiunto ({max_iterations}), stop scoring.")
                break

            cand_url = cand["url"]
            cand_name = cand.get("name") or ""
            jlog(f"[{i + 1}/{len(candidates_pool)}] score: {cand_name} ({cand_url})")

            # Dedup vs DB (signature: has_recent_asset(source_url, asset_type, max_age_days))
            if refresh_days >= 0:
                try:
                    if db.has_recent_asset(cand_url, output_asset_type, max_age_days=refresh_days):
                        jlog(f"  ⏭️ skip: già in DB recente (refresh={refresh_days}d)")
                        audit.log_event("SKIP_RECENT_ASSET", url=cand_url)
                        continue
                except Exception as e:
                    jlog(f"  ⚠️ has_recent_asset error (procedo): {e}")

            # Apri profilo + extract
            try:
                await page.goto(cand_url, wait_until="domcontentloaded", timeout=30_000)
                await asyncio.sleep(2.0)
                profile_data = await facebook_recon.extract_profile_data(page)
            except Exception as e:
                jlog(f"  ⚠️ extract_profile error: {e}")
                audit.log_event("EXTRACT_FAIL", url=cand_url, error=str(e)[:200])
                continue

            # LLM score
            score, reason = await _llm_score_profile(
                brief, profile_data,
                llm_base_url=llm_base_url, llm_api_key=llm_api_key,
                llm_model=llm_model, jlog=jlog,
            )
            jlog(f"  score: {score}/10 — {reason[:80]}")
            audit.log_event("PROFILE_SCORED", url=cand_url, score=score, reason=reason[:200])

            if score >= score_threshold:
                asset_id = _save_match_to_db_and_jsonl(
                    task_id=int(task["id"]),
                    job_id=job_id,
                    profile_url=cand_url,
                    profile_name=cand_name,
                    profile_data=profile_data,
                    score=score,
                    reason=reason,
                    asset_type=output_asset_type,
                    profiles_path=profiles_path,
                    jlog=jlog,
                )
                if asset_id:
                    saved_count += 1
                    jlog(f"  ✅ SAVE (asset #{asset_id}) — totali: {saved_count}/{max_targets}")
                    audit.log_event("MATCH_SAVED", url=cand_url, asset_id=asset_id, score=score)
            else:
                jlog(f"  ⏭️ skip: score {score} < threshold {score_threshold}")

            await sleep_with_jitter()

    finally:
        if context:
            try:
                await context.close()
            except Exception:
                pass
        if p_handle:
            try:
                await p_handle.stop()
            except Exception:
                pass

    # ---- 7. Report markdown ----
    try:
        lines = [
            f"# Audience discovery — task #{task['id']}",
            "",
            f"**Brief**: {brief}",
            "",
            f"**Account FB**: #{account['id']} `{account.get('username')}`",
            f"**Speed profile**: {speed} (pause {pause_min}-{pause_max}s)",
            "",
            "## Risultati",
            "",
            f"- Profili matched salvati: **{saved_count}** / cap {max_targets}",
            f"- Candidati scoperti: {len(candidates_pool)}",
            f"- Keyword dedotte: {keywords_used}",
            f"- Anchor profili usati: {len(anchor_urls)}",
            f"- Gruppi esplorati: {len(groups_explored)}",
            "",
            "## File output",
            "",
            f"- `profiles.jsonl` — {saved_count} righe (1 per match)",
            "- `recon_audit_log.jsonl` — log azioni (filename ereditato da ReconAudit)",
            "",
            f"_Generated at {datetime.now(timezone.utc).isoformat()}_",
        ]
        report_path.write_text("\n".join(lines), encoding="utf-8")
    except Exception as e:
        jlog(f"⚠️ report write fail: {e}")

    audit.log_event("RUN_END", saved=saved_count, candidates=len(candidates_pool))
    db.update_job(
        job_id, status="done", finished_at=db.now_iso(),
        result_path=str(report_path),
    )
    jlog(f"✅ Done. Saved {saved_count} matched profiles. Report: {report_path}")
    return str(report_path)
