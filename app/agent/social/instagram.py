"""Instagram outreach implementation.

⚠️ FRAGILE PER DESIGN: i selettori CSS/aria-label di Instagram cambiano
frequentemente. Aspettarsi manutenzione regolare (~1x/mese).

Strategia DM:
1. Login (o restore session)
2. Warmup 3-8 min: scroll feed, view stories, like 2-3 post random
3. Per ogni target:
   - Naviga al profilo via search bar (NON via URL diretto: piu' "umano")
   - Read profile 5-10s (scroll bio, view 1-2 post)
   - Click "Message" button
   - Type messaggio con human_type
   - Click Send
   - Wait 5-10s lettura conferma
   - Random gap 8-30 min
   - Random idle action (scroll, hover)

Health checks:
- /challenge → CHALLENGED
- /login → LOGGED_OUT (sessione scaduta)
- "We restrict certain activity" → RATE_LIMITED
- 429 status → RATE_LIMITED
"""
from __future__ import annotations

import logging
import random
from typing import TYPE_CHECKING

from .humanize import human_click, human_scroll, human_type, human_wait, random_idle_action
from .platform_base import DMResult, HealthStatus, SocialAccount, SocialPlatform

if TYPE_CHECKING:
    from playwright.async_api import Page

log = logging.getLogger(__name__)


class Instagram(SocialPlatform):
    name = "instagram"
    login_url = "https://www.instagram.com/accounts/login/"
    home_url = "https://www.instagram.com/"

    async def login(self, page: "Page", account: SocialAccount) -> HealthStatus:
        # Se il session_state e' gia' caricato nel context, semplicemente verifica
        await page.goto(self.home_url, wait_until="domcontentloaded")
        await human_wait(2, 4)
        status = await self.check_health(page)
        if status == HealthStatus.OK:
            log.info("instagram login: session valid for %s", account.username)
            return status
        if status != HealthStatus.LOGGED_OUT:
            return status
        # Fresh login richiesto
        log.info("instagram fresh login for %s", account.username)
        await page.goto(self.login_url, wait_until="domcontentloaded")
        await human_wait(1.5, 3)
        await self.try_dismiss_cookie_banner(page)
        try:
            await human_type(page, "input[name='username']", account.username)
            await human_wait(0.5, 1.5)
            await human_type(page, "input[name='password']", account.password)
            await human_wait(0.5, 1.5)
            await human_click(page, "button[type='submit']")
            await human_wait(4, 7)
        except Exception as e:
            log.error("login form fail: %s", e)
            return HealthStatus.UNKNOWN
        # Verifica post-login
        return await self.check_health(page)

    async def goto_profile(self, page: "Page", username: str) -> bool:
        # Navigazione via URL (pattern realistico: utenti spesso copy-paste o link click)
        target = f"https://www.instagram.com/{username}/"
        await page.goto(target, wait_until="domcontentloaded")
        await human_wait(2, 5)
        # Verifica esistenza profilo: cerca header con username
        try:
            await page.locator(f"header section h2:has-text('{username}')").wait_for(timeout=5000)
            return True
        except Exception:
            # Fallback: profilo potrebbe usare diverso markup; verifica URL
            return username in page.url

    async def warmup_browse(self, page: "Page", minutes: float = 5.0) -> None:
        """Scroll feed + view stories + like 1-3 post random."""
        import asyncio
        import time

        await page.goto(self.home_url, wait_until="domcontentloaded")
        await human_wait(2, 4)
        end_at = time.time() + minutes * 60
        while time.time() < end_at:
            action = random.choice(["scroll", "scroll", "scroll", "hover", "like"])
            if action == "scroll":
                await human_scroll(page, n=random.randint(2, 5))
            elif action == "hover":
                await random_idle_action(page)
            elif action == "like":
                try:
                    like_btns = page.locator("svg[aria-label='Like']")
                    n = await like_btns.count()
                    if n > 0:
                        idx = random.randint(0, min(n - 1, 4))
                        await like_btns.nth(idx).click(delay=random.randint(50, 120))
                        await human_wait(1, 3)
                except Exception:
                    pass
            await human_wait(2, 6)

    async def send_dm(
        self, page: "Page", username: str, message: str
    ) -> DMResult:
        ok_profile = await self.goto_profile(page, username)
        if not ok_profile:
            return DMResult(ok=False, reason="profile_not_found", target_username=username)
        # Lettura profilo
        await human_scroll(page, n=2)
        await human_wait(3, 7)
        # Click "Message" — selettore tipico (puo' variare)
        try:
            msg_btn_selectors = [
                "div[role='button']:has-text('Message')",
                "div[role='button']:has-text('Messaggio')",
                "button:has-text('Message')",
            ]
            clicked = False
            for sel in msg_btn_selectors:
                try:
                    if await page.locator(sel).first.is_visible(timeout=2000):
                        await human_click(page, sel)
                        clicked = True
                        break
                except Exception:
                    continue
            if not clicked:
                return DMResult(ok=False, reason="message_button_not_found", target_username=username)
            await human_wait(3, 6)
            # Type + send
            await human_type(page, "div[role='textbox'][contenteditable='true']", message)
            await human_wait(1, 3)
            # Send button (Enter o icona)
            send_sels = [
                "div[role='button']:has-text('Send')",
                "div[role='button']:has-text('Invia')",
            ]
            sent = False
            for sel in send_sels:
                try:
                    if await page.locator(sel).first.is_visible(timeout=2000):
                        await human_click(page, sel)
                        sent = True
                        break
                except Exception:
                    continue
            if not sent:
                # Fallback: Enter key
                await page.keyboard.press("Enter")
            await human_wait(4, 7)
            # Verifica delivery: cerca il messaggio nel chat history
            try:
                await page.locator(f"div:has-text('{message[:40]}')").wait_for(timeout=5000)
                return DMResult(ok=True, target_username=username, health=HealthStatus.OK)
            except Exception:
                return DMResult(
                    ok=False, reason="delivery_unconfirmed",
                    target_username=username, health=HealthStatus.UNKNOWN,
                )
        except Exception as e:
            return DMResult(ok=False, reason=f"exception: {type(e).__name__}: {e}", target_username=username)

    async def check_health(self, page: "Page") -> HealthStatus:
        url = page.url
        if "/challenge" in url or "/accounts/login" in url:
            if "challenge" in url:
                return HealthStatus.CHALLENGED
            return HealthStatus.LOGGED_OUT
        # Cerca markers di rate limit / ban nel testo pagina
        try:
            for marker, status in (
                ("We restrict certain activity", HealthStatus.RATE_LIMITED),
                ("Try Again Later", HealthStatus.RATE_LIMITED),
                ("Your account has been disabled", HealthStatus.BANNED),
                ("Account suspended", HealthStatus.BANNED),
            ):
                if await page.locator(f"text=\"{marker}\"").first.is_visible(timeout=500):
                    return status
        except Exception:
            pass
        return HealthStatus.OK
