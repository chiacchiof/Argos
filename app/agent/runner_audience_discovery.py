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
from .run_reporter import RunReporter
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

    finish_reason = None
    try:
        async with httpx.AsyncClient(timeout=120) as client:
            r = await client.post(
                f"{llm_base_url.rstrip('/')}/chat/completions",
                json=payload,
                headers={"Authorization": f"Bearer {llm_api_key}"},
            )
            r.raise_for_status()
            data = r.json()
            choice = data.get("choices", [{}])[0]
            txt = (choice.get("message", {}).get("content", "") or "").strip()
            finish_reason = choice.get("finish_reason")
    except Exception as e:
        jlog(f"⚠️ LLM keyword deduction fail: {e}")
        return []

    # Heuristica thinking-mode: content vuoto + finish_reason='length' (Ollama
    # OpenAI compat layer non espone message.thinking, lo butta via). Modelli
    # qwen3.6, qwen3-thinking, deepseek-r1, ecc. cadono qui se max_tokens basso.
    if not txt and finish_reason == "length":
        jlog(
            f"❌ Il modello '{llm_model}' sembra essere in thinking-mode "
            "(content vuoto + finish_reason=length). Ollama OpenAI compat non "
            "espone message.thinking. Cambia il modello del task a un "
            "non-thinking: qwen3-coder:30b o gpt-oss:20b locale, oppure "
            "gpt-4o-mini cloud. Vedi _PLANNER_MANUAL sezione 'Scelta del campo model'."
        )
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
- testi di sotto-pagine ad alto valore se disponibili:
  - "about_overview": sezione "Informazioni" (può contenere data di nascita,
    città, lavoro, scuola — segnali demografici espliciti)
  - "likes_pages" / "pages_liked": pagine pubbliche seguite dall'utente
    (eccellente segnale di interesse: pagine vintage/anni 80/moda/musica/ecc.)
- contesto di provenienza opzionale: il gruppo Facebook tematico in cui il
  candidato è stato trovato attivo (postando o commentando).

Strategia di scoring (importante):
- Se il brief richiede caratteristiche demografiche (età, città, ecc.) e i
  dati le ESPLICITAMENTE confermano → score alto (8-10).
- Se NON dichiarate ma INFERIBILI da segnali indiretti coerenti (es. anno
  scolastico, foto epoca, lessico, like a pagine specifiche) → score medio
  (5-7) con prudenza.
- Se NON dichiarate e nessun segnale indiretto → score basso (1-4): non
  bocciare per assenza di dato, ma non confermare senza evidenza.
- Privilegia i SEGNALI DI INTERESSE (post + likes_pages) sui dati anagrafici
  spesso assenti su FB. Un utente che segue "Moda anni 80" e "Magliette
  vintage" è un match forte anche senza età dichiarata.
- **PROVENIENZA DAL GRUPPO = SEGNALE FORTE (peso doppio nello scoring)**: se
  il candidato è stato trovato attivo (postando, commentando) in un gruppo
  Facebook il cui NOME è semanticamente coerente col brief, considera che
  la sola appartenenza al gruppo è già un'EVIDENZA DI INTERESSE diretta. Es:
  brief "magliette anni 80" + provenienza "Nostalgia Anni 80 Italia" → il
  candidato HA mostrato attivamente interesse per il tema, non serve cercare
  conferme nei dati profilo. In questo caso almeno score 6-7. Se anche le
  sotto-pagine confermano (likes_pages tematici, età compatibile) → 8-9.
  Non penalizzare la mancanza di età esplicita quando la provenienza dal
  gruppo è coerente: chi partecipa a "Anni 80" verosimilmente ha vissuto
  quegli anni (= demografia compatibile inferibile).

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
    subpage_texts: dict[str, str] | None = None,
    source_context: dict[str, str] | None = None,
) -> tuple[int, str]:
    """1 LLM call per profilo: ritorna (score 0-10, reason). Score 0 se errore.

    `subpage_texts` opzionale: {label: body_text} prodotto da
    `facebook_recon.collect_subpage_texts` (es. {"about_overview": "...",
    "likes_pages": "..."}). Quando presente, viene incluso nel prompt come
    "Sotto-pagine ad alto valore" per arricchire lo scoring (segnali
    demografici espliciti + pagine seguite = indicatori di interesse).

    `source_context` opzionale: {"group_name": str, "group_url": str} con il
    gruppo Facebook in cui il candidato è stato trovato attivo. Quando presente,
    è iniettato nel prompt come segnale forte di interesse coerente col brief
    (il system prompt istruisce il LLM a usarlo per peso doppio nello scoring).
    Risolve il caso job #163: i candidati venivano scartati per "età non
    dichiarata" anche se erano attivi in gruppi tematici "Magliette Anni 80".
    """
    profile_json = json.dumps(profile_data, ensure_ascii=False, default=str)[:6000]
    # Subpage block opzionale (max 2000 char per label per non sforare il context).
    subpage_block = ""
    if subpage_texts:
        parts: list[str] = []
        for label, text in subpage_texts.items():
            snippet = (text or "").strip()[:2000]
            if snippet:
                parts.append(f"--- {label} ---\n{snippet}")
        if parts:
            subpage_block = "\n\nSotto-pagine ad alto valore:\n" + "\n\n".join(parts)
    # Source-group block opzionale: provenienza tematica come segnale di interesse.
    source_block = ""
    if source_context:
        gname = (source_context.get("group_name") or "").strip()
        gurl = (source_context.get("group_url") or "").strip()
        if gname or gurl:
            source_block = (
                "\n\nProvenienza candidato (SEGNALE FORTE — il candidato è stato "
                "trovato ATTIVO in questo gruppo Facebook tematico):\n"
                f"- Nome gruppo: \"{gname or '(senza nome)'}\"\n"
                f"- URL gruppo: {gurl}\n"
                "Il gruppo è stato individuato cercando keyword del brief: la sola "
                "presenza ATTIVA del candidato nel gruppo dimostra interesse coerente "
                "col brief. Considera che ha già auto-segnalato compatibilità con il "
                "tema (vedi regola PROVENIENZA DAL GRUPPO nel system prompt)."
            )
    payload = {
        "model": llm_model,
        "messages": [
            {"role": "system", "content": _SCORE_SYSTEM_PROMPT},
            {"role": "user", "content": (
                f"Brief audience:\n{brief}\n\n"
                f"Dati profilo:\n{profile_json}"
                f"{subpage_block}"
                f"{source_block}\n\n"
                "Produci il JSON {\"score\": N, \"reason\": \"...\"}."
            )},
        ],
        "temperature": 0.2,
        "max_tokens": 400,
    }
    maybe_add_keep_alive(payload, llm_base_url)

    finish_reason = None
    try:
        async with httpx.AsyncClient(timeout=120) as client:
            r = await client.post(
                f"{llm_base_url.rstrip('/')}/chat/completions",
                json=payload,
                headers={"Authorization": f"Bearer {llm_api_key}"},
            )
            r.raise_for_status()
            data = r.json()
            choice = data.get("choices", [{}])[0]
            txt = (choice.get("message", {}).get("content", "") or "").strip()
            finish_reason = choice.get("finish_reason")
    except Exception as e:
        jlog(f"⚠️ LLM score fail: {e}")
        return 0, "llm_error"

    if not txt and finish_reason == "length":
        jlog(
            f"❌ Score: il modello '{llm_model}' sembra thinking-mode "
            "(content vuoto + finish_reason=length). Cambia il modello del task."
        )
        return 0, "thinking_mode_detected"

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
    source_context: dict[str, str] | None = None,
) -> int | None:
    """Salva il match come asset DB (con tag) + append a profiles.jsonl.
    Ritorna asset_id o None se fail.

    `source_context` opzionale: {"group_name": str, "group_url": str} con il
    gruppo Facebook di provenienza del candidato. Se valorizzato, viene
    scritto come tag `source_group_url` + `source_group_name` per consentire
    filtering downstream (es. outreach mirato "manda DM a chi viene dal gruppo X").

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
        # Tag provenienza gruppo (filtering downstream): pattern coerente con
        # gli altri tag `source_*` di sistema. Skippa se vuoto.
        if source_context:
            g_url = (source_context.get("group_url") or "").strip()
            g_name = (source_context.get("group_name") or "").strip()
            if g_url:
                tags["source_group_url"] = [g_url]
            if g_name:
                tags["source_group_name"] = [g_name[:200]]
        # display_name dal profile_data se disponibile (più completo del cand_name)
        display_name = profile_data.get("display_name") or profile_name or ""
        # raw_json arricchito col source_context (nel caso un futuro consumer
        # voglia leggerlo strutturato senza passare per i tag).
        raw_payload = dict(profile_data)
        if source_context:
            raw_payload["_source_group"] = {
                "url": source_context.get("group_url") or "",
                "name": source_context.get("group_name") or "",
            }
        asset_id = db.upsert_asset(
            {
                "source_url": profile_url,
                "asset_type": asset_type,
                "title": (display_name or "")[:300],
                "display_name": (display_name or None),
                "source_task_id": task_id,
                "source_job_id": job_id,
                "raw_json": json.dumps(raw_payload, ensure_ascii=False, default=str),
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
        if source_context:
            rec["source_group_url"] = source_context.get("group_url") or ""
            rec["source_group_name"] = source_context.get("group_name") or ""
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
    # RunReporter: produce report.md arricchito alla fine (con diagnostica
    # & suggerimenti basati sulle metriche per-fase registrate sotto).
    reporter = RunReporter(task, job_id, run_dir)
    reporter.add_output("profiles.jsonl", "1 riga JSON per profilo matched salvato")
    reporter.add_output("recon_audit_log.jsonl", "log strutturato eventi del run")
    reporter.add_output("screenshots/", "screenshot periodici (anti-ban + debug)")

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
        p_keywords = reporter.start_phase("keywords", description="Deduzione keyword dal brief (1 LLM call)")
        keywords = await _llm_deduce_keywords(
            brief, llm_base_url=llm_base_url, llm_api_key=llm_api_key,
            llm_model=llm_model, jlog=jlog,
        )
        if not keywords:
            jlog("⚠️ Nessuna keyword dedotta dall'LLM. Procedo solo con anchor friends-of.")
            reporter.end_phase(p_keywords, status="empty", items_in=1, items_out=0, keywords=[])
        else:
            jlog(f"Keyword dedotte ({len(keywords)}): {keywords}")
            keywords_used = keywords
            reporter.end_phase(p_keywords, status="ok", items_in=1, items_out=len(keywords), keywords=keywords)
        audit.log_event("KEYWORDS_DEDUCED", keywords=keywords)

        # ============================================================
        # Fase 2: friends-of per ogni anchor profile
        # ============================================================
        if anchor_urls:
            jlog(f"─── Fase 2: friends-of per {len(anchor_urls)} anchor ───")
            p_anchor = reporter.start_phase(
                "anchor_friends",
                description=f"friends-of per {len(anchor_urls)} anchor profili",
            )
            n_friends_total = 0
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
                            n_friends_total += 1
                    audit.log_event("FRIENDS_OF_DONE", anchor=anchor, n_friends=len(friends))
                except Exception as e:
                    jlog(f"  ⚠️ friends_of_profile error: {e}")
                await sleep_with_jitter()
            reporter.end_phase(
                p_anchor,
                status="ok" if n_friends_total > 0 else "empty",
                items_in=len(anchor_urls),
                items_out=n_friends_total,
            )
        else:
            jlog("─── Fase 2: nessun anchor → salto ───")
            reporter.skip_phase("anchor_friends", reason="nessun anchor profile nel seed")

        # ============================================================
        # Fase 3: search_groups per ogni keyword
        # ============================================================
        groups_pool: list[dict[str, Any]] = []
        if keywords:
            jlog(f"─── Fase 3: search_groups per {len(keywords)} keyword ───")
            p_sg = reporter.start_phase(
                "search_groups",
                description=f"search_groups per {len(keywords)} keyword",
            )
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
            reporter.end_phase(
                p_sg,
                status="ok" if groups_pool else "empty",
                items_in=len(keywords),
                items_out=len(groups_pool),
            )
        else:
            reporter.skip_phase("search_groups", reason="nessuna keyword da Fase 1")

        # ============================================================
        # Fase 4: per ogni gruppo, raccogli autori post recenti
        # ============================================================
        if groups_pool:
            jlog(f"─── Fase 4: open_group per {min(10, len(groups_pool))} gruppi (cap interno) ───")
            p_og = reporter.start_phase(
                "open_groups",
                description="raccolta autori post dai primi 10 gruppi",
            )
            n_authors_added = 0
            zero_author_groups = 0
            n_groups_to_open = min(10, len(groups_pool))
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
                    added_this_group = 0
                    for m in members:
                        if m["url"] not in seen_urls:
                            seen_urls.add(m["url"])
                            # Traccia provenienza gruppo: usato in Fase 5 come segnale
                            # forte per lo scoring LLM (chi posta in un gruppo tematico
                            # ha già un interesse coerente col brief) e come tag su
                            # asset per filtering downstream (outreach mirato per gruppo).
                            candidates_pool.append({
                                **m,
                                "source": f"group:{g['url']}",
                                "source_group_url": g["url"],
                                "source_group_name": g.get("name") or "",
                            })
                            n_authors_added += 1
                            added_this_group += 1
                    if added_this_group == 0:
                        zero_author_groups += 1
                    audit.log_event("OPEN_GROUP_DONE", group=g["url"], n_members=len(members))
                except Exception as e:
                    jlog(f"  ⚠️ open_group error: {e}")
                await sleep_with_jitter()
            reporter.end_phase(
                p_og,
                status="ok" if n_authors_added > 0 else "empty",
                items_in=n_groups_to_open,
                items_out=n_authors_added,
                zero_author_groups=zero_author_groups,
                groups_total_in_pool=len(groups_pool),
            )
        else:
            reporter.skip_phase("open_groups", reason="nessun gruppo da Fase 3")

        jlog(f"─── Fase 5: scoring di {len(candidates_pool)} candidati ───")

        # ============================================================
        # Fase 5: dedup vs DB + apri profilo + LLM score + save_match
        # ============================================================
        p_sc = reporter.start_phase(
            "scoring",
            description="dedup + extract_profile + LLM score + save",
        )
        score_distribution: list[int] = []
        n_skipped_low = 0
        n_skipped_dedup = 0
        n_extract_fail = 0
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
                        n_skipped_dedup += 1
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
                n_extract_fail += 1
                continue

            # Sotto-pagine ad alto valore (/about + /likes_pages) per scoring
            # arricchito. Best-effort: pagine private/redirect non bloccano,
            # ritornano dict vuoto. Costo: +2 goto FB per candidato (= +30-60s
            # con speed_profile=balanced) ma migliora drasticamente la qualità
            # dello score perché bio/post visibili spesso non bastano (vedi
            # job #162: score medio 1.9 senza /about+/likes_pages).
            subpage_texts: dict[str, str] = {}
            try:
                subpage_texts = await facebook_recon.collect_subpage_texts(
                    page, safe, cand_url, jlog=jlog,
                )
                if subpage_texts:
                    jlog(f"  📄 sub-pages: {list(subpage_texts.keys())}")
            except Exception as e:
                jlog(f"  ⚠️ collect_subpage_texts error (procedo senza): {e}")

            # Source context: il gruppo Facebook di provenienza è un segnale di
            # interesse forte (chi posta in un gruppo tematico ha già auto-dichiarato
            # compatibilità col tema). Passato al LLM scorer, gestito con peso doppio
            # nel system prompt. Fix #163: senza questo, il LLM bocciava per "età non
            # dichiarata" candidati che erano attivi in "Magliette Anni 80".
            source_context = None
            if cand.get("source_group_url"):
                source_context = {
                    "group_name": cand.get("source_group_name") or "",
                    "group_url": cand.get("source_group_url") or "",
                }

            # LLM score (con dati arricchiti se presenti)
            score, reason = await _llm_score_profile(
                brief, profile_data,
                llm_base_url=llm_base_url, llm_api_key=llm_api_key,
                llm_model=llm_model, jlog=jlog,
                subpage_texts=subpage_texts or None,
                source_context=source_context,
            )
            jlog(f"  score: {score}/10 — {reason[:80]}")
            audit.log_event("PROFILE_SCORED", url=cand_url, score=score, reason=reason[:200])
            score_distribution.append(score)

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
                    source_context=source_context,
                )
                if asset_id:
                    saved_count += 1
                    jlog(f"  ✅ SAVE (asset #{asset_id}) — totali: {saved_count}/{max_targets}")
                    audit.log_event("MATCH_SAVED", url=cand_url, asset_id=asset_id, score=score)
            else:
                jlog(f"  ⏭️ skip: score {score} < threshold {score_threshold}")
                n_skipped_low += 1

            await sleep_with_jitter()

        reporter.end_phase(
            p_sc,
            status="ok",
            items_in=len(candidates_pool),
            items_out=saved_count,
            saved=saved_count,
            skipped_low_score=n_skipped_low,
            skipped_dedup=n_skipped_dedup,
            extract_fail=n_extract_fail,
            score_distribution=score_distribution,
        )

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

    # ---- 7. Report arricchito via RunReporter ----
    # Registra metriche aggregate finali (le fasi si sono auto-popolate sopra).
    reporter.add_metric("saved_count", saved_count)
    reporter.add_metric("candidates_scoperti", len(candidates_pool))
    reporter.add_metric("anchor_profili_usati", len(anchor_urls))
    reporter.add_metric("gruppi_esplorati", len(groups_explored))
    reporter.add_metric("keyword_dedotte", keywords_used)
    reporter.add_metric("speed_profile", f"{speed} (pause {pause_min}-{pause_max}s)")
    reporter.add_metric("account_fb", f"#{account['id']} {account.get('username')!r}")
    reporter.set_final_status("ok")
    try:
        reporter.write(report_path)
    except Exception as e:
        jlog(f"⚠️ report write fail: {e}")

    audit.log_event("RUN_END", saved=saved_count, candidates=len(candidates_pool))
    db.update_job(
        job_id, status="done", finished_at=db.now_iso(),
        result_path=str(report_path),
    )
    jlog(f"✅ Done. Saved {saved_count} matched profiles. Report: {report_path}")
    return str(report_path)
