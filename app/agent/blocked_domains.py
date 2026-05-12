"""Blocked domains policy — gate centrale per tutti i runner.

Lista hard-coded di domini verso cui e' vietato qualunque traffico (scraping,
probe, fetch). Importata da ogni runner che fa richieste a siti esterni.

Aggiornamento: la lista vive in memoria del CLI (cartella memory/) come
direttiva persistente. Qui la replichiamo come fonte autoritativa runtime.
"""
from __future__ import annotations

from urllib.parse import urlparse

from .runner_bulk_extract import _registrable_domain


# Domini bloccati: nessuna richiesta automatizzata da nessun runner.
# Varianti singolare/plurale + dominio di redirect attuale.
BLOCKED_HOSTS: frozenset[str] = frozenset({
    "mondocamgirl.com", "www.mondocamgirl.com",
    "mondocamgirls.com", "www.mondocamgirls.com",
    "camlive.com", "www.camlive.com",
    # Login-only: i profili individuali sono dietro auth, non scrapabili.
    # Test 2026-05-12: la sola cosa pubblica sono pagine listing/tag che
    # generano falsi positivi (217 asset spazzatura). Vedi memoria.
    "trovagnocca.com", "www.trovagnocca.com",
})


def is_blocked(url: str) -> bool:
    """True se l'URL appartiene a un dominio bloccato dalla policy."""
    if not url:
        return False
    host = (urlparse(url).hostname or "").lower()
    if not host:
        return False
    if host in BLOCKED_HOSTS:
        return True
    reg = _registrable_domain(host)
    return reg in BLOCKED_HOSTS


def filter_blocked(urls: list[str]) -> tuple[list[str], list[str]]:
    """Separa una lista di URL in (allowed, blocked)."""
    allowed: list[str] = []
    blocked: list[str] = []
    for u in urls:
        if is_blocked(u):
            blocked.append(u)
        else:
            allowed.append(u)
    return allowed, blocked


def assert_no_blocked_seeds(seed_queries: list[str]) -> list[str]:
    """Ritorna la lista degli URL bloccati trovati nei seed_queries.

    Da chiamare all'inizio di ogni run_agent dei runner che fanno traffico
    esterno. Se la lista non e' vuota, il runner deve abortire con error
    esplicativo (vedi block_runner_if_seeds_blocked()).
    """
    return [u for u in (seed_queries or []) if u and is_blocked(u)]
