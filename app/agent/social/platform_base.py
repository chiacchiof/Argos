"""Interfaccia astratta delle piattaforme social.

Ogni implementazione (instagram.py, tiktok.py) eredita da `SocialPlatform`
e implementa i 5 metodi minimi:
- `login(page, account)` — esegue il login (o ripristina sessione)
- `goto_profile(page, username)` — naviga al profilo target
- `warmup_browse(page, minutes)` — simula browsing umano per N min
- `send_dm(page, username, message)` — invia un DM al profilo
- `check_health(page)` — ritorna stato sessione (ok / challenged / banned)

Comportamento comune (umanizzazione, gestione cookie banner, screenshot
diagnostici) e' nel base class.
"""
from __future__ import annotations

import abc
import logging
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from playwright.async_api import Page, BrowserContext

log = logging.getLogger(__name__)


class HealthStatus(str, Enum):
    OK = "ok"
    CHALLENGED = "challenged"  # captcha / verifica email / SMS
    LOGGED_OUT = "logged_out"  # sessione scaduta
    RATE_LIMITED = "rate_limited"
    BANNED = "banned"
    UNKNOWN = "unknown"


@dataclass
class SocialAccount:
    """Account social usato per outreach. Le credenziali sono DECIFRATE in
    questa struct (mai persistite in chiaro al di fuori del runtime)."""
    uuid: str
    platform: str  # "instagram" | "tiktok" | ...
    username: str
    password: str   # decifrato a runtime
    proxy_label: str | None = None
    daily_dm_cap: int = 10
    status: str = "active"  # active | quarantine | banned
    session_dir: str | None = None  # Playwright user_data_dir (WA Web persistent)


@dataclass
class DMResult:
    ok: bool
    reason: str = ""
    target_username: str | None = None
    health: HealthStatus = HealthStatus.UNKNOWN


class SocialPlatform(abc.ABC):
    """Base class per ogni piattaforma social."""

    name: str = "base"
    login_url: str = ""
    home_url: str = ""

    @abc.abstractmethod
    async def login(self, page: "Page", account: SocialAccount) -> HealthStatus:
        """Esegue login con credenziali. Ritorna HealthStatus.

        Se la session_state e' gia' caricata, deve detectare e skippare il
        form di login.
        """
        ...

    @abc.abstractmethod
    async def goto_profile(self, page: "Page", username: str) -> bool:
        """Naviga al profilo `username`. Ritorna True se il profilo esiste."""
        ...

    @abc.abstractmethod
    async def warmup_browse(self, page: "Page", minutes: float = 5.0) -> None:
        """Simula browsing umano: scroll feed, view qualche post, hover, ecc.

        Da chiamare PRIMA del primo DM in una sessione. Riduce drasticamente
        il segnale "fresh login → immediato spam".
        """
        ...

    @abc.abstractmethod
    async def send_dm(
        self,
        page: "Page",
        username: str,
        message: str,
        *,
        speed_profile: str | None = None,
    ) -> DMResult:
        """Invia un DM a `username`. Ritorna DMResult.

        Implementazione deve:
        1. Navigare al profilo (riusare goto_profile se utile)
        2. Cliccare bottone messaggi/DM con human_click
        3. Aspettare panel apertura
        4. Type message con human_type (passandogli `profile=speed_profile`)
        5. Click send con human_click
        6. Verificare delivery (check DOM per messaggio nel chat history)

        `speed_profile`: None|'safe'|'balanced'|'aggressive' — modula i delay
        umani (reading_pause / human_type / human_wait). Default safe.
        """
        ...

    @abc.abstractmethod
    async def check_health(self, page: "Page") -> HealthStatus:
        """Verifica stato sessione: ok, challenged, banned, ecc.

        Da chiamare periodicamente durante la sessione. Se ritorna != OK,
        il caller deve fermare ulteriori azioni e mettere l'account in
        quarantine.
        """
        ...

    async def try_dismiss_cookie_banner(self, page: "Page") -> None:
        """Tenta di chiudere cookie banner / overlay comuni. Cross-platform.

        Note FB/Meta: usa `<div role='button'>` con label nested in `<span>`.
        `:has-text()` di Playwright a volte non scende correttamente; usiamo
        `get_by_role` che attinge dall'accessibility tree (piu' robusto).

        Il banner puo' apparire con ritardo (anche 5s dopo il goto) → polling
        fino a 12s totali.
        """
        from .humanize import human_wait
        labels = [
            # English (Meta sceglie l'UI in base alla locale del context: en-US)
            "Allow all cookies",
            "Allow all",
            "Accept all cookies",
            "Accept all",
            "Accept",
            "OK",
            "Got it",
            # Italiano
            "Consenti tutti i cookie",
            "Consenti cookie",
            "Accetta tutti i cookie",
            "Accetta tutto",
            "Accetta tutti",
            "Accetta",
            "Ho capito",
        ]
        deadline_s = 12.0
        import asyncio, time
        start = time.time()
        # Mini-wait iniziale: la modale FB appare con 1-3s di ritardo
        await asyncio.sleep(1.0)
        while time.time() - start < deadline_s:
            # 1) Path piu' robusto: accessibility tree (matcha tutti i widget
            # con role=button e nome accessibile = label, anche se il DOM e'
            # <div role='button'><span><span>...</span></span></div>).
            for lbl in labels:
                try:
                    loc = page.get_by_role("button", name=lbl, exact=True).first
                    if await loc.is_visible(timeout=400):
                        await loc.click(delay=80)
                        await human_wait(0.6, 1.5)
                        return
                except Exception:
                    pass
            # 2) Fallback su selettori CSS espliciti
            for lbl in labels:
                for tag_sel in (
                    f"div[aria-label='{lbl}']",
                    f"button:has-text('{lbl}')",
                    f"[role='button']:has-text('{lbl}')",
                ):
                    try:
                        loc = page.locator(tag_sel).first
                        if await loc.is_visible(timeout=300):
                            await loc.click(delay=80)
                            await human_wait(0.6, 1.5)
                            return
                    except Exception:
                        continue
            await asyncio.sleep(0.5)
