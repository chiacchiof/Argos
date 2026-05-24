"""Route per gestione email accounts (SMTP/IMAP multi-account).

Endpoint:
- GET  /accounts/email                       — lista
- GET  /accounts/email/new                   — form add
- POST /accounts/email/new                   — insert (cifra SMTP/IMAP password)
- GET  /accounts/email/{id}/edit             — form edit
- POST /accounts/email/{id}/edit             — update (preserve-on-empty per pwd)
- POST /accounts/email/{id}/delete           — delete
- POST /accounts/email/{id}/test             — smoke test SMTP (mittente → mittente)
- POST /accounts/email/{id}/toggle-status    — active <-> quarantine

Sicurezza:
- Password SMTP/IMAP cifrate Fernet via `crypto_creds.encrypt` prima del save.
- Visibilità multi-tenant: tenant_user vede default "i miei", super_admin vede "tenant".
"""
from __future__ import annotations

import logging
import secrets

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from .. import db, db_cloud
from ..agent.social.crypto_creds import decrypt, encrypt, is_configured
from ..auth import require_architect_or_admin
from ..templates import templates

router = APIRouter(dependencies=[Depends(require_architect_or_admin)])
log = logging.getLogger(__name__)


@router.get("/accounts/email", response_class=HTMLResponse)
async def email_accounts_list(request: Request, author: str = "", flash: str = "", error: str = ""):
    current_user = getattr(request.state, "current_user", None)
    is_super_admin = bool(current_user and current_user.is_super_admin)
    current_uid = db.current_user_id()
    default_author = "tenant" if is_super_admin else "mine"
    author_norm = (author or default_author).strip().lower()
    if author_norm not in ("mine", "tenant"):
        author_norm = default_author

    filter_uid = current_uid if (author_norm == "mine" and current_uid is not None) else None
    accounts = db.list_email_accounts(created_by_user_id=filter_uid)
    total_tenant = (
        len(db.list_email_accounts()) if author_norm == "mine" else len(accounts)
    )

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
        "accounts_email.html",
        {
            "accounts": accounts,
            "is_secret_configured": is_configured(),
            "author_filter": author_norm,
            "total_tenant": total_tenant,
            "current_user_authenticated": current_uid is not None,
            "current_user_id": current_uid,
            "tenant_users": tenant_users,
            "flash": flash,
            "error": error,
        },
    )


@router.get("/accounts/email/new", response_class=HTMLResponse)
async def email_account_new_form(request: Request):
    current_user = getattr(request.state, "current_user", None)
    is_super_admin = bool(current_user and current_user.is_super_admin)

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
        "accounts_email_edit.html",
        {
            "account": None,  # None = new
            "is_secret_configured": is_configured(),
            "tenant_users": tenant_users,
        },
    )


@router.post("/accounts/email/new")
async def email_account_create(
    request: Request,
    label: str = Form(...),
    from_address: str = Form(...),
    reply_to: str = Form(""),
    smtp_host: str = Form(...),
    smtp_port: int = Form(587),
    smtp_user: str = Form(...),
    smtp_password: str = Form(...),
    smtp_use_tls: str = Form("1"),
    imap_host: str = Form(""),
    imap_port: str = Form("993"),
    imap_user: str = Form(""),
    imap_password: str = Form(""),
    imap_folder: str = Form("INBOX"),
    daily_send_cap: int = Form(200),
    rate_limit_per_minute: int = Form(10),
    notes: str = Form(""),
    owner_user_id: str = Form(""),
):
    if not is_configured():
        raise HTTPException(
            status_code=400,
            detail=(
                "ARGOS_SECRET non impostata in .env. Aggiungi: "
                "ARGOS_SECRET=<stringa-segreta-30+-caratteri>"
            ),
        )

    label = label.strip()
    from_address = from_address.strip().lower()
    smtp_host = smtp_host.strip()
    smtp_user = smtp_user.strip()
    if not (label and from_address and smtp_host and smtp_user and smtp_password):
        raise HTTPException(status_code=400, detail="campi obbligatori mancanti")
    if "@" not in from_address:
        raise HTTPException(status_code=400, detail="from_address non valido")

    # Owner: tenant_user → self, super_admin → scelta o self
    current_user = getattr(request.state, "current_user", None)
    is_super_admin = bool(current_user and current_user.is_super_admin)
    current_uid = db.current_user_id()
    raw_owner = (owner_user_id or "").strip()
    target_owner_id = int(raw_owner) if raw_owner.isdigit() else None
    if not is_super_admin:
        target_owner_id = current_uid
    elif target_owner_id is None:
        target_owner_id = current_uid

    encrypted_smtp = encrypt(smtp_password)
    encrypted_imap = encrypt(imap_password) if imap_password else None
    imap_port_int = int(imap_port) if (imap_port or "").strip().isdigit() else None

    uuid = f"email-{secrets.token_hex(8)}"
    try:
        account_id = db.insert_email_account(
            {
                "uuid": uuid,
                "label": label,
                "from_address": from_address,
                "reply_to": (reply_to.strip() or None),
                "smtp_host": smtp_host,
                "smtp_port": int(smtp_port),
                "smtp_user": smtp_user,
                "encrypted_smtp_password": encrypted_smtp,
                "smtp_use_tls": str(smtp_use_tls).strip() in ("1", "true", "on", "yes"),
                "imap_host": (imap_host.strip() or None),
                "imap_port": imap_port_int,
                "imap_user": (imap_user.strip() or None),
                "encrypted_imap_password": encrypted_imap,
                "imap_folder": (imap_folder.strip() or "INBOX"),
                "status": "active",
                "daily_send_cap": max(1, int(daily_send_cap)),
                "rate_limit_per_minute": max(1, int(rate_limit_per_minute)),
                "notes": (notes.strip() or None),
            },
            created_by_user_id=target_owner_id,
        )
        log.info(
            "email account created: id=%s from=%s owner=%s",
            account_id, from_address, target_owner_id,
        )
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"errore DB (forse from_address gia' esistente): {e}")
    return RedirectResponse(
        url=f"/accounts/email?flash=Account+%23{account_id}+creato",
        status_code=303,
    )


@router.get("/accounts/email/{account_id}/edit", response_class=HTMLResponse)
async def email_account_edit_form(request: Request, account_id: int):
    a = db.get_email_account(account_id)
    if not a:
        raise HTTPException(status_code=404, detail="account non trovato")
    return templates.TemplateResponse(
        request,
        "accounts_email_edit.html",
        {
            "account": a,
            "is_secret_configured": is_configured(),
            "tenant_users": [],
        },
    )


@router.post("/accounts/email/{account_id}/edit")
async def email_account_update(
    request: Request,
    account_id: int,
    label: str = Form(...),
    from_address: str = Form(...),
    reply_to: str = Form(""),
    smtp_host: str = Form(...),
    smtp_port: int = Form(587),
    smtp_user: str = Form(...),
    smtp_password: str = Form(""),
    smtp_use_tls: str = Form("1"),
    imap_host: str = Form(""),
    imap_port: str = Form("993"),
    imap_user: str = Form(""),
    imap_password: str = Form(""),
    imap_folder: str = Form("INBOX"),
    daily_send_cap: int = Form(200),
    rate_limit_per_minute: int = Form(10),
    status: str = Form("active"),
    notes: str = Form(""),
):
    existing = db.get_email_account(account_id)
    if not existing:
        raise HTTPException(status_code=404, detail="account non trovato")

    status = status.strip().lower()
    if status not in ("active", "quarantine", "banned"):
        raise HTTPException(status_code=400, detail=f"status '{status}' non valido")
    imap_port_int = int(imap_port) if (imap_port or "").strip().isdigit() else None

    fields: dict[str, object] = {
        "label": label.strip(),
        "from_address": from_address.strip().lower(),
        "reply_to": (reply_to.strip() or None),
        "smtp_host": smtp_host.strip(),
        "smtp_port": int(smtp_port),
        "smtp_user": smtp_user.strip(),
        "smtp_use_tls": 1 if str(smtp_use_tls).strip() in ("1", "true", "on", "yes") else 0,
        "imap_host": (imap_host.strip() or None),
        "imap_port": imap_port_int,
        "imap_user": (imap_user.strip() or None),
        "imap_folder": (imap_folder.strip() or "INBOX"),
        "daily_send_cap": max(1, int(daily_send_cap)),
        "rate_limit_per_minute": max(1, int(rate_limit_per_minute)),
        "status": status,
        "notes": (notes.strip() or None),
    }

    # Preserve-on-empty per le password: vuoto = nessun cambio.
    smtp_pwd = (smtp_password or "").strip()
    if smtp_pwd:
        if not is_configured():
            raise HTTPException(
                status_code=400,
                detail="ARGOS_SECRET non impostata: impossibile cifrare la nuova password.",
            )
        fields["encrypted_smtp_password"] = encrypt(smtp_pwd)
    imap_pwd = (imap_password or "").strip()
    if imap_pwd:
        if imap_pwd.upper() == "CLEAR":
            fields["encrypted_imap_password"] = None
        else:
            if not is_configured():
                raise HTTPException(
                    status_code=400,
                    detail="ARGOS_SECRET non impostata.",
                )
            fields["encrypted_imap_password"] = encrypt(imap_pwd)

    db.update_email_account(account_id, **fields)
    log.info("email account updated: id=%s status=%s pwd_changed=%s", account_id, status, bool(smtp_pwd))
    return RedirectResponse(
        url=f"/accounts/email?flash=Account+%23{account_id}+aggiornato",
        status_code=303,
    )


@router.post("/accounts/email/{account_id}/delete")
async def email_account_delete(account_id: int):
    db.delete_email_account(account_id)
    return RedirectResponse(
        url="/accounts/email?flash=Account+eliminato",
        status_code=303,
    )


@router.post("/accounts/email/{account_id}/toggle-status")
async def email_account_toggle_status(account_id: int):
    a = db.get_email_account(account_id)
    if not a:
        raise HTTPException(status_code=404, detail="account non trovato")
    current = a.get("status") or "active"
    new_status = "quarantine" if current == "active" else "active"
    db.update_email_account(account_id, status=new_status)
    return RedirectResponse(url="/accounts/email", status_code=303)


@router.post("/accounts/email/{account_id}/test")
async def email_account_test(account_id: int):
    """Smoke test SMTP: invia un'email a se stesso (from_address → from_address)
    per verificare host/port/credenziali. Restituisce flash con esito."""
    a = db.get_email_account(account_id)
    if not a:
        raise HTTPException(status_code=404, detail="account non trovato")

    from ..channels import email as ch_email  # lazy import per non rompere se aiosmtplib non c'è

    try:
        pwd = decrypt(a["encrypted_smtp_password"])
        # Sintetizza un account-like dict compatibile con la firma estesa di
        # send_email che arriverà in PR 7. Per ora costruiamo il msg manualmente.
        # Uso direttamente aiosmtplib qui per non aspettare la refactor.
        import asyncio
        import aiosmtplib
        from email.message import EmailMessage

        msg = EmailMessage()
        msg["From"] = a["from_address"]
        msg["To"] = a["from_address"]
        msg["Subject"] = f"Argos · test connessione account #{account_id}"
        msg.set_content(
            f"Test connessione SMTP per account #{account_id} ({a['label']}). "
            "Se ricevi questa email, le credenziali sono corrette."
        )

        async def _send():
            await aiosmtplib.send(
                msg,
                hostname=a["smtp_host"],
                port=int(a["smtp_port"]),
                start_tls=bool(a.get("smtp_use_tls", 1)),
                username=a["smtp_user"],
                password=pwd,
                timeout=20,
            )

        asyncio.run(_send())
        log.info("email account #%s test OK", account_id)
        return RedirectResponse(
            url=f"/accounts/email?flash=Test+OK+su+account+%23{account_id}",
            status_code=303,
        )
    except Exception as e:
        log.warning("email account #%s test FAILED: %s", account_id, e)
        from urllib.parse import quote
        return RedirectResponse(
            url=f"/accounts/email?error=Test+fallito+per+%23{account_id}%3A+{quote(str(e)[:200])}",
            status_code=303,
        )
