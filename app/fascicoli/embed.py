"""Embedding via Ollama locale.

Default model: `nomic-embed-text` (768 dim, qualita' buona, taglia piccola).
Override con env var `ARGOS_EMBED_MODEL`. Ollama base URL via
`OLLAMA_BASE_URL` (default http://localhost:11434).

Niente numpy: cosine similarity in puro Python. Per progetti piccoli (~migliaia
di chunk) e' veloce a sufficienza; vector store dedicato (sqlite-vss / LanceDB)
arrivera' nella v2 quando avremo casi >10k chunks.
"""
from __future__ import annotations

import logging
import math
import os

import httpx

log = logging.getLogger(__name__)


DEFAULT_EMBED_MODEL = "nomic-embed-text"
DEFAULT_OLLAMA_BASE = "http://localhost:11434"


def ollama_base() -> str:
    return (os.environ.get("OLLAMA_BASE_URL") or DEFAULT_OLLAMA_BASE).rstrip("/")


def embed_model() -> str:
    return os.environ.get("ARGOS_EMBED_MODEL") or DEFAULT_EMBED_MODEL


def embed_texts(texts: list[str], *, batch_logs: bool = False) -> list[list[float]]:
    """Restituisce gli embedding (vettori float) per ciascun testo.

    NB: Ollama `/api/embeddings` accetta un solo prompt per chiamata; iteriamo.
    Per progetti grandi varra' la pena async + concorrenza; per ora seriale.
    """
    base = ollama_base()
    model = embed_model()
    url = f"{base}/api/embeddings"
    out: list[list[float]] = []
    with httpx.Client(timeout=60.0) as client:
        for i, t in enumerate(texts):
            try:
                r = client.post(url, json={"model": model, "prompt": t})
                r.raise_for_status()
                data = r.json()
                emb = data.get("embedding") or []
                if not emb:
                    log.warning("ollama returned empty embedding for chunk %d", i)
                out.append(list(emb))
            except httpx.HTTPError as exc:
                log.error("embedding call failed for chunk %d: %s", i, exc)
                out.append([])
            if batch_logs and i and (i % 20 == 0):
                log.info("embedded %d/%d", i + 1, len(texts))
    return out


def cosine(a: list[float], b: list[float]) -> float:
    """Cosine similarity puro Python. 0.0 se uno dei due vettori e' vuoto o nullo."""
    if not a or not b:
        return 0.0
    dot = 0.0
    na = 0.0
    nb = 0.0
    for x, y in zip(a, b):
        dot += x * y
        na += x * x
        nb += y * y
    if na <= 0.0 or nb <= 0.0:
        return 0.0
    return dot / (math.sqrt(na) * math.sqrt(nb))
