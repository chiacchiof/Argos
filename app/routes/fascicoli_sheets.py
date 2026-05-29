"""Argos Fogli collaborativi — routes HTTP.

Endpoint (tenant-scoped via ContextVar del middleware HTTP):
  GET  /sheets                          -> lista fogli del tenant (standalone + agganciati)
  POST /sheets                          -> crea foglio (standalone o agganciato a un fascicolo)
  GET  /sheets/{id}                     -> editor (pagina full-screen, griglia)
  GET  /sheets/{id}/snapshot            -> JSON snapshot celle + revisione + permessi
  POST /sheets/{id}/patch               -> applica patch celle (fallback HTTP non-realtime)
  POST /sheets/{id}/rename              -> rinomina (manage)
  POST /sheets/{id}/visibility          -> cambia visibilita' (manage)
  POST /sheets/{id}/archive             -> archivia (manage)
  POST /sheets/{id}/restore             -> ripristina (manage)
  POST /sheets/{id}/delete              -> elimina (manage)
  GET  /sheets/{id}/export.csv          -> export CSV (anti formula-injection)
  GET  /sheets/{id}/export.xlsx         -> export XLSX (apribile in Excel/Google Sheets)

Il WebSocket realtime e' in app/routes/fascicoli_sheets_ws.py.

Permessi: vedi app/fascicoli/acl.py. Modello tenant-collaborativo: ogni
operatore/architetto del tenant apre e modifica i fogli 'tenant'; gestione
(rename/archive/visibilita'/delete) riservata a creatore/architetto/super-admin
o a chi gestisce il fascicolo agganciato.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from .. import db_cloud
from ..auth import CurrentUser, get_current_user
from ..fascicoli import acl as facl
from ..fascicoli import db as fdb
from ..fascicoli import share as fshare
from ..fascicoli import sheets_db as sdb
from ..fascicoli import sheets_export
from ..templates import templates

log = logging.getLogger(__name__)

router = APIRouter(prefix="/sheets", dependencies=[Depends(get_current_user)])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _base_template(user: CurrentUser) -> str:
    """Template base coerente col ruolo (operatori usano la shell semplificata)."""
    return "operator_base.html" if user.is_operator else "base.html"


def _load_sheet_or_404(
    sheet_id: int,
    user: CurrentUser,
    *,
    require_edit: bool = False,
    require_manage: bool = False,
) -> tuple[dict, dict | None]:
    """Carica (sheet, project) applicando tenant + visibilita' + ACL.

    Ritorna 404 se il foglio non esiste/non e' visibile (no information leak su
    esistenza), 403 se manca il permesso di edit/manage richiesto."""
    architect_view = user.can_manage_architecture
    sheet = sdb.get_sheet(sheet_id, current_user_id=user.id, architect_view=architect_view)
    if not sheet:
        raise HTTPException(404, "Foglio non trovato o non accessibile.")
    project = None
    if sheet.get("project_id"):
        project = fdb.get_project(
            sheet["project_id"], current_user_id=user.id, architect_view=architect_view
        )
    if not facl.can_open_sheet(sheet, project, user):
        raise HTTPException(404, "Foglio non trovato o non accessibile.")
    if require_edit and not facl.can_edit_sheet_cells(sheet, project, user):
        raise HTTPException(403, "Non puoi modificare questo foglio.")
    if require_manage and not facl.can_manage_sheet(sheet, project, user):
        raise HTTPException(403, "Non puoi gestire questo foglio.")
    return sheet, project


# ---------------------------------------------------------------------------
# Lista + creazione
# ---------------------------------------------------------------------------

@router.get("", response_class=HTMLResponse)
async def sheets_list(
    request: Request,
    current_user: CurrentUser = Depends(get_current_user),
    include_archived: int = 0,
):
    architect_view = current_user.can_manage_architecture
    sheets = sdb.list_sheets(
        current_user_id=current_user.id,
        architect_view=architect_view,
        include_archived=bool(include_archived),
    )
    # annota can_manage per foglio (mostra il menu ⋮ solo a chi puo' gestire)
    for s in sheets:
        project = None
        if s.get("project_id"):
            project = fdb.get_project(s["project_id"], current_user_id=current_user.id,
                                      architect_view=architect_view)
        s["_can_manage"] = facl.can_manage_sheet(s, project, current_user)
    standalone = [s for s in sheets if not s.get("project_id")]
    attached = [s for s in sheets if s.get("project_id")]
    resp = templates.TemplateResponse(
        request,
        "sheets_list.html",
        {
            "base_template": _base_template(current_user),
            "sheets": sheets,
            "standalone": standalone,
            "attached": attached,
            "include_archived": bool(include_archived),
        },
    )
    # no-store: l'HTML (con CSS/JS inline di menu e tab) deve essere sempre fresco
    resp.headers["Cache-Control"] = "no-store, must-revalidate"
    return resp


@router.post("")
async def sheets_create(
    request: Request,
    title: str = Form(""),
    visibility: str = Form("tenant"),
    project_id: str = Form(""),
    current_user: CurrentUser = Depends(get_current_user),
):
    if current_user.tenant_id is None:
        raise HTTPException(403, "Solo utenti del tenant possono creare fogli.")
    if visibility not in ("tenant", "user"):
        raise HTTPException(400, f"visibility non valida: {visibility}")

    pid: int | None = None
    if (project_id or "").strip():
        try:
            pid = int(project_id)
        except ValueError:
            raise HTTPException(400, "project_id non valido.")
        # Verifica accesso al progetto: deve essere visibile + modificabile
        # dall'utente (un viewer non puo' creare fogli nel fascicolo).
        project = fdb.get_project(
            pid, current_user_id=current_user.id, architect_view=current_user.can_manage_architecture
        )
        if not project:
            raise HTTPException(404, "Fascicolo non trovato o non accessibile.")
        if not facl.can_edit_project(project, current_user):
            raise HTTPException(403, "Non puoi creare fogli in questo fascicolo.")

    try:
        sheet_id = sdb.create_sheet(
            title=title,
            project_id=pid,
            visibility=visibility,
            tenant_id=current_user.tenant_id,
            created_by_user_id=current_user.id,
        )
    except sdb.SheetForbidden:
        raise HTTPException(403, "Fascicolo non valido per questo tenant.")
    log.info("sheet created id=%s project=%s by=%s", sheet_id, pid, current_user.email)
    return RedirectResponse(url=f"/sheets/{sheet_id}", status_code=303)


# ---------------------------------------------------------------------------
# Editor (pagina full-screen)
# ---------------------------------------------------------------------------

@router.get("/{sheet_id}", response_class=HTMLResponse)
async def sheet_editor(
    request: Request,
    sheet_id: int,
    current_user: CurrentUser = Depends(get_current_user),
):
    sheet, project = _load_sheet_or_404(sheet_id, current_user)
    can_edit = facl.can_edit_sheet_cells(sheet, project, current_user)
    can_manage = facl.can_manage_sheet(sheet, project, current_user)
    resp = templates.TemplateResponse(
        request,
        "sheet_editor.html",
        {
            "sheet": sheet,
            "project": project,
            "can_edit": can_edit,
            "can_manage": can_manage,
            # back link: al fascicolo se agganciato, altrimenti alla lista fogli
            "back_url": f"/fascicoli/{sheet['project_id']}" if sheet.get("project_id") else "/sheets",
        },
    )
    # No-store: l'HTML dell'editor deve sempre ricaricare l'ultima versione di
    # sheets.js/css (il ?v=mtime nel markup cambia solo se l'HTML e' fresco).
    resp.headers["Cache-Control"] = "no-store, must-revalidate"
    return resp


# ---------------------------------------------------------------------------
# JSON: snapshot + patch (HTTP fallback; il realtime passa dal WebSocket)
# ---------------------------------------------------------------------------

@router.get("/{sheet_id}/snapshot")
async def sheet_snapshot(
    sheet_id: int,
    current_user: CurrentUser = Depends(get_current_user),
):
    sheet, project = _load_sheet_or_404(sheet_id, current_user)
    cells = sdb.get_cells(sheet_id)
    return {
        "type": "snapshot",
        "sheet_id": sheet_id,
        "revision": int(sheet["revision"]),
        "n_rows": sheet["n_rows"],
        "n_cols": sheet["n_cols"],
        "title": sheet["title"],
        "can_edit": facl.can_edit_sheet_cells(sheet, project, current_user),
        "cells": cells,
        "users": [],
    }


@router.post("/{sheet_id}/patch")
async def sheet_patch_http(
    request: Request,
    sheet_id: int,
    current_user: CurrentUser = Depends(get_current_user),
):
    """Fallback HTTP per applicare una patch quando il WebSocket non e'
    disponibile. Stesso percorso transazionale del WS."""
    sheet, project = _load_sheet_or_404(sheet_id, current_user, require_edit=True)
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "Body JSON non valido.")
    if not isinstance(body, dict):
        raise HTTPException(400, "Payload non valido.")
    cells = body.get("cells")
    try:
        result = sdb.apply_cell_patch(sheet_id, cells, actor_user_id=current_user.id)
    except sdb.SheetValidationError as exc:
        raise HTTPException(400, str(exc))
    except sdb.SheetForbidden:
        raise HTTPException(404, "Foglio non trovato.")

    # Fan-out realtime ai client WS collegati (se il modulo realtime e' caricato).
    # In Fase 2 (no WS) e' un no-op silenzioso.
    try:
        from ..fascicoli import realtime
        await realtime.broadcast_revision(
            sheet_id,
            revision=result["revision"],
            actor_user_id=current_user.id,
            cells=result["cells"],
            origin_patch_id=body.get("patch_id"),
        )
    except Exception:  # pragma: no cover - realtime opzionale
        pass

    return {
        "type": "revision_patch",
        "sheet_id": sheet_id,
        "revision": result["revision"],
        "patch_id": body.get("patch_id"),
        "cells": result["cells"],
    }


# ---------------------------------------------------------------------------
# Condivisione (modale: utenti del tenant + permessi)
# ---------------------------------------------------------------------------

def _share_modal_response(request: Request, sheet: dict, current_user: CurrentUser):
    members = sdb.list_sheet_members(sheet["id"])
    member_role = {m["user_id"]: m["role"] for m in members}
    tenant_users = db_cloud.list_users(tenant_id=current_user.tenant_id) if current_user.tenant_id else []
    ctx = fshare.build_share_context(
        kind="sheet", title=sheet["title"], base=f"/sheets/{sheet['id']}",
        visibility=sheet.get("visibility", "tenant"),
        owner_user_id=sheet.get("created_by_user_id"),
        member_role=member_role, tenant_users=tenant_users,
    )
    return templates.TemplateResponse(request, "share_modal.html", {"share": ctx})


@router.get("/{sheet_id}/share", response_class=HTMLResponse)
async def sheet_share_get(
    request: Request,
    sheet_id: int,
    current_user: CurrentUser = Depends(get_current_user),
):
    sheet, _ = _load_sheet_or_404(sheet_id, current_user, require_manage=True)
    return _share_modal_response(request, sheet, current_user)


@router.post("/{sheet_id}/share", response_class=HTMLResponse)
async def sheet_share_set(
    request: Request,
    sheet_id: int,
    user_id: int = Form(...),
    role: str = Form(...),
    current_user: CurrentUser = Depends(get_current_user),
):
    sheet, _ = _load_sheet_or_404(sheet_id, current_user, require_manage=True)
    if role == "none":
        sdb.remove_sheet_member(sheet_id, user_id)
    elif role in ("viewer", "editor"):
        try:
            sdb.add_sheet_member(sheet_id, user_id, role)
        except sdb.SheetForbidden:
            raise HTTPException(400, "Utente non valido per questo foglio.")
    else:
        raise HTTPException(400, "Ruolo non valido.")
    return _share_modal_response(request, sheet, current_user)


@router.post("/{sheet_id}/share/visibility", response_class=HTMLResponse)
async def sheet_share_visibility(
    request: Request,
    sheet_id: int,
    visibility: str = Form(...),
    current_user: CurrentUser = Depends(get_current_user),
):
    sheet, _ = _load_sheet_or_404(sheet_id, current_user, require_manage=True)
    if visibility not in ("tenant", "user"):
        raise HTTPException(400, "visibility non valida.")
    sdb.set_sheet_visibility(sheet_id, visibility)
    sheet["visibility"] = visibility
    return _share_modal_response(request, sheet, current_user)


# ---------------------------------------------------------------------------
# Export (CSV anti-injection / XLSX apribile in Excel/Google Sheets)
# ---------------------------------------------------------------------------

@router.get("/{sheet_id}/export.csv")
async def sheet_export_csv(
    sheet_id: int,
    current_user: CurrentUser = Depends(get_current_user),
):
    sheet, _ = _load_sheet_or_404(sheet_id, current_user)
    cells = sdb.get_cells(sheet_id)
    data = sheets_export.to_csv(cells)
    fname = sheets_export.safe_filename(sheet["title"], "csv")
    return Response(
        content=data,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


@router.get("/{sheet_id}/export.xlsx")
async def sheet_export_xlsx(
    sheet_id: int,
    current_user: CurrentUser = Depends(get_current_user),
):
    sheet, _ = _load_sheet_or_404(sheet_id, current_user)
    cells = sdb.get_cells(sheet_id)
    data = sheets_export.to_xlsx(cells)
    fname = sheets_export.safe_filename(sheet["title"], "xlsx")
    return Response(
        content=data,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


# ---------------------------------------------------------------------------
# Gestione (manage)
# ---------------------------------------------------------------------------

@router.post("/{sheet_id}/rename")
async def sheet_rename(
    sheet_id: int,
    title: str = Form(...),
    current_user: CurrentUser = Depends(get_current_user),
):
    _load_sheet_or_404(sheet_id, current_user, require_manage=True)
    title = (title or "").strip()
    if not title:
        raise HTTPException(400, "Titolo vuoto.")
    sdb.rename_sheet(sheet_id, title)
    return RedirectResponse(url=f"/sheets/{sheet_id}", status_code=303)


@router.post("/{sheet_id}/visibility")
async def sheet_set_visibility(
    sheet_id: int,
    visibility: str = Form(...),
    current_user: CurrentUser = Depends(get_current_user),
):
    _load_sheet_or_404(sheet_id, current_user, require_manage=True)
    if visibility not in ("tenant", "user"):
        raise HTTPException(400, "visibility non valida.")
    sdb.set_sheet_visibility(sheet_id, visibility)
    return RedirectResponse(url=f"/sheets/{sheet_id}", status_code=303)


@router.post("/{sheet_id}/archive")
async def sheet_archive(
    sheet_id: int,
    current_user: CurrentUser = Depends(get_current_user),
):
    sheet, _ = _load_sheet_or_404(sheet_id, current_user, require_manage=True)
    sdb.set_sheet_archived(sheet_id, True)
    dest = f"/fascicoli/{sheet['project_id']}" if sheet.get("project_id") else "/sheets"
    return RedirectResponse(url=dest, status_code=303)


@router.post("/{sheet_id}/restore")
async def sheet_restore(
    sheet_id: int,
    current_user: CurrentUser = Depends(get_current_user),
):
    _load_sheet_or_404(sheet_id, current_user, require_manage=True)
    sdb.set_sheet_archived(sheet_id, False)
    return RedirectResponse(url=f"/sheets/{sheet_id}", status_code=303)


@router.post("/{sheet_id}/delete")
async def sheet_delete(
    sheet_id: int,
    current_user: CurrentUser = Depends(get_current_user),
):
    sheet, _ = _load_sheet_or_404(sheet_id, current_user, require_manage=True)
    dest = f"/fascicoli/{sheet['project_id']}" if sheet.get("project_id") else "/sheets"
    sdb.delete_sheet(sheet_id)
    return RedirectResponse(url=dest, status_code=303)
