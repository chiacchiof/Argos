"""Settings: configurazione orchestrator.

Le route POST per email/telegram (singleton legacy via channel_config) sono
state rimosse il 2026-05-22 — le credenziali si gestiscono ora SOLO da:
  /accounts/email      (multi-account email)
  /accounts/messaging  (multi-account telegram bot)

La migration al boot (`db.migrate_legacy_channels_to_accounts`) ha gia'
spostato eventuali config legacy in righe `email_accounts` / `telegram_bots`
con label `legacy-default`.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from .. import db
from ..agent.llm_providers import env_key_status, get_provider, list_providers
from ..agent.ollama import list_models
from ..auth import require_architect_or_admin
from ..config import settings as app_settings
from ..templates import templates


router = APIRouter(dependencies=[Depends(require_architect_or_admin)])


def _orchestrator_defaults() -> dict[str, Any]:
    return {
        "use_llm": False,
        "llm_provider": "ollama",
        "planner_model": "",
        "llm_base_url": "",
        "llm_credential_id": None,
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
    orchestrator_row = db.get_channel_config("orchestrator") or {}
    orchestrator_cfg = {**_orchestrator_defaults(), **(orchestrator_row.get("config") or {})}
    orchestrator_provider = orchestrator_cfg.get("llm_provider") or "ollama"
    try:
        orchestrator_credentials = db.list_llm_api_keys(
            provider=orchestrator_provider, status="active"
        )
    except Exception:
        orchestrator_credentials = []

    # Mostra solo provider con almeno una chiave attiva in DB, oppure senza
    # bisogno di chiave (ollama, custom), oppure con env var settata (legacy).
    try:
        _providers_with_creds = {
            k["provider"] for k in db.list_llm_api_keys(status="active")
        }
    except Exception:
        _providers_with_creds = set()
    _eks = env_key_status()
    visible_providers = [
        p for p in list_providers()
        if not p["needs_key"] or p["key"] in _providers_with_creds or _eks.get(p["key"])
    ]

    return templates.TemplateResponse(
        request,
        "settings.html",
        {
            "orchestrator_cfg": orchestrator_cfg,
            "orchestrator_enabled": bool(orchestrator_row.get("enabled")),
            "orchestrator_credentials": orchestrator_credentials,
            "llm_providers": visible_providers,
            "orchestrator_planner_models": await _planner_model_options(orchestrator_provider),
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
    llm_provider: str = Form("ollama"),
    planner_model: str = Form(""),
    llm_base_url: str = Form(""),
    llm_credential_id: str = Form(""),
):
    """Salva config orchestrator. La chiave non viene piu' messa qui in plaintext:
    si seleziona una credenziale dal pannello /accounts/llm-keys (cifrata)."""
    cfg: dict[str, Any] = {
        "use_llm": bool(use_llm),
        "llm_provider": (llm_provider or "ollama").strip() or "ollama",
        "planner_model": planner_model.strip() or None,
        "llm_base_url": llm_base_url.strip() or None,
        "llm_credential_id": (
            int(llm_credential_id) if str(llm_credential_id).strip().isdigit() else None
        ),
    }
    db.save_channel_config("orchestrator", cfg, enabled=bool(use_llm))
    return RedirectResponse(url="/settings?flash=Orchestrator+config+salvata", status_code=303)
