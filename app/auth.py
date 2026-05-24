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

    @property
    def is_architect(self) -> bool:
        """Utente tenant con poteri 'architetto': crea task/workflow, gestisce
        asset, configura LLM. UI completa con sidebar."""
        return self.role == "tenant_architect"

    @property
    def is_operator(self) -> bool:
        """Utente tenant con UI semplificata: dashboard agenti, chat drawer.
        Non puo' creare task/workflow ne' accedere a configurazioni."""
        return self.role == "tenant_user"

    @property
    def can_manage_architecture(self) -> bool:
        """Comodita': True se super_admin o tenant_architect (accesso a pagine
        di costruzione: tasks, workflows, asset, settings, llm-keys, ecc.)."""
        return self.is_super_admin or self.is_architect

    @property
    def display_name(self) -> str:
        """Nome user-friendly per saluti UI ('Ciao, Marco'). Usa first_name se
        disponibile, altrimenti deriva dall'email (parte locale capitalizzata)."""
        # first_name e last_name non sono sul dataclass (li teniamo light) — quindi
        # ricaviamo solo dall'email.
        local = (self.email or "").split("@", 1)[0]
        import re as _re
        parts = [p for p in _re.split(r"[._\-+]", local) if p]
        if parts:
            return parts[0].capitalize()
        return "Utente"

    @property
    def initials(self) -> str:
        """Iniziali (max 2 char) derivate dall'email per l'UI compact pill.
        Es. 'francesco.castiglione@x.com' -> 'FC',
            'mario_rossi@x.com'           -> 'MR',
            'chifer81@gmail.com'          -> 'CH'."""
        local = (self.email or "").split("@", 1)[0]
        # Split su separatori comuni
        import re as _re
        parts = [p for p in _re.split(r"[._\-+]", local) if p]
        if len(parts) >= 2:
            return (parts[0][:1] + parts[1][:1]).upper()
        if parts:
            return parts[0][:2].upper()
        return "?"


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


def require_architect_or_admin(
    request: Request,
    current_user: CurrentUser = Depends(get_current_user),
) -> CurrentUser:
    """Gate per pagine "da architetto": creazione task/workflow, configurazione
    asset/llm-keys/social/settings/memoria sito. Bloccato per tenant_user
    (operator). Super-admin sempre OK.

    UX: l'operator che capita su una rotta architect (es. bookmark vecchio,
    URL digitato a mano) viene redirezionato a `/home` invece di vedere un 403
    crudo. Per richieste HTMX usiamo `HX-Redirect` cosi' il client navigates.
    """
    if not current_user.can_manage_architecture:
        is_htmx = request.headers.get("HX-Request") == "true"
        if current_user.is_operator:
            # Redirect gentile a /home per l'operator
            if is_htmx:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Operator non puo' accedere a questa sezione.",
                    headers={"HX-Redirect": "/home"},
                )
            raise HTTPException(
                status_code=status.HTTP_307_TEMPORARY_REDIRECT,
                detail="Operator redirect",
                headers={"Location": "/home"},
            )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "Questa sezione e' riservata agli architetti. La tua utenza ha"
                " accesso solo alla dashboard semplificata."
            ),
        )
    return current_user


def require_operator(
    request: Request,
    current_user: CurrentUser = Depends(get_current_user),
) -> CurrentUser:
    """Gate per pagine specifiche operator (dashboard /home, /agents/, /messages).
    Bloccato per super-admin e architect (loro hanno la UI completa).

    UX: architect/admin che capita su rotta operator viene redirezionato a `/`
    (loro home) invece di vedere un 403 crudo.
    """
    if not current_user.is_operator:
        is_htmx = request.headers.get("HX-Request") == "true"
        if current_user.can_manage_architecture:
            if is_htmx:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Sezione operator-only.",
                    headers={"HX-Redirect": "/"},
                )
            raise HTTPException(
                status_code=status.HTTP_307_TEMPORARY_REDIRECT,
                detail="Architect redirect",
                headers={"Location": "/"},
            )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Sezione riservata agli utenti operator.",
        )
    return current_user
