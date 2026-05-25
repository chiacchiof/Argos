"""Route per servire le guide markdown da `docs/` (USER_GUIDE, ADMIN_GUIDE).

Whitelist hardcoded: solo i file noti per evitare path traversal. Le guide sono
versionate in repo e rispecchiano lo stato corrente dell'app — la fonte di
verita' resta il markdown, qui le rendiamo HTML per consumo in-app.
"""
from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import HTMLResponse

from ..markdown_render import render_docs_markdown
from ..templates import templates


log = logging.getLogger(__name__)
router = APIRouter()


# Repo root: app/routes/docs.py -> app/routes -> app -> repo_root
_REPO_ROOT = Path(__file__).resolve().parents[2]
_DOCS_DIR = _REPO_ROOT / "docs"


# Whitelist nome -> (titolo, descrizione) - super-admin only flag.
_AVAILABLE_GUIDES: dict[str, dict] = {
    "USER_GUIDE": {
        "title": "Guida utente",
        "description": "Come si organizza il lavoro: task, workflow, asset, outreach, inbox, site memory.",
        "icon": "📘",
        "admin_only": False,
    },
    "ADMIN_GUIDE": {
        "title": "Guida super-admin",
        "description": "Gestione tenant, utenti, filtri cross-tenant, vault chiavi LLM, best practices.",
        "icon": "👑",
        "admin_only": True,
    },
}


@router.get("/docs", response_class=HTMLResponse)
async def docs_index(request: Request):
    """Indice delle guide disponibili.

    Mostra USER_GUIDE a tutti, ADMIN_GUIDE solo ai super-admin.
    """
    current_user = getattr(request.state, "current_user", None)
    is_super_admin = bool(current_user and getattr(current_user, "is_super_admin", False))
    visible: list[dict] = []
    for slug, info in _AVAILABLE_GUIDES.items():
        if info["admin_only"] and not is_super_admin:
            continue
        visible.append({"slug": slug, **info})
    return templates.TemplateResponse(
        request,
        "docs_index.html",
        {"guides": visible, "is_super_admin": is_super_admin},
    )


@router.get("/docs/{name}", response_class=HTMLResponse)
async def docs_view(request: Request, name: str):
    """Renderizza una guida markdown a HTML.

    Whitelist enforcement: `name` deve essere in `_AVAILABLE_GUIDES`.
    Gate super-admin per le guide marcate `admin_only`.
    """
    info = _AVAILABLE_GUIDES.get(name)
    if not info:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Guida non trovata")

    if info["admin_only"]:
        current_user = getattr(request.state, "current_user", None)
        is_super_admin = bool(current_user and getattr(current_user, "is_super_admin", False))
        if not is_super_admin:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Questa guida e' riservata ai super-admin.",
            )

    md_path = _DOCS_DIR / f"{name}.md"
    if not md_path.exists():
        log.warning("Guida %s non trovata in %s", name, md_path)
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="File guida non trovato")

    try:
        md_src = md_path.read_text(encoding="utf-8")
    except OSError as exc:
        log.error("Errore lettura guida %s: %s", name, exc)
        raise HTTPException(status_code=500, detail="Errore lettura guida")

    html = render_docs_markdown(md_src)
    return templates.TemplateResponse(
        request,
        "docs_view.html",
        {
            "guide_name": name,
            "guide_title": info["title"],
            "guide_icon": info["icon"],
            "guide_html": html,
        },
    )
