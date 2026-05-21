"""Client async minimale per Ollama /api/chat con tool calling."""
from __future__ import annotations

import os
from typing import Any

import httpx

from ..config import settings


def ollama_keep_alive() -> str:
    """Valore di `keep_alive` per i payload Ollama (durata permanenza modello
    in VRAM dopo la risposta). Default 30m (vs 5m hard-coded di Ollama) per
    ridurre cold-start fra job consecutivi. Override via env var
    `ARGOS_OLLAMA_KEEP_ALIVE` (con fallback `AGENTSCRAPER_OLLAMA_KEEP_ALIVE` pre-rebrand 2026-05-21).
    Formato Ollama: "30m", "1h", "-1"=forever, "0"=eject subito."""
    val = (
        os.environ.get("ARGOS_OLLAMA_KEEP_ALIVE")
        or os.environ.get("AGENTSCRAPER_OLLAMA_KEEP_ALIVE")
        or "30m"
    )
    return val.strip() or "30m"


async def chat(
    model: str,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None = None,
    temperature: float = 0.2,
) -> dict[str, Any]:
    """Ritorna il dict 'message' della risposta Ollama."""
    payload: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "stream": False,
        "options": {"temperature": temperature},
        "keep_alive": ollama_keep_alive(),
    }
    if tools:
        payload["tools"] = tools

    async with httpx.AsyncClient(timeout=300) as client:
        r = await client.post(f"{settings.ollama_url}/api/chat", json=payload)
        r.raise_for_status()
        data = r.json()
    return data.get("message", {})


async def chat_openai_compat(
    base_url: str,
    api_key: str,
    model: str,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None = None,
    temperature: float = 0.2,
) -> dict[str, Any]:
    """Chiama /v1/chat/completions su un endpoint OpenAI-compatible e ritorna
    il dict `message` (stessa shape di `chat()` Ollama: {content, tool_calls?}).

    Usato per dispatchare il loop ReAct verso provider cloud
    (OpenAI/Anthropic/Gemini/Grok/custom) oltre che verso Ollama via /v1.
    """
    payload: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
    }
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"
    maybe_add_keep_alive(payload, base_url)

    headers = {"Authorization": f"Bearer {api_key}"}
    url = f"{base_url.rstrip('/')}/chat/completions"
    async with httpx.AsyncClient(timeout=300) as client:
        r = await client.post(url, json=payload, headers=headers)
        r.raise_for_status()
        data = r.json()
    return (data.get("choices") or [{}])[0].get("message", {}) or {}


def maybe_add_keep_alive(payload: dict[str, Any], base_url: str) -> None:
    """In-place: aggiunge `keep_alive` al payload se l'endpoint sembra Ollama.
    Da chiamare PRIMA di POST a /v1/chat/completions su endpoint locali.
    No-op su provider cloud (OpenAI, Anthropic, ecc.) per evitare 400."""
    u = (base_url or "").lower()
    if ("11434" in u) or ("ollama" in u):
        payload["keep_alive"] = ollama_keep_alive()


async def preload(model: str, *, base_url: str | None = None) -> None:
    """Pre-warm: forza il caricamento del modello in VRAM senza generare.
    Da chiamare all'inizio di un job LLM-heavy per ammortizzare il cold-start
    durante il setup (login WA, lettura asset, ecc.).

    Usa POST /api/generate con prompt vuoto + keep_alive — Ollama carica e
    ritorna subito. Errori vengono ignorati (best-effort)."""
    url = (base_url or settings.ollama_url).rstrip("/")
    payload = {"model": model, "keep_alive": ollama_keep_alive()}
    try:
        async with httpx.AsyncClient(timeout=120) as client:
            await client.post(f"{url}/api/generate", json=payload)
    except Exception:
        pass


async def list_models() -> list[str]:
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.get(f"{settings.ollama_url}/api/tags")
        r.raise_for_status()
        data = r.json()
    return [m["name"] for m in data.get("models", [])]
