"""Route per gestione account social (Instagram/TikTok) usati dal runner
outreach_social.

Endpoint:
- GET  /social/accounts                    — lista account + stato
- POST /social/accounts                    — aggiunge nuovo account (cifra password)
- POST /social/accounts/{id}/delete        — cancella account
- POST /social/accounts/{id}/toggle-status — toggle active/quarantine

Sicurezza:
- Le password sono cifrate via Fernet (`app.agent.social.crypto_creds`) prima
  del salvataggio. La master key vive in env `AGENTSCRAPER_SECRET`.
- Le credenziali in chiaro non vengono MAI loggate ne' rimandate al template.
"""
from __future__ import annotations

import logging
import secrets

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from .. import db, db_cloud
from ..agent.social.crypto_creds import encrypt, is_configured
from ..templates import templates

router = APIRouter()
log = logging.getLogger(__name__)


@router.get("/social/accounts", response_class=HTMLResponse)
async def social_accounts_list(request: Request, author: str = ""):
    """Lista social account. `author`:
       - `mine` (default per tenant_user): solo account creati dall'utente
       - `tenant` (default per super_admin): tutti gli account del tenant
    """
    current_user = getattr(request.state, "current_user", None)
    is_super_admin = bool(current_user and current_user.is_super_admin)
    current_uid = db.current_user_id()
    default_author = "tenant" if is_super_admin else "mine"
    author_norm = (author or default_author).strip().lower()
    if author_norm not in ("mine", "tenant", "all"):
        author_norm = default_author

    filter_uid = current_uid if (author_norm == "mine" and current_uid is not None) else None
    accounts = db.list_social_accounts(created_by_user_id=filter_uid)
    # Conteggio "tutti del tenant" per badge sul toggle (anche se author=mine)
    total_tenant = (
        len(db.list_social_accounts()) if author_norm == "mine" else len(accounts)
    )
    # Arricchisci con dms_today (count da log)
    for a in accounts:
        try:
            a["dms_today"] = db.count_social_dms_today(a["id"])
        except Exception:
            a["dms_today"] = 0

    # Lista utenti del tenant per dropdown "owner" nel form Aggiungi.
    # Super-admin senza tenant_id corrente vede tutti (puo' assegnare a chiunque).
    tenant_users: list[dict] = []
    tenant_id_ctx = db.current_tenant_id()
    if tenant_id_ctx is not None:
        try:
            tenant_users = list(db_cloud.list_users(tenant_id=tenant_id_ctx))
        except Exception:
            tenant_users = []
    elif is_super_admin:
        try:
            tenant_users = list(db_cloud.list_users(tenant_id=None))
        except Exception:
            tenant_users = []

    return templates.TemplateResponse(
        request,
        "social_accounts.html",
        {
            "accounts": accounts,
            "is_secret_configured": is_configured(),
            "author_filter": author_norm,
            "total_tenant": total_tenant,
            "current_user_authenticated": current_uid is not None,
            "current_user_id": current_uid,
            "tenant_users": tenant_users,
        },
    )


@router.post("/social/accounts")
async def create_social_account(
    request: Request,
    platform: str = Form(...),
    username: str = Form(...),
    password: str = Form(...),
    daily_dm_cap: int = Form(10),
    proxy_label: str = Form(""),
    notes: str = Form(""),
    owner_user_id: str = Form(""),
):
    """Aggiunge un nuovo account social. La password viene cifrata in Fernet.

    `owner_user_id`: id dell'utente del tenant che possiede l'account.
    Se vuoto, default = current_user (audit dell'azione).
    Tenant_user puo' assegnare solo a se stesso (silently override).
    Super_admin puo' assegnare a qualunque utente del tenant target.
    """
    if not is_configured():
        raise HTTPException(
            status_code=400,
            detail=(
                "AGENTSCRAPER_SECRET non impostata in .env. Le credenziali non "
                "possono essere cifrate. Aggiungi al file .env: "
                "AGENTSCRAPER_SECRET=<stringa-segreta-30+-caratteri>"
            ),
        )
    platform = platform.strip().lower()
    if platform not in ("instagram", "tiktok", "facebook"):
        raise HTTPException(status_code=400, detail="platform non supportata")
    username = username.strip().lstrip("@")
    if not username or not password:
        raise HTTPException(status_code=400, detail="username e password obbligatori")

    # Owner: tenant_user puo' settare solo se stesso; super_admin puo' altri
    current_user = getattr(request.state, "current_user", None)
    is_super_admin = bool(current_user and current_user.is_super_admin)
    current_uid = db.current_user_id()
    target_owner_id: int | None = None
    raw_owner = (owner_user_id or "").strip()
    if raw_owner.isdigit():
        target_owner_id = int(raw_owner)
    if not is_super_admin:
        # Tenant_user: ignora il form, force = current_uid
        target_owner_id = current_uid
    elif target_owner_id is None:
        target_owner_id = current_uid  # super_admin senza scelta → default self

    encrypted = encrypt(password)
    uuid = f"{platform}-{secrets.token_hex(8)}"
    try:
        account_id = db.create_social_account(
            {
                "uuid": uuid,
                "platform": platform,
                "username": username,
                "encrypted_password": encrypted,
                "proxy_label": (proxy_label.strip() or None),
                "daily_dm_cap": max(1, min(daily_dm_cap, 50)),
                "status": "warming_up",
                "notes": notes.strip() or None,
            },
            created_by_user_id=target_owner_id,
        )
        log.info(
            "social account created: id=%s platform=%s username=%s owner=%s",
            account_id, platform, username, target_owner_id,
        )
    except Exception as e:
        # Probabile UNIQUE constraint (platform, username)
        raise HTTPException(status_code=400, detail=f"Account gia' esistente o errore DB: {e}")
    return RedirectResponse(url="/social/accounts", status_code=303)


@router.post("/social/accounts/{account_id}/delete")
async def delete_social_account(account_id: int):
    db.delete_social_account(account_id)
    return RedirectResponse(url="/social/accounts", status_code=303)


@router.post("/social/accounts/{account_id}/toggle-status")
async def toggle_social_account_status(account_id: int):
    a = db.get_social_account(account_id)
    if not a:
        raise HTTPException(status_code=404, detail="account non trovato")
    current = a.get("status") or "active"
    # active <-> quarantine. banned resta banned (manuale).
    new_status = "quarantine" if current == "active" else "active"
    db.update_social_account(account_id, status=new_status)
    return RedirectResponse(url="/social/accounts", status_code=303)


@router.get("/social/accounts/{account_id}/edit", response_class=HTMLResponse)
async def edit_social_account_form(request: Request, account_id: int):
    """Form di modifica metadati account social (username, password,
    daily_dm_cap, proxy_label, notes, status). La password ha preserve-on-empty:
    se l'utente lascia il campo vuoto, la password salvata resta com'era."""
    a = db.get_social_account(account_id)
    if not a:
        raise HTTPException(status_code=404, detail="account non trovato")
    return templates.TemplateResponse(
        request,
        "social_account_edit.html",
        {"account": a, "is_secret_configured": is_configured()},
    )


@router.post("/social/accounts/{account_id}/edit")
async def update_social_account(
    request: Request,
    account_id: int,
    username: str = Form(...),
    password: str = Form(""),
    daily_dm_cap: int = Form(10),
    proxy_label: str = Form(""),
    notes: str = Form(""),
    status: str = Form("active"),
):
    """Salva modifiche. Pattern preserve-on-empty per la password:
    - se `password` è vuoto → non tocchiamo `encrypted_password` esistente
    - se `password` è "CLEAR" (sentinel) → azzeriamo (account non potrà più loggarsi)
    - altrimenti → cifriamo e sostituiamo
    """
    existing = db.get_social_account(account_id)
    if not existing:
        raise HTTPException(status_code=404, detail="account non trovato")

    username = username.strip().lstrip("@")
    if not username:
        raise HTTPException(status_code=400, detail="username obbligatorio")
    status = status.strip().lower()
    if status not in ("active", "warming_up", "quarantine", "banned"):
        raise HTTPException(status_code=400, detail=f"status '{status}' non valido")

    fields: dict[str, object] = {
        "username": username,
        "daily_dm_cap": max(1, min(daily_dm_cap, 500)),
        "proxy_label": (proxy_label.strip() or None),
        "notes": (notes.strip() or None),
        "status": status,
    }

    pwd = (password or "").strip()
    if pwd:
        if pwd.upper() == "CLEAR":
            fields["encrypted_password"] = None
        else:
            if not is_configured():
                raise HTTPException(
                    status_code=400,
                    detail=(
                        "AGENTSCRAPER_SECRET non impostata: impossibile cifrare la "
                        "nuova password. Lascia il campo vuoto per preservare la "
                        "password esistente."
                    ),
                )
            fields["encrypted_password"] = encrypt(pwd)
    # se pwd vuoto → niente da fare (preserve)

    db.update_social_account(account_id, **fields)
    log.info(
        "social account updated: id=%s username=%s status=%s pwd_changed=%s",
        account_id, username, status, bool(pwd),
    )
    return RedirectResponse(url="/social/accounts", status_code=303)
