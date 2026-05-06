"""Fetch HTTP + estrazione testo principale tramite readability-lxml."""
from __future__ import annotations

import re

import httpx
from readability import Document
from selectolax.parser import HTMLParser

from ...config import settings


class FetchResult:
    def __init__(self, url: str, status: int, text: str, title: str = "", needs_browser: bool = False):
        self.url = url
        self.status = status
        self.text = text
        self.title = title
        self.needs_browser = needs_browser

    def to_dict(self) -> dict:
        return {
            "url": self.url,
            "status": self.status,
            "title": self.title,
            "text": self.text,
            "needs_browser": self.needs_browser,
        }


_WS = re.compile(r"\s+")


def _clean(text: str) -> str:
    return _WS.sub(" ", text).strip()


def _extract_main_text(html: str) -> tuple[str, str]:
    try:
        doc = Document(html)
        title = (doc.short_title() or "").strip()
        summary_html = doc.summary(html_partial=True)
        body_text = HTMLParser(summary_html).body.text(separator="\n", strip=True) if summary_html else ""
    except Exception:
        title = ""
        body_text = ""

    if not body_text:
        # fallback: tutto il body
        try:
            body_text = HTMLParser(html).body.text(separator="\n", strip=True)
        except Exception:
            body_text = ""

    # collassa whitespace ma preserva paragrafi
    lines = [_clean(line) for line in body_text.splitlines()]
    return title, "\n".join(line for line in lines if line)


async def fetch_http(url: str, max_chars: int = 12000) -> FetchResult:
    headers = {"User-Agent": settings.http_user_agent}
    try:
        async with httpx.AsyncClient(
            timeout=settings.http_timeout, follow_redirects=True, headers=headers
        ) as client:
            r = await client.get(url)
        status = r.status_code
        ct = r.headers.get("content-type", "")
        if status >= 400:
            return FetchResult(url, status, f"HTTP {status}", needs_browser=False)
        if "html" not in ct.lower() and "xml" not in ct.lower():
            # plain text / json / pdf: ritorna i primi N caratteri grezzi
            body = r.text[:max_chars]
            return FetchResult(url, status, body, title="", needs_browser=False)

        title, body = _extract_main_text(r.text)
        if len(body) < 200:
            return FetchResult(url, status, body, title=title, needs_browser=True)
        return FetchResult(url, status, body[:max_chars], title=title, needs_browser=False)
    except httpx.HTTPError as e:
        return FetchResult(url, 0, f"errore di rete: {e}", needs_browser=False)
