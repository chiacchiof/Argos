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
    `AGENTSCRAPER_OLLAMA_KEEP_ALIVE` — formato Ollama: "30m", "1h", "-1"=forever, "0"=eject subito."""
    return os.environ.get("AGENTSCRAPER_OLLAMA_KEEP_ALIVE", "30m").strip() or "30m"


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
