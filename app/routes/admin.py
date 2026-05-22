"""Sezione amministrativa: solo super-admin.

Gestisce CRUD di tenant e utenti via interfaccia web. Tutte le route sono
protette dalla dependency `require_super_admin` che verifica:
- l'utente è loggato (cookie session valido)
- l'utente ha role='super_admin'

Vedi SETUP_CLOUD_DB_TENANT.md sezione "Sezione admin frontend".
"""
from __future__ import annotations

import logging
import re

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import RedirectResponse

from .. import db_cloud
from ..auth import CurrentUser, hash_password, require_super_admin
from ..templates import templates


log = logging.getLogger(__name__)
router = APIRouter(prefix="/admin", dependencies=[Depends(require_super_admin)])


_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _slugify(text: str) -> str:
    slug = _SLUG_RE.sub("-", text.strip().lower()).strip("-")
    return slug or "tenant"


def _flash(request: Request, level: str, message: str) -> None:
    """Mini-sistema di flash messages via session (single-shot)."""
    request.session.setdefault("flash", []).append({"level": level, "message": message})


def _pop_flashes(request: Request) -> list[dict]:
    flashes = request.session.pop("flash", []) if hasattr(request, "session") else []
    return flashes if isinstance(flashes, list) else []


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------

@router.get("")
@router.get("/")
def dashboard(request: Request, current_user: CurrentUser = Depends(require_super_admin)):
    tenants = db_cloud.list_tenants()
    users = db_cloud.list_users()
    n_super = sum(1 for u in users if u["role"] == "super_admin")
    return templates.TemplateResponse(
        request,
        "admin/dashboard.html",
        {
            "current_user": current_user,
            "tenants_count": len(tenants),
            "users_count": len(users),
            "super_admins_count": n_super,
            "flashes": _pop_flashes(request),
        },
    )


# ---------------------------------------------------------------------------
# Tenants
# ---------------------------------------------------------------------------

@router.get("/tenants")
def tenants_list(request: Request, current_user: CurrentUser = Depends(require_super_admin)):
    tenants = db_cloud.list_tenants()
    users = db_cloud.list_users()
    # Conteggio utenti per tenant
    counts: dict[int, int] = {}
    for u in users:
        if u.get("tenant_id"):
            counts[u["tenant_id"]] = counts.get(u["tenant_id"], 0) + 1
    for t in tenants:
        t["users_count"] = counts.get(t["id"], 0)
    return templates.TemplateResponse(
        request,
        "admin/tenants.html",
        {
            "current_user": current_user,
            "tenants": tenants,
            "flashes": _pop_flashes(request),
        },
    )


@router.post("/tenants")
def tenants_create(
    request: Request,
    name: str = Form(...),
    slug: str = Form(""),
    current_user: CurrentUser = Depends(require_super_admin),
):
    name = name.strip()
    if not name:
        _flash(request, "error", "Nome tenant obbligatorio.")
        return RedirectResponse(url="/admin/tenants", status_code=303)
    final_slug = _slugify(slug or name)
    try:
        if db_cloud.get_tenant_by_slug(final_slug):
            _flash(request, "error", f"Slug '{final_slug}' già usato.")
            return RedirectResponse(url="/admin/tenants", status_code=303)
        tenant_id = db_cloud.create_tenant(name, final_slug)
        _flash(request, "success", f"Tenant '{name}' creato (id={tenant_id}, slug={final_slug}).")
    except Exception as exc:
        log.error("Errore creazione tenant: %s", exc)
        _flash(request, "error", f"Errore creazione tenant: {exc}")
    return RedirectResponse(url="/admin/tenants", status_code=303)


@router.post("/tenants/{tenant_id}/toggle")
def tenants_toggle(
    request: Request,
    tenant_id: int,
    current_user: CurrentUser = Depends(require_super_admin),
):
    t = db_cloud.get_tenant(tenant_id)
    if not t:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tenant non trovato")
    new_state = not bool(t["is_active"])
    db_cloud.update_tenant(tenant_id, is_active=new_state)
    _flash(request, "success", f"Tenant '{t['name']}' ora {'attivo' if new_state else 'disattivato'}.")
    return RedirectResponse(url="/admin/tenants", status_code=303)


@router.post("/tenants/{tenant_id}/toggle-site-memory")
def tenants_toggle_site_memory(
    request: Request,
    tenant_id: int,
    current_user: CurrentUser = Depends(require_super_admin),
):
    """Toggle del flag `site_memory_shared`: se ON il tenant accede a tutta la
    memoria del sito (cross-tenant), se OFF vede solo le righe taggate con il
    suo tenant_id. Le scritture restano sempre taggate con il tenant corrente.
    """
    t = db_cloud.get_tenant(tenant_id)
    if not t:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tenant non trovato")
    new_state = not bool(t.get("site_memory_shared"))
    db_cloud.update_tenant(tenant_id, site_memory_shared=new_state)
    msg = "accede alla memoria condivisa" if new_state else "torna a memoria isolata"
    _flash(request, "success", f"Tenant '{t['name']}' ora {msg}.")
    return RedirectResponse(url="/admin/tenants", status_code=303)


@router.post("/tenants/{tenant_id}/delete")
def tenants_delete(
    request: Request,
    tenant_id: int,
    current_user: CurrentUser = Depends(require_super_admin),
):
    t = db_cloud.get_tenant(tenant_id)
    if not t:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tenant non trovato")
    db_cloud.delete_tenant(tenant_id)
    _flash(request, "success", f"Tenant '{t['name']}' eliminato (utenti collegati eliminati a cascata).")
    return RedirectResponse(url="/admin/tenants", status_code=303)


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------

@router.get("/users")
def users_list(request: Request, current_user: CurrentUser = Depends(require_super_admin)):
    users = db_cloud.list_users()
    tenants = db_cloud.list_tenants()
    return templates.TemplateResponse(
        request,
        "admin/users.html",
        {
            "current_user": current_user,
            "users": users,
            "tenants": tenants,
            "flashes": _pop_flashes(request),
        },
    )


@router.post("/users")
def users_create(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    role: str = Form("tenant_user"),
    tenant_id: str = Form(""),
    first_name: str = Form(""),
    last_name: str = Form(""),
    current_user: CurrentUser = Depends(require_super_admin),
):
    email = email.strip().lower()
    password = password.strip()
    if not email or not password:
        _flash(request, "error", "Email/username e password obbligatori.")
        return RedirectResponse(url="/admin/users", status_code=303)
    if len(password) < 6:
        _flash(request, "error", "Password troppo corta (min 6 caratteri).")
        return RedirectResponse(url="/admin/users", status_code=303)
    if role not in ("super_admin", "tenant_user"):
        _flash(request, "error", f"Ruolo non valido: {role}")
        return RedirectResponse(url="/admin/users", status_code=303)

    parsed_tenant_id: int | None = None
    if role == "tenant_user":
        if not tenant_id or not tenant_id.isdigit():
            _flash(request, "error", "Per un tenant_user devi selezionare un tenant.")
            return RedirectResponse(url="/admin/users", status_code=303)
        parsed_tenant_id = int(tenant_id)
        if not db_cloud.get_tenant(parsed_tenant_id):
            _flash(request, "error", "Tenant selezionato non esiste.")
            return RedirectResponse(url="/admin/users", status_code=303)

    if db_cloud.get_user_by_email(email):
        _flash(request, "error", f"Esiste già un utente con email/username '{email}'.")
        return RedirectResponse(url="/admin/users", status_code=303)

    try:
        new_id = db_cloud.create_user(
            tenant_id=parsed_tenant_id,
            email=email,
            password_hash=hash_password(password),
            role=role,
            first_name=first_name,
            last_name=last_name,
        )
        _flash(request, "success", f"Utente '{email}' creato (id={new_id}, ruolo={role}).")
    except Exception as exc:
        log.error("Errore creazione utente: %s", exc)
        _flash(request, "error", f"Errore: {exc}")
    return RedirectResponse(url="/admin/users", status_code=303)


@router.post("/users/{user_id}/edit")
def users_edit(
    request: Request,
    user_id: int,
    first_name: str = Form(""),
    last_name: str = Form(""),
    current_user: CurrentUser = Depends(require_super_admin),
):
    """Aggiorna first_name/last_name di un utente esistente."""
    u = db_cloud.get_user(user_id)
    if not u:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Utente non trovato")
    db_cloud.update_user(
        user_id,
        first_name=first_name,
        last_name=last_name,
    )
    _flash(request, "success", f"Anagrafica utente '{u['email']}' aggiornata.")
    return RedirectResponse(url="/admin/users", status_code=303)


@router.post("/users/{user_id}/toggle")
def users_toggle(
    request: Request,
    user_id: int,
    current_user: CurrentUser = Depends(require_super_admin),
):
    if user_id == current_user.id:
        _flash(request, "error", "Non puoi disattivare il tuo stesso utente.")
        return RedirectResponse(url="/admin/users", status_code=303)
    u = db_cloud.get_user(user_id)
    if not u:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Utente non trovato")
    new_state = not bool(u["is_active"])
    db_cloud.update_user(user_id, is_active=new_state)
    _flash(request, "success", f"Utente '{u['email']}' ora {'attivo' if new_state else 'disattivato'}.")
    return RedirectResponse(url="/admin/users", status_code=303)


@router.post("/users/{user_id}/reset-password")
def users_reset_password(
    request: Request,
    user_id: int,
    new_password: str = Form(...),
    current_user: CurrentUser = Depends(require_super_admin),
):
    new_password = new_password.strip()
    if len(new_password) < 6:
        _flash(request, "error", "Password troppo corta (min 6 caratteri).")
        return RedirectResponse(url="/admin/users", status_code=303)
    u = db_cloud.get_user(user_id)
    if not u:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Utente non trovato")
    db_cloud.update_user(user_id, password_hash=hash_password(new_password))
    _flash(request, "success", f"Password di '{u['email']}' aggiornata.")
    return RedirectResponse(url="/admin/users", status_code=303)


@router.post("/users/{user_id}/delete")
def users_delete(
    request: Request,
    user_id: int,
    current_user: CurrentUser = Depends(require_super_admin),
):
    if user_id == current_user.id:
        _flash(request, "error", "Non puoi eliminare il tuo stesso utente.")
        return RedirectResponse(url="/admin/users", status_code=303)
    u = db_cloud.get_user(user_id)
    if not u:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Utente non trovato")
    db_cloud.delete_user(user_id)
    _flash(request, "success", f"Utente '{u['email']}' eliminato.")
    return RedirectResponse(url="/admin/users", status_code=303)
