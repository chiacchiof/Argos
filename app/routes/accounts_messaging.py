"""Route per gestione account messaging (WhatsApp browser + WA Cloud API +
Telegram bots), organizzati in un hub `/accounts/messaging` con tabs.

In PR 4 questo file contiene SOLO la parte Telegram (greenfield) + l'hub
con placeholder che linkano a /settings/whatsapp per le tab WA. In PR 5 il
codice di settings_whatsapp.py sarà migrato qui e i path WhatsApp diventeranno
/accounts/messaging/whatsapp/* (con redirect 301 dai vecchi /settings/whatsapp).

Endpoint:
- GET  /accounts/messaging[?tab=browser|api|telegram]   — hub
- POST /accounts/messaging/telegram/new                 — insert bot
- GET  /accounts/messaging/telegram/{id}/edit
- POST /accounts/messaging/telegram/{id}/edit
- POST /accounts/messaging/telegram/{id}/delete
- POST /accounts/messaging/telegram/{id}/test           — chiama /getMe
- POST /accounts/messaging/telegram/{id}/toggle-status
"""
from __future__ import annotations

import logging
import secrets

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from .. import db, db_cloud
from ..agent.social.crypto_creds import decrypt, encrypt, is_configured
from ..templates import templates

router = APIRouter()
log = logging.getLogger(__name__)


@router.get("/accounts/messaging", response_class=HTMLResponse)
async def messaging_hub(
    request: Request,
    tab: str = "telegram",
    author: str = "",
    flash: str = "",
    error: str = "",
):
    """Hub messaging con tabs server-side (`?tab=browser|api|telegram`).
    Default = telegram (l'unica tab completamente implementata in PR 4)."""
    current_user = getattr(request.state, "current_user", None)
    is_super_admin = bool(current_user and current_user.is_super_admin)
    current_uid = db.current_user_id()
    default_author = "tenant" if is_super_admin else "mine"
    author_norm = (author or default_author).strip().lower()
    if author_norm not in ("mine", "tenant"):
        author_norm = default_author

    tab = tab.strip().lower()
    if tab not in ("browser", "api", "telegram"):
        tab = "telegram"

    filter_uid = current_uid if (author_norm == "mine" and current_uid is not None) else None

    # Per ora solo i Telegram bots sono live. Le tab WA sono placeholder.
    telegram_bots = db.list_telegram_bots(created_by_user_id=filter_uid)
    total_tenant_telegram = (
        len(db.list_telegram_bots()) if author_norm == "mine" else len(telegram_bots)
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
        "accounts_messaging.html",
        {
            "tab": tab,
            "telegram_bots": telegram_bots,
            "total_tenant_telegram": total_tenant_telegram,
            "is_secret_configured": is_configured(),
            "author_filter": author_norm,
            "current_user_authenticated": current_uid is not None,
            "current_user_id": current_uid,
            "tenant_users": tenant_users,
            "flash": flash,
            "error": error,
        },
    )


# ---------------------------------------------------------------------------
# Telegram bots
# ---------------------------------------------------------------------------

@router.get("/accounts/messaging/telegram/new", response_class=HTMLResponse)
async def telegram_bot_new_form(request: Request):
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
        "accounts_telegram_edit.html",
        {"bot": None, "is_secret_configured": is_configured(), "tenant_users": tenant_users},
    )


@router.post("/accounts/messaging/telegram/new")
async def telegram_bot_create(
    request: Request,
    label: str = Form(...),
    bot_token: str = Form(...),
    bot_username: str = Form(""),
    daily_msg_cap: int = Form(500),
    poll_interval_seconds: int = Form(30),
    notes: str = Form(""),
    owner_user_id: str = Form(""),
):
    if not is_configured():
        raise HTTPException(
            status_code=400,
            detail="ARGOS_SECRET non impostata. Aggiungi a .env per cifrare il bot token.",
        )

    label = label.strip()
    bot_token = bot_token.strip()
    bot_username = bot_username.strip().lstrip("@").lower() or None
    if not (label and bot_token):
        raise HTTPException(status_code=400, detail="label e bot_token obbligatori")

    current_user = getattr(request.state, "current_user", None)
    is_super_admin = bool(current_user and current_user.is_super_admin)
    current_uid = db.current_user_id()
    raw_owner = (owner_user_id or "").strip()
    target_owner_id = int(raw_owner) if raw_owner.isdigit() else None
    if not is_super_admin:
        target_owner_id = current_uid
    elif target_owner_id is None:
        target_owner_id = current_uid

    encrypted_token = encrypt(bot_token)
    uuid = f"tg-{secrets.token_hex(8)}"
    try:
        bot_id = db.insert_telegram_bot(
            {
                "uuid": uuid,
                "label": label,
                "bot_username": bot_username,
                "encrypted_bot_token": encrypted_token,
                "status": "active",
                "daily_msg_cap": max(1, int(daily_msg_cap)),
                "poll_interval_seconds": max(5, int(poll_interval_seconds)),
                "notes": (notes.strip() or None),
            },
            created_by_user_id=target_owner_id,
        )
        log.info("telegram bot created: id=%s label=%s owner=%s", bot_id, label, target_owner_id)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"errore DB (forse bot_username gia' esistente): {e}")
    return RedirectResponse(
        url=f"/accounts/messaging?tab=telegram&flash=Bot+%23{bot_id}+creato",
        status_code=303,
    )


@router.get("/accounts/messaging/telegram/{bot_id}/edit", response_class=HTMLResponse)
async def telegram_bot_edit_form(request: Request, bot_id: int):
    b = db.get_telegram_bot(bot_id)
    if not b:
        raise HTTPException(status_code=404, detail="bot non trovato")
    return templates.TemplateResponse(
        request,
        "accounts_telegram_edit.html",
        {"bot": b, "is_secret_configured": is_configured(), "tenant_users": []},
    )


@router.post("/accounts/messaging/telegram/{bot_id}/edit")
async def telegram_bot_update(
    request: Request,
    bot_id: int,
    label: str = Form(...),
    bot_token: str = Form(""),
    bot_username: str = Form(""),
    daily_msg_cap: int = Form(500),
    poll_interval_seconds: int = Form(30),
    status: str = Form("active"),
    notes: str = Form(""),
):
    existing = db.get_telegram_bot(bot_id)
    if not existing:
        raise HTTPException(status_code=404, detail="bot non trovato")

    status = status.strip().lower()
    if status not in ("active", "quarantine", "banned"):
        raise HTTPException(status_code=400, detail=f"status '{status}' non valido")

    fields: dict[str, object] = {
        "label": label.strip(),
        "bot_username": (bot_username.strip().lstrip("@").lower() or None),
        "daily_msg_cap": max(1, int(daily_msg_cap)),
        "poll_interval_seconds": max(5, int(poll_interval_seconds)),
        "status": status,
        "notes": (notes.strip() or None),
    }
    token = (bot_token or "").strip()
    if token:
        if not is_configured():
            raise HTTPException(
                status_code=400,
                detail="ARGOS_SECRET non impostata: impossibile cifrare il nuovo token.",
            )
        fields["encrypted_bot_token"] = encrypt(token)

    db.update_telegram_bot(bot_id, **fields)
    log.info("telegram bot updated: id=%s status=%s token_changed=%s", bot_id, status, bool(token))
    return RedirectResponse(
        url=f"/accounts/messaging?tab=telegram&flash=Bot+%23{bot_id}+aggiornato",
        status_code=303,
    )


@router.post("/accounts/messaging/telegram/{bot_id}/delete")
async def telegram_bot_delete(bot_id: int):
    db.delete_telegram_bot(bot_id)
    return RedirectResponse(
        url="/accounts/messaging?tab=telegram&flash=Bot+eliminato",
        status_code=303,
    )


@router.post("/accounts/messaging/telegram/{bot_id}/toggle-status")
async def telegram_bot_toggle_status(bot_id: int):
    b = db.get_telegram_bot(bot_id)
    if not b:
        raise HTTPException(status_code=404, detail="bot non trovato")
    current = b.get("status") or "active"
    new_status = "quarantine" if current == "active" else "active"
    db.update_telegram_bot(bot_id, status=new_status)
    return RedirectResponse(url="/accounts/messaging?tab=telegram", status_code=303)


@router.post("/accounts/messaging/telegram/{bot_id}/test")
async def telegram_bot_test(bot_id: int):
    """Smoke test: chiama Telegram Bot API `/getMe` per verificare token valido.
    Se ok=True, aggiorna `bot_username` con il valore restituito dall'API."""
    b = db.get_telegram_bot(bot_id)
    if not b:
        raise HTTPException(status_code=404, detail="bot non trovato")

    from urllib.parse import quote
    try:
        token = decrypt(b["encrypted_bot_token"])
        import httpx
        url = f"https://api.telegram.org/bot{token}/getMe"
        with httpx.Client(timeout=10) as client:
            resp = client.get(url)
        data = resp.json()
        if not data.get("ok"):
            err = data.get("description", "unknown error")
            log.warning("telegram bot #%s test FAILED: %s", bot_id, err)
            return RedirectResponse(
                url=f"/accounts/messaging?tab=telegram&error=Test+fallito+per+%23{bot_id}%3A+{quote(err[:200])}",
                status_code=303,
            )
        result = data.get("result", {})
        username = (result.get("username") or "").strip().lower() or None
        # Auto-aggiorna bot_username se l'utente non l'aveva fornito.
        if username and not b.get("bot_username"):
            try:
                db.update_telegram_bot(bot_id, bot_username=username)
            except Exception:
                pass  # username conflict — non bloccante
        log.info("telegram bot #%s test OK: @%s", bot_id, username)
        return RedirectResponse(
            url=f"/accounts/messaging?tab=telegram&flash=Test+OK+su+%23{bot_id}+%28%40{username or 'unknown'}%29",
            status_code=303,
        )
    except Exception as e:
        log.warning("telegram bot #%s test exception: %s", bot_id, e)
        return RedirectResponse(
            url=f"/accounts/messaging?tab=telegram&error=Test+fallito+per+%23{bot_id}%3A+{quote(str(e)[:200])}",
            status_code=303,
        )
