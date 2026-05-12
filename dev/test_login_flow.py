"""Test standalone del browser stealth setup — verifica se il nostro setup
(patchright + playwright-stealth + Chrome120 UA + viewport realistic) viene
flaggato come bot da siti di detection.

Run con: .venv/Scripts/python.exe dev/test_login_flow.py

NON tocca account reali. Verifica due cose:

1. **bot.sannysoft.com** (sito open-source di anti-bot detection)
   - WebDriver: should be FALSE
   - Chrome: should be present
   - Permissions: should be denied
   - Plugins length: should be > 0
   - Languages: should be set
   - WebGL Vendor: should NOT be empty/Google SwiftShader (indicatore headless)

2. **arh.antoinevastel.com/bots/areyouheadless** (un altro test simile)

Se passa entrambi, il setup e' adeguato per Instagram/TikTok base (livello
bot detection commerciale standard). Non garantisce Cloudflare Enterprise.

Modalita':
- --patchright: usa patchright (default ON, fallback se non installato)
- --headed: browser visibile (default ON)
- --proxy URL: passa un proxy specifico
"""
import argparse
import asyncio
import logging
import sys
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger(__name__)


# Import dal modulo dev (no `app/` dependency)
sys.path.insert(0, str(Path(__file__).parent / "social_outreach"))


TEST_URLS = [
    "https://bot.sannysoft.com",
    "https://arh.antoinevastel.com/bots/areyouheadless",
]


async def _import_pw(use_patchright: bool):
    if use_patchright:
        try:
            from patchright.async_api import async_playwright
            return async_playwright, "patchright"
        except ImportError:
            log.warning("patchright non installato → fallback playwright")
    from playwright.async_api import async_playwright
    return async_playwright, "playwright"


async def run_test(use_patchright: bool, headed: bool, proxy_url: str | None) -> int:
    ap_fn, backend = await _import_pw(use_patchright)
    log.info("Backend: %s", backend)
    log.info("Headed: %s", headed)
    log.info("Proxy: %s", proxy_url or "none")

    async with ap_fn() as p:
        ctx_kwargs: dict = {
            "viewport": {"width": 1280, "height": 800},
            "user_agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            ),
            "locale": "en-US",
            "timezone_id": "Europe/Rome",
        }
        if proxy_url:
            ctx_kwargs["proxy"] = {"server": proxy_url}

        browser = await p.chromium.launch(
            headless=not headed,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-default-browser-check",
                "--disable-features=IsolateOrigins,site-per-process",
            ],
        )
        context = await browser.new_context(**ctx_kwargs)

        # Applica stealth (Patchright fa molto da solo, ma stealth aggiunge ulteriori patches)
        try:
            from playwright_stealth import Stealth
            stealth = Stealth()
            await stealth.apply_stealth_async(context)
            log.info("playwright-stealth applicato")
        except Exception as e:
            log.warning("stealth apply failed: %s", e)

        page = await context.new_page()

        for url in TEST_URLS:
            log.info("---")
            log.info("Testing: %s", url)
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=30000)
                await asyncio.sleep(3)  # lascia che JS detection scriva risultati
                # Dump del body text (anti-bot tipici mettono i risultati come testo)
                body_text = await page.evaluate("document.body.innerText")
                # Print solo le prime 2000 char
                snippet = body_text[:2000].replace("\n", " | ")
                log.info("Body snippet: %s", snippet)

                # Heuristic flag detection nel testo del body
                bad_signals = [
                    "WebDriver: present",
                    "WebDriver (New): present",
                    "Headless: present",
                    "You are headless",
                    "You are a bot",
                ]
                good_signals = [
                    "WebDriver: missing",
                    "WebDriver: not present",
                    "Headless: missing",
                    "You are not headless",
                    "Plugins length: 5",
                ]
                bad = [s for s in bad_signals if s.lower() in body_text.lower()]
                good = [s for s in good_signals if s.lower() in body_text.lower()]
                log.info("  good signals matched: %s", good or "(none)")
                log.info("  bad signals matched: %s", bad or "(none)")

                # Screenshot per ispezione manuale
                screenshot_path = Path(__file__).parent / f"stealth_test_{url.replace('https://', '').replace('/', '_')}.png"
                await page.screenshot(path=str(screenshot_path), full_page=True)
                log.info("Screenshot: %s", screenshot_path)
            except Exception as e:
                log.exception("Errore su %s: %s", url, e)

        if headed:
            log.info("\n👀 Browser headed: ispeziona visualmente. Premi Enter qui per chiudere.")
            try:
                input()
            except (EOFError, KeyboardInterrupt):
                pass

        await context.close()
        await browser.close()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-patchright", action="store_true", help="usa playwright standard invece di patchright")
    parser.add_argument("--headless", action="store_true", help="modalita' headless (sconsigliato)")
    parser.add_argument("--proxy", default=None, help="proxy URL es. http://user:pass@host:port")
    args = parser.parse_args()

    return asyncio.run(run_test(
        use_patchright=not args.no_patchright,
        headed=not args.headless,
        proxy_url=args.proxy,
    ))


if __name__ == "__main__":
    sys.exit(main())
