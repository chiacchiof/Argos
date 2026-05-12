"""Engine principale outreach social: orchestra account pool + platform + stealth.

Da chiamare dal futuro runner `outreach_social.run_agent(task, job_id)`.
Per ora e' un modulo standalone con API auto-contenuta:

    engine = OutreachEngine(accounts=[...], proxies=[...])
    await engine.run_dm(
        platform="instagram",
        target_username="creator123",
        message="Ciao Sara, ho visto il tuo profilo...",
    )

Riusa playwright + patchright + stealth. La modalita' headed (no headless) e'
caldamente raccomandata per evitare detection: vedi parametro `headed=True`.
"""
from __future__ import annotations

import asyncio
import logging
import random
from typing import TYPE_CHECKING, Callable

from .account_pool import AccountPool
from .humanize import (
    human_wait,
    idle_session,
    is_active_hour,
    random_gap_between_dms_min,
    random_session_duration_min,
)
from .facebook import Facebook
from .instagram import Instagram
from .platform_base import DMResult, HealthStatus, SocialAccount, SocialPlatform
from .proxy_pool import assign_proxy_to_account, proxy_to_playwright_kwargs
from .session_manager import load_session_state, save_session
from .tiktok import TikTok

if TYPE_CHECKING:
    pass

log = logging.getLogger(__name__)


PLATFORMS: dict[str, type[SocialPlatform]] = {
    "instagram": Instagram,
    "tiktok": TikTok,
    "facebook": Facebook,
}


class OutreachEngine:
    def __init__(
        self,
        accounts: list[SocialAccount],
        *,
        headed: bool = True,
        use_patchright: bool = True,
    ):
        self.pool = AccountPool(accounts)
        self.headed = headed
        self.use_patchright = use_patchright

    def _import_playwright(self):
        """Sceglie tra patchright e playwright standard a runtime.

        Patchright e' un drop-in replacement con anti-detection migliorato.
        Se non disponibile, fallback su playwright + playwright-stealth.
        """
        if self.use_patchright:
            try:
                from patchright.async_api import async_playwright
                return async_playwright, "patchright"
            except ImportError:
                log.warning("patchright non disponibile, fallback su playwright standard")
        from playwright.async_api import async_playwright
        return async_playwright, "playwright"

    async def _open_browser_for_account(self, account: SocialAccount, p):
        """Apre browser + context per un account con proxy + session restore + stealth."""
        proxy_cfg = assign_proxy_to_account(account.uuid)
        proxy_kwargs = proxy_to_playwright_kwargs(proxy_cfg)
        browser = await p.chromium.launch(
            headless=not self.headed,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-default-browser-check",
            ],
        )
        ctx_kwargs = {
            "viewport": {"width": 1280, "height": 800},
            "user_agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            ),
            "locale": "en-US",
            "timezone_id": "Europe/Rome",
        }
        ctx_kwargs.update(proxy_kwargs)
        state = load_session_state(account.uuid)
        if state:
            ctx_kwargs["storage_state"] = state
        context = await browser.new_context(**ctx_kwargs)

        # Applica playwright-stealth SOLO se NON stiamo già usando patchright.
        # Conflitto verificato 2026-05-12: applicare playwright_stealth sopra
        # patchright rompe la risoluzione DNS (ERR_NAME_NOT_RESOLVED) — i due
        # set di patch stealth si pestano i piedi. Patchright e' gia' stealth
        # di suo, quindi quando e' attivo lo lasciamo da solo.
        if not self.use_patchright:
            try:
                from playwright_stealth import Stealth
                stealth = Stealth()
                await stealth.apply_stealth_async(context)
            except Exception as e:
                log.debug("stealth apply failed: %s", e)

        page = await context.new_page()
        return browser, context, page

    def get_platform(self, name: str) -> SocialPlatform:
        cls = PLATFORMS.get(name)
        if cls is None:
            raise ValueError(f"Piattaforma non supportata: {name}")
        return cls()

    async def run_session(
        self,
        platform_name: str,
        targets: list[tuple[str, str]],  # [(username, message), ...]
        *,
        warmup_min: float = 5.0,
        max_dms_per_session: int = 5,
        jlog: "Callable[[str], None] | None" = None,
    ) -> list[DMResult]:
        """Una sessione = login → warmup → N DM con gap umani → close.

        Acquisisce 1 account dal pool, esegue gli N DM, rilascia.
        `jlog`, se passato, riceve i log-eventi chiave (login fail, off-hours,
        warmup, DM esito) — utile per propagare alla job-log del runner.
        """
        def _say(line: str) -> None:
            if jlog:
                try:
                    jlog(line)
                except Exception:
                    pass

        if not is_active_hour():
            log.warning("Fuori da active hours (9-22). Sessione skip.")
            _say("Fuori da active hours (9-22 locale). Sessione saltata.")
            return []

        rt = self.pool.acquire_next(platform=platform_name)
        if rt is None:
            log.warning("Nessun account disponibile per %s", platform_name)
            _say(f"Nessun account '{platform_name}' disponibile dal pool (cap raggiunto o quarantine).")
            return []

        platform = self.get_platform(platform_name)
        results: list[DMResult] = []
        ap, _backend = self._import_playwright()

        _say(f"Apertura browser per account '{rt.account.username}' (headed={self.headed}, patchright={self.use_patchright})")
        try:
            async with ap() as p:
                browser, context, page = await self._open_browser_for_account(rt.account, p)
                try:
                    # Login / restore session
                    _say(f"Login {platform_name} per {rt.account.username}...")
                    health = await platform.login(page, rt.account)
                    if health != HealthStatus.OK:
                        log.error(
                            "Login fail [%s/%s]: %s",
                            platform_name, rt.account.username, health.value,
                        )
                        _say(f"❌ Login {platform_name}/{rt.account.username} fail: {health.value}")
                        # Diagnostic snapshot: URL, body snippet, screenshot in
                        # data/sessions/login_fail_<uuid>_<ts>.png. Senza questo
                        # un fail "logged_out" non lascia traccia di cosa FB
                        # abbia mostrato (2FA inline, captcha, password errata).
                        try:
                            cur_url = page.url
                            _say(f"  ↳ URL al fail: {cur_url}")
                            try:
                                title = await page.title()
                                _say(f"  ↳ title: {title!r}")
                            except Exception:
                                pass
                            try:
                                snippet = await page.evaluate(
                                    "document.body && document.body.innerText"
                                    " ? document.body.innerText.slice(0, 400) : ''"
                                )
                                snippet = (snippet or "").replace("\n", " | ").strip()
                                if snippet:
                                    _say(f"  ↳ body: {snippet[:300]}")
                            except Exception:
                                pass
                            try:
                                from datetime import datetime, timezone
                                from pathlib import Path
                                ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
                                shot_dir = Path("data/sessions")
                                shot_dir.mkdir(parents=True, exist_ok=True)
                                shot_path = shot_dir / f"login_fail_{rt.account.uuid}_{ts}.png"
                                await page.screenshot(path=str(shot_path), full_page=False)
                                _say(f"  ↳ screenshot: {shot_path}")
                            except Exception as e:
                                _say(f"  ↳ screenshot fail: {e}")
                        except Exception as e:
                            _say(f"  ↳ diagnostic snapshot crash: {e}")
                        self.pool.release(rt.account.uuid, dm_sent=False, health=health)
                        return results
                    # Save session post-login (gli account in stato "logged in" sono valuable)
                    _say("Login OK — sessione salvata.")
                    await save_session(context, rt.account.uuid)

                    # Warmup
                    log.info("[%s/%s] warmup browse %d min", platform_name, rt.account.username, warmup_min)
                    _say(f"Warmup browse ~{warmup_min} min...")
                    await platform.warmup_browse(page, minutes=warmup_min)

                    # Loop DM
                    n_to_send = min(len(targets), max_dms_per_session, rt.account.daily_dm_cap - rt.dms_today)
                    _say(f"Pronto a inviare {n_to_send} DM (cap restante={rt.account.daily_dm_cap - rt.dms_today})")
                    for i, (username, message) in enumerate(targets[:n_to_send]):
                        log.info("[%s/%s] DM %d/%d -> %s",
                                 platform_name, rt.account.username, i + 1, n_to_send, username)
                        _say(f"DM {i+1}/{n_to_send} → @{username}")
                        result = await platform.send_dm(page, username, message)
                        results.append(result)
                        rt.dms_today += 1 if result.ok else 0
                        if result.ok:
                            _say(f"  ✅ inviato a @{username}")
                        else:
                            _say(f"  ❌ fail a @{username}: {result.reason or '(no reason)'}")
                        # Health check periodico
                        health_now = await platform.check_health(page)
                        if health_now != HealthStatus.OK:
                            log.warning("[%s/%s] health degradata: %s — interrompo sessione",
                                        platform_name, rt.account.username, health_now.value)
                            _say(f"⚠️ health degradata ({health_now.value}) — interrompo sessione")
                            self.pool.release(rt.account.uuid, dm_sent=result.ok, health=health_now)
                            return results
                        # Random gap human-like (compresso a min se siamo a fine sessione)
                        if i < n_to_send - 1:
                            gap_min = random_gap_between_dms_min()
                            log.debug("Gap %.1f min...", gap_min)
                            await asyncio.sleep(gap_min * 60)
                    # Save session aggiornata
                    await save_session(context, rt.account.uuid)
                finally:
                    await context.close()
                    await browser.close()
        except Exception as e:
            log.exception("Engine session crash: %s", e)
            _say(f"💥 engine crash: {type(e).__name__}: {e}")
            self.pool.release(rt.account.uuid, dm_sent=False, health=HealthStatus.UNKNOWN)
            return results

        self.pool.release(rt.account.uuid, dm_sent=any(r.ok for r in results), health=HealthStatus.OK)
        return results
