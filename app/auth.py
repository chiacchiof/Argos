"""Autenticazione: hashing password (bcrypt) + dipendenze FastAPI per current_user.

Strategia: cookie session firmato (Starlette `SessionMiddleware`), NO JWT.
Per il rationale completo vedi `SETUP_CLOUD_DB_TENANT.md` sezione "Autenticazione utenti".
"""
from __future__ import annotations

from dataclasses import dataclass

import bcrypt
from fastapi import Depends, HTTPException, Request, status

from . import db_cloud


# bcrypt ha un limite hard di 72 byte sulla password in input. Tronchiamo
# silenziosamente: per password "normali" l'effetto è nullo, per password
# eccezionalmente lunghe è un comportamento documentato (cfr. OWASP).
_BCRYPT_MAX_BYTES = 72


def _to_bcrypt_bytes(password: str) -> bytes:
    pw = password.encode("utf-8")
    if len(pw) > _BCRYPT_MAX_BYTES:
        pw = pw[:_BCRYPT_MAX_BYTES]
    return pw


@dataclass(frozen=True)
class CurrentUser:
    id: int
    email: str
    role: str
    tenant_id: int | None
    tenant_name: str | None
    is_active: bool

    @property
    def is_super_admin(self) -> bool:
        return self.role == "super_admin"


def hash_password(password: str) -> str:
    return bcrypt.hashpw(_to_bcrypt_bytes(password), bcrypt.gensalt()).decode("ascii")


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(_to_bcrypt_bytes(password), password_hash.encode("ascii"))
    except Exception:
        return False


def _user_from_row(row: dict) -> CurrentUser:
    tenant_id = int(row["tenant_id"]) if row.get("tenant_id") is not None else None
    tenant_name: str | None = row.get("tenant_name")
    if tenant_name is None and tenant_id is not None:
        try:
            t = db_cloud.get_tenant(tenant_id)
            tenant_name = t["name"] if t else None
        except Exception:
            tenant_name = None
    return CurrentUser(
        id=int(row["id"]),
        email=row["email"],
        role=row["role"],
        tenant_id=tenant_id,
        tenant_name=tenant_name,
        is_active=bool(row.get("is_active", True)),
    )


def get_optional_user(request: Request) -> CurrentUser | None:
    """Ritorna l'utente loggato o None. Non solleva mai eccezioni.

    Se il cloud DB non è configurato (modalità legacy single-user), ritorna sempre None
    — significa che le route che vogliono current_user devono trattare None come "auth disabilitata".
    """
    if not db_cloud.is_configured():
        return None
    try:
        session = request.session
    except (AttributeError, AssertionError):
        # SessionMiddleware non montato (es. test senza middleware)
        return None
    user_id = session.get("user_id")
    if not user_id:
        return None
    try:
        row = db_cloud.get_user(int(user_id))
    except Exception:
        return None
    if not row or not row.get("is_active"):
        return None
    return _user_from_row(row)


def get_current_user(request: Request) -> CurrentUser:
    """Dependency FastAPI: ritorna l'utente loggato o solleva 401/redirect.

    Per richieste HTMX (header `HX-Request: true`) usa header `HX-Redirect` per
    forzare il redirect lato client. Per richieste HTML normali usa 307 + Location.
    """
    user = get_optional_user(request)
    if user is not None:
        return user

    is_htmx = request.headers.get("HX-Request") == "true"
    next_url = request.url.path
    if is_htmx:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required",
            headers={"HX-Redirect": f"/login?next={next_url}"},
        )
    raise HTTPException(
        status_code=status.HTTP_307_TEMPORARY_REDIRECT,
        detail="Login required",
        headers={"Location": f"/login?next={next_url}"},
    )


def require_super_admin(
    current_user: CurrentUser = Depends(get_current_user),
) -> CurrentUser:
    if not current_user.is_super_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Super-admin only",
        )
    return current_user
