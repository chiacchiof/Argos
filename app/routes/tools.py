"""Strumenti: the architect-facing launcher suite for messaging/social tools.

A grid of service tiles (WhatsApp, Telegram, Messenger, Facebook, Instagram, LinkedIn,
Email) that open the service's web app in a new tab. Tiles for which the tenant has
configured account(s) are highlighted. Pure launcher — no outreach is performed here.

Inoltre ospita la gestione delle **macro di compilazione portali** (portal_fill):
lista/crea/modifica/elimina una macro, fai login una volta (sessione persistente),
registra i campi del form col recorder live, e lancia la compilazione su un foglio.
"""
from __future__ import annotations

import asyncio
import json
import logging

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from .. import db, jobs
from ..auth import CurrentUser, get_current_user, require_architect_or_admin
from ..fascicoli import sheets_db
from ..templates import templates
from ..messaging_suite import build_suite

router = APIRouter(dependencies=[Depends(require_architect_or_admin)])
log = logging.getLogger(__name__)


@router.get("/strumenti", response_class=HTMLResponse)
async def strumenti(request: Request, current_user: CurrentUser = Depends(get_current_user)):
    ctx = {
        "request": request,
        "current_user": current_user,
        "suite": build_suite(current_user),
    }
    return templates.TemplateResponse(request, "strumenti.html", ctx)


# ===========================================================================
# Portal macros (compilazione assistita portali — agent_mode='portal_fill')
# ===========================================================================

def _parse_fields(raw: str) -> list[dict]:
    """Parsa l'editor JSON dei campi della macro. [] su input vuoto/non valido."""
    raw = (raw or "").strip()
    if not raw:
        return []
    try:
        data = json.loads(raw)
        return data if isinstance(data, list) else []
    except json.JSONDecodeError:
        return []


@router.get("/strumenti/portal-macros", response_class=HTMLResponse)
async def portal_macros_list(request: Request, flash: str = "", error: str = ""):
    from .portal_recorder_state import recorder_status_map  # local import (vedi sotto)
    macros = db.list_portal_macros()
    return templates.TemplateResponse(
        request, "portal_macros.html",
        {
            "request": request,
            "macros": macros,
            "recorder_status": recorder_status_map([m["id"] for m in macros]),
            "flash": flash,
            "error": error,
        },
    )


@router.get("/strumenti/portal-macros/new", response_class=HTMLResponse)
async def portal_macro_new_form(request: Request):
    return templates.TemplateResponse(
        request, "portal_macro_edit.html",
        {"request": request, "macro": None, "sheets": sheets_db.list_sheets(architect_view=True)},
    )


@router.post("/strumenti/portal-macros")
async def portal_macro_create(
    request: Request,
    name: str = Form(""),
    portal_url: str = Form(""),
    submit_selector: str = Form(""),
    auto_submit: str = Form(""),
    fields_json: str = Form("[]"),
):
    name = (name or "").strip()
    if not name:
        return RedirectResponse("/strumenti/portal-macros?error=Nome+obbligatorio", status_code=303)
    macro_id = db.create_portal_macro(
        {
            "name": name,
            "portal_url": (portal_url or "").strip(),
            "submit_selector": (submit_selector or "").strip() or None,
            "auto_submit": str(auto_submit).strip() in ("1", "on", "true", "yes"),
            "fields": _parse_fields(fields_json),
            "login_session_key": None,  # impostato al primo login (default macro-{id})
        }
    )
    log.info("portal_macro creata id=%s name=%s", macro_id, name)
    return RedirectResponse(
        f"/strumenti/portal-macros/{macro_id}/edit?flash=Macro+creata", status_code=303
    )


@router.get("/strumenti/portal-macros/{macro_id}/edit", response_class=HTMLResponse)
async def portal_macro_edit_form(request: Request, macro_id: int, flash: str = ""):
    from .portal_recorder_state import recorder_status_one
    macro = db.get_portal_macro(macro_id)
    if not macro:
        return RedirectResponse("/strumenti/portal-macros?error=Macro+non+trovata", status_code=303)
    return templates.TemplateResponse(
        request, "portal_macro_edit.html",
        {
            "request": request,
            "macro": macro,
            "sheets": sheets_db.list_sheets(architect_view=True),
            "recorder": recorder_status_one(macro_id),
        },
    )


@router.post("/strumenti/portal-macros/{macro_id}/edit")
async def portal_macro_update(
    request: Request,
    macro_id: int,
    name: str = Form(""),
    portal_url: str = Form(""),
    submit_selector: str = Form(""),
    auto_submit: str = Form(""),
    fields_json: str = Form("[]"),
):
    macro = db.get_portal_macro(macro_id)
    if not macro:
        return RedirectResponse("/strumenti/portal-macros?error=Macro+non+trovata", status_code=303)
    db.update_portal_macro(
        macro_id,
        {
            "name": (name or "").strip() or macro["name"],
            "portal_url": (portal_url or "").strip(),
            "submit_selector": (submit_selector or "").strip() or None,
            "auto_submit": str(auto_submit).strip() in ("1", "on", "true", "yes"),
            "fields": _parse_fields(fields_json),
        },
    )
    return RedirectResponse(
        f"/strumenti/portal-macros/{macro_id}/edit?flash=Macro+salvata", status_code=303
    )


@router.post("/strumenti/portal-macros/{macro_id}/delete")
async def portal_macro_delete(request: Request, macro_id: int):
    db.delete_portal_macro(macro_id)
    return RedirectResponse("/strumenti/portal-macros?flash=Macro+eliminata", status_code=303)


# ---- Login sessione on-demand (browser headed, login manuale) -------------

@router.post("/strumenti/portal-macros/{macro_id}/login")
async def portal_macro_login(request: Request, macro_id: int):
    macro = db.get_portal_macro(macro_id)
    if not macro:
        return RedirectResponse("/strumenti/portal-macros?error=Macro+non+trovata", status_code=303)
    session_key = macro.get("login_session_key") or f"macro-{macro_id}"
    if not macro.get("login_session_key"):
        db.update_portal_macro(macro_id, {"login_session_key": session_key})
    portal_url = (macro.get("portal_url") or "").strip()

    from ..agent.portal import recorder

    asyncio.create_task(
        jobs._run_in_proactor_thread(
            lambda: recorder.run_login(macro_id, portal_url, session_key),
            job_id=-macro_id,
        )
    )
    return RedirectResponse(
        f"/strumenti/portal-macros/{macro_id}/edit?flash=Finestra+Chromium+in+apertura."
        f"+Fai+login+sul+portale,+poi+chiudi+la+finestra.",
        status_code=303,
    )


# ---- Recorder live (cattura campi cliccati via expose_binding) ------------

@router.post("/strumenti/portal-macros/{macro_id}/record")
async def portal_macro_record(request: Request, macro_id: int):
    macro = db.get_portal_macro(macro_id)
    if not macro:
        return RedirectResponse("/strumenti/portal-macros?error=Macro+non+trovata", status_code=303)
    session_key = macro.get("login_session_key") or f"macro-{macro_id}"
    portal_url = (macro.get("portal_url") or "").strip()

    from ..agent.portal import recorder

    # on_capture: accoda lo step catturato alla macro, preservando la FASE.
    # Ogni step (fill / click di navigazione / submit) è una voce della sequenza;
    # il runner la esegue secondo il loop warmup → (ritorno → attività)×righe → chiusura.
    _VALID_PHASES = ("warmup", "activity", "return", "closing")

    def _persist(field: dict) -> None:
        try:
            cur = db.get_portal_macro(macro_id)
            if not cur:
                return
            phase = str(field.get("phase") or "activity").strip().lower()
            if phase not in _VALID_PHASES:
                phase = "activity"
            action = field.get("action") or "fill"
            existing = json.loads(cur.get("fields_json") or "[]")
            # dedup per (selettore, fase): lo stesso elemento può comparire in fasi
            # diverse (es. un link cliccato sia in warmup sia in ritorno).
            if any(e.get("selector") == field.get("selector") and e.get("phase") == phase
                   for e in existing):
                return
            existing.append({
                "selector": field.get("selector"),
                "strategy": field.get("strategy") or "css",
                "semantic_label": field.get("label") or "",
                "source": "column" if action == "fill" else "const",
                "column_name": "",
                "const_value": "",
                "action": action,
                "phase": phase,
            })
            db.update_portal_macro(macro_id, {"fields": existing})
        except Exception as e:
            log.warning("persist captured step fail: %s", e)

    asyncio.create_task(
        jobs._run_in_proactor_thread(
            lambda: recorder.run_record(macro_id, portal_url, session_key, _persist),
            job_id=-macro_id,
        )
    )
    return RedirectResponse(
        f"/strumenti/portal-macros/{macro_id}/edit?flash=Recorder+attivo."
        f"+Clicca+i+campi+del+form,+poi+chiudi+la+finestra+e+ricarica+questa+pagina.",
        status_code=303,
    )


@router.post("/strumenti/portal-macros/{macro_id}/record/stop")
async def portal_macro_record_stop(request: Request, macro_id: int):
    from ..agent.portal import recorder
    recorder.stop(macro_id)
    return RedirectResponse(
        f"/strumenti/portal-macros/{macro_id}/edit?flash=Sessione+chiusa", status_code=303
    )


# ---- Colonne di un foglio (per il pannello di mapping) --------------------

@router.get("/strumenti/portal-macros/{macro_id}/columns")
async def portal_macro_sheet_columns(request: Request, macro_id: int, sheet_id: int = 0):
    """JSON con i nomi colonna (riga header) del foglio scelto. Usato dal pannello
    di mapping campo->colonna nella pagina di edit macro."""
    from fastapi.responses import JSONResponse
    if not sheet_id:
        return JSONResponse({"columns": []})
    cols = sheets_db.sheet_column_names(int(sheet_id))
    return JSONResponse({"columns": cols})


# ---- Esecuzione: crea un task portal_fill e lancia il job -----------------

@router.post("/strumenti/portal-macros/{macro_id}/run")
async def portal_macro_run(
    request: Request,
    macro_id: int,
    portal_sheet_id: str = Form(""),
    portal_auto_submit: str = Form(""),
):
    macro = db.get_portal_macro(macro_id)
    if not macro:
        return RedirectResponse("/strumenti/portal-macros?error=Macro+non+trovata", status_code=303)
    if not str(portal_sheet_id).strip().isdigit():
        return RedirectResponse(
            f"/strumenti/portal-macros/{macro_id}/edit?error=Scegli+un+foglio+per+eseguire",
            status_code=303,
        )
    # auto_submit del task = spunta nel form di run OPPURE default ereditato dalla
    # macro (impostato una volta sulla macro, vale per ogni esecuzione).
    run_checked = str(portal_auto_submit).strip() in ("1", "on", "true", "yes")
    auto_submit = run_checked or bool(macro.get("auto_submit"))
    task_id = db.create_task({
        "name": f"Compila portale: {macro['name']}",
        "objective": f"Compila il form su {macro.get('portal_url')} dal foglio scelto.",
        "agent_mode": "portal_fill",
        "portal_macro_id": macro_id,
        "portal_sheet_id": int(portal_sheet_id),
        "portal_auto_submit": auto_submit,
        "headed": 1,
    })
    job_id = jobs.start_job(task_id)
    log.info("portal_fill avviato: macro=%s task=%s job=%s", macro_id, task_id, job_id)
    return RedirectResponse(f"/jobs/{job_id}/status", status_code=303)
