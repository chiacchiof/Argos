"""Settings: configurazione orchestrator, canali email + telegram, test invio."""
from __future__ import annotations

import os
from typing import Any

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from .. import db
from ..agent.llm_providers import get_provider, list_providers
from ..agent.ollama import list_models
from ..channels import email as ch_email
from ..channels import telegram as ch_telegram
from ..config import settings as app_settings
from ..templates import templates


router = APIRouter()


def _email_env_status() -> dict[str, bool]:
    return {
        "smtp_password": bool(os.environ.get("SMTP_PASSWORD")),
        "imap_password": bool(os.environ.get("IMAP_PASSWORD")),
    }


def _telegram_env_status() -> dict[str, bool]:
    return {"bot_token": bool(os.environ.get("TELEGRAM_BOT_TOKEN"))}


def _orchestrator_defaults() -> dict[str, Any]:
    return {
        "use_llm": False,
        "llm_provider": "ollama",
        "planner_model": "",
        "llm_base_url": "",
        "chat_web_enabled": False,
        "chat_files_enabled": False,
    }


async def _planner_model_options(provider_key: str) -> list[dict[str, str]]:
    if provider_key == "ollama":
        try:
            models = await list_models()
        except Exception:
            models = [app_settings.default_model]
        return [{"id": m, "desc": "(installato in locale)"} for m in models]

    info = get_provider(provider_key)
    return list(info.get("suggested_models") or [])


@router.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request):
    email_cfg = db.get_channel_config("email") or {}
    telegram_cfg = db.get_channel_config("telegram") or {}
    orchestrator_row = db.get_channel_config("orchestrator") or {}
    orchestrator_cfg = {**_orchestrator_defaults(), **(orchestrator_row.get("config") or {})}
    orchestrator_provider = orchestrator_cfg.get("llm_provider") or "ollama"
    return templates.TemplateResponse(
        request,
        "settings.html",
        {
            "orchestrator_cfg": orchestrator_cfg,
            "orchestrator_enabled": bool(orchestrator_row.get("enabled")),
            "llm_providers": list_providers(),
            "orchestrator_planner_models": await _planner_model_options(orchestrator_provider),
            "email_cfg": email_cfg.get("config", {}),
            "email_enabled": bool(email_cfg.get("enabled")),
            "telegram_cfg": telegram_cfg.get("config", {}),
            "telegram_enabled": bool(telegram_cfg.get("enabled")),
            "email_env": _email_env_status(),
            "telegram_env": _telegram_env_status(),
            "flash": request.query_params.get("flash"),
            "error": request.query_params.get("error"),
        },
    )


@router.get("/settings/orchestrator/model-field", response_class=HTMLResponse)
async def orchestrator_model_field(
    request: Request,
    llm_provider: str = "ollama",
    planner_model: str = "",
):
    provider_key = (llm_provider or "ollama").strip()
    models = await _planner_model_options(provider_key)
    default_model = models[0]["id"] if models else ""
    return templates.TemplateResponse(
        request,
        "partials/orchestrator_planner_model_field.html",
        {
            "provider_key": provider_key,
            "planner_models": models,
            "current_model": planner_model.strip(),
            "default_model": default_model,
        },
    )


@router.post("/settings/orchestrator")
async def save_orchestrator_config(
    use_llm: str = Form(""),
    chat_web_enabled: str = Form(""),
    chat_files_enabled: str = Form(""),
    llm_provider: str = Form("ollama"),
    planner_model: str = Form(""),
    llm_base_url: str = Form(""),
    llm_api_key: str = Form(""),
):
    existing = (db.get_channel_config("orchestrator") or {}).get("config", {})
    cfg: dict[str, Any] = {
        "use_llm": bool(use_llm),
        "llm_provider": (llm_provider or "ollama").strip() or "ollama",
        "planner_model": planner_model.strip() or None,
        "llm_base_url": llm_base_url.strip() or None,
        "chat_web_enabled": bool(chat_web_enabled),
        "chat_files_enabled": bool(chat_files_enabled),
    }
    key_action = (llm_api_key or "").strip()
    if key_action.upper() == "CLEAR":
        pass
    elif key_action:
        cfg["llm_api_key"] = key_action
    elif existing.get("llm_api_key"):
        cfg["llm_api_key"] = existing["llm_api_key"]
    db.save_channel_config("orchestrator", cfg, enabled=bool(use_llm))
    return RedirectResponse(url="/settings?flash=Orchestrator+config+salvata", status_code=303)


@router.post("/settings/email")
async def save_email_config(
    smtp_host: str = Form(""),
    smtp_port: int = Form(587),
    smtp_user: str = Form(""),
    smtp_password: str = Form(""),
    smtp_use_tls: str = Form("on"),
    imap_host: str = Form(""),
    imap_port: int = Form(993),
    imap_user: str = Form(""),
    imap_password: str = Form(""),
    imap_folder: str = Form("INBOX"),
    from_address: str = Form(""),
    reply_to: str = Form(""),
    rate_limit_per_minute: int = Form(10),
    enabled: str = Form(""),
):
    cfg: dict[str, Any] = {
        "smtp_host": smtp_host.strip() or None,
        "smtp_port": int(smtp_port),
        "smtp_user": smtp_user.strip() or None,
        "smtp_use_tls": bool(smtp_use_tls),
        "imap_host": imap_host.strip() or None,
        "imap_port": int(imap_port),
        "imap_user": imap_user.strip() or None,
        "imap_folder": imap_folder.strip() or "INBOX",
        "from_address": from_address.strip() or None,
        "reply_to": reply_to.strip() or None,
        "rate_limit_per_minute": int(rate_limit_per_minute),
    }
    # password salvate solo se compilate (override env-var)
    if smtp_password.strip():
        cfg["smtp_password"] = smtp_password.strip()
    elif (db.get_channel_config("email") or {}).get("config", {}).get("smtp_password"):
        # mantieni quella precedente in DB se non sovrascritta
        cfg["smtp_password"] = db.get_channel_config("email")["config"]["smtp_password"]
    if imap_password.strip():
        cfg["imap_password"] = imap_password.strip()
    elif (db.get_channel_config("email") or {}).get("config", {}).get("imap_password"):
        cfg["imap_password"] = db.get_channel_config("email")["config"]["imap_password"]
    db.save_channel_config("email", cfg, enabled=bool(enabled))
    return RedirectResponse(url="/settings?flash=Email+config+salvata", status_code=303)


@router.post("/settings/telegram")
async def save_telegram_config(
    bot_token: str = Form(""),
    enabled: str = Form(""),
):
    existing = (db.get_channel_config("telegram") or {}).get("config", {})
    cfg: dict[str, Any] = {
        "polling_offset": int(existing.get("polling_offset") or 0),
    }
    if bot_token.strip():
        cfg["bot_token"] = bot_token.strip()
    elif existing.get("bot_token"):
        cfg["bot_token"] = existing["bot_token"]
    db.save_channel_config("telegram", cfg, enabled=bool(enabled))
    return RedirectResponse(url="/settings?flash=Telegram+config+salvata", status_code=303)


@router.post("/settings/test-email")
async def test_email(to_address: str = Form("")):
    if not to_address.strip():
        return RedirectResponse(
            url="/settings?error=Indirizzo+email+vuoto", status_code=303
        )
    try:
        msg_id = await ch_email.send_test_email(to_address.strip())
        return RedirectResponse(
            url=f"/settings?flash=Email+di+test+inviata+a+{to_address}+(Message-ID%3A+{msg_id})",
            status_code=303,
        )
    except Exception as e:
        return RedirectResponse(
            url=f"/settings?error=Test+email+fallito%3A+{type(e).__name__}+{str(e)[:200]}",
            status_code=303,
        )


@router.post("/settings/test-telegram")
async def test_telegram(chat_id: str = Form("")):
    if not chat_id.strip():
        return RedirectResponse(
            url="/settings?error=chat_id+vuoto", status_code=303
        )
    try:
        msg_id = await ch_telegram.send_test_message(chat_id.strip())
        return RedirectResponse(
            url=f"/settings?flash=Messaggio+Telegram+inviato+(message_id%3D{msg_id})",
            status_code=303,
        )
    except Exception as e:
        return RedirectResponse(
            url=f"/settings?error=Test+telegram+fallito%3A+{type(e).__name__}+{str(e)[:200]}",
            status_code=303,
        )


@router.post("/settings/test-imap")
async def test_imap_fetch():
    try:
        msgs = await ch_email.fetch_inbound(limit=5)
        return RedirectResponse(
            url=f"/settings?flash=IMAP+fetch+ok%3A+{len(msgs)}+messaggi+nuovi+(verranno+processati+al+prossimo+poll)",
            status_code=303,
        )
    except Exception as e:
        return RedirectResponse(
            url=f"/settings?error=IMAP+fallito%3A+{type(e).__name__}+{str(e)[:200]}",
            status_code=303,
        )
