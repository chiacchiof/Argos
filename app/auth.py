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


_OPERATOR_AGENT_EDIT_PATH_RE = __import__("re").compile(
    r"^/(tasks|workflows)/(\d+)"
    r"(?:"
    r"|/edit|/jobs|/runs"                       # view, edit form, lista run/job
    r"|/append_asset_id|/remove_asset_id|/clear_audience"  # audience asset editing
    r"|/append_qualified_set|/append_assets_set"  # audience bulk editing (qualified|assets)
    r"|/promote_legacy_contacts"                # migra contact_ids -> asset_ids
    r"|/edges|/nodes/\d+/replace|/nodes/\d+/delete"  # workflow graph editing
    r")?/?$"
)

# Route ANCILLARY usate da task_form.html / workflow_form.html via HTMX al
# caricamento o sulla compilazione dei campi. Tutte read-only. Devono essere
# raggiungibili anche dall'operator quando edita un agente pubblicato, altrimenti
# l'HTMX `hx-trigger="load"` riceve 307 → HX-Redirect → l'utente viene sbattuto
# fuori dalla pagina edit appena caricata. Operazioni di lettura senza side-effects.
_OPERATOR_EDIT_SUPPORT_PATHS = frozenset({
    "/templates/extraction",
    "/artifacts/jsonl",
    "/providers/model-field",
    "/providers/credential-field",
    "/assets/search",
    "/assets/tag_values",
    "/assets/tag_keys",
    "/api/models",
})


def _operator_can_pass_for_published_agent(request: Request) -> bool:
    """Per route /tasks/{id}, /tasks/{id}/edit, /tasks/{id}/jobs (e analoghi
    workflows), l'operator passa se il task/workflow e' `is_published_agent`.

    Allarga anche alle route ANCILLARY del form edit (lista modelli, credenziali,
    template estrazione, ecc.) che HTMX chiama al caricamento del form.

    Estrae l'id direttamente dal path con regex perche' `request.path_params`
    NON e' garantito essere popolato quando una dep e' applicata a livello router
    (timing della dependency injection rispetto al path matching di Starlette).
    """
    from . import db  # late import per evitare circolare
    path = request.url.path
    # Ancillary read-only paths: passa sempre (l'operator gia' arrivato qui da
    # un edit di agente pubblicato; queste route popolano dropdown e schema).
    if path in _OPERATOR_EDIT_SUPPORT_PATHS:
        return True
    m = _OPERATOR_AGENT_EDIT_PATH_RE.match(path)
    if not m:
        return False
    kind, obj_id_str = m.group(1), m.group(2)
    try:
        obj_id = int(obj_id_str)
        if kind == "tasks":
            t = db.get_task(obj_id)
            return bool(t and t.get("is_published_agent"))
        elif kind == "workflows":
            w = db.get_workflow(obj_id)
            return bool(w and w.get("is_published_agent"))
    except Exception:
        return False
    return False


def require_architect_or_admin(
    request: Request,
    current_user: CurrentUser = Depends(get_current_user),
) -> CurrentUser:
    """Gate per pagine "da architetto": creazione task/workflow, configurazione
    asset/llm-keys/social/settings/memoria sito. Bloccato per tenant_user
    (operator). Super-admin sempre OK.

    Eccezione operator: l'operator puo' VEDERE e MODIFICARE i task/workflow
    `is_published_agent=TRUE` (riusa la "ricetta" su target diversi). Tutto il
    resto delle pagine architect (list /, /tasks/new, delete, clone, publish,
    settings, ecc.) resta bloccato e lo redireziona a /home.

    UX: l'operator che capita su una rotta architect non-permessa (es. bookmark
    vecchio, URL digitato a mano) viene redirezionato a `/home` invece di vedere
    un 403 crudo. Per richieste HTMX usiamo `HX-Redirect`.
    """
    if current_user.can_manage_architecture:
        return current_user
    # Operator: permetti edit/view/jobs di agenti pubblicati.
    if current_user.is_operator and _operator_can_pass_for_published_agent(request):
        return current_user
    is_htmx = request.headers.get("HX-Request") == "true"
    if current_user.is_operator:
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


def can_edit_published_agent(user: "CurrentUser", task_or_workflow: dict) -> bool:
    """Verifica se `user` puo' modificare un task o workflow.

    Regole:
    - super_admin: sempre OK
    - tenant_architect: sempre OK (gestione architetturale completa)
    - tenant_user (operator): OK SOLO se `is_published_agent=TRUE`
      e same-tenant. Per agenti non pubblicati non vede nemmeno l'esistenza.
    - anonymous: NO

    Usato dalle route edit/save di tasks e workflows per consentire all'operator
    di riconfigurare agenti pubblicati riutilizzandoli su target diversi.
    """
    if not user or not task_or_workflow:
        return False
    if user.is_super_admin:
        return True
    if user.is_architect:
        return True
    if user.is_operator:
        # Tenant_user puo' editare SOLO agenti pubblicati. Same-tenant gia'
        # garantito dal middleware (`db.list_*` filtra per tenant_id).
        return bool(task_or_workflow.get("is_published_agent"))
    return False


def require_can_edit_agent_or_redirect(
    request: Request,
    task_or_workflow: dict,
    current_user: "CurrentUser",
) -> None:
    """Solleva HTTPException 403/redirect se l'utente non puo' editare l'agente.

    Per architect/super_admin: ok.
    Per operator: ok solo se `is_published_agent=TRUE`. Altrimenti redirect a /home.
    """
    if can_edit_published_agent(current_user, task_or_workflow):
        return
    is_htmx = request.headers.get("HX-Request") == "true"
    if current_user.is_operator:
        if is_htmx:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Questo agente non e' modificabile (non pubblicato).",
                headers={"HX-Redirect": "/home"},
            )
        raise HTTPException(
            status_code=status.HTTP_307_TEMPORARY_REDIRECT,
            detail="Agente non modificabile",
            headers={"Location": "/home"},
        )
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="Non autorizzato a modificare questo task/workflow.",
    )


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
