"""Pagina /update — istruzioni per aggiornare il client.

Mostra:
- Versione corrente locale (app.__version__)
- Versione più recente su GitHub (se release_check è attivo)
- Note di rilascio (markdown)
- Comandi PowerShell concreti da eseguire (riferimento a scripts/update_client.ps1)

Endpoint pubblico (no login richiesto, è solo lettura + istruzioni).
"""
from __future__ import annotations

from fastapi import APIRouter, Request

from .. import __version__, release_check
from ..templates import templates


router = APIRouter()


@router.get("/update")
def update_page(request: Request):
    rel = release_check.latest_release() if release_check.is_enabled() else None
    info = release_check.update_available() if release_check.is_enabled() else None
    return templates.TemplateResponse(
        request,
        "update.html",
        {
            "current": __version__,
            "release": rel,
            "update_info": info,
            "github_repo_configured": release_check.is_enabled(),
        },
    )
