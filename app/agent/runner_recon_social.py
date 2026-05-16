"""Runner recon_social — esplorazione di profili social con account loggato.

Agent mode: `recon_social`. Due modalità:
  - R1 (`recon_mode='url_driven'`): lista URL → per ogni URL → goto + extract
    + LLM riempie schema utente → append profiles.jsonl. NESSUNA decisione LLM
    su navigazione, lista chiusa.
  - R2 (`recon_mode='exploration'`): agente ReAct (TODO Fase 2). Parte dal
    proprio profilo + obiettivo NL.

Riusa l'infrastruttura `app/agent/social/`:
- `social_accounts` cifrato (qui solo per IG/TikTok che hanno password;
  FB session via QR-like flow, idem WA)
- Engine Playwright headed/headless con stealth
- Persistent context (`launch_persistent_context`) per ogni account social
  → IndexedDB persistito → niente re-login ogni run

Vincoli safety (Fase 1):
- Solo `goto` + `extract` (no click su like/commenta/etc → R2 quando avremo
  l'agente). SafeBrowser è usato solo per audit log.
- Kill-switch globale `RECON_SOCIAL_DISABLED=1`.

Output:
- `data/results/<task_id>/<ts>/profiles.jsonl` — UNA riga per URL processato
- `data/results/<task_id>/<ts>/recon_audit_log.jsonl` — audit completo azioni
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

from .. import db
from ..config import RESULTS_DIR
from .llm_providers import resolve_api_key, resolve_base_url
from .runner_control import RunnerStopped, wait_if_paused_or_stop
from .social import facebook_recon, instagram_recon, tiktok_recon
from .social.crypto_creds import is_configured
from .social.safe_browser import (
    BlockedActionError,
    ReconAudit,
    SafeBrowser,
    is_recon_disabled,
)


log = logging.getLogger(__name__)


# Mapping platform → modulo recon
_RECON_BY_PLATFORM = {
    "facebook": facebook_recon,
    "instagram": instagram_recon,
    "tiktok": tiktok_recon,
}


def _detect_platform(url: str) -> str | None:
    """Determina quale modulo recon usare in base all'host."""
    try:
        host = (urlparse(url).hostname or "").lower()
    except Exception:
        return None
    if "facebook.com" in host or "fb.com" in host or "m.facebook.com" in host:
        return "facebook"
    if "instagram.com" in host:
        return "instagram"
    if "tiktok.com" in host:
        return "tiktok"
    return None


def _looks_like_url(s: str) -> bool:
    """True se la stringa sembra una URL (http/https), False se è testo libero."""
    s = (s or "").strip()
    if not s:
        return False
    if s.startswith(("http://", "https://")):
        return True
    # Bare domains: "facebook.com/foo"
    if re.match(r"^(?:www\.)?[a-z0-9-]+\.[a-z]{2,}/", s):
        return True
    return False


async def _resolve_seed_to_url(
    page, seed_entry: str, account_platform: str, jlog,
    *, debug_dir=None,
) -> tuple[str | None, str | None, str]:
    """Per ogni riga del seed: se è già URL, ritornala. Altrimenti fai search
    sulla platform dell'account loggato e prendi il primo match certo.

    Se `debug_dir` è valorizzato e la piattaforma supporta diagnostica
    (es. facebook), il modulo dumpa HTML+screenshot+anchor list nel dir.

    Ritorna (url_risolto_o_None, platform_detected, reason).
    """
    s = (seed_entry or "").strip()
    if not s:
        return None, None, "empty"

    if _looks_like_url(s):
        if not s.startswith(("http://", "https://")):
            s = "https://" + s
        plat = _detect_platform(s)
        return (s if plat else None), plat, "is_url"

    # NON è una URL → assumiamo sia un nome da cercare sulla platform dell'account
    plat = (account_platform or "").lower()
    module = _RECON_BY_PLATFORM.get(plat)
    if not module or not hasattr(module, "search_user_by_name"):
        return None, plat, f"search_not_implemented_for_{plat}"

    try:
        # Solo facebook_recon supporta debug_dir per ora; passalo con getattr
        import inspect as _ins
        sig = _ins.signature(module.search_user_by_name)
        kwargs = {"jlog": jlog}
        if "debug_dir" in sig.parameters and debug_dir is not None:
            kwargs["debug_dir"] = debug_dir
        url, reason = await module.search_user_by_name(page, s, **kwargs)
        return url, plat, reason
    except Exception as e:
        return None, plat, f"search_error: {type(e).__name__}: {e}"


async def _llm_fill_schema(
    text_context: str,
    schema_text: str,
    *,
    llm_base_url: str,
    llm_api_key: str,
    llm_model: str,
) -> tuple[dict[str, Any] | None, str]:
    """Chiama l'LLM per riempire lo schema dal testo estratto dalla pagina.

    Ritorna (dict_o_None, raw_response).
    """
    import httpx

    # Cap totale del contesto inviato all'LLM: 18k char (≈ 4500 token).
    # Lo schema profile_interests può ricevere main_body (~6k) + 2-3 sotto-pagine
    # (~4k l'una) + dati strutturati. Tronchiamo solo se eccediamo.
    text_truncated = text_context[:18000]
    payload = {
        "model": llm_model,
        "messages": [
            {
                "role": "system",
                "content": (
                    "Sei un parser deterministico e un analista di profili social. "
                    "Ricevi (1) il testo estratto da MULTIPLE sezioni di una "
                    "pagina-profilo (pagina principale + sotto-pagine etichettate "
                    "tipo '=== SOTTO-PAGINA: /about ===' e '=== SOTTO-PAGINA: "
                    "/likes_pages ===') e (2) uno schema JSON. Ritorna UN JSON "
                    "con i campi richiesti. Se un campo non è presente, metti "
                    "null o lista vuota. NON inventare valori; cita evidenze. "
                    "Solo JSON, niente prosa. Il campo `narrative_summary`, se "
                    "richiesto dallo schema, deve essere un testo italiano di "
                    "300-500 parole come stringa unica (con \\n per a-capo)."
                ),
            },
            {
                "role": "user",
                "content": (
                    f"TESTO PROFILO (più sezioni etichettate):\n{text_truncated}\n\n"
                    f"SCHEMA:\n{schema_text}\n\nRitorna il JSON ora."
                ),
            },
        ],
        "temperature": 0.0,
        "max_tokens": 2200,
        "response_format": {"type": "json_object"},
    }
    # Per Ollama OpenAI-compat: forza format=json
    if "11434" in llm_base_url or ("/v1" in llm_base_url and
        "openai.com" not in llm_base_url and "anthropic.com" not in llm_base_url):
        payload["format"] = "json"

    raw = ""
    try:
        async with httpx.AsyncClient(timeout=120) as client:
            r = await client.post(
                f"{llm_base_url.rstrip('/')}/chat/completions",
                json=payload,
                headers={"Authorization": f"Bearer {llm_api_key}"},
            )
            r.raise_for_status()
            data = r.json()
            raw = (data.get("choices", [{}])[0].get("message", {}).get("content", "") or "").strip()
    except Exception as e:
        log.warning("LLM fill schema fail: %s", e)
        return None, f"<HTTP_ERROR: {e}>"

    if not raw:
        return None, ""

    # Strip <think>...</think> dei modelli Qwen3
    cleaned = re.sub(r"<think>[\s\S]*?</think>", "", raw, flags=re.IGNORECASE).strip()
    for candidate in (cleaned, raw):
        try:
            obj = json.loads(candidate)
            if isinstance(obj, dict):
                return obj, raw
        except json.JSONDecodeError:
            continue
    m = re.search(r"\{[\s\S]+\}", cleaned)
    if m:
        try:
            obj = json.loads(m.group(0))
            if isinstance(obj, dict):
                return obj, raw
        except json.JSONDecodeError:
            pass
    return None, raw


async def _llm_generate_narrative(
    text_context: str,
    structured_obj: dict[str, Any],
    *,
    llm_base_url: str,
    llm_api_key: str,
    llm_model: str,
) -> str:
    """Fallback dedicato: genera SOLO il `narrative_summary` quando la chiamata
    principale lo lascia vuoto (LLM aperto che esaurisce token o ignora il campo).

    Niente response_format JSON, niente schema: chiediamo prosa libera.
    Le info strutturate già estratte (`structured_obj`) entrano nel context
    come "fatti accertati", così il LLM ha un punto di partenza solido.
    """
    import httpx

    facts_parts: list[str] = []
    for k in (
        "display_name", "location", "professional_field", "education",
        "language",
    ):
        v = structured_obj.get(k)
        if v:
            facts_parts.append(f"  - {k}: {v}")
    for k in ("hobbies", "interests_inferred", "liked_pages_visible",
              "joined_groups_visible", "recent_topics", "work_history",
              "tagged_themes"):
        v = structured_obj.get(k) or []
        if isinstance(v, list) and v:
            facts_parts.append(f"  - {k}: {', '.join(str(x) for x in v[:10])}")
    facts_text = "\n".join(facts_parts) if facts_parts else "  (nessun fatto strutturato disponibile)"

    user_prompt = (
        "Sei uno scout di profili social. Hai ricevuto il TESTO RAW della "
        "pagina-profilo (concatenato da pagina principale + sotto-pagine come "
        "/about) e i FATTI ESTRATTI da un parser deterministico.\n\n"
        f"FATTI ESTRATTI:\n{facts_text}\n\n"
        f"TESTO RAW DELLA PAGINA (multi-sezione):\n{text_context[:14000]}\n\n"
        "Scrivi una sintesi narrativa in italiano di 300-500 parole su questa "
        "persona: chi è, cosa fa nella vita, interessi e passioni con evidenze "
        "puntuali tratte dal testo, tono dei contenuti, eventuali ipotesi su "
        "lifestyle / età / contesto sociale. CITA testualmente 1-3 elementi "
        "(pagine liked, post, dati anagrafici) per dare colore. Non inventare. "
        "Se i dati sono pochi, scrivi 100-150 parole spiegando il perché.\n\n"
        "Output: SOLO il testo italiano della sintesi, niente prefissi o JSON."
    )

    payload = {
        "model": llm_model,
        "messages": [
            {"role": "system", "content": "Sei un analista di profili social. Scrivi in italiano fluido."},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0.3,
        "max_tokens": 1200,
    }

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
        log.warning("narrative fallback LLM fail: %s", e)
        return ""

    # Strip thinking tags + eventuale frame JSON spurio
    txt = re.sub(r"<think>[\s\S]*?</think>", "", txt, flags=re.IGNORECASE).strip()
    # A volte i modelli restituiscono ancora `{"narrative_summary": "..."}` — estrai
    try:
        candidate = json.loads(txt)
        if isinstance(candidate, dict):
            inner = candidate.get("narrative_summary") or candidate.get("summary") or ""
            if inner:
                return str(inner).strip()
    except Exception:
        pass
    return txt


async def _open_persistent_browser(
    *, headed: bool, user_data_dir: Path
):
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


async def _ensure_logged_in(
    page, context, account: dict, jlog, audit,
) -> bool:
    """Naviga alla home della platform e verifica che la sessione sia loggata.

    Se non loggato E esiste uno storage_state legacy (salvato da outreach_social
    via `data/sessions/<uuid>.json`), prova migrazione one-shot dei cookies
    nel persistent context, poi reload e re-check.

    Ritorna True se loggato (o non determinabile), False se sicuramente non
    loggato anche dopo migrazione.
    """
    plat = (account.get("platform") or "").lower()
    home_url = {
        "facebook": "https://www.facebook.com/",
        "instagram": "https://www.instagram.com/",
        "tiktok": "https://www.tiktok.com/",
    }.get(plat)
    if not home_url:
        return True  # platform sconosciuta → lascia procedere, fail dopo

    try:
        await page.goto(home_url, wait_until="domcontentloaded", timeout=30_000)
    except Exception as e:
        jlog(f"  ⚠️ goto({home_url}) fallita: {e}")
        return False
    await asyncio.sleep(2.0)

    # Tenta di accettare il banner cookie (IG mostra "Consenti tutti i cookie"
    # / "Rifiuta cookie facoltativi" su prima visita). Se non c'e' va bene.
    async def _dismiss_cookie_banner() -> None:
        cookie_buttons = [
            'button:has-text("Consenti tutti")',
            'button:has-text("Allow all")',
            'button:has-text("Accept All")',
            'button:has-text("Accetta tutto")',
            'button:has-text("Rifiuta")',
            'button:has-text("Decline")',
        ]
        for sel in cookie_buttons:
            try:
                btn = page.locator(sel).first
                if await btn.is_visible(timeout=600):
                    await btn.click()
                    jlog(f"  🍪 cookie banner accettato (selettore: {sel!r})")
                    await asyncio.sleep(1.2)
                    return
            except Exception:
                continue
    await _dismiss_cookie_banner()

    # Detection "non loggato" platform-specific. Strategia POSITIVA: cerco
    # segnali SPECIFICI di account loggato. Se non trovo NULLA di loggato,
    # assumo non loggato.
    async def _is_login_required() -> bool:
        url_low = (page.url or "").lower()
        if plat == "facebook":
            try:
                if await page.locator('input[name="email"]').first.is_visible(timeout=1500):
                    if await page.locator('input[name="pass"]').first.is_visible(timeout=800):
                        return True
            except Exception:
                pass
            if "/login" in url_low:
                return True
        elif plat == "instagram":
            # URL check: se è in /accounts/login/emailsignup → sicuramente NON loggato
            if "/accounts/login" in url_low or "/accounts/emailsignup" in url_low:
                return True
            # Segnali di NON loggato (presenti SOLO sulla landing anonima):
            # bottone "Accedi" o "Iscriviti" prominenti in topbar
            negative_markers = [
                'a[href="/accounts/login/"]',
                'a[href*="/accounts/emailsignup"]',
                'button:has-text("Accedi")',
                'button:has-text("Log in")',
                'div:has-text("Hai un account?")',
                'div:has-text("Have an account?")',
            ]
            for sel in negative_markers:
                try:
                    if await page.locator(sel).first.is_visible(timeout=600):
                        return True  # vedo bottone Accedi → NOT logged
                except Exception:
                    continue
            # Segnale POSITIVO di loggato: presenza di nav-icon SPECIFICI
            # per utente loggato (Casa con aria-label preciso, NON 'Instagram')
            positive_markers = [
                'a[href="/"][role="link"] svg[aria-label="Home"]',
                'a[href="/"][role="link"] svg[aria-label="Casa"]',
                'a[href*="/direct/"] svg',  # icona messaggi DM (solo loggato)
                'svg[aria-label="Direct"]',
                'svg[aria-label="Messenger"]',
            ]
            for sel in positive_markers:
                try:
                    if await page.locator(sel).first.is_visible(timeout=600):
                        return False  # icone loggato visibili → LOGGED
                except Exception:
                    continue
            # Default conservativo: se non vedo né markers di loggato né
            # di non-loggato, assumo NON LOGGATO (più sicuro che procedere
            # con session sbagliata e prendere "5 skeleton followers")
            return True
        elif plat == "tiktok":
            try:
                if await page.locator('button:has-text("Log in")').first.is_visible(timeout=1500):
                    return True
            except Exception:
                pass
        return False

    not_logged = await _is_login_required()
    if not not_logged:
        audit.log_event("LOGIN_CHECK", platform=plat, status="logged_in_native")
        jlog(f"  ✅ {plat}: sessione loggata rilevata")
        return True
    jlog(f"  ⚠️ {plat}: NON loggato (landing page o login wall rilevato)")

    # NON loggato: prova migrazione legacy storage_state (one-shot, da
    # vecchi outreach_social). Se non c'è, skipperemo la migrazione e
    # passeremo direttamente al wait login manuale.
    jlog(f"  ⚠️ Sessione persistent vuota per {plat}. Provo migrazione one-shot da legacy storage_state...")
    from .social.session_manager import load_session_state
    state = load_session_state(account["uuid"])
    if state and state.get("cookies"):
        try:
            await context.add_cookies(state["cookies"])
            jlog(f"  🔄 importati {len(state['cookies'])} cookies legacy")
            audit.log_event("LOGIN_MIGRATED_COOKIES", platform=plat, n_cookies=len(state["cookies"]))
            try:
                await page.goto(home_url, wait_until="domcontentloaded", timeout=30_000)
            except Exception:
                pass
            await asyncio.sleep(2.0)
            still_not_logged = await _is_login_required()
            if not still_not_logged:
                jlog(f"  ✅ Login OK dopo migrazione cookies. Sessione persistita.")
                audit.log_event("LOGIN_OK_AFTER_MIGRATION", platform=plat)
                return True
            jlog(f"  ⚠️ Migrazione cookies insufficiente (sessione legacy scaduta).")
        except Exception as e:
            jlog(f"  ⚠️ add_cookies fail: {e}")
    else:
        jlog(f"  ℹ️ Nessun legacy state per {account['uuid']}. Skip migrazione, vado al login manuale.")
        audit.log_event("LOGIN_NO_LEGACY_STATE", platform=plat)

    # WAIT LOGIN MANUALE: il browser è headed (raccomandato per recon_social).
    # Naviga al form di login e ASPETTA fino a 15 minuti che l'utente faccia
    # login a mano. La sessione persistente conserverà i cookie per i run
    # successivi → niente login richiesto al prossimo run.
    jlog(f"  ⏳ Apro la pagina di login e ATTENDO che tu faccia login a mano (max 15 min).")
    audit.log_event("LOGIN_WAIT_MANUAL", platform=plat)
    login_url = {
        "facebook": "https://www.facebook.com/login",
        "instagram": "https://www.instagram.com/accounts/login/",
        "tiktok": "https://www.tiktok.com/login",
    }.get(plat, home_url)
    try:
        await page.goto(login_url, wait_until="domcontentloaded", timeout=30_000)
        await asyncio.sleep(1.5)
        # Riprova ad accettare il cookie banner se si è ripresentato
        await _dismiss_cookie_banner()
    except Exception:
        pass

    jlog(f"  👤 Vai sul browser e fai login con account {account.get('username')!r}. "
         f"Hai tempo (max 15 min, anche con 2FA/email). Il task riprende AUTOMATICAMENTE "
         f"appena rilevo la sessione.")

    # Polling NON invasivo: max 15 min, check ogni 8s. is_visible() è read-only
    # quindi non interferisce con il login dell'utente.
    max_wait_s = 900
    poll_every_s = 8
    elapsed = 0
    while elapsed < max_wait_s:
        await asyncio.sleep(poll_every_s)
        elapsed += poll_every_s
        try:
            still_not_logged = await _is_login_required()
        except Exception:
            still_not_logged = True
        if not still_not_logged:
            jlog(f"  ✅ Login rilevato dopo {elapsed}s. Sessione ora persistita per i run futuri "
                 f"(niente più login richiesto, salvo scadenza cookie).")
            audit.log_event("LOGIN_OK_AFTER_MANUAL", platform=plat, wait_s=elapsed)
            return True
        # Log progress ogni 60s circa
        if elapsed % 64 < poll_every_s:
            mins_left = max(0, (max_wait_s - elapsed) // 60)
            jlog(f"  ⏳ Attendo login... ({elapsed}s elapsed, ~{mins_left} min rimasti)")

    jlog(f"  ❌ Timeout {max_wait_s}s: login manuale non rilevato. Abort.")
    audit.log_event("LOGIN_MANUAL_TIMEOUT", platform=plat, wait_s=max_wait_s)
    return False


async def run_agent(task: dict[str, Any], job_id: int) -> str:
    """Entry-point recon_social. Sola R1 implementata in Fase 1."""
    db.update_job(job_id, status="running", started_at=db.now_iso())
    db.set_control_signal(job_id, None)

    def jlog(line: str) -> None:
        db.append_job_log(job_id, line)

    jlog(f'Avvio recon_social per task #{task["id"]} "{task["name"]}"')

    # ---- 0. Speed profile (anti-ban tuning) ----
    # `safe` (default, conservativo), `balanced` (~40% più veloce),
    # `aggressive` (~65% più veloce, alto rischio ban).
    speed = (task.get("speed_profile") or "safe").lower()
    if speed not in ("safe", "balanced", "aggressive"):
        speed = "safe"
    pause_min, pause_max = {
        "safe": (30, 180),
        "balanced": (15, 60),
        "aggressive": (10, 30),
    }[speed]
    do_subpages = speed != "aggressive"
    jlog(
        f"⚡ Speed profile: '{speed}' — pause anti-ban {pause_min}-{pause_max}s, "
        f"sub-pages={'ON' if do_subpages else 'OFF'}"
    )

    # ---- 1. Kill-switch + validazioni ----
    if is_recon_disabled():
        msg = "RECON_SOCIAL_DISABLED=1 in env: recon disattivato globalmente. Abort."
        jlog(f"⛔ {msg}")
        db.update_job(job_id, status="error", error=msg, finished_at=db.now_iso())
        return ""

    if not is_configured():
        msg = "AGENTSCRAPER_SECRET non settata: niente decifratura credenziali. Abort."
        jlog(f"❌ {msg}")
        db.update_job(job_id, status="error", error=msg, finished_at=db.now_iso())
        return ""

    recon_mode = (task.get("recon_mode") or "url_driven").strip()
    if recon_mode not in ("url_driven", "exploration", "follower_scrape"):
        msg = (f"recon_mode '{recon_mode}' non valido. Accettati: "
               "'url_driven', 'exploration', 'follower_scrape'.")
        jlog(f"❌ {msg}")
        db.update_job(job_id, status="error", error=msg, finished_at=db.now_iso())
        return ""

    if recon_mode == "exploration":
        msg = ("recon_mode='exploration' (R2) non ancora implementato in Fase 1. "
               "Usa 'url_driven' (R1). R2 nel backlog B-014.")
        jlog(f"⚠️ {msg}")
        db.update_job(job_id, status="error", error=msg, finished_at=db.now_iso())
        return ""

    # ---- 2. Account social loggato ----
    sender_id = task.get("recon_social_account_id")
    if not sender_id:
        msg = "recon_social_account_id non settato nel task. Vai a edit task → Step Recon."
        jlog(f"❌ {msg}")
        db.update_job(job_id, status="error", error=msg, finished_at=db.now_iso())
        return ""

    account = db.get_social_account(int(sender_id))
    if not account:
        msg = f"social_account #{sender_id} non trovato. Abort."
        jlog(f"❌ {msg}")
        db.update_job(job_id, status="error", error=msg, finished_at=db.now_iso())
        return ""
    if account.get("status") != "active":
        msg = (f"social_account #{sender_id} ('{account.get('username')}') ha "
               f"status='{account.get('status')}', non active. Fail-fast.")
        jlog(f"❌ {msg}")
        db.update_job(job_id, status="error", error=msg, finished_at=db.now_iso())
        return ""

    account_platform = (account.get("platform") or "").lower()
    jlog(
        f"Account: #{account['id']} '{account.get('username')}' "
        f"platform={account_platform}, auth={account.get('auth_method')}"
    )

    # ---- 3. Liste target: amici (friend/following list) + generica ----
    seed_generic = task.get("seed_queries") or []
    if isinstance(seed_generic, str):
        seed_generic = [u.strip() for u in seed_generic.splitlines() if u.strip()]
    seed_generic = [u for u in seed_generic if u]

    seed_friends = task.get("seed_queries_friends") or []
    if isinstance(seed_friends, str):
        seed_friends = [u.strip() for u in seed_friends.splitlines() if u.strip()]
    seed_friends = [u for u in seed_friends if u]

    # follower_scrape può popolare i target anche dai contatti selezionati
    # (sezione "📇 Target IG da contatti DB" nella UI): salta l'abort se
    # ci sono `target_contact_ids` valorizzati.
    _tcids_for_recon = task.get("target_contact_ids") or []
    if (
        not seed_generic
        and not seed_friends
        and not (recon_mode == "follower_scrape" and _tcids_for_recon)
    ):
        msg = ("Nessun target. Per follower_scrape: scrivi un handle nel seed "
               "OPPURE spunta dei contatti IG nella sezione 'Target IG da contatti DB'. "
               "Per url_driven: scrivi URL/nomi nel seed.")
        jlog(f"❌ {msg}")
        db.update_job(job_id, status="error", error=msg, finished_at=db.now_iso())
        return ""

    # Combina seed: tuple (kind, entry, source_meta).
    # source_meta: per kind=='follower' è l'handle del target di cui era follower.
    # In follower_scrape mode il seed iniziale è la lista degli account TARGET di
    # cui scaricare i follower; viene poi ESPANSA sotto in lista di follower URLs.
    seed: list[tuple[str, str, str | None]] = (
        [("friend", x, None) for x in seed_friends]
        + [("generic", x, None) for x in seed_generic]
    )
    jlog(f"Target totali: {len(seed)} ({len(seed_friends)} amici + {len(seed_generic)} generici)")

    # ---- 4. LLM config (per schema fill) ----
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
    schema_text = (task.get("extraction_schema") or "").strip() or (
        '{"display_name": "string|null", "bio": "string|null", '
        '"interests_inferred": ["string"], "language": "string|null"}'
    )

    # ---- 5. Run dir + audit ----
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_dir = RESULTS_DIR / str(task["id"]) / ts
    run_dir.mkdir(parents=True, exist_ok=True)
    profiles_path = run_dir / "profiles.jsonl"
    audit = ReconAudit(run_dir, screenshot_every_n=5, enabled_screenshots=True)
    audit.log_event("RUN_START", job_id=job_id, account_id=account["id"],
                    n_targets=len(seed), llm_model=llm_model)

    # ---- 6. recon_runs DB row ----
    with db.connect() as con:
        cur = con.execute(
            """INSERT INTO recon_runs (task_id, job_id, social_account_id,
                                       status, started_at, last_active_at)
               VALUES (%s, %s, %s, 'running', %s, %s)""",
            (task["id"], job_id, account["id"], db.now_iso(), db.now_iso()),
        )
        recon_run_id = int(cur.lastrowid)
    audit.log_event("RECON_RUN_CREATED", recon_run_id=recon_run_id)

    # ---- 7. Apri browser persistent ----
    sess_dir = account.get("session_dir") or (
        Path("data/social_sessions") / account["uuid"]
    )
    if not isinstance(sess_dir, Path):
        sess_dir = Path(str(sess_dir))

    headed = bool(int(task.get("headed", 1) or 0))
    jlog(f"Apertura browser (headed={headed}) con session_dir={sess_dir}")
    p_handle = None
    context = None
    n_ok = 0
    n_fail = 0
    n_skip = 0

    try:
        p_handle, context, page = await _open_persistent_browser(
            headed=headed, user_data_dir=sess_dir
        )
        safe = SafeBrowser(page, audit, strict=False)  # R1: solo audit, no enforcement

        # ---- 7b. Verifica login (con migrazione one-shot da legacy storage_state) ----
        ok = await _ensure_logged_in(page, context, account, jlog, audit)
        if not ok:
            msg = (
                f"Account #{account['id']} ({account.get('platform')}/{account.get('username')}) "
                "non loggato e nessuna sessione recuperabile. Vai a /social/accounts e fai login."
            )
            jlog(f"❌ {msg}")
            db.update_job(job_id, status="error", error=msg, finished_at=db.now_iso())
            return ""

        # ---- 7c. Modalità friend list (solo se ci sono seed_friends) ----
        # 2 strategie possibili per piattaforma:
        #   - LIVE SEARCH (preferita): naviga UNA volta a /friends/list e usa
        #     il search box dentro la pagina, type-by-type. ~5s per nome.
        #     Implementata in `module.search_friend_via_friendlist`.
        #   - BULK LOAD (fallback): carica TUTTA la friend/following list in
        #     memoria, poi matcha in-memory. Lenta (~9 min per 500 amici).
        #     Implementata in `module.load_friend_list` / `load_following_list`.
        friend_list: dict[str, str] = {}
        friend_live_searcher = None  # callable async name → (label, url) | None
        if seed_friends:
            module = _RECON_BY_PLATFORM.get(account_platform)
            live_fn = getattr(module, "search_friend_via_friendlist", None) if module else None
            if live_fn is not None:
                friend_live_searcher = live_fn
                jlog(f"🔗 Friend matching: modalità live-search "
                     f"(search box dentro /friends/list) per platform={account_platform}")
                audit.log_event("FRIEND_SEARCH_MODE", platform=account_platform, mode="live")
            else:
                loader = getattr(module, "load_friend_list", None) if module else None
                if loader is None:
                    loader = getattr(module, "load_following_list", None) if module else None
                if loader:
                    jlog(f"🔗 Friend matching: modalità bulk-load per platform={account_platform}...")
                    audit.log_event("FRIEND_SEARCH_MODE", platform=account_platform, mode="bulk")
                    try:
                        if account_platform == "facebook":
                            friend_list = await loader(
                                page, safe,
                                jlog=jlog,
                                debug_dir=run_dir / "search_debug",
                            )
                        else:
                            friend_list = await loader(
                                page, safe, account.get("username"),
                                jlog=jlog,
                                debug_dir=run_dir / "search_debug",
                            )
                        audit.log_event(
                            "FRIEND_LIST_LOADED",
                            platform=account_platform,
                            n_entries=len(friend_list),
                        )
                        jlog(f"  ✅ {len(friend_list)} amici/following caricati in cache")
                    except Exception as e:
                        jlog(f"  ❌ load friend list fail: {type(e).__name__}: {e}")
                        audit.log_event(
                            "FRIEND_LIST_FAIL", platform=account_platform, error=str(e)
                        )
                        friend_list = {}
                else:
                    jlog(f"  ⚠️ platform={account_platform} non ha matcher friend list — "
                         f"i seed amici saranno fatti via search globale (fallback)")

        # ---- 7d. FOLLOWER_SCRAPE mode: espandi target accounts → follower URLs ----
        # Per ogni target account (sia dal seed_queries testuale, sia pescato
        # dai `target_contact_ids` selezionati nella UI), enumera i suoi
        # follower (cap N) e sostituisci il seed con la lista espansa di URL
        # follower.
        if recon_mode == "follower_scrape":
            if account_platform != "instagram":
                msg = (f"recon_mode='follower_scrape' è implementato solo per Instagram. "
                       f"L'account selezionato è {account_platform}.")
                jlog(f"❌ {msg}")
                db.update_job(job_id, status="error", error=msg, finished_at=db.now_iso())
                return ""

            # Pesca anche dai contatti selezionati: estrai handle/URL IG dal
            # social_json del contatto e aggiungili al seed di target.
            extra_from_contacts: list[str] = []
            # Dedup target_contact_ids preservando ordine (il form può inviare
            # duplicati se due sezioni del template hanno entrambe `name="target_contact_ids"`)
            target_cids_raw = task.get("target_contact_ids") or []
            seen_cids: set[int] = set()
            target_cids: list[int] = []
            for x in target_cids_raw:
                try:
                    xi = int(x)
                except (TypeError, ValueError):
                    continue
                if xi not in seen_cids:
                    seen_cids.add(xi)
                    target_cids.append(xi)
            if len(target_cids) < len(target_cids_raw):
                jlog(f"  ℹ️ dedup target_contact_ids: {len(target_cids_raw)} → {len(target_cids)} unici")
            if target_cids:
                jlog(f"📇 Recupero handle IG da {len(target_cids)} contatti selezionati...")
                for cid in target_cids:
                    try:
                        c = db.get_contact(int(cid))
                    except Exception:
                        continue
                    if not c:
                        continue
                    sj = c.get("social_json") or ""
                    try:
                        import json as _json
                        arr = _json.loads(sj) if sj else []
                    except Exception:
                        arr = []
                    ig_entry = next(
                        (e for e in arr if isinstance(e, dict) and e.get("platform") == "instagram"),
                        None,
                    )
                    if not ig_entry:
                        continue
                    handle_or_url = (
                        ig_entry.get("handle")
                        or ig_entry.get("url")
                        or ""
                    ).strip()
                    if handle_or_url:
                        extra_from_contacts.append(handle_or_url)
                        jlog(f"  + contact#{cid} {c.get('display_name')!r} → {handle_or_url}")
                jlog(f"  ✅ {len(extra_from_contacts)} handle IG aggiunti dai contatti")

            # Combina seed testuale + contatti
            combined_targets = list(seed) + [
                ("generic", h, None) for h in extra_from_contacts
            ]
            if not combined_targets:
                msg = ("recon_mode='follower_scrape': nessun target. Inserisci nomi/URL "
                       "nel Seed o spunta dei contatti IG nella sezione 'Target IG da contatti DB'.")
                jlog(f"❌ {msg}")
                db.update_job(job_id, status="error", error=msg, finished_at=db.now_iso())
                return ""

            cap_per_target = int(task.get("recon_max_targets_per_day") or 100)
            expanded_seed: list[tuple[str, str, str | None]] = []
            for _, target_entry, _ in combined_targets:
                # Estrai handle IG dal target (URL o handle libero)
                te = (target_entry or "").strip().lstrip("@")
                if te.startswith(("http://", "https://")):
                    handle_from_url = instagram_recon._ig_username_from_url(te)
                    target_handle = handle_from_url or ""
                elif re.match(r"^[A-Za-z0-9._]{1,30}$", te):
                    target_handle = te
                else:
                    target_handle = ""
                if not target_handle:
                    jlog(f"  ⚠️ Skip '{target_entry}': handle IG non estrabile")
                    continue
                jlog(f"📥 Enumero follower di @{target_handle} (cap={cap_per_target})...")
                audit.log_event("FOLLOWERS_ENUM_START", target=target_handle, cap=cap_per_target)
                try:
                    followers = await instagram_recon.enumerate_followers_of_target(
                        page, safe, target_handle,
                        cap=cap_per_target, jlog=jlog,
                        debug_dir=run_dir / "follower_lists",
                    )
                except Exception as e:
                    jlog(f"  ❌ enumeration fail per @{target_handle}: {type(e).__name__}: {e}")
                    audit.log_event("FOLLOWERS_ENUM_FAIL", target=target_handle, error=str(e))
                    continue
                audit.log_event(
                    "FOLLOWERS_ENUM_DONE", target=target_handle, n_followers=len(followers),
                )
                for f in followers:
                    expanded_seed.append(
                        ("follower", f["profile_url"], f"@{target_handle}")
                    )
                # Pausa anti-bot tra account target (oltre il cap pause già
                # nel loop dei profili sotto)
                if expanded_seed:
                    await asyncio.sleep(random.uniform(20, 60))
            if not expanded_seed:
                msg = "Nessun follower enumerato da nessun target. Abort."
                jlog(f"❌ {msg}")
                db.update_job(job_id, status="error", error=msg, finished_at=db.now_iso())
                return ""
            seed = expanded_seed
            jlog(f"Follower totali da processare: {len(seed)}")

        # ---- 8. Loop target (amici prima, poi generici/follower) ----
        for i, (seed_kind, seed_entry, source_meta) in enumerate(seed, start=1):
            try:
                await wait_if_paused_or_stop(job_id, jlog)
            except RunnerStopped:
                jlog("⏹ Stop richiesto: termino sessione.")
                break

            # Pre-step: risolvi il seed_entry → URL profilo
            #   seed_kind="friend": prova prima friend matching (live-search o
            #                       bulk-load cache), poi fallback search globale
            #   seed_kind="generic": URL diretto o search globale
            url = None
            plat_hint = None
            resolve_reason = ""
            if seed_kind == "friend" and not _looks_like_url(seed_entry):
                module = _RECON_BY_PLATFORM.get(account_platform)
                match = None
                match_strategy = "none"
                if friend_live_searcher is not None:
                    # LIVE SEARCH (FB: search box dentro /friends/list)
                    try:
                        match = await friend_live_searcher(
                            page, safe, seed_entry,
                            jlog=jlog, debug_dir=run_dir / "search_debug",
                        )
                        match_strategy = "live"
                    except Exception as e:
                        jlog(f"  [{i}/{len(seed)}] live search fail per '{seed_entry}': {e}")
                        audit.log_event(
                            "FRIEND_LIVE_SEARCH_FAIL",
                            seed_entry=seed_entry, error=str(e),
                        )
                elif friend_list:
                    # BULK MATCH (IG/TT: friend_list cache in memoria)
                    matcher = getattr(module, "match_friend", None) if module else None
                    if matcher:
                        match = matcher(seed_entry, friend_list)
                        match_strategy = "bulk"

                if match:
                    friend_name, friend_url = match
                    url = friend_url
                    plat_hint = account_platform
                    resolve_reason = f"friend_{match_strategy}_match: '{friend_name}'"
                    jlog(f"  [{i}/{len(seed)}] '{seed_entry}' → friend match ({match_strategy}): "
                         f"'{friend_name}' → {url}")
                    audit.log_event(
                        "FRIEND_MATCH",
                        seed_entry=seed_entry,
                        matched_name=friend_name,
                        resolved_url=url,
                        strategy=match_strategy,
                    )
                else:
                    jlog(f"  [{i}/{len(seed)}] '{seed_entry}' non trovato in friend list "
                         f"(strategy={match_strategy}) — fallback search globale")
                    audit.log_event(
                        "FRIEND_MATCH_MISS",
                        seed_entry=seed_entry, strategy=match_strategy,
                    )

            if not url:
                # Fallback: URL diretto o search globale (slug match)
                url, plat_hint, resolve_reason = await _resolve_seed_to_url(
                    page, seed_entry, account_platform, jlog,
                    debug_dir=run_dir / "search_debug",
                )
            if not url:
                jlog(f"  [{i}/{len(seed)}] '{seed_entry}' → SKIP ({resolve_reason})")
                audit.log_event(
                    "SKIP_UNRESOLVED",
                    seed_entry=seed_entry,
                    reason=resolve_reason,
                    platform=plat_hint,
                    seed_kind=seed_kind,
                )
                n_skip += 1
                continue

            if not _looks_like_url(seed_entry):
                # Era un nome, risolto via friend list o search
                jlog(f"  [{i}/{len(seed)}] '{seed_entry}' risolto → {url}")
                audit.log_event(
                    "NAME_RESOLVED",
                    seed_entry=seed_entry,
                    resolved_url=url,
                    reason=resolve_reason,
                    seed_kind=seed_kind,
                )

            plat = _detect_platform(url)
            if not plat:
                jlog(f"  [{i}/{len(seed)}] {url} → platform sconosciuta, skip")
                audit.log_event("SKIP_UNKNOWN_PLATFORM", url=url)
                n_skip += 1
                continue
            if account_platform and account_platform != plat:
                jlog(f"  [{i}/{len(seed)}] ⚠️ account è {account_platform} ma URL è {plat}: provo comunque")

            # DEDUP RECENTE: skip se esiste già un asset di questo type con
            # source_url + updated_at < refresh_policy_days. Default 7 giorni.
            # -1 = sempre re-scrape (no skip).
            refresh_days = int(task.get("refresh_policy_days") or 7)
            output_atype = (task.get("output_asset_type") or "social_profile").strip().lower()
            if refresh_days >= 0:
                try:
                    if db.has_recent_asset(url, output_atype, max_age_days=refresh_days):
                        jlog(f"  [{i}/{len(seed)}] {url} → SKIP (asset recente, ≤{refresh_days}d)")
                        audit.log_event(
                            "SKIP_RECENT", url=url, asset_type=output_atype,
                            max_age_days=refresh_days,
                        )
                        n_skip += 1
                        continue
                except Exception as e:
                    log.debug("has_recent_asset fail (proseguo): %s", e)

            jlog(f"  [{i}/{len(seed)}] {plat} ← {url}")
            try:
                await safe.safe_goto(url, label=f"target_{i}")
                # Soft wait + scroll lieve per lazy load
                await asyncio.sleep(random.uniform(1.5, 3.0))
                await safe.safe_scroll(dy=600, label="lazy_load_trigger")
                await asyncio.sleep(random.uniform(1.0, 2.0))

                module = _RECON_BY_PLATFORM[plat]
                profile_data = await module.extract_profile_data(page)
                audit.log_event("EXTRACT_DONE", url=url, platform=plat,
                                error=profile_data.get("error"))

                # Salva in recon_visited (idempotente)
                try:
                    with db.connect() as con:
                        con.execute(
                            """INSERT OR IGNORE INTO recon_visited
                               (run_id, target_url, target_platform, visited_at, classified)
                               VALUES (%s, %s, %s, %s, 0)""",
                            (recon_run_id, url, plat, db.now_iso()),
                        )
                except Exception as e:
                    log.warning("recon_visited insert fail: %s", e)

                # Costruisci testo per LLM fill schema.
                # Prima: campi strutturati (display_name, bio, post) estratti via
                # selettori specifici della platform. Quelli sono "preciso ma fragile".
                text_chunks = []
                for key in ("display_name", "username", "title", "intro_box", "bio_text"):
                    v = profile_data.get(key)
                    if v:
                        text_chunks.append(f"{key}: {v}")
                if profile_data.get("recent_posts"):
                    for j, p in enumerate(profile_data["recent_posts"][:5], 1):
                        text_chunks.append(f"post#{j}: {p.get('text', '')[:300]}")
                structured_text = "\n".join(text_chunks)

                # FALLBACK ROBUSTO: aggiungo SEMPRE anche il body text completo
                # della pagina principale (max 6000 char). Se i selettori specifici
                # falliscono (FB cambia DOM spesso), l'LLM può comunque ricavare
                # gli interessi dal testo bruto.
                body_text = ""
                try:
                    body_text = await page.evaluate("document.body.innerText")
                    body_text = (body_text or "")[:6000]
                except Exception as e:
                    log.debug("body innerText fail: %s", e)

                # ARRICCHIMENTO platform-specific: visita 1-2 sotto-pagine ad alto
                # valore (FB: /about + /likes_pages; IG: /reels + /tagged; TT:
                # /playlists). Best-effort: se non accessibili, skip silenzioso.
                # speed_profile='aggressive' → skip totale per velocità (-25-30s/profilo).
                sub_texts: dict[str, str] = {}
                collect_fn = getattr(module, "collect_subpage_texts", None)
                if collect_fn and do_subpages:
                    try:
                        # piccola pausa "umana" tra main e sub-pages
                        await asyncio.sleep(random.uniform(2.0, 4.0))
                        sub_texts = await collect_fn(page, safe, url, jlog=jlog)
                        audit.log_event(
                            "SUBPAGES_DONE", url=url, platform=plat,
                            n_pages=len(sub_texts), labels=list(sub_texts.keys()),
                            total_chars=sum(len(v) for v in sub_texts.values()),
                        )
                    except Exception as e:
                        log.debug("collect_subpage_texts fail: %s", e)
                        audit.log_event("SUBPAGES_FAIL", url=url, error=str(e))
                elif not do_subpages:
                    audit.log_event("SUBPAGES_SKIPPED", url=url, reason="speed_aggressive")

                text_for_llm_parts = []
                if structured_text.strip():
                    text_for_llm_parts.append(
                        "=== DATI ESTRATTI (selettori specifici, pagina principale) ===\n"
                        + structured_text
                    )
                if body_text.strip():
                    text_for_llm_parts.append(
                        "=== BODY TEXT PROFILO (pagina principale, raw) ===\n" + body_text
                    )
                for sub_label, sub_text in sub_texts.items():
                    if sub_text and sub_text.strip():
                        text_for_llm_parts.append(
                            f"=== SOTTO-PAGINA: {sub_label} ===\n{sub_text}"
                        )
                text_for_llm = "\n\n".join(text_for_llm_parts)

                if not text_for_llm.strip():
                    jlog(f"    ⚠️ niente testo estratto (login_required? profilo privato?)")
                    n_fail += 1
                    with profiles_path.open("a", encoding="utf-8") as f:
                        f.write(json.dumps({
                            "url": url, "platform": plat,
                            "error": profile_data.get("error") or "no_text_extracted",
                            "raw_data": profile_data,
                        }, ensure_ascii=False) + "\n")
                    continue
                audit.log_event(
                    "TEXT_FOR_LLM_BUILT",
                    url=url,
                    structured_chars=len(structured_text),
                    body_chars=len(body_text),
                    sub_chars=sum(len(v) for v in sub_texts.values()),
                    sub_pages=list(sub_texts.keys()),
                )

                obj, raw = await _llm_fill_schema(
                    text_for_llm, schema_text,
                    llm_base_url=llm_base_url, llm_api_key=llm_api_key, llm_model=llm_model,
                )
                if not obj:
                    jlog(f"    ⚠️ LLM non ha riempito lo schema (raw[:120]={raw[:120]!r})")
                    n_fail += 1
                    with profiles_path.open("a", encoding="utf-8") as f:
                        f.write(json.dumps({
                            "url": url, "platform": plat,
                            "error": "llm_no_json", "raw_response": raw[:500],
                            "raw_data": profile_data,
                        }, ensure_ascii=False) + "\n")
                    continue

                # Fallback narrative_summary: se vuoto/None, fai una chiamata
                # mirata che produce SOLO il narrative (~500 token). Costa
                # un'altra ~3s LLM ma garantisce il campo.
                narr = obj.get("narrative_summary")
                if not narr or (isinstance(narr, str) and len(narr.strip()) < 80):
                    try:
                        narr_only = await _llm_generate_narrative(
                            text_for_llm, obj,
                            llm_base_url=llm_base_url,
                            llm_api_key=llm_api_key,
                            llm_model=llm_model,
                        )
                        if narr_only and len(narr_only.strip()) >= 80:
                            obj["narrative_summary"] = narr_only
                            audit.log_event(
                                "NARRATIVE_FALLBACK_OK",
                                url=url, len=len(narr_only),
                            )
                            jlog(f"    📝 narrative_summary rigenerato via follow-up "
                                 f"({len(narr_only)} char)")
                    except Exception as e:
                        log.debug("narrative fallback fail: %s", e)
                        audit.log_event(
                            "NARRATIVE_FALLBACK_FAIL", url=url, error=str(e)
                        )

                # Iniezione metadata: SOVRASCRIVI sempre (l'LLM mette None nel
                # source_url anche se gli passi lo schema con la stringa). Il
                # runner sa il valore canonico, è autoritativo.
                obj["source_url"] = url
                obj["source_domain"] = urlparse(url).hostname
                obj["platform"] = plat
                obj["crawled_at"] = db.now_iso()
                if source_meta:  # follower_scrape mode
                    obj["source_follower_of"] = source_meta
                obj["_recon_raw"] = profile_data  # debug

                # MATERIALIZZA in DB se output_asset_type valorizzato sul task.
                # asset_type viene definito dall'utente nel form (es. 'ig_profile').
                # Default: 'social_profile' se non specificato.
                output_atype = (task.get("output_asset_type") or "social_profile").strip().lower()
                try:
                    # Costruisci tag derivati dai campi dell'obj:
                    asset_tags: dict[str, list[str]] = {"platform": [plat]}
                    if source_meta:
                        asset_tags["source_follower_of"] = [source_meta]
                    if obj.get("location"):
                        asset_tags["location"] = [str(obj["location"])[:200]]
                    if obj.get("professional_field"):
                        asset_tags["professional_field"] = [str(obj["professional_field"])[:200]]
                    if obj.get("language"):
                        asset_tags["language"] = [str(obj["language"])[:10]]
                    for k in ("hobbies", "interests_inferred", "liked_pages_visible"):
                        v = obj.get(k) or []
                        if isinstance(v, list):
                            cleaned = [str(x)[:120] for x in v if x and isinstance(x, (str, int))]
                            if cleaned:
                                asset_tags[k] = cleaned[:30]  # cap a 30 per tag_key

                    title = obj.get("display_name") or obj.get("username") or url
                    asset_id = db.upsert_asset(
                        {
                            "asset_type": output_atype,
                            "source_task_id": task["id"],
                            "source_job_id": job_id,
                            "source_url": url,
                            "source_domain": obj.get("source_domain"),
                            "title": str(title)[:200],
                            "raw_json": obj,  # tutto incluso narrative_summary
                            "notes": (obj.get("evidence_quote") or "")[:300] or None,
                        },
                        tags=asset_tags,
                    )
                    # Crea/aggiorna contact linkato all'asset, con social_json
                    # per il profilo IG/FB/TT. Display name = il nome estratto.
                    social_entry = {"platform": plat, "url": url}
                    if obj.get("username"):
                        social_entry["handle"] = str(obj["username"])[:60]
                    elif obj.get("username_or_id"):
                        social_entry["handle"] = str(obj["username_or_id"])[:60]
                    db.upsert_contact({
                        "asset_id": asset_id,
                        "source_task_id": task["id"],
                        "source_job_id": job_id,
                        "source_url": url,
                        "source_domain": obj.get("source_domain"),
                        "display_name": str(title)[:200],
                        "social": [social_entry],
                        "raw_json": json.dumps(obj, ensure_ascii=False),
                        "status": "new",
                    })
                    audit.log_event(
                        "ASSET_MATERIALIZED", url=url, asset_id=asset_id,
                        asset_type=output_atype, n_tags=sum(len(v) for v in asset_tags.values()),
                    )
                    # Incrementa n_ok SUBITO dopo materializzazione riuscita.
                    # Spostato qui dal fondo del block per evitare che fallimenti
                    # successivi (write profiles.jsonl, update recon_runs)
                    # mascherino come "fallito" un profilo che è stato salvato in DB.
                    n_ok += 1
                    audit.log_event("PROFILE_OK", url=url, platform=plat)
                except Exception as e:
                    log.warning("upsert asset/contact fail: %s", e)
                    audit.log_event("ASSET_MATERIALIZE_FAIL", url=url, error=str(e))
                    n_fail += 1

                try:
                    with profiles_path.open("a", encoding="utf-8") as f:
                        f.write(json.dumps(obj, ensure_ascii=False) + "\n")
                except Exception as e:
                    log.warning("profiles.jsonl write fail: %s", e)

                # update recon_runs.target_count
                with db.connect() as con:
                    con.execute(
                        "UPDATE recon_runs SET target_count = %s, last_active_at = %s WHERE id = %s",
                        (n_ok, db.now_iso(), recon_run_id),
                    )

                # Periodic screenshot
                await audit.capture_step(page, step_label=url)

                # Pausa anti-bot: range dipende da speed_profile (safe 30-180s,
                # balanced 15-60s, aggressive 10-30s)
                if i < len(seed):
                    pause = random.uniform(pause_min, pause_max)
                    jlog(f"    pause {pause:.0f}s [{speed}]")
                    await asyncio.sleep(pause)

            except BlockedActionError as e:
                # Non dovrebbe mai succedere in R1 (strict=False), ma per sicurezza
                jlog(f"    ⛔ azione bloccata: {e}")
                audit.log_event("BLOCKED_ACTION", url=url, error=str(e))
                n_fail += 1
            except Exception as e:
                jlog(f"    ❌ errore: {type(e).__name__}: {e}")
                audit.log_event("ERROR", url=url, error=f"{type(e).__name__}: {e}")
                n_fail += 1

    finally:
        try:
            if context is not None:
                await context.close()
        except Exception:
            pass
        try:
            if p_handle is not None:
                await p_handle.stop()
        except Exception:
            pass
        audit.log_event("RUN_END", n_ok=n_ok, n_fail=n_fail, n_skip=n_skip)
        audit.close()
        with db.connect() as con:
            con.execute(
                "UPDATE recon_runs SET status = 'done', finished_at = %s, target_count = %s WHERE id = %s",
                (db.now_iso(), n_ok, recon_run_id),
            )

    # ---- 9. Report ----
    report = (
        f"# Recon social — {task['name']}\n\n"
        f"- Modalità: **url_driven** (R1)\n"
        f"- Account: #{account['id']} ({account.get('username')}, {account_platform})\n"
        f"- URL totali: {len(seed)}\n"
        f"- Profili estratti: {n_ok}\n"
        f"- Falliti: {n_fail}\n"
        f"- Skippati (platform sconosciuta): {n_skip}\n"
        f"- Schema usato: {schema_text[:200]}\n\n"
        f"## File\n"
        f"- `profiles.jsonl` — output strutturato\n"
        f"- `recon_audit_log.jsonl` — tutte le azioni DOM (per audit)\n"
        f"- `screenshots/` — screenshot ogni 5 step\n"
    )
    report_path = run_dir / "report.md"
    report_path.write_text(report, encoding="utf-8")

    jlog(
        f"✅ recon_social completato: {n_ok} profili estratti, "
        f"{n_fail} falliti, {n_skip} skippati"
    )
    db.update_job(
        job_id, status="done", finished_at=db.now_iso(),
        result_path=str(report_path),
    )
    return str(report_path)
