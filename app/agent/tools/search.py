"""Web search via DuckDuckGo (libreria ddgs, no API key)."""
from __future__ import annotations

import asyncio
from typing import Any

from ddgs import DDGS


def _domain_of(url: str) -> str:
    try:
        from urllib.parse import urlparse
        host = urlparse(url).hostname or ""
        return host.lower()
    except Exception:
        return ""


def _allowed(url: str, allowed: list[str], blocked: list[str]) -> bool:
    host = _domain_of(url)
    if not host:
        return False
    if blocked and any(host == d or host.endswith("." + d) for d in blocked):
        return False
    if allowed and not any(host == d or host.endswith("." + d) for d in allowed):
        return False
    return True


def _search_sync(query: str, max_results: int) -> list[dict[str, Any]]:
    with DDGS() as ddgs:
        results = list(ddgs.text(query, max_results=max_results))
    out = []
    for r in results:
        out.append(
            {
                "title": r.get("title") or "",
                "url": r.get("href") or r.get("url") or "",
                "snippet": r.get("body") or r.get("snippet") or "",
            }
        )
    return out


async def web_search(
    query: str,
    max_results: int = 8,
    allowed_domains: list[str] | None = None,
    blocked_domains: list[str] | None = None,
) -> list[dict[str, Any]]:
    raw = await asyncio.to_thread(_search_sync, query, max(1, min(int(max_results), 20)))
    if allowed_domains or blocked_domains:
        raw = [r for r in raw if _allowed(r["url"], allowed_domains or [], blocked_domains or [])]
    return raw
