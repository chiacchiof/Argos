"""Route per gestione chiavi API LLM multi-account, tenant-scoped.

Pattern parallelo a `accounts_email` / `accounts_messaging`: l'utente del
tenant aggiunge una o piu' chiavi per provider con un label simbolico
("prod", "dev", "cliente X"). In fase di creazione task sceglie il provider
e poi la chiave dal dropdown — niente piu' API key plaintext sparse nei task.

In futuro il super-admin potra' configurare chiavi globali (tenant_id=NULL)
selezionabili dai tenant ma non leggibili in chiaro: il modello DB e' gia'
predisposto, la UI verra' aggiunta quando serve il billing cross-tenant.

Endpoint:
- GET  /accounts/llm-keys                — lista chiavi del tenant
- GET  /accounts/llm-keys/new            — form add
- POST /accounts/llm-keys/new            — insert (cifra api_key Fernet)
- GET  /accounts/llm-keys/{id}/edit      — form edit
- POST /accounts/llm-keys/{id}/edit      — update (preserve-on-empty per key)
- POST /accounts/llm-keys/{id}/delete    — delete (FK nei task → NULL)
- POST /accounts/llm-keys/{id}/test      — smoke test (chiama /v1/models o /api/tags)
- POST /accounts/llm-keys/{id}/toggle-status  — active <-> quarantine
"""
from __future__ import annotations

import logging
import secrets
from urllib.parse import quote

import httpx
from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from .. import db, db_cloud
from ..agent.llm_providers import PROVIDERS, get_provider, resolve_base_url
from ..agent.social.crypto_creds import decrypt, encrypt, is_configured
from ..templates import templates

router = APIRouter()
log = logging.getLogger(__name__)


def _provider_choices() -> list[dict]:
    """Lista provider per il select del form. Esposta come dict semplificati."""
    return [
        {
            "key": k,
            "name": info["name"],
            "needs_key": bool(info.get("needs_key")),
            "env_key": info.get("env_key"),
            "hint": info.get("hint", ""),
        }
        for k, info in PROVIDERS.items()
    ]


@router.get("/accounts/llm-keys", response_class=HTMLResponse)
async def llm_keys_list(
    request: Request, author: str = "", flash: str = "", error: str = ""
):
    current_user = getattr(request.state, "current_user", None)
    is_super_admin = bool(current_user and current_user.is_super_admin)
    current_uid = db.current_user_id()
    default_author = "tenant" if is_super_admin else "mine"
    author_norm = (author or default_author).strip().lower()
    if author_norm not in ("mine", "tenant"):
        author_norm = default_author

    filter_uid = current_uid if (author_norm == "mine" and current_uid is not None) else None
    keys = db.list_llm_api_keys(created_by_user_id=filter_uid)
    total_tenant = (
        len(db.list_llm_api_keys()) if author_norm == "mine" else len(keys)
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
        "accounts_llm_keys.html",
        {
            "keys": keys,
            "providers": _provider_choices(),
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


@router.get("/accounts/llm-keys/new", response_class=HTMLResponse)
async def llm_key_new_form(request: Request):
    return templates.TemplateResponse(
        request,
        "accounts_llm_keys_edit.html",
        {
            "key": None,  # None = new
            "providers": _provider_choices(),
            "is_secret_configured": is_configured(),
        },
    )


@router.post("/accounts/llm-keys/new")
async def llm_key_create(
    request: Request,
    label: str = Form(...),
    provider: str = Form(...),
    api_key: str = Form(""),
    base_url: str = Form(""),
    notes: str = Form(""),
):
    label = label.strip()
    provider = provider.strip().lower()
    api_key = (api_key or "").strip()
    base_url = (base_url or "").strip().rstrip("/") or None

    if not label:
        raise HTTPException(status_code=400, detail="label obbligatoria")
    if provider not in PROVIDERS:
        raise HTTPException(status_code=400, detail=f"provider '{provider}' sconosciuto")

    info = get_provider(provider)
    needs_key = bool(info.get("needs_key"))
    if needs_key and not api_key:
        raise HTTPException(
            status_code=400,
            detail=f"il provider '{provider}' richiede un'API key",
        )
    if api_key and not is_configured():
        raise HTTPException(
            status_code=400,
            detail=(
                "ARGOS_SECRET non impostata in .env: impossibile cifrare la chiave. "
                "Aggiungi ARGOS_SECRET=<stringa-30+-caratteri>."
            ),
        )

    encrypted = encrypt(api_key) if api_key else None
    uuid = f"llmkey-{secrets.token_hex(8)}"
    try:
        key_id = db.insert_llm_api_key({
            "uuid": uuid,
            "label": label,
            "provider": provider,
            "encrypted_api_key": encrypted,
            "base_url": base_url,
            "status": "active",
            "notes": (notes.strip() or None),
        })
        log.info("llm api key created: id=%s provider=%s label=%s", key_id, provider, label)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"errore DB: {e}")
    return RedirectResponse(
        url=f"/accounts/llm-keys?flash=Chiave+%23{key_id}+creata",
        status_code=303,
    )


@router.get("/accounts/llm-keys/{key_id}/edit", response_class=HTMLResponse)
async def llm_key_edit_form(request: Request, key_id: int):
    k = db.get_llm_api_key(key_id)
    if not k:
        raise HTTPException(status_code=404, detail="chiave non trovata")
    return templates.TemplateResponse(
        request,
        "accounts_llm_keys_edit.html",
        {
            "key": k,
            "providers": _provider_choices(),
            "is_secret_configured": is_configured(),
        },
    )


@router.post("/accounts/llm-keys/{key_id}/edit")
async def llm_key_update(
    request: Request,
    key_id: int,
    label: str = Form(...),
    provider: str = Form(...),
    api_key: str = Form(""),
    base_url: str = Form(""),
    status: str = Form("active"),
    notes: str = Form(""),
):
    existing = db.get_llm_api_key(key_id)
    if not existing:
        raise HTTPException(status_code=404, detail="chiave non trovata")

    provider = provider.strip().lower()
    if provider not in PROVIDERS:
        raise HTTPException(status_code=400, detail=f"provider '{provider}' sconosciuto")
    status = status.strip().lower()
    if status not in ("active", "quarantine", "banned"):
        raise HTTPException(status_code=400, detail=f"status '{status}' non valido")

    fields: dict[str, object] = {
        "label": label.strip(),
        "provider": provider,
        "base_url": ((base_url or "").strip().rstrip("/") or None),
        "status": status,
        "notes": (notes.strip() or None),
    }
    new_key = (api_key or "").strip()
    if new_key:
        if new_key.upper() == "CLEAR":
            fields["encrypted_api_key"] = None
        else:
            if not is_configured():
                raise HTTPException(
                    status_code=400,
                    detail="ARGOS_SECRET non impostata: impossibile cifrare la nuova chiave.",
                )
            fields["encrypted_api_key"] = encrypt(new_key)

    db.update_llm_api_key(key_id, **fields)
    log.info(
        "llm api key updated: id=%s provider=%s status=%s key_changed=%s",
        key_id, provider, status, bool(new_key),
    )
    return RedirectResponse(
        url=f"/accounts/llm-keys?flash=Chiave+%23{key_id}+aggiornata",
        status_code=303,
    )


@router.post("/accounts/llm-keys/{key_id}/delete")
async def llm_key_delete(key_id: int):
    db.delete_llm_api_key(key_id)
    return RedirectResponse(
        url="/accounts/llm-keys?flash=Chiave+eliminata",
        status_code=303,
    )


@router.post("/accounts/llm-keys/{key_id}/toggle-status")
async def llm_key_toggle_status(key_id: int):
    k = db.get_llm_api_key(key_id)
    if not k:
        raise HTTPException(status_code=404, detail="chiave non trovata")
    current = k.get("status") or "active"
    new_status = "quarantine" if current == "active" else "active"
    db.update_llm_api_key(key_id, status=new_status)
    return RedirectResponse(url="/accounts/llm-keys", status_code=303)


@router.post("/accounts/llm-keys/{key_id}/test")
async def llm_key_test(key_id: int):
    """Smoke test: chiama l'endpoint /models del provider per validare la chiave.

    Per ollama: GET /api/tags (no auth richiesta).
    Per gli altri: GET {base_url}/models con Authorization Bearer.
    """
    k = db.get_llm_api_key(key_id)
    if not k:
        raise HTTPException(status_code=404, detail="chiave non trovata")

    provider = (k.get("provider") or "").lower()
    base_url = (k.get("base_url") or "").rstrip("/") or None

    try:
        if provider == "ollama":
            url = (base_url or resolve_base_url("ollama", None)).rstrip("/")
            # Ollama: /v1 e' OpenAI-compat ma /api/tags da' la lista nativa.
            tags_url = url.replace("/v1", "") + "/api/tags"
            with httpx.Client(timeout=10) as client:
                resp = client.get(tags_url)
            resp.raise_for_status()
            n_models = len((resp.json() or {}).get("models", []))
            msg = f"OK+%28{n_models}+modelli+disponibili%29"
        else:
            # OpenAI-compat: GET /models con Bearer auth.
            api_key_enc = k.get("encrypted_api_key")
            if not api_key_enc:
                raise RuntimeError("la chiave non e' impostata: impossibile testare")
            api_key = decrypt(api_key_enc)
            url = (base_url or resolve_base_url(provider, None)).rstrip("/")
            models_url = url + "/models"
            with httpx.Client(timeout=15) as client:
                resp = client.get(models_url, headers={"Authorization": f"Bearer {api_key}"})
            if resp.status_code == 401:
                raise RuntimeError("401 Unauthorized — la chiave non e' valida")
            resp.raise_for_status()
            data = resp.json() or {}
            n_models = len(data.get("data", []) or [])
            msg = f"OK+%28{n_models}+modelli+visibili%29"
        log.info("llm api key #%s test OK (provider=%s)", key_id, provider)
        return RedirectResponse(
            url=f"/accounts/llm-keys?flash=Test+chiave+%23{key_id}+{msg}",
            status_code=303,
        )
    except Exception as e:
        log.warning("llm api key #%s test FAILED: %s", key_id, e)
        return RedirectResponse(
            url=f"/accounts/llm-keys?error=Test+fallito+per+%23{key_id}%3A+{quote(str(e)[:200])}",
            status_code=303,
        )


# ---------------------------------------------------------------------------
# HTMX fragments per task_form.html
# ---------------------------------------------------------------------------

@router.get("/providers/credential-field", response_class=HTMLResponse)
async def credential_field_fragment(
    request: Request,
    provider: str = "",
    slot: str = "main",
    selected: str = "",
):
    """Ritorna il partial con la `<select>` delle credenziali disponibili per
    il provider scelto. Usato in HTMX dal task_form.html sui 3 slot LLM
    (main / discovery / browser).

    slot: 'main' | 'discovery' | 'browser' (determina il name dell'input)
    selected: id della credenziale attualmente assegnata (per pre-selezionare)
    """
    provider = (provider or "").strip().lower()
    keys: list[dict] = []
    if provider:
        keys = db.list_llm_api_keys(provider=provider, status="active")

    name_map = {
        "main": "llm_credential_id",
        "discovery": "discovery_llm_credential_id",
        "browser": "browser_llm_credential_id",
        "orchestrator": "llm_credential_id",
    }
    field_name = name_map.get(slot, "llm_credential_id")

    info = get_provider(provider) if provider else None
    needs_key = bool(info and info.get("needs_key"))

    try:
        selected_id = int(selected) if str(selected).strip().isdigit() else None
    except (TypeError, ValueError):
        selected_id = None

    return templates.TemplateResponse(
        request,
        "partials/credential_field.html",
        {
            "field_name": field_name,
            "slot": slot,
            "provider": provider,
            "needs_key": needs_key,
            "env_key": (info or {}).get("env_key"),
            "keys": keys,
            "selected_id": selected_id,
        },
    )
