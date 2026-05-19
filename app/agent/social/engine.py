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
    pick_gap_minutes,
    random_session_duration_min,
)
from .facebook import Facebook
from .instagram import Instagram
from .platform_base import DMResult, HealthStatus, SocialAccount, SocialPlatform
from .proxy_pool import assign_proxy_to_account, proxy_to_playwright_kwargs
from .session_manager import load_session_state, save_session
from .tiktok import TikTok
from .whatsapp_browser import WhatsAppBrowser

if TYPE_CHECKING:
    pass

log = logging.getLogger(__name__)


def _account_recently_active(account_uuid: str, *, threshold_minutes: float) -> bool:
    """True se l'account ha inviato un DM ok nei `threshold_minutes` precedenti.

    Usa la session file timestamp come proxy "sessione viva recente": dopo ogni
    invio ok save_session() riscrive il file. Niente query DB → zero overhead.
    Falsi positivi possibili (es. save manuale) ma il costo è solo "skip warmup":
    se l'account è stale, WA/IG riproporranno login/captcha e il runner fallirà
    al passo successivo come oggi.
    """
    import os
    import time
    from pathlib import Path

    from ...config import settings as _app_settings

    base = Path(getattr(_app_settings, "data_dir", "data")) / "sessions" / account_uuid
    # Cerca un file recente nella session dir (storage_state.json o cookies).
    if not base.exists():
        return False
    try:
        candidates = [p for p in base.iterdir() if p.is_file()]
    except OSError:
        return False
    if not candidates:
        return False
    newest_mtime = max(p.stat().st_mtime for p in candidates)
    age_s = time.time() - newest_mtime
    return age_s <= threshold_minutes * 60.0


PLATFORMS: dict[str, type[SocialPlatform]] = {
    "instagram": Instagram,
    "tiktok": TikTok,
    "facebook": Facebook,
    "whatsapp_browser": WhatsAppBrowser,
}


def _get_session_dir_from_db(uuid: str) -> str | None:
    """Recupera session_dir per un account WA browser dal DB (fallback se non
    è già nell'oggetto SocialAccount in memoria)."""
    try:
        from ... import db as _db
        rows = _db.list_social_accounts(platform="whatsapp_browser")
        for r in rows:
            if r.get("uuid") == uuid:
                return r.get("session_dir") or None
    except Exception:
        pass
    return None


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
        """Apre browser + context per un account con proxy + session restore + stealth.

        WhatsApp ha bisogno di IndexedDB persistito (chat keys E2E), che
        `storage_state` NON gestisce. Per account con `auth_method='qr_session'`
        E `session_dir` valida, usiamo `launch_persistent_context(user_data_dir)`
        invece di `browser.new_context()`. Così il QR viene scansionato una volta
        sola e la sessione resta valida fino a quando WA non la invalida
        (~14 giorni in genere).

        Per IG/TikTok/Facebook resta il flusso classico browser.new_context +
        storage_state (cookies/localStorage) — funziona bene per quelle.
        """
        proxy_cfg = assign_proxy_to_account(account.uuid)
        proxy_kwargs = proxy_to_playwright_kwargs(proxy_cfg)
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

        # Persistent context per WhatsApp (user_data_dir = path Playwright completo).
        # Restituiamo (None, context, page): browser è inglobato dentro il context
        # ed è chiuso quando si chiude il context.
        sess_dir = getattr(account, "session_dir", None) or _get_session_dir_from_db(account.uuid)
        if sess_dir and getattr(account, "platform", "") == "whatsapp_browser":
            log.info("[wa] open browser via launch_persistent_context (dir=%s)", sess_dir)
            from pathlib import Path as _P
            _P(sess_dir).mkdir(parents=True, exist_ok=True)
            context = await p.chromium.launch_persistent_context(
                user_data_dir=sess_dir,
                headless=not self.headed,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--no-default-browser-check",
                ],
                **ctx_kwargs,
            )
            # Use existing about:blank page if available, else open new
            page = context.pages[0] if context.pages else await context.new_page()
            return None, context, page

        # Path classico (IG/TikTok/Facebook): browser + new_context + storage_state
        browser = await p.chromium.launch(
            headless=not self.headed,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-default-browser-check",
            ],
        )
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
        gap_min_minutes: float | None = None,
        gap_max_minutes: float | None = None,
        speed_profile: str | None = None,
        skip_warmup_if_recent_minutes: float | None = 30.0,
    ) -> list[DMResult]:
        """Una sessione = login → warmup → N DM con gap umani → close.

        Acquisisce 1 account dal pool, esegue gli N DM, rilascia.
        `jlog`, se passato, riceve i log-eventi chiave (login fail, off-hours,
        warmup, DM esito) — utile per propagare alla job-log del runner.

        `gap_min_minutes` / `gap_max_minutes`: override per-task del gap
        anti-ban (B-011). None → default per-platform da humanize.
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
                                    " %s document.body.innerText.slice(0, 400) : ''"
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
                    # Hot-skip: se l'account ha avuto un DM ok nei `skip_warmup_if_recent_minutes`
                    # ultimi minuti, la sessione WA/social è già "calda" lato platform
                    # (account già visto da IG/WA/TT/FB) → salta warmup risparmiando 30-50s.
                    effective_warmup = warmup_min
                    if skip_warmup_if_recent_minutes and skip_warmup_if_recent_minutes > 0:
                        try:
                            if _account_recently_active(
                                rt.account.uuid, threshold_minutes=skip_warmup_if_recent_minutes,
                            ):
                                effective_warmup = 0.0
                                _say(
                                    f"Warmup skipped: account attivo entro "
                                    f"{int(skip_warmup_if_recent_minutes)} min (hot session)."
                                )
                        except Exception as e:
                            log.debug("hot-skip check failed: %s", e)
                    if effective_warmup > 0:
                        log.info("[%s/%s] warmup browse %.2f min", platform_name, rt.account.username, effective_warmup)
                        _say(f"Warmup browse ~{effective_warmup:.1f} min...")
                        await platform.warmup_browse(page, minutes=effective_warmup)

                    # Loop DM
                    n_to_send = min(len(targets), max_dms_per_session, rt.account.daily_dm_cap - rt.dms_today)
                    _say(f"Pronto a inviare {n_to_send} DM (cap restante={rt.account.daily_dm_cap - rt.dms_today})")
                    for i, (username, message) in enumerate(targets[:n_to_send]):
                        log.info("[%s/%s] DM %d/%d -> %s",
                                 platform_name, rt.account.username, i + 1, n_to_send, username)
                        _say(f"DM {i+1}/{n_to_send} → @{username}")
                        result = await platform.send_dm(page, username, message, speed_profile=speed_profile)
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
                        # Random gap human-like tra un DM e il successivo.
                        # B-011: pick_gap_minutes onora override per-task se
                        # gap_min/max_minutes passati, altrimenti default per
                        # platform da humanize.DEFAULT_GAP_RANGE_MIN
                        # (WA: 0.15-0.35 min = 9-21s; IG/TT/FB: 8-30 min).
                        # Scriviamo SEMPRE nel job log così l'utente sa che il
                        # runner sta facendo idle, non e' bloccato.
                        if i < n_to_send - 1:
                            gap_min = pick_gap_minutes(
                                platform_name,
                                task_min=gap_min_minutes,
                                task_max=gap_max_minutes,
                            )
                            log.debug("Gap %.2f min...", gap_min)
                            if gap_min < 1.0:
                                _say(f"  ⏳ gap anti-ban: idle per {gap_min*60:.0f}s prima del prossimo DM")
                            else:
                                _say(f"  ⏳ gap anti-ban: idle per {gap_min:.1f} min prima del prossimo DM")
                            await asyncio.sleep(gap_min * 60)
                    # Save session aggiornata
                    await save_session(context, rt.account.uuid)
                finally:
                    # Per persistent_context (WhatsApp) browser è None: chiudere
                    # solo il context (chiude internamente il chromium child).
                    try:
                        await context.close()
                    except Exception:
                        pass
                    if browser is not None:
                        try:
                            await browser.close()
                        except Exception:
                            pass
        except Exception as e:
            log.exception("Engine session crash: %s", e)
            _say(f"💥 engine crash: {type(e).__name__}: {e}")
            self.pool.release(rt.account.uuid, dm_sent=False, health=HealthStatus.UNKNOWN)
            return results

        self.pool.release(rt.account.uuid, dm_sent=any(r.ok for r in results), health=HealthStatus.OK)
        return results
