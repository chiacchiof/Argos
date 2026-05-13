"""Route /settings/whatsapp — gestione account browser (Motore A) + API config (Motore B).

Pagina dedicata per WhatsApp invece di un tab dentro /settings perché:
- WhatsApp è strutturalmente diverso da email/telegram (browser automation vs API)
- I caveat ToS vanno mostrati prominentemente
- QR-login flow richiede UI dedicata

QR-login (Motore A): l'utente clicca "Login QR" su un account in pending_login
→ il server lancia Playwright headed sul DESKTOP dell'utente (non headless)
→ l'utente vede la finestra Chromium aperta sul suo monitor e scansiona il QR
   col telefono
→ il task background aggiorna status='active' quando rileva chat-list
→ l'utente refresh la pagina settings per vedere il nuovo status.
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from .. import db
from ..agent.social import crypto_creds
from ..agent.social.whatsapp_api import WhatsAppAPI
from ..agent.social.whatsapp_browser import WhatsAppBrowser
from ..config import DATA_DIR
from ..templates import templates


router = APIRouter()
log = logging.getLogger(__name__)


def _wa_sessions_dir() -> Path:
    p = DATA_DIR / "whatsapp_sessions"
    p.mkdir(parents=True, exist_ok=True)
    return p


# ===========================================================================
# Vista principale
# ===========================================================================

@router.get("/settings/whatsapp", response_class=HTMLResponse)
async def whatsapp_settings_page(request: Request):
    accounts = db.list_social_accounts(platform="whatsapp_browser")
    api_configs = db.list_whatsapp_api_config()
    flash = request.query_params.get("flash")
    error = request.query_params.get("error")
    return templates.TemplateResponse(
        request,
        "settings_whatsapp.html",
        {
            "accounts": accounts,
            "api_configs": api_configs,
            "flash": flash,
            "error": error,
            "secret_configured": crypto_creds.is_configured(),
        },
    )


# ===========================================================================
# Motore A — Account browser (CRUD + QR login)
# ===========================================================================

@router.post("/settings/whatsapp/account/new")
async def whatsapp_account_new(
    label: str = Form(...),
    phone_number: str = Form(""),
    daily_dm_cap: int = Form(100),
    proxy_label: str = Form(""),
):
    """Crea un nuovo account WA browser in stato 'pending_login'.

    L'utente DOPO clicca "Avvia QR login" per aprire Chromium e scansionare.
    """
    if not crypto_creds.is_configured():
        return RedirectResponse(
            "/settings/whatsapp?error=AGENTSCRAPER_SECRET+non+configurata+in+.env",
            status_code=303,
        )

    label = label.strip()
    phone = phone_number.strip()
    if not label:
        return RedirectResponse(
            "/settings/whatsapp?error=label+obbligatoria", status_code=303
        )

    acc_uuid = str(uuid.uuid4())
    session_dir = _wa_sessions_dir() / acc_uuid
    session_dir.mkdir(parents=True, exist_ok=True)

    # Per WA non c'è password decifrabile: salviamo un placeholder cifrato
    placeholder_enc = crypto_creds.encrypt("__qr_session_placeholder__")

    try:
        db.create_social_account({
            "uuid": acc_uuid,
            "platform": "whatsapp_browser",
            "username": label,  # WA non ha username canonico, usiamo la label
            "encrypted_password": placeholder_enc,
            "proxy_label": proxy_label.strip() or None,
            "daily_dm_cap": int(daily_dm_cap),
            "status": "pending_login",
        })
    except Exception as e:
        return RedirectResponse(
            f"/settings/whatsapp?error=DB+error:+{e}", status_code=303
        )

    # Salva phone_number + session_dir (post-insert update)
    # Cerco l'id appena creato:
    rows = db.list_social_accounts(platform="whatsapp_browser")
    new_acc = next((r for r in rows if r["uuid"] == acc_uuid), None)
    if new_acc:
        db.update_social_account(
            new_acc["id"],
            phone_number=phone or None,
            auth_method="qr_session",
            session_dir=str(session_dir),
        )

    return RedirectResponse(
        f"/settings/whatsapp?flash=Account+{label}+creato.+Clicca+'Avvia+QR+login'+per+scansionare.",
        status_code=303,
    )


@router.post("/settings/whatsapp/account/{account_id}/login")
async def whatsapp_account_login(account_id: int):
    """Lancia Playwright headed in background per QR scan. Risposta immediata.

    L'utente vede una finestra Chromium aperta sul desktop e scansiona dal
    telefono. Quando WA Web rileva session attiva (CHAT_LIST), il task aggiorna
    status='active' e chiude il browser.
    """
    acc = db.get_social_account(account_id)
    if not acc or acc.get("platform") != "whatsapp_browser":
        return JSONResponse({"error": "account non trovato"}, status_code=404)

    from .. import jobs as jobs_mod
    # Fire-and-forget: avvia il login in proactor thread (richiede subprocess
    # Chromium → ProactorEventLoop su Windows).
    asyncio.create_task(
        jobs_mod._run_in_proactor_thread(
            lambda: _do_qr_login(account_id), job_id=-account_id  # job_id negativo = pseudo
        )
    )
    return RedirectResponse(
        f"/settings/whatsapp?flash=Finestra+Chromium+in+apertura+per+account+{acc.get('username')}."
        f"+Scansiona+il+QR+col+telefono.+Refresh+questa+pagina+dopo+lo+scan.",
        status_code=303,
    )


async def _do_qr_login(account_id: int) -> None:
    """Background task: apre Playwright headed, aspetta scan QR, salva session."""
    acc = db.get_social_account(account_id)
    if not acc:
        return
    sess_dir = acc.get("session_dir")
    if not sess_dir:
        log.error("QR login: session_dir mancante per account %s", account_id)
        return

    try:
        try:
            from patchright.async_api import async_playwright as _ap
        except ImportError:
            from playwright.async_api import async_playwright as _ap

        async with _ap() as p:
            context = await p.chromium.launch_persistent_context(
                user_data_dir=sess_dir,
                headless=False,  # SEMPRE headed per QR
                viewport={"width": 1280, "height": 800},
                args=["--disable-blink-features=AutomationControlled"],
            )
            page = await context.new_page()
            wa = WhatsAppBrowser()
            from ..agent.social.platform_base import SocialAccount
            sa = SocialAccount(
                uuid=acc["uuid"],
                platform="whatsapp_browser",
                username=acc.get("phone_number") or acc.get("username") or "",
                password="",
                daily_dm_cap=int(acc.get("daily_dm_cap") or 100),
            )
            health = await wa.login(page, sa)
            log.info("QR login outcome for account %s: %s", account_id, health)
            new_status = "active" if str(health) == "ok" or getattr(health, "value", "") == "ok" else "pending_login"
            db.update_social_account(account_id, status=new_status)
            # Lascia 2s al context per persistere lo storage, poi chiudi
            await asyncio.sleep(2.0)
            await context.close()
    except Exception as e:
        log.exception("QR login failed for account %s: %s", account_id, e)
        try:
            db.update_social_account(account_id, status="pending_login", notes=f"login_error: {e}")
        except Exception:
            pass


@router.post("/settings/whatsapp/account/{account_id}/delete")
async def whatsapp_account_delete(account_id: int):
    acc = db.get_social_account(account_id)
    if not acc:
        return RedirectResponse("/settings/whatsapp?error=non+trovato", status_code=303)
    # Rimuove anche la session_dir su disco
    sess_dir = acc.get("session_dir")
    if sess_dir:
        import shutil
        try:
            shutil.rmtree(sess_dir, ignore_errors=True)
        except Exception as e:
            log.warning("rmtree fail per %s: %s", sess_dir, e)
    db.delete_social_account(account_id)
    return RedirectResponse(
        f"/settings/whatsapp?flash=Account+eliminato", status_code=303
    )


@router.post("/settings/whatsapp/account/{account_id}/disable")
async def whatsapp_account_disable(account_id: int):
    db.update_social_account(account_id, status="disabled")
    return RedirectResponse("/settings/whatsapp?flash=Account+disabilitato", status_code=303)


@router.post("/settings/whatsapp/account/{account_id}/enable")
async def whatsapp_account_enable(account_id: int):
    db.update_social_account(account_id, status="active")
    return RedirectResponse("/settings/whatsapp?flash=Account+riattivato", status_code=303)


# ===========================================================================
# Motore B — API config (CRUD + test)
# ===========================================================================

@router.post("/settings/whatsapp/api/new")
async def whatsapp_api_new(
    label: str = Form(...),
    phone_number_id: str = Form(...),
    business_account_id: str = Form(...),
    app_id: str = Form(""),
    access_token: str = Form(...),
    default_template_name: str = Form(""),
    default_template_language: str = Form("it"),
    daily_msg_cap: int = Form(250),
):
    if not crypto_creds.is_configured():
        return RedirectResponse(
            "/settings/whatsapp?error=AGENTSCRAPER_SECRET+non+configurata+in+.env",
            status_code=303,
        )
    try:
        enc = crypto_creds.encrypt(access_token.strip())
    except Exception as e:
        return RedirectResponse(
            f"/settings/whatsapp?error=Cifratura+fallita:+{e}", status_code=303
        )
    try:
        cfg_id = db.insert_whatsapp_api_config({
            "label": label.strip(),
            "phone_number_id": phone_number_id.strip(),
            "business_account_id": business_account_id.strip(),
            "app_id": app_id.strip() or None,
            "encrypted_access_token": enc,
            "default_template_name": default_template_name.strip() or None,
            "default_template_language": default_template_language.strip() or "it",
            "daily_msg_cap": int(daily_msg_cap),
        })
    except Exception as e:
        return RedirectResponse(
            f"/settings/whatsapp?error=DB+error:+{e}", status_code=303
        )
    return RedirectResponse(
        f"/settings/whatsapp?flash=Config+API+#{cfg_id}+creata", status_code=303
    )


@router.post("/settings/whatsapp/api/{config_id}/test")
async def whatsapp_api_test(config_id: int):
    cfg = db.get_whatsapp_api_config(config_id)
    if not cfg:
        return JSONResponse({"error": "config non trovata"}, status_code=404)
    try:
        api = WhatsAppAPI(cfg)
    except Exception as e:
        return JSONResponse({"ok": False, "message": str(e)}, status_code=200)
    ok, message = await api.verify_credentials()
    return JSONResponse({"ok": ok, "message": message})


@router.post("/settings/whatsapp/api/{config_id}/delete")
async def whatsapp_api_delete(config_id: int):
    db.delete_whatsapp_api_config(config_id)
    return RedirectResponse(
        "/settings/whatsapp?flash=Config+API+eliminata", status_code=303
    )


@router.post("/settings/whatsapp/api/{config_id}/disable")
async def whatsapp_api_disable(config_id: int):
    db.update_whatsapp_api_config(config_id, status="disabled")
    return RedirectResponse("/settings/whatsapp?flash=Config+API+disabilitata", status_code=303)


@router.post("/settings/whatsapp/api/{config_id}/enable")
async def whatsapp_api_enable(config_id: int):
    db.update_whatsapp_api_config(config_id, status="active")
    return RedirectResponse("/settings/whatsapp?flash=Config+API+riattivata", status_code=303)
