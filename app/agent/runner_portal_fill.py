"""Runner portal_fill — compilazione assistita di form su portali web.

Agent mode: `portal_fill`. Dato un foglio collaborativo (sorgente dati) e una
macro registrata (mapping campi→colonne + sessione di login), apre il portale con
la sessione loggata e compila il form una riga alla volta.

Flusso:
  1. carica la macro (tenant-scoped) e le righe del foglio come dict {colonna:valore}
  2. apre Chromium con persistent context (sessione loggata salvata in precedenza)
  3. per ogni riga: goto(portal_url) → fill_form (auto-riparante via LLM) →
     submit SOLO se auto_submit è abilitato sulla macro E richiesto dal task
  4. logga l'esito per-riga in portal_fill_log; se l'LLM ri-mappa un selettore,
     aggiorna la macro (auto-riparazione persistita)

Sicurezza: il default è NON inviare il form (stop_before_submit). L'auto-submit è
opt-in esplicito per-macro (campo auto_submit). Niente invii irreversibili senza
che l'utente l'abbia abilitato su quella macro.

Riusa: app/agent/portal/form_fill.py (locate/llm_remap/fill_form), il pattern di
apertura persistent-context di recon_social, e il lifecycle job standard.
"""
from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

from .. import db
from ..fascicoli import sheets_db
from .llm_providers import resolve_api_key, resolve_base_url
from .portal.form_fill import (
    PHASES,
    LLMConfig,
    MacroField,
    portal_session_dir,
    run_steps,
)
from .runner_control import RunnerStopped, wait_if_paused_or_stop

log = logging.getLogger(__name__)

MAX_ROWS_PER_RUN = 200  # safety cap sul batch


def _steps_by_phase(
    steps: list[MacroField], *, auto_submit: bool, submit_selector: str
) -> dict[str, list[MacroField]]:
    """Raggruppa gli step per fase preservando l'ordine. Retro-compat:
    - step senza phase valida → 'activity' (gestito da MacroField.from_dict)
    - submit_selector legacy → aggiunge uno step submit finale all'activity, se
      l'activity non contiene già uno step submit.
    - se auto_submit è OFF, viene rimosso SOLO il submit della fase ATTIVITÀ
      (l'invio del record, gated dalla doppia conferma di sicurezza). I 'submit'
      delle fasi di navigazione (warmup/ritorno/chiusura) sono passaggi del
      percorso — es. un bottone "Avanti"/"Nuovo" che tecnicamente fa submit — e
      vanno SEMPRE eseguiti, altrimenti la navigazione si rompe (bug 2026-05-31:
      "compila solo la seconda riga" perché il 'Nuovo' del warmup veniva rimosso)."""
    by_phase: dict[str, list[MacroField]] = {p: [] for p in PHASES}
    for s in steps:
        by_phase.get(s.phase, by_phase["activity"]).append(s)

    activity = by_phase["activity"]
    has_submit = any(s.action == "submit" for s in activity)
    if not has_submit and submit_selector:
        activity.append(MacroField(
            selector=submit_selector, semantic_label="Invia",
            action="submit", source="const", phase="activity",
        ))

    if not auto_submit:
        by_phase["activity"] = [s for s in by_phase["activity"] if s.action != "submit"]
    return by_phase


async def _open_persistent_browser(*, headed: bool, user_data_dir: Path):
    """Apre Chromium con persistent context (sessione loggata). Stesso pattern di
    recon_social._open_persistent_browser: patchright se disponibile, fallback
    playwright. NB: niente playwright_stealth sopra patchright (rompe il DNS)."""
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


def _llm_cfg_from_task(task: dict[str, Any]) -> LLMConfig:
    """Costruisce la LLMConfig per il remap, provider-agnostica (Ollama o remoto).

    Riusa i campi llm_provider/llm_base_url/llm_api_key già presenti sul task,
    come gli altri runner. Default: Ollama locale.
    """
    provider = (task.get("llm_provider") or "ollama").strip()
    try:
        base_url = resolve_base_url(provider, task.get("llm_base_url"))
        api_key = resolve_api_key(provider, task.get("llm_api_key"))
    except Exception as e:
        log.warning("portal_fill: risoluzione LLM provider fallita (%s) → remap disabilitato", e)
        return LLMConfig(enabled=False)
    model = (task.get("model") or "").strip() or "qwen3-coder:30b"
    return LLMConfig(base_url=base_url, api_key=api_key, model=model, enabled=True)


def _load_fields(macro: dict[str, Any]) -> list[MacroField]:
    raw = macro.get("fields_json") or "[]"
    try:
        items = json.loads(raw) if isinstance(raw, str) else (raw or [])
    except json.JSONDecodeError:
        items = []
    return [MacroField.from_dict(d) for d in items if isinstance(d, dict)]


async def run_agent(task: dict[str, Any], job_id: int) -> str:
    """Entry-point portal_fill."""
    db.update_job(job_id, status="running", started_at=db.now_iso())
    db.set_control_signal(job_id, None)

    def jlog(line: str) -> None:
        db.append_job_log(job_id, line)

    tenant_id = task.get("tenant_id")
    jlog(f"Avvio portal_fill per task #{task['id']} \"{task.get('name')}\"")

    # ---- 1. macro + foglio ----
    macro_id = task.get("portal_macro_id")
    sheet_id = task.get("portal_sheet_id")
    if not macro_id or not sheet_id:
        msg = "portal_fill richiede portal_macro_id e portal_sheet_id sul task."
        jlog(f"❌ {msg}")
        db.update_job(job_id, status="error", error=msg, finished_at=db.now_iso())
        return ""

    macro = db.get_portal_macro(int(macro_id), tenant_id=tenant_id)
    if not macro:
        msg = f"Macro #{macro_id} non trovata (o di altro tenant)."
        jlog(f"❌ {msg}")
        db.update_job(job_id, status="error", error=msg, finished_at=db.now_iso())
        return ""

    fields = _load_fields(macro)
    if not fields:
        msg = f"Macro #{macro_id} non ha campi definiti (fields_json vuoto)."
        jlog(f"❌ {msg}")
        db.update_job(job_id, status="error", error=msg, finished_at=db.now_iso())
        return ""

    rows = sheets_db.rows_as_dicts(int(sheet_id), tenant_id=tenant_id)
    if not rows:
        msg = f"Foglio #{sheet_id} vuoto o non accessibile: niente da compilare."
        jlog(f"⚠️ {msg}")
        db.update_job(job_id, status="done", finished_at=db.now_iso())
        return ""

    if len(rows) > MAX_ROWS_PER_RUN:
        jlog(f"⚠️ Foglio con {len(rows)} righe: limito alle prime {MAX_ROWS_PER_RUN} per questo run.")
        rows = rows[:MAX_ROWS_PER_RUN]

    portal_url = (macro.get("portal_url") or "").strip()
    # auto_submit: deciso dal TASK (opt-in unico). Il task lo eredita dalla macro
    # alla creazione (route /run) o lo si setta nel form task. Niente più doppio
    # flag macro-AND-task: l'utente lo configura una volta sola.
    auto_submit = bool(task.get("portal_auto_submit"))
    submit_selector = (macro.get("submit_selector") or "").strip()
    speed_profile = (task.get("speed_profile") or "safe").strip() or "safe"
    headed = bool(int(task.get("headed", 1) or 0))
    llm_cfg = _llm_cfg_from_task(task)

    jlog(
        f"Macro '{macro.get('name')}' → {portal_url} | {len(rows)} righe | "
        f"auto_submit={'ON' if auto_submit else 'OFF (stop prima del submit)'} | "
        f"remap LLM={'ON' if llm_cfg.enabled else 'OFF'}"
    )

    session_dir = portal_session_dir(macro.get("login_session_key") or f"macro-{macro_id}")

    by_phase = _steps_by_phase(fields, auto_submit=auto_submit, submit_selector=submit_selector)
    warmup, activity, return_steps, closing = (
        by_phase["warmup"], by_phase["activity"], by_phase["return"], by_phase["closing"]
    )
    jlog(
        f"Fasi: warmup={len(warmup)}, attività={len(activity)}, "
        f"ritorno={len(return_steps)}, chiusura={len(closing)} step."
    )

    p_handle = None
    context = None
    n_ok = n_fail = n_stopped = n_challenged = 0
    macro_dirty = False
    completed_all = False

    def _pflog(idx, status, detail):
        db.insert_portal_fill_log(
            job_id=job_id, macro_id=int(macro_id), sheet_id=int(sheet_id),
            row_idx=idx, status=status, detail=detail, tenant_id=tenant_id,
        )

    try:
        p_handle, context, page = await _open_persistent_browser(
            headed=headed, user_data_dir=session_dir
        )

        # Navigazione iniziale al portale, poi warmup (una tantum).
        try:
            await page.goto(portal_url, wait_until="domcontentloaded", timeout=45_000)
        except Exception as e:
            jlog(f"❌ Apertura portale fallita: {e}")
            db.update_job(job_id, status="error", error=f"goto iniziale: {e}", finished_at=db.now_iso())
            return ""

        if warmup:
            jlog("▶ Warmup…")
            wres = await run_steps(page, warmup, None, llm_cfg=llm_cfg, speed_profile=speed_profile)
            macro_dirty = macro_dirty or wres.macro_updated
            if wres.challenged:
                jlog("🛑 Captcha/verifica durante il warmup — interrompo.")
                _pflog(None, "challenged", "captcha durante warmup")
                n_challenged += 1
            elif not wres.ok:
                failed = "; ".join(f"{s.label}: {s.detail}" for s in wres.steps if not s.ok)
                jlog(f"⚠️ Warmup con errori: {failed[:200]}")

        for idx, row in enumerate(rows):
            try:
                await wait_if_paused_or_stop(job_id, jlog)
            except RunnerStopped:
                jlog("⏹ Stop richiesto dall'utente.")
                break

            # Per le righe dopo la prima: fase di ritorno (o ri-naviga all'URL se
            # non ci sono step di ritorno registrati — comportamento legacy).
            if idx > 0:
                if return_steps:
                    rres = await run_steps(page, return_steps, row, llm_cfg=llm_cfg, speed_profile=speed_profile)
                    macro_dirty = macro_dirty or rres.macro_updated
                    if rres.challenged:
                        _pflog(idx, "challenged", "captcha durante ritorno"); n_challenged += 1; break
                    if not rres.ok:
                        _pflog(idx, "error", "ritorno fallito: " + "; ".join(s.detail for s in rres.steps if not s.ok)[:500])
                        n_fail += 1
                        jlog(f"  riga {idx}: ⚠️ ritorno al form fallito — salto.")
                        continue
                else:
                    try:
                        await page.goto(portal_url, wait_until="domcontentloaded", timeout=45_000)
                    except Exception as e:
                        _pflog(idx, "error", f"ri-goto fallito: {e}"); n_fail += 1
                        jlog(f"  riga {idx}: ❌ ri-apertura URL fallita ({e})"); continue

            # Fase attività: compila i campi + (se presente) submit.
            res = await run_steps(page, activity, row, llm_cfg=llm_cfg, speed_profile=speed_profile)
            macro_dirty = macro_dirty or res.macro_updated

            if res.challenged:
                n_challenged += 1
                _pflog(idx, "challenged", "captcha/verifica anti-bot rilevata")
                jlog(f"  riga {idx}: 🛑 captcha/verifica rilevata — interrompo il run.")
                break

            failed = [s for s in res.steps if not s.ok]
            if failed:
                n_fail += 1
                detail = "; ".join(f"{s.label}: {s.detail}" for s in failed)
                _pflog(idx, "error", detail[:1000])
                jlog(f"  riga {idx}: ⚠️ {len(failed)} step non riusciti ({detail[:200]})")
                continue

            submitted = any(s.action == "submit" for s in activity)
            if submitted:
                n_ok += 1
                _pflog(idx, "ok", "compilato + inviato")
                jlog(f"  riga {idx}: ✅ compilato e inviato.")
            else:
                n_stopped += 1
                _pflog(idx, "stopped_before_submit", "compilato, in attesa di conferma manuale")
                jlog(f"  riga {idx}: ✅ compilato (stop prima del submit).")
                # Senza submit (auto_submit OFF) ci si ferma alla prima riga per
                # la revisione manuale — tranne se ci sono fasi di ritorno definite
                # (allora l'utente vuole comunque vedere il ciclo, ma senza invio
                # non avrebbe senso proseguire): manteniamo lo stop sicuro.
                jlog("  (nessuno step submit / auto_submit OFF) Mi fermo per la revisione manuale.")
                break
        else:
            completed_all = True

        # Fase chiusura: solo se il loop è arrivato in fondo a tutte le righe.
        if closing and completed_all:
            jlog("▶ Chiusura…")
            cres = await run_steps(page, closing, None, llm_cfg=llm_cfg, speed_profile=speed_profile)
            macro_dirty = macro_dirty or cres.macro_updated

        # ---- auto-riparazione: persisti i selettori ri-mappati dall'LLM ----
        if macro_dirty:
            db.update_portal_macro(
                int(macro_id),
                {"fields": [f.to_dict() for f in fields]},
                tenant_id=tenant_id,
            )
            jlog("🔧 Macro aggiornata con i selettori ri-mappati dall'LLM (auto-riparazione).")

    except Exception as e:
        log.exception("portal_fill job %s failed", job_id)
        jlog(f"ERRORE: {type(e).__name__}: {e}")
        db.update_job(job_id, status="error", error=str(e), finished_at=db.now_iso())
        raise
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

    summary = (
        f"portal_fill completato: {n_ok} inviati, {n_stopped} compilati (no submit), "
        f"{n_fail} falliti, {n_challenged} bloccati da captcha."
    )
    jlog(f"✅ {summary}")
    db.update_job(job_id, status="done", finished_at=db.now_iso())
    return summary
