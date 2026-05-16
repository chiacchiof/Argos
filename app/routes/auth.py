"""Route di autenticazione: login / logout.

Endpoint pubblici (non richiedono auth):
- GET  /login   -> form HTML
- POST /login   -> verifica credenziali, setta cookie session, redirect a next
- POST /logout  -> svuota la sessione e redirect a /login
- GET  /logout  -> idem (utile per link <a href="/logout">)
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse

from .. import db_cloud
from ..auth import verify_password
from ..templates import templates


log = logging.getLogger(__name__)
router = APIRouter()


def _is_safe_next(path: str | None) -> bool:
    """Evita open-redirect: accetta solo path relativi che iniziano con /."""
    if not path:
        return False
    if not path.startswith("/"):
        return False
    if path.startswith("//"):
        return False
    return True


@router.get("/login")
def login_form(request: Request, next: str = "/", error: str | None = None):
    if not db_cloud.is_configured():
        # Modalità legacy: niente auth, redirect a home
        return RedirectResponse(url="/", status_code=302)
    return templates.TemplateResponse(
        request,
        "login.html",
        {"next": next if _is_safe_next(next) else "/", "error": error},
    )


@router.post("/login")
def login_submit(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    next: str = Form("/"),
):
    if not db_cloud.is_configured():
        return RedirectResponse(url="/", status_code=302)

    try:
        row = db_cloud.get_user_by_email(email)
    except Exception as exc:
        log.error("Login: errore lookup utente: %s", exc)
        return templates.TemplateResponse(
            request,
            "login.html",
            {
                "next": next if _is_safe_next(next) else "/",
                "error": "Servizio non disponibile, riprova più tardi.",
            },
            status_code=503,
        )

    if not row or not row.get("is_active") or not verify_password(password, row["password_hash"]):
        return templates.TemplateResponse(
            request,
            "login.html",
            {
                "next": next if _is_safe_next(next) else "/",
                "error": "Credenziali non valide.",
            },
            status_code=401,
        )

    request.session["user_id"] = int(row["id"])
    target = next if _is_safe_next(next) else "/"
    return RedirectResponse(url=target, status_code=303)


@router.api_route("/logout", methods=["GET", "POST"])
def logout(request: Request):
    try:
        request.session.clear()
    except (AttributeError, AssertionError):
        pass
    return RedirectResponse(url="/login", status_code=303)
