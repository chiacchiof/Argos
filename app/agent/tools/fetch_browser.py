"""Fallback Playwright (lazy-import). Usato solo se fetch_http è insufficiente."""
from __future__ import annotations

from .fetch_http import FetchResult, _extract_main_text


async def fetch_browser(url: str, max_chars: int = 12000, wait_ms: int = 2000) -> FetchResult:
    try:
        from playwright.async_api import async_playwright  # type: ignore
    except ImportError:
        return FetchResult(
            url,
            0,
            "Playwright non installato. Installa con: pip install -e .[browser] && playwright install chromium",
            needs_browser=False,
        )

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            ctx = await browser.new_context()
            page = await ctx.new_page()
            resp = await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_timeout(wait_ms)
            html = await page.content()
            status = resp.status if resp else 0
            await browser.close()
        title, body = _extract_main_text(html)
        return FetchResult(url, status, body[:max_chars], title=title, needs_browser=False)
    except Exception as e:
        return FetchResult(url, 0, f"errore Playwright: {e}", needs_browser=False)
