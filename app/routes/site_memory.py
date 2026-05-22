"""Memoria sito (CRUD): gestione `site_patterns` + `site_playbooks`.

`site_patterns` = regex di pattern URL apprese dai runner per dominio.
`site_playbooks` = istruzioni operative cross-runner (Stage 2 knowledge transfer).

Pagina unica per ispezionare e cancellare entries selettive o l'intera memoria.
Utile prima di test "puliti da 0" o quando un sito cambia struttura.
"""
from __future__ import annotations

import json

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from .. import db
from ..templates import templates


router = APIRouter()


@router.get("/site_memory", response_class=HTMLResponse)
async def site_memory_list(
    request: Request,
    domain: str | None = None,
):
    """Lista pattern + playbook visibili al tenant corrente (o tutti per
    super_admin / tenant con `site_memory_shared=TRUE`).

    Il filtraggio per tenant_id avviene dentro `db.list_site_patterns` /
    `db.list_site_playbooks` via `_site_memory_tenant_filter`.
    """
    domain = (domain or "").strip().lower() or None

    patterns = db.list_site_patterns(registrable_domain=domain, limit=500)
    playbooks_raw = db.list_site_playbooks(registrable_domain=domain, limit=500)

    # I playbook hanno il campo `playbook` JSON-serializzato: parsiamolo per la view.
    playbooks = []
    for r in playbooks_raw:
        pb_text = ""
        pb_blockers: list = []
        try:
            obj = json.loads(r.get("playbook") or "{}")
            pb_text = obj.get("text") or ""
            pb_blockers = obj.get("blockers") or []
        except Exception:
            pb_text = r.get("playbook") or ""
        playbooks.append({**r, "playbook_text": pb_text, "playbook_blockers": pb_blockers})

    # Domini distinti per il dropdown filtro
    all_domains = sorted({
        *(p.get("registrable_domain") for p in patterns if p.get("registrable_domain")),
        *(p.get("registrable_domain") for p in playbooks if p.get("registrable_domain")),
    })

    # Info visibilita' per la banner-info nel template.
    current_user = getattr(request.state, "current_user", None)
    is_super_admin = bool(current_user and current_user.is_super_admin)
    visibility_mode = "all"
    if not is_super_admin:
        # Lookup del flag del tenant corrente per mostrare il "perche'" all'utente.
        tenant_id = db.current_tenant_id()
        visibility_mode = "shared" if db._can_see_all_site_memory(tenant_id) else "isolated"

    return templates.TemplateResponse(
        request,
        "site_memory.html",
        {
            "patterns": patterns,
            "playbooks": playbooks,
            "filter_domain": domain or "",
            "all_domains": all_domains,
            "n_patterns": len(patterns),
            "n_playbooks": len(playbooks),
            "is_super_admin": is_super_admin,
            "visibility_mode": visibility_mode,
        },
    )


@router.post("/site_memory/pattern/{pattern_id}/delete")
async def site_memory_delete_pattern(pattern_id: int, request: Request):
    db.delete_site_pattern(pattern_id)
    return RedirectResponse(url="/site_memory", status_code=303)


@router.post("/site_memory/playbook/{playbook_id}/delete")
async def site_memory_delete_playbook(playbook_id: int, request: Request):
    db.delete_site_playbook(playbook_id)
    return RedirectResponse(url="/site_memory", status_code=303)


@router.post("/site_memory/domain/delete")
async def site_memory_delete_domain(domain: str = Form("")):
    """Cancella tutti i pattern + playbook di un singolo dominio."""
    domain = (domain or "").strip().lower()
    if not domain:
        raise HTTPException(status_code=400, detail="domain mancante")
    n_pat = db.delete_site_patterns_by_domain(domain)
    n_pb = db.delete_site_playbooks_by_domain(domain)
    return RedirectResponse(
        url=f"/site_memory?_msg=cancellati+{n_pat}+pattern+e+{n_pb}+playbook+per+{domain}",
        status_code=303,
    )


@router.post("/site_memory/flush_all")
async def site_memory_flush_all(confirm: str = Form("")):
    """Svuota completamente la memoria sito. Richiede confirm='YES'."""
    if confirm.strip().upper() != "YES":
        raise HTTPException(status_code=400, detail="confirm='YES' richiesto")
    res = db.truncate_site_memory()
    return RedirectResponse(
        url=f"/site_memory?_msg=svuotata+memoria:+{res['site_patterns']}+pattern+{res['site_playbooks']}+playbook",
        status_code=303,
    )
