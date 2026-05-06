"""Client async minimale per Ollama /api/chat con tool calling."""
from __future__ import annotations

from typing import Any

import httpx

from ..config import settings


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
    }
    if tools:
        payload["tools"] = tools

    async with httpx.AsyncClient(timeout=300) as client:
        r = await client.post(f"{settings.ollama_url}/api/chat", json=payload)
        r.raise_for_status()
        data = r.json()
    return data.get("message", {})


async def list_models() -> list[str]:
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.get(f"{settings.ollama_url}/api/tags")
        r.raise_for_status()
        data = r.json()
    return [m["name"] for m in data.get("models", [])]
