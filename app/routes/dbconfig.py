"""Pagina /dbconfig — configurazione DSN del DB cloud.

Pagina riservata a un singolo utente "DBadmin" con credenziali offuscate.
Permette di switchare tra DB locale (dev) e DB remoto (prod) senza editare
`.env`, ad esempio per testare modifiche di schema in locale prima di
applicarle al Postgres di produzione.

Flusso:
1. GET /dbconfig — se non autenticato come DBadmin, mostra form di login.
2. POST /dbconfig/login — verifica username + password (bcrypt). Setta un
   flag nella session.
3. POST /dbconfig/save — scrive la nuova DSN nel file cifrato `data/db_config.enc`.
4. POST /dbconfig/clear — rimuove l'override, torna alla DSN di `.env`.
5. POST /dbconfig/logout — pulisce il flag.

Nota: la nuova DSN diventa attiva solo al RIAVVIO dell'app (banner in UI).

Credenziali: l'username e l'hash bcrypt sono hardcoded sotto. La password
in chiaro NON appare nel sorgente; il hash è non-reversibile. Cambiarla
richiede di generare un nuovo hash e modificare il codice + redeploy.
"""
from __future__ import annotations

import logging
import os
from typing import Any

import bcrypt
from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse

from .. import _runtime_db_override as db_override
from ..templates import templates


log = logging.getLogger(__name__)
router = APIRouter(prefix="/dbconfig")


# ---------------------------------------------------------------------------
# Credenziali "offuscate": username + hash bcrypt della password.
# Per ruotarle: python -c "import bcrypt; print(bcrypt.hashpw(b'NUOVA_PWD', bcrypt.gensalt(rounds=12)).decode())"
# ---------------------------------------------------------------------------
_DBADMIN_USER = "DBadmin"
_DBADMIN_PWD_HASH = "$2b$12$2R9lbW4Yp98psD..m0JtLesubzOhicJgU73LY6SILXOVhCpIafxWm"

_SESSION_FLAG = "dbadmin_authed"


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------

def _is_authenticated(request: Request) -> bool:
    try:
        return bool(request.session.get(_SESSION_FLAG))
    except (AttributeError, AssertionError):
        return False


def _check_credentials(username: str, password: str) -> bool:
    if username.strip() != _DBADMIN_USER:
        return False
    try:
        return bcrypt.checkpw(password.encode("utf-8")[:72], _DBADMIN_PWD_HASH.encode("ascii"))
    except Exception:
        return False


def _mask_dsn(dsn: str) -> str:
    """Maschera la password dentro una connection string per la visualizzazione."""
    if not dsn:
        return ""
    # postgresql://user:password@host/db  ->  postgresql://user:****@host/db
    if "://" not in dsn or "@" not in dsn:
        return dsn
    scheme, rest = dsn.split("://", 1)
    creds, hostpart = rest.split("@", 1)
    if ":" in creds:
        user, _ = creds.split(":", 1)
        return f"{scheme}://{user}:****@{hostpart}"
    return dsn


def _context(request: Request, **extra: Any) -> dict:
    """Context comune per i template /dbconfig."""
    override = db_override.read_override()
    env_dsn = os.environ.get("DATABASE_URL", "")
    return {
        "request": request,
        "authed": _is_authenticated(request),
        "override_active": override is not None,
        "active_label": (override or {}).get("active_label", ""),
        "current_dsn_masked": _mask_dsn(env_dsn),
        "current_dsn_raw_present": bool(env_dsn),
        **extra,
    }


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@router.get("")
@router.get("/")
def dbconfig_home(request: Request):
    if not _is_authenticated(request):
        return templates.TemplateResponse(request, "dbconfig/login.html", _context(request))
    return templates.TemplateResponse(request, "dbconfig/panel.html", _context(request))


@router.post("/login")
def dbconfig_login(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
):
    if not _check_credentials(username, password):
        return templates.TemplateResponse(
            request,
            "dbconfig/login.html",
            _context(request, error="Credenziali non valide."),
            status_code=401,
        )
    request.session[_SESSION_FLAG] = True
    return RedirectResponse(url="/dbconfig", status_code=303)


@router.api_route("/logout", methods=["GET", "POST"])
def dbconfig_logout(request: Request):
    try:
        request.session.pop(_SESSION_FLAG, None)
    except (AttributeError, AssertionError):
        pass
    return RedirectResponse(url="/dbconfig", status_code=303)


@router.post("/save")
def dbconfig_save(
    request: Request,
    database_url: str = Form(""),
    active_label: str = Form(""),
):
    if not _is_authenticated(request):
        return RedirectResponse(url="/dbconfig", status_code=303)

    dsn = database_url.strip()
    if not dsn:
        return templates.TemplateResponse(
            request,
            "dbconfig/panel.html",
            _context(request, error="La connection string non può essere vuota."),
            status_code=400,
        )

    # Validazione minimale: deve iniziare con postgres / postgresql
    if not dsn.startswith(("postgresql://", "postgres://")):
        return templates.TemplateResponse(
            request,
            "dbconfig/panel.html",
            _context(
                request,
                error="La DSN deve iniziare con `postgresql://` o `postgres://`.",
            ),
            status_code=400,
        )

    try:
        db_override.write_override(dsn, active_label or "")
    except Exception as exc:
        log.error("Errore scrittura override DSN: %s", exc)
        return templates.TemplateResponse(
            request,
            "dbconfig/panel.html",
            _context(request, error=f"Errore: {exc}"),
            status_code=500,
        )

    return templates.TemplateResponse(
        request,
        "dbconfig/panel.html",
        _context(
            request,
            success=(
                f"DSN salvata (label='{active_label or '—'}'). "
                "RIAVVIA l'app per applicare la nuova connessione."
            ),
        ),
    )


@router.post("/clear")
def dbconfig_clear(request: Request):
    if not _is_authenticated(request):
        return RedirectResponse(url="/dbconfig", status_code=303)
    try:
        db_override.clear_override()
    except Exception as exc:
        log.error("Errore clear override: %s", exc)
        return templates.TemplateResponse(
            request,
            "dbconfig/panel.html",
            _context(request, error=f"Errore: {exc}"),
            status_code=500,
        )
    return templates.TemplateResponse(
        request,
        "dbconfig/panel.html",
        _context(
            request,
            success="Override rimosso. Al prossimo riavvio l'app userà la DSN di .env (o nessuna).",
        ),
    )
