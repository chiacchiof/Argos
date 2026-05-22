"""Catalogo dei provider LLM supportati per la modalità browser-use.

Tutti i provider esposti qui parlano l'API OpenAI-compatible (Anthropic, xAI Grok,
Google Gemini hanno tutti un endpoint compat). Browser-use usa ChatOpenAI come
client, quindi basta swappare base_url + api_key.

Le API key si risolvono in priorita':
1. credential_id passato al runner (lookup in llm_api_keys, decrypt Fernet)
2. project_key salvato direttamente sul task (legacy, plaintext)
3. variabile d'ambiente del provider
"""
from __future__ import annotations

import logging
import os
from typing import TypedDict

from ..config import settings


log = logging.getLogger(__name__)


class ProviderModel(TypedDict):
    id: str
    desc: str


class ProviderInfo(TypedDict, total=False):
    key: str
    name: str
    base_url: str | None
    env_key: str | None
    needs_key: bool
    suggested_models: list[ProviderModel]
    docs_url: str
    hint: str


PROVIDERS: dict[str, ProviderInfo] = {
    "ollama": {
        "key": "ollama",
        "name": "Ollama (locale)",
        "base_url": None,  # popolato da settings.ollama_url + /v1
        "env_key": None,
        "needs_key": False,
        "suggested_models": [],  # popolati dinamicamente via /api/tags
        "docs_url": "https://ollama.com",
        "hint": "Locale, gratis. JSON tool-calling spesso fragile su modelli ≤20B.",
    },
    "openai": {
        "key": "openai",
        "name": "OpenAI",
        "base_url": "https://api.openai.com/v1",
        "env_key": "OPENAI_API_KEY",
        "needs_key": True,
        "suggested_models": [
            {"id": "gpt-4o-mini", "desc": "veloce, economico, ~$0.15/1M token input"},
            {"id": "gpt-4o", "desc": "frontier, ~$2.50/1M input, qualità eccellente"},
            {"id": "gpt-4-turbo", "desc": "vecchio frontier, costoso"},
        ],
        "docs_url": "https://platform.openai.com",
        "hint": "Best-in-class per browser-use. Parti da gpt-4o-mini.",
    },
    "anthropic": {
        "key": "anthropic",
        "name": "Anthropic (Claude)",
        "base_url": "https://api.anthropic.com/v1/",
        "env_key": "ANTHROPIC_API_KEY",
        "needs_key": True,
        "suggested_models": [
            {"id": "claude-haiku-4-5", "desc": "veloce ed economico"},
            {"id": "claude-sonnet-4-6", "desc": "qualità alta, ~$3/1M input"},
            {"id": "claude-opus-4-7", "desc": "frontier, premium"},
        ],
        "docs_url": "https://console.anthropic.com",
        "hint": "Endpoint OpenAI-compat di Anthropic. Sonnet 4.6 è il sweet spot.",
    },
    "grok": {
        "key": "grok",
        "name": "xAI Grok",
        "base_url": "https://api.x.ai/v1",
        "env_key": "XAI_API_KEY",
        "needs_key": True,
        "suggested_models": [
            {"id": "grok-2-latest", "desc": "Grok 2 generalista"},
            {"id": "grok-beta", "desc": "preview / beta"},
        ],
        "docs_url": "https://x.ai/api",
        "hint": "API OpenAI-compat. Buono su JSON, meno testato con browser-use.",
    },
    "gemini": {
        "key": "gemini",
        "name": "Google Gemini",
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai/",
        "env_key": "GEMINI_API_KEY",
        "needs_key": True,
        "suggested_models": [
            {"id": "gemini-2.5-flash", "desc": "veloce, contesto lungo, economico"},
            {"id": "gemini-2.5-pro", "desc": "frontier, contesto enorme"},
        ],
        "docs_url": "https://aistudio.google.com",
        "hint": "Endpoint OpenAI-compat di Google. Contesto enorme, utile su pagine lunghe.",
    },
    "custom": {
        "key": "custom",
        "name": "Endpoint personalizzato (OpenAI-compat)",
        "base_url": None,
        "env_key": "CUSTOM_API_KEY",
        "needs_key": False,
        "suggested_models": [],
        "docs_url": "",
        "hint": "Imposta tu base URL e variabile d'ambiente CUSTOM_API_KEY.",
    },
}

DEFAULT_PROVIDER = "ollama"


def get_provider(key: str | None) -> ProviderInfo:
    return PROVIDERS.get(key or DEFAULT_PROVIDER, PROVIDERS[DEFAULT_PROVIDER])


def list_providers() -> list[ProviderInfo]:
    return list(PROVIDERS.values())


def env_key_status() -> dict[str, bool]:
    """{provider_key: True se la sua env var API key è impostata}."""
    out: dict[str, bool] = {}
    for k, info in PROVIDERS.items():
        if not info.get("needs_key"):
            out[k] = True  # non serve chiave
            continue
        env_var = info.get("env_key")
        out[k] = bool(env_var and os.environ.get(env_var))
    return out


def resolve_base_url(provider_key: str, custom_base_url: str | None) -> str:
    """Ritorna la base URL effettiva, considerando i settings per ollama e custom."""
    info = get_provider(provider_key)
    if provider_key == "ollama":
        return f"{settings.ollama_url.rstrip('/')}/v1"
    if provider_key == "custom":
        url = (custom_base_url or "").strip().rstrip("/")
        if not url:
            raise RuntimeError(
                "Provider 'custom' richiede 'llm_base_url' nel progetto."
            )
        return url
    return info["base_url"] or ""


def resolve_api_key(provider_key: str, project_key: str | None = None) -> str:
    """Ritorna l'API key per il provider.

    Priorità:
    1. project_key (se passata e non vuota) — chiave salvata nel progetto
    2. variabile d'ambiente (info['env_key'])
    3. errore se needs_key=True

    Per 'ollama' ritorna sempre 'ollama-local' (placeholder accettato dall'endpoint).
    """
    info = get_provider(provider_key)
    if provider_key == "ollama":
        return "ollama-local"

    if project_key and project_key.strip():
        return project_key.strip()

    env_var = info.get("env_key")
    key = os.environ.get(env_var) if env_var else None
    if key:
        return key

    if info.get("needs_key"):
        raise RuntimeError(
            f"API key mancante per provider '{provider_key}': "
            f"compila il campo 'API key' nel progetto OPPURE imposta la "
            f"variabile d'ambiente {env_var} nel file .env."
        )
    return "no-key"


def resolve_credential(
    credential_id: int | None,
    provider_key: str,
    *,
    project_key: str | None = None,
    custom_base_url: str | None = None,
) -> tuple[str, str, int | None]:
    """Risolve api_key + base_url per una chiamata LLM, partendo da un eventuale
    credential_id (FK a llm_api_keys). Fallback a project_key/env var come prima.

    Ritorna: (api_key, base_url, resolved_credential_id).
    `resolved_credential_id` e' l'id usato (utile per il billing log); None
    se non e' stato risolto da DB ma da env/legacy.
    """
    # Import lazy per evitare cicli (db.py importa modelli che potrebbero
    # indirettamente importare llm_providers).
    from .. import db
    from .social.crypto_creds import decrypt

    cred = None
    if credential_id:
        try:
            cred = db.get_llm_api_key(int(credential_id))
        except Exception:
            log.exception("resolve_credential: lookup credential_id=%s failed", credential_id)
            cred = None

    if cred:
        cred_provider = (cred.get("provider") or "").lower()
        if cred_provider and cred_provider != provider_key:
            log.warning(
                "resolve_credential: credential #%s e' per provider '%s' ma il "
                "task chiede '%s' — uso la chiave comunque",
                cred.get("id"), cred_provider, provider_key,
            )
        enc = cred.get("encrypted_api_key")
        api_key = ""
        if enc:
            try:
                api_key = decrypt(enc)
            except Exception:
                log.exception("resolve_credential: decrypt failed for credential #%s", cred.get("id"))
                api_key = ""
        base_url_override = (cred.get("base_url") or "").strip().rstrip("/") or None
        base_url = resolve_base_url(provider_key, base_url_override or custom_base_url)
        # Se la chiave decifrata e' vuota ma il provider non la richiede (ollama),
        # va bene; altrimenti fallback alla env var prima di arrendersi.
        if not api_key:
            api_key = resolve_api_key(provider_key, project_key)
        return api_key, base_url, int(cred["id"])

    # Nessuna credenziale: fallback path legacy.
    api_key = resolve_api_key(provider_key, project_key)
    base_url = resolve_base_url(provider_key, custom_base_url)
    return api_key, base_url, None
