"""WhatsApp Web — Motore A (browser automation).

Implementa SocialPlatform per WhatsApp via Playwright. Differenze chiave dalle
piattaforme social tradizionali (IG/TikTok):

1. **Login via QR code**, non username+password. La sessione è persistita in
   `user_data_dir` (Playwright persistent context). Riapertura = login zero.
2. **Identifier = phone number** (E.164), NON username/handle. Il parametro
   `username` dei metodi della base class viene interpretato come numero.
3. **Endpoint chat diretto**: `wa.me/send?phone=<digits>` apre la chat senza
   doversi cercare un profilo.
4. **Niente warmup**: WA Web non ha feed da scrollare; la sessione è già "calda"
   se il QR è scansionato.

Strategia anti-ban (rispetto vincoli ToS):
- Rate-limit per ora (default 30) + daily cap (default 100/account)
- Pause random 30-180s tra DM
- human_type del messaggio (per-char delay)
- Skip account con `status='banned'` o `'rate_limited'`
- check_health prima di ogni invio (se rileva logout/ban → quarantine)
"""
from __future__ import annotations

import asyncio
import logging
import re
from typing import TYPE_CHECKING

from .humanize import human_click, human_type, human_wait, reading_pause
from .platform_base import (
    DMResult,
    HealthStatus,
    SocialAccount,
    SocialPlatform,
)
from .whatsapp_selectors import (
    CHAT_HEADER,
    CHAT_LIST,
    CHECKMARK_SENT,
    LOGOUT_OR_BAN,
    MESSAGE_INPUT,
    MSG_FAILED,
    QR_CANVAS,
    SEND_BUTTON,
    SEND_PHONE_INVALID,
    SESSION_EXPIRED,
    USE_WEB_BUTTON,
    WA_SEND_URL,
    WA_WEB_URL,
)

if TYPE_CHECKING:
    from playwright.async_api import Page


log = logging.getLogger(__name__)


# Timeout login QR: 2 minuti (richiede scan dal telefono fisico).
LOGIN_QR_TIMEOUT_S = 120

# Timeout caricamento chat dopo navigate /send: 30s
CHAT_LOAD_TIMEOUT_S = 30

# Timeout post-invio per detectare spunta sent: 10s
DELIVERY_CONFIRM_TIMEOUT_S = 10


def _normalize_phone(raw: str) -> str:
    """Estrae solo cifre da un numero (rimuove +, spazi, trattini, parentesi).

    WA endpoint /send vuole solo cifre, senza prefisso '+'. Es:
    '+39 333 1234567' → '393331234567'.
    """
    return re.sub(r"\D", "", raw or "")


async def _first_visible(page: "Page", selectors: list[str], timeout_ms: int = 1500):
    """Ritorna il primo locator visibile da una lista di selettori (in ordine).
    Restituisce None se nessuno è visibile entro timeout_ms.
    """
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            if await loc.is_visible(timeout=timeout_ms):
                return loc
        except Exception:
            continue
    return None


async def _wait_for_any(page: "Page", selectors: list[str], timeout_s: float) -> bool:
    """Aspetta che almeno uno dei selettori diventi visibile entro timeout_s.

    Polling ogni 500ms. Ritorna True se trovato, False se timeout.
    """
    import time
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        for sel in selectors:
            try:
                if await page.locator(sel).first.is_visible(timeout=300):
                    return True
            except Exception:
                continue
        await asyncio.sleep(0.5)
    return False


class WhatsAppBrowser(SocialPlatform):
    """WhatsApp Web automation engine."""

    name = "whatsapp_browser"
    login_url = WA_WEB_URL
    home_url = WA_WEB_URL

    async def login(self, page: "Page", account: SocialAccount) -> HealthStatus:
        """Apre web.whatsapp.com; se sessione già valida, ritorna OK senza QR.
        Altrimenti aspetta che l'utente scansioni il QR (timeout
        LOGIN_QR_TIMEOUT_S).

        Note:
        - `account.password` è IGNORATO per WhatsApp (auth via QR).
        - L'integrazione con la UI "mostra il QR all'utente" è nel runner,
          non qui: questa funzione solo ASPETTA che diventi loggato.
        - Dopo login successful, WhatsApp Web sincronizza le chat. Se l'account
          non si logga da diversi giorni la sync puo' richiedere fino a ~60s.
          Aspettiamo fino a SYNC_TIMEOUT_S con log progressivo (vedi screenshot
          "Caricamento delle chat in corso N%").
        """
        log.info("[wa] navigate to %s", WA_WEB_URL)
        try:
            await page.goto(WA_WEB_URL, wait_until="domcontentloaded", timeout=30_000)
        except Exception as e:
            log.warning("[wa] login: goto failed: %s", e)
            return HealthStatus.UNKNOWN

        # Caso 1: sessione persistita → CHAT_LIST appare. Timeout esteso a 60s
        # per coprire il caso "account non loggato da giorni → sync chat lenta".
        # Logga il progress della sync ("Caricamento delle chat in corso N%") ogni 5s.
        SYNC_TIMEOUT_S = 60.0
        sync_ok = await self._wait_chat_list_or_progress(page, SYNC_TIMEOUT_S)
        if sync_ok:
            log.info("[wa] login: session restored (no QR needed)")
            return HealthStatus.OK

        # Caso 2: QR canvas presente → utente deve scansionare
        if await _first_visible(page, QR_CANVAS, timeout_ms=2000):
            log.info(
                "[wa] login: QR code shown, waiting up to %ds for scan...",
                LOGIN_QR_TIMEOUT_S,
            )
            ok = await _wait_for_any(page, CHAT_LIST, timeout_s=LOGIN_QR_TIMEOUT_S)
            if ok:
                log.info("[wa] login: QR scanned, session active")
                return HealthStatus.OK
            log.warning("[wa] login: QR scan timeout (%ds elapsed)", LOGIN_QR_TIMEOUT_S)
            return HealthStatus.CHALLENGED

        # Caso 3: niente QR e niente chat-list → stato sconosciuto
        log.warning("[wa] login: neither QR nor chat-list visible after %.0fs", SYNC_TIMEOUT_S)
        return HealthStatus.UNKNOWN

    async def _wait_chat_list_or_progress(self, page: "Page", timeout_s: float) -> bool:
        """Aspetta CHAT_LIST con polling intelligente: logga il progresso quando
        WhatsApp Web sta sincronizzando le chat ("Caricamento delle chat in
        corso N%" / "Loading your chats N%"). Ritorna True se CHAT_LIST appare
        entro timeout_s, False altrimenti."""
        import time, re
        deadline = time.monotonic() + timeout_s
        last_progress_log = 0.0
        while time.monotonic() < deadline:
            # Check 1: CHAT_LIST visibile?
            for sel in CHAT_LIST:
                try:
                    if await page.locator(sel).first.is_visible(timeout=300):
                        return True
                except Exception:
                    continue
            # Check 2: log progress sync ogni ~5s
            now = time.monotonic()
            if now - last_progress_log > 5.0:
                last_progress_log = now
                try:
                    body_txt = await page.locator("body").inner_text(timeout=1000)
                    # Pattern italiano + inglese
                    if (
                        "Caricamento delle chat" in body_txt
                        or "Loading your chats" in body_txt
                        or "Loading chats" in body_txt
                    ):
                        m = re.search(r"(\d{1,3}\s*%)", body_txt)
                        pct = m.group(1) if m else "?"
                        elapsed = int(now - (deadline - timeout_s))
                        log.info(
                            "[wa] login: WhatsApp sta sincronizzando le chat (%s) — attendo (%ds/%.0fs)",
                            pct, elapsed, timeout_s,
                        )
                except Exception:
                    pass
            await asyncio.sleep(1.0)
        return False

    async def goto_profile(self, page: "Page", username: str) -> bool:
        """Per WhatsApp `username` è il numero di telefono (E.164 o solo cifre).

        Naviga a /send?phone=N. Ritorna True se la chat si apre (numero valido +
        ha WA), False se invalido / non su WhatsApp.
        """
        digits = _normalize_phone(username)
        if not digits or len(digits) < 7:
            log.warning("[wa] goto_profile: phone too short: %r", username)
            return False
        url = WA_SEND_URL.format(digits=digits)
        log.debug("[wa] goto_profile: %s", url)
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
        except Exception as e:
            log.warning("[wa] goto_profile failed: %s", e)
            return False

        # Possibile dialog "Use WhatsApp Web in browser" — click se appare
        await asyncio.sleep(1.5)
        use_web = await _first_visible(page, USE_WEB_BUTTON, timeout_ms=2000)
        if use_web:
            try:
                await use_web.click()
                await human_wait(1.0, 2.0)
            except Exception:
                pass

        # Poll: o appare l'errore "numero non valido", o appare la chat aperta
        import time
        deadline = time.monotonic() + CHAT_LOAD_TIMEOUT_S
        while time.monotonic() < deadline:
            if await _first_visible(page, SEND_PHONE_INVALID, timeout_ms=300):
                log.info("[wa] goto_profile: phone not on WhatsApp: %s", digits)
                return False
            if await _first_visible(page, CHAT_HEADER, timeout_ms=300):
                return True
            await asyncio.sleep(0.5)
        log.warning("[wa] goto_profile: chat not loaded after %ds", CHAT_LOAD_TIMEOUT_S)
        return False

    async def warmup_browse(self, page: "Page", minutes: float = 1.0) -> None:
        """WhatsApp Web NON ha un feed da browsare. Warmup = idle nella chat list
        per simulare "utente che apre WA e legge messaggi" prima di scrivere.

        Hard-cap a `minutes` REALI (clamped a max 1.0 min): usiamo `time.monotonic`
        invece di una stima per-iter, e ad ogni step controlliamo il deadline
        prima di attendere — così il warmup non sfora mai il budget richiesto.
        """
        import time
        # Hard cap a 1 minuto: niente warmup eterno su WhatsApp Web.
        budget_s = max(0.0, min(float(minutes), 1.0) * 60.0)
        log.debug("[wa] warmup: idle %.1fs (capped 60s)", budget_s)
        if not await _first_visible(page, CHAT_LIST, timeout_ms=2000):
            try:
                await page.goto(WA_WEB_URL, wait_until="domcontentloaded", timeout=15_000)
            except Exception:
                pass
        start = time.monotonic()
        while time.monotonic() - start < budget_s:
            remaining = budget_s - (time.monotonic() - start)
            if remaining <= 1.0:
                break
            # Reading short snippet (~3-6s, non 20-30s come con text_length=80)
            await reading_pause(text_length=15)
            try:
                await page.mouse.wheel(0, 200)
            except Exception:
                pass
            # Cap il sleep alla finestra rimanente per non sforare il budget
            remaining = budget_s - (time.monotonic() - start)
            if remaining <= 0:
                break
            await asyncio.sleep(min(remaining, 2.0))

    async def send_dm(
        self, page: "Page", username: str, message: str
    ) -> DMResult:
        """Invia un DM al numero `username` con `message`.

        Flusso:
        1. goto_profile(numero) — apre la chat
        2. focus su MESSAGE_INPUT
        3. human_type(message)
        4. press Enter (o click send button)
        5. attendi spunta SENT entro DELIVERY_CONFIRM_TIMEOUT_S
        """
        digits = _normalize_phone(username)
        target_repr = f"+{digits}" if digits else username

        if not await self.goto_profile(page, username):
            return DMResult(
                ok=False,
                reason="phone_not_on_whatsapp_or_invalid",
                target_username=target_repr,
                health=HealthStatus.OK,  # account è ancora ok, è il target invalido
            )

        # Localizza input messaggio
        input_loc = await _first_visible(page, MESSAGE_INPUT, timeout_ms=5000)
        if not input_loc:
            return DMResult(
                ok=False,
                reason="message_input_not_found",
                target_username=target_repr,
                health=HealthStatus.UNKNOWN,
            )

        # Focus + reading pause prima di scrivere (umano legge il contesto)
        try:
            await input_loc.click()
        except Exception:
            pass
        await reading_pause(text_length=len(message))

        # Type: usiamo input_loc.type direttamente (l'editor è contenteditable,
        # human_type da humanize.py si aspetta un selector — qui scriviamo
        # diretto sul locator per evitare ricerche multiple).
        try:
            for ch in message:
                await input_loc.type(ch, delay=0)
                # Delay random per-char (50-180ms con bias verso 80-120)
                import random
                d = random.uniform(0.05, 0.18)
                await asyncio.sleep(d)
        except Exception as e:
            log.warning("[wa] type message failed: %s", e)
            return DMResult(
                ok=False,
                reason=f"type_failed: {e}",
                target_username=target_repr,
                health=HealthStatus.UNKNOWN,
            )

        await human_wait(0.5, 1.5)

        # Invio: preferenza Enter (più naturale) → fallback click send button
        try:
            await page.keyboard.press("Enter")
        except Exception:
            send_btn = await _first_visible(page, SEND_BUTTON, timeout_ms=2000)
            if send_btn:
                try:
                    await send_btn.click()
                except Exception as e:
                    return DMResult(
                        ok=False,
                        reason=f"send_click_failed: {e}",
                        target_username=target_repr,
                        health=HealthStatus.UNKNOWN,
                    )
            else:
                return DMResult(
                    ok=False,
                    reason="send_button_not_found",
                    target_username=target_repr,
                    health=HealthStatus.UNKNOWN,
                )

        # Conferma delivery: spunta SENT entro 10s
        confirmed = await _wait_for_any(
            page, CHECKMARK_SENT, timeout_s=DELIVERY_CONFIRM_TIMEOUT_S
        )
        if not confirmed:
            # Check errore esplicito
            if await _first_visible(page, MSG_FAILED, timeout_ms=500):
                return DMResult(
                    ok=False,
                    reason="message_failed_in_dom",
                    target_username=target_repr,
                    health=HealthStatus.UNKNOWN,
                )
            return DMResult(
                ok=False,
                reason="no_sent_checkmark_within_timeout",
                target_username=target_repr,
                health=HealthStatus.UNKNOWN,
            )

        log.info("[wa] DM sent to %s (%d chars)", target_repr, len(message))
        return DMResult(
            ok=True,
            reason="sent",
            target_username=target_repr,
            health=HealthStatus.OK,
        )

    async def check_health(self, page: "Page") -> HealthStatus:
        """Verifica stato sessione attualmente caricata nella page.

        - QR canvas visibile → LOGGED_OUT
        - "You've been logged out" → LOGGED_OUT
        - CHAT_LIST visibile → OK
        - tutto il resto → UNKNOWN
        """
        try:
            if await _first_visible(page, LOGOUT_OR_BAN, timeout_ms=500):
                return HealthStatus.LOGGED_OUT
            if await _first_visible(page, SESSION_EXPIRED, timeout_ms=500):
                return HealthStatus.LOGGED_OUT
            if await _first_visible(page, QR_CANVAS, timeout_ms=500):
                return HealthStatus.LOGGED_OUT
            if await _first_visible(page, CHAT_LIST, timeout_ms=1500):
                return HealthStatus.OK
        except Exception as e:
            log.debug("[wa] check_health exception: %s", e)
        return HealthStatus.UNKNOWN
