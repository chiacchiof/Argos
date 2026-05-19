"""TikTok outreach implementation.

⚠️ FRAGILE: TikTok aggiorna i selettori spesso e ha challenge piu' aggressivi
di Instagram. Aspettarsi manutenzione frequente.

Note specifiche TikTok:
- DM disponibili da `https://www.tiktok.com/messages` o cliccando l'icona
  paper-airplane sul profilo.
- Per inviare DM serve essere FOLLOWERS o avere "Everyone" abilitato sui DM
  del target (default e' "Friends only" → potresti non riuscire a scrivere
  a profili senza follow reciproco).
- Login spesso richiede verifica via puzzle slider (DataDome captcha).

Strategia DM analoga a Instagram (warmup → goto profile → click message → type → send),
ma con selettori TikTok-specifici.
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


class TikTok(SocialPlatform):
    name = "tiktok"
    login_url = "https://www.tiktok.com/login/phone-or-email/email"
    home_url = "https://www.tiktok.com/"

    async def login(self, page: "Page", account: SocialAccount) -> HealthStatus:
        await page.goto(self.home_url, wait_until="domcontentloaded")
        await human_wait(2, 5)
        status = await self.check_health(page)
        if status == HealthStatus.OK:
            log.info("tiktok login: session valid for %s", account.username)
            return status
        if status != HealthStatus.LOGGED_OUT:
            return status
        log.info("tiktok fresh login for %s", account.username)
        await page.goto(self.login_url, wait_until="domcontentloaded")
        await human_wait(2, 4)
        await self.try_dismiss_cookie_banner(page)
        try:
            await human_type(page, "input[name='username']", account.username)
            await human_wait(0.8, 2)
            await human_type(page, "input[type='password']", account.password)
            await human_wait(0.8, 2)
            await human_click(page, "button[type='submit']")
            await human_wait(5, 10)
        except Exception as e:
            log.error("tiktok login form fail: %s", e)
            return HealthStatus.UNKNOWN
        return await self.check_health(page)

    async def goto_profile(self, page: "Page", username: str) -> bool:
        target = f"https://www.tiktok.com/@{username.lstrip('@')}"
        await page.goto(target, wait_until="domcontentloaded")
        await human_wait(2, 5)
        try:
            # TikTok mostra l'username nel <h1> o data-e2e="user-page"
            await page.locator("[data-e2e='user-title']").first.wait_for(timeout=5000)
            return True
        except Exception:
            return f"@{username.lstrip('@')}" in page.url

    async def warmup_browse(self, page: "Page", minutes: float = 5.0) -> None:
        import time

        await page.goto(self.home_url, wait_until="domcontentloaded")
        await human_wait(3, 6)
        end_at = time.time() + minutes * 60
        while time.time() < end_at:
            action = random.choice(["scroll", "scroll", "watch", "hover"])
            if action == "scroll":
                await human_scroll(page, n=random.randint(1, 3))
            elif action == "watch":
                # Lascia il video andare 5-15s (TikTok ha autoplay)
                await human_wait(5, 15)
            elif action == "hover":
                await random_idle_action(page)
            await human_wait(2, 5)

    async def send_dm(
        self,
        page: "Page",
        username: str,
        message: str,
        *,
        speed_profile: str | None = None,
    ) -> DMResult:
        ok_profile = await self.goto_profile(page, username)
        if not ok_profile:
            return DMResult(ok=False, reason="profile_not_found", target_username=username)
        await human_scroll(page, n=1)
        await human_wait(3, 7, profile=speed_profile)
        try:
            # TikTok mostra icona paper-airplane (Message) sul profile header
            msg_sels = [
                "[data-e2e='message-button']",
                "button:has-text('Message')",
                "button:has-text('Messaggio')",
            ]
            clicked = False
            for sel in msg_sels:
                try:
                    if await page.locator(sel).first.is_visible(timeout=2000):
                        await human_click(page, sel)
                        clicked = True
                        break
                except Exception:
                    continue
            if not clicked:
                return DMResult(ok=False, reason="message_button_not_found", target_username=username)
            await human_wait(3, 6, profile=speed_profile)
            input_sels = [
                "div[contenteditable='true']",
                "textarea[placeholder*='message']",
                "textarea[placeholder*='Messaggio']",
            ]
            typed = False
            for sel in input_sels:
                try:
                    if await page.locator(sel).first.is_visible(timeout=2000):
                        await human_type(page, sel, message, profile=speed_profile)
                        typed = True
                        break
                except Exception:
                    continue
            if not typed:
                return DMResult(ok=False, reason="dm_input_not_found", target_username=username)
            await human_wait(1, 3, profile=speed_profile)
            # Send: cerca button send, fallback Enter
            send_sels = [
                "[data-e2e='message-send']",
                "button[type='submit']",
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
                await page.keyboard.press("Enter")
            await human_wait(4, 7)
            return DMResult(ok=True, target_username=username, health=HealthStatus.OK)
        except Exception as e:
            return DMResult(ok=False, reason=f"exception: {type(e).__name__}: {e}", target_username=username)

    async def check_health(self, page: "Page") -> HealthStatus:
        url = page.url
        if "/login" in url:
            return HealthStatus.LOGGED_OUT
        if "/captcha" in url or "/verify" in url:
            return HealthStatus.CHALLENGED
        try:
            for marker, status in (
                ("Too many attempts", HealthStatus.RATE_LIMITED),
                ("Try again later", HealthStatus.RATE_LIMITED),
                ("Account has been banned", HealthStatus.BANNED),
                ("Account suspended", HealthStatus.BANNED),
            ):
                if await page.locator(f"text=\"{marker}\"").first.is_visible(timeout=500):
                    return status
        except Exception:
            pass
        return HealthStatus.OK
