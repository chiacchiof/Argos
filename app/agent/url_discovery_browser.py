"""URL discovery via browser headless (Playwright) per siti infinite-scroll.

Usato come tool da `site_explorer` quando un fetch HTTP mostra pochi target ma
l'objective dichiara "tutti" / "centinaia" / il sito ha indicatori JS-load.
Risolve il limite strutturale del runner HTTP-only: vede solo il "first paint"
del DOM, perde tutto quello che viene caricato via scroll/AJAX.

Strategia:
1. Apre l'URL in Chromium headless via Playwright
2. Gestisce cookie banner / verifica età (click su pattern noti)
3. Scrolla N volte aspettando T secondi (per dare tempo al JS di caricare)
4. Raccoglie TUTTI gli href del DOM finale
5. Filtra per dominio + pattern hint (regex o substring)
6. Ritorna la lista deduplicata

NON usa LLM: navigation puramente deterministica. Costo: 0 token, ~10-30s di
compute locale per 200-500 URL raccolti.

Generalista: funziona per qualunque sito infinite-scroll (camgirl, e-commerce,
news feed, social, directory).
"""
from __future__ import annotations

import asyncio
import logging
import re
from typing import Any
from urllib.parse import urljoin, urlparse

from .runner_bulk_extract import _registrable_domain


log = logging.getLogger(__name__)


# Cap di sicurezza
_MAX_SCROLLS = 100
_MAX_URLS = 2000
_SCROLL_DELAY_S = 1.5
_PAGE_TIMEOUT_S = 30
_TOTAL_BUDGET_S = 180  # max 3 min totali per discovery

# Selettori comuni per cookie banner / verifica età (case-insensitive, locale-agnostic)
_DISMISS_BUTTON_TEXTS = (
    # Italiano
    "accetta", "accetta tutti", "accetta tutto", "ok", "ho capito", "continua",
    "sono maggiorenne", "ho 18 anni", "conferma", "consenti tutto", "chiudi",
    # English
    "accept", "accept all", "i agree", "agree", "continue", "got it",
    "i am 18", "yes, i am 18", "enter", "close",
    # Spanish / French / German (basics)
    "aceptar", "j'accepte", "akzeptieren",
)


async def _try_dismiss_overlay(page) -> int:
    """Tenta di chiudere cookie banner / age gate cliccando bottoni con testo
    noto. Ritorna n. di click eseguiti."""
    n_clicks = 0
    for text in _DISMISS_BUTTON_TEXTS:
        try:
            # Cerca bottoni / link con testo case-insensitive
            locator = page.get_by_role("button", name=re.compile(rf"^\s*{re.escape(text)}\s*$", re.IGNORECASE))
            count = await locator.count()
            if count > 0:
                await locator.first.click(timeout=2000)
                n_clicks += 1
                await asyncio.sleep(0.5)
                continue
        except Exception:
            pass
        # Fallback: link/elementi cliccabili con testo
        try:
            locator = page.locator(f"a:has-text(\"{text}\"), button:has-text(\"{text}\")").first
            if await locator.count() > 0:
                await locator.click(timeout=2000)
                n_clicks += 1
                await asyncio.sleep(0.5)
        except Exception:
            pass
    return n_clicks


def _filter_urls(
    raw_urls: list[str],
    base_url: str,
    seed_reg_domain: str,
    pattern_hint: str | None,
) -> list[str]:
    """Filtra URL: stesso dominio registrabile, http(s), match pattern_hint."""
    seen: set[str] = set()
    out: list[str] = []
    pattern_re = None
    if pattern_hint:
        try:
            pattern_re = re.compile(pattern_hint, re.IGNORECASE)
        except re.error:
            # Se non e' regex valida, usa come substring
            pattern_re = None

    for raw in raw_urls:
        if not raw:
            continue
        try:
            absolute = urljoin(base_url, raw)
            if not absolute.startswith(("http://", "https://")):
                continue
            host = (urlparse(absolute).hostname or "").lower()
            if not host:
                continue
            if _registrable_domain(host) != seed_reg_domain:
                continue
            if pattern_re:
                if not pattern_re.search(absolute):
                    continue
            elif pattern_hint:
                if pattern_hint.lower() not in absolute.lower():
                    continue
            if absolute not in seen:
                seen.add(absolute)
                out.append(absolute)
                if len(out) >= _MAX_URLS:
                    break
        except Exception:
            continue
    return out


async def discover_urls_via_scroll(
    *,
    url: str,
    scrolls: int = 20,
    pattern_hint: str | None = None,
    seed_reg_domain: str | None = None,
) -> dict[str, Any]:
    """Apri url in browser headless, scrolla N volte, raccogli tutti gli href.

    Args:
        url: URL della pagina di partenza (tipicamente listing con infinite scroll)
        scrolls: numero di scroll. Default 20. Cap a 100.
        pattern_hint: regex o substring per filtrare URL raccolti. Es. 'mondocamgirls.com/' per sub-domain.
        seed_reg_domain: dominio registrabile (auto-detected dal url se None).

    Returns:
        {
          "ok": bool,
          "url": str,
          "scrolls_done": int,
          "n_urls_total": int,
          "n_urls_filtered": int,
          "urls": [...],  # cap 2000
          "error": str | None,
        }
    """
    scrolls = max(1, min(int(scrolls or 20), _MAX_SCROLLS))
    if not url or not url.startswith(("http://", "https://")):
        return {"ok": False, "error": "URL non valido"}

    if seed_reg_domain is None:
        try:
            host = (urlparse(url).hostname or "").lower()
            seed_reg_domain = _registrable_domain(host)
        except Exception:
            return {"ok": False, "error": "impossibile determinare il dominio"}

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return {
            "ok": False,
            "error": (
                "Playwright non installato. Esegui `playwright install chromium` "
                "dopo aver installato il pacchetto. Senza Playwright il tool "
                "discover_via_browser non e' disponibile."
            ),
        }

    # Stealth (mascheramento marker automazione): se disponibile, applichiamo
    # patches che fanno apparire il browser come Chrome reale (no navigator.webdriver,
    # canvas/WebGL spoofing, plugins, ecc.). Bypassa molti anti-bot livello base/medio.
    try:
        from playwright_stealth import Stealth as _Stealth
        _stealth = _Stealth()
        _stealth_available = True
    except ImportError:
        _stealth = None
        _stealth_available = False

    # Wrappiamo Playwright SYNC dentro asyncio.to_thread per evitare problemi di
    # event loop policy su Windows (la versione async richiederebbe ProactorEventLoop
    # che non e' il default di asyncio.run / loop FastAPI).
    def _do_discovery() -> dict[str, Any]:
        raw_urls_local: list[str] = []
        scrolls_done_local = 0
        error_local: str | None = None
        try:
            with sync_playwright() as p:
                # Wrap p con stealth se disponibile (intercetta tutte le pages
                # create da p.chromium.launch(...))
                p_ctx = p
                if _stealth_available:
                    try:
                        p_ctx = _stealth.use_sync(p)
                    except Exception as _e:
                        log.debug("stealth wrap fallito: %s", _e)
                        p_ctx = p
                browser = p_ctx.chromium.launch(headless=True)
                try:
                    context = browser.new_context(
                        user_agent=(
                            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                            "AppleWebKit/537.36 (KHTML, like Gecko) "
                            "Chrome/120.0 Safari/537.36"
                        ),
                        viewport={"width": 1280, "height": 900},
                    )
                    page = context.new_page()
                    page.set_default_timeout(_PAGE_TIMEOUT_S * 1000)
                    page.goto(url, wait_until="domcontentloaded")
                    # wait for networkidle per dare tempo a JS di completare render
                    # (fix bug yield basso vs httpx)
                    try:
                        page.wait_for_load_state("networkidle", timeout=8000)
                    except Exception:
                        pass
                    # Dismiss overlay (cookie/age gate). Pattern compatto: per ogni
                    # testo noto, prova a cliccare il primo bottone/link che ne contiene.
                    import time as _time
                    for text in _DISMISS_BUTTON_TEXTS:
                        try:
                            loc = page.locator(
                                f"button:has-text(\"{text}\"), a:has-text(\"{text}\")"
                            ).first
                            if loc.count() > 0:
                                loc.click(timeout=2000)
                                _time.sleep(0.4)
                        except Exception:
                            pass
                    _time.sleep(1.5)
                    for i in range(scrolls):
                        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                        scrolls_done_local = i + 1
                        _time.sleep(_SCROLL_DELAY_S)
                    raw_urls_local = page.evaluate(
                        "Array.from(document.querySelectorAll('a[href]')).map(a => a.getAttribute('href'))"
                    )
                finally:
                    browser.close()
        except Exception as e:
            error_local = f"{type(e).__name__}: {e}"
        return {
            "raw_urls": raw_urls_local,
            "scrolls_done": scrolls_done_local,
            "error": error_local,
        }

    error_msg: str | None = None
    raw_urls: list[str] = []
    scrolls_done = 0
    try:
        result = await asyncio.wait_for(
            asyncio.to_thread(_do_discovery), timeout=_TOTAL_BUDGET_S
        )
        raw_urls = result.get("raw_urls") or []
        scrolls_done = result.get("scrolls_done") or 0
        if result.get("error"):
            return {"ok": False, "error": result["error"]}
    except asyncio.TimeoutError:
        error_msg = f"discovery timeout dopo {_TOTAL_BUDGET_S}s — usato output parziale"
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    filtered = _filter_urls(raw_urls or [], url, seed_reg_domain, pattern_hint)
    return {
        "ok": True,
        "url": url,
        "scrolls_done": scrolls_done,
        "n_urls_total": len(raw_urls or []),
        "n_urls_filtered": len(filtered),
        "urls": filtered,
        "error": error_msg,
    }
