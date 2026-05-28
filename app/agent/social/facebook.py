"""Facebook outreach implementation.

⚠️ FRAGILE: i selettori di Facebook cambiano per A/B test frequenti.
Aspettati manutenzione regolare.

Note specifiche Facebook:
- Login: `https://www.facebook.com/login` con `input[name='email']` + `input[name='pass']`
- Profile URL: `https://www.facebook.com/<username>` oppure `/profile.php?id=<id>`
- DM: piu' robusto navigare direttamente a `https://www.facebook.com/messages/t/<username>`
  invece di cliccare il button "Messaggio" sul profilo (il bottone cambia spesso).
- Cookie banner inglese/italiano: "Allow all cookies" / "Consenti tutti i cookie"
- 2FA / SMS verify: gestiti manualmente all'apertura sessione (browser headed).
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


class Facebook(SocialPlatform):
    name = "facebook"
    login_url = "https://www.facebook.com/login"
    home_url = "https://www.facebook.com/"

    async def login(self, page: "Page", account: SocialAccount) -> HealthStatus:
        # Verifica prima session esistente
        await page.goto(self.home_url, wait_until="domcontentloaded")
        await human_wait(2, 4)
        status = await self.check_health(page)
        if status == HealthStatus.OK:
            log.info("facebook login: session valid for %s", account.username)
            return status
        if status != HealthStatus.LOGGED_OUT:
            return status
        # Fresh login
        log.info("facebook fresh login for %s", account.username)
        await page.goto(self.login_url, wait_until="domcontentloaded")
        await human_wait(1.5, 3)
        await self.try_dismiss_cookie_banner(page)
        try:
            await human_type(page, "input[name='email']", account.username)
            await human_wait(0.5, 1.5)
            await human_type(page, "input[name='pass']", account.password)
            await human_wait(0.5, 1.5)
            # Bottone "Log in" FB: id='loginbutton' (storico), name='login',
            # data-testid='royal_login_button' (versioni recenti). FB ne ha
            # cambiato i selettori piu' volte → provo in cascata.
            clicked = False
            css_candidates = [
                "button#loginbutton",
                "button[name='login']",
                "button[data-testid='royal_login_button']",
                "[data-testid='royal_login_button']",
                "button[type='submit']",
            ]
            for sel in css_candidates:
                try:
                    if await page.locator(sel).first.is_visible(timeout=1200):
                        await human_click(page, sel)
                        clicked = True
                        break
                except Exception:
                    continue
            # Accessibility tree fallback (matcha aria-label / nome testo del button)
            if not clicked:
                for name in ("Log in", "Log In", "Login", "Accedi"):
                    try:
                        loc = page.get_by_role("button", name=name, exact=True).first
                        if await loc.is_visible(timeout=1000):
                            await loc.click(delay=80)
                            clicked = True
                            break
                    except Exception:
                        continue
            if not clicked:
                # Ultimo fallback: premi Enter sul password field
                await page.keyboard.press("Enter")
            # Aspetta che FB lasci /login (success o challenge): max 30s.
            # `human_wait` fisso 5-9s non bastava — il bottone "Log in" puo'
            # restare in loading molto piu' a lungo prima del redirect, e il
            # check_health partiva mentre il submit era ancora in corso.
            try:
                await page.wait_for_url(
                    lambda u: "/login" not in u or "/checkpoint" in u
                              or "/two_factor" in u or "/recover" in u,
                    timeout=30_000,
                )
            except Exception:
                # Timeout: probabilmente login bloccato — passa al check_health
                # che propaghera' LOGGED_OUT (e l'engine salvera' screenshot).
                pass
            await human_wait(1.5, 3)
        except Exception as e:
            log.error("facebook login form fail: %s", e)
            return HealthStatus.UNKNOWN
        return await self.check_health(page)

    async def goto_profile(self, page: "Page", username: str) -> bool:
        # Facebook accetta sia /<username> sia /profile.php?id=<id>
        username = (username or "").strip().lstrip("@")
        if username.isdigit():
            target = f"https://www.facebook.com/profile.php?id={username}"
        else:
            target = f"https://www.facebook.com/{username}"
        await page.goto(target, wait_until="domcontentloaded")
        await human_wait(2, 5)
        # Verifica esistenza profilo: cerca elementi tipici nel profilo header
        try:
            await page.locator("h1, [role='main']").first.wait_for(timeout=5000)
            # Anti-pattern: pagina /<random> ridireziona spesso a home o errore
            cur = page.url
            if username.lower() not in cur.lower() and not cur.startswith(target):
                # Forse redirect: prova lo stesso, vediamo l'header
                pass
            return True
        except Exception:
            return False

    async def warmup_browse(self, page: "Page", minutes: float = 5.0) -> None:
        """Browse feed + view qualche post + like random."""
        import time

        await page.goto(self.home_url, wait_until="domcontentloaded")
        await human_wait(3, 6)
        end_at = time.time() + minutes * 60
        while time.time() < end_at:
            action = random.choice(["scroll", "scroll", "scroll", "hover", "like"])
            if action == "scroll":
                await human_scroll(page, n=random.randint(2, 4))
            elif action == "hover":
                await random_idle_action(page)
            elif action == "like":
                try:
                    # Bottoni "Mi piace" / "Like" hanno aria-label localizzato
                    like_btns = page.locator(
                        "div[aria-label='Mi piace']:not([aria-pressed='true']), "
                        "div[aria-label='Like']:not([aria-pressed='true'])"
                    )
                    n = await like_btns.count()
                    if n > 0:
                        idx = random.randint(0, min(n - 1, 4))
                        await like_btns.nth(idx).click(delay=random.randint(60, 130))
                        await human_wait(1.5, 3.5)
                except Exception:
                    pass
            await human_wait(2.5, 6)

    async def send_dm(
        self,
        page: "Page",
        username: str,
        message: str,
        *,
        speed_profile: str | None = None,
    ) -> DMResult:
        """Invia DM. Strategia: naviga direttamente a messages/t/<username> che
        e' piu' robusto del click sul bottone Messaggio del profilo."""
        username_clean = (username or "").strip().lstrip("@")
        if not username_clean:
            return DMResult(ok=False, reason="username vuoto", target_username=username)
        # /messages/t/<username> apre direttamente la chat (se permesso DM)
        target = f"https://www.facebook.com/messages/t/{username_clean}"
        try:
            await page.goto(target, wait_until="domcontentloaded")
            await human_wait(4, 7, profile=speed_profile)
            # Modali da chiudere prima di poter scrivere:
            # 1) "Invia un codice monouso per ripristinare la cronologia delle tue chat"
            #    (FB mostra al primo accesso Messenger su nuovo dispositivo per le
            #    chat E2E). NON vogliamo "Invia codice" — chiudiamo con X.
            # 2) Altri popup di onboarding Messenger ("Notifiche", "Tema", ecc.).
            await self._dismiss_messenger_modals(page)
            # Banner E2E (rollout 2024-2025): aprendo una chat pre-cifratura, FB
            # mostra "Questi messaggi non sono stati inviati perché questa chat
            # fosse protetta con la crittografia end-to-end" + bottone "Continua"
            # che copre il textbox finche' non lo clicchi.
            await self._dismiss_e2e_continue_banner(page)
            # Se non puoi messaggiare (es. profilo non amico o blocked):
            # FB mostra messaggio "Questa persona non puo' ricevere messaggi"
            try:
                body_text = await page.evaluate("document.body.innerText")
                low = (body_text or "").lower()
                blockers = [
                    "this person isn't receiving messages",
                    "questa persona non riceve",
                    "non puoi inviare messaggi",
                    "cannot send messages",
                ]
                if any(b in low for b in blockers):
                    return DMResult(
                        ok=False, reason="dm_disabled_by_target",
                        target_username=username_clean,
                    )
            except Exception:
                pass
            # Cerca il textbox del messaggio.
            # `aria-label="Scrivi a <Nome>"` e' il textbox della chat E2EE (Lexical).
            # `aria-label="Messaggio"`/"Message" sono il composer classico pre-E2EE.
            # Match per startswith su "Scrivi a"/"Write to" perche' include il nome
            # del destinatario (es. "Scrivi a Carlotta Castoro").
            input_sels = [
                "div[contenteditable='true'][role='textbox'][aria-label^='Scrivi a']",
                "div[contenteditable='true'][role='textbox'][aria-label^='Write to']",
                "[aria-label='Messaggio']",
                "[aria-label='Message']",
                "div[contenteditable='true'][role='textbox']",
                "div[contenteditable='true']",
            ]
            # FB Messenger e' una SPA: domcontentloaded scatta prima che React
            # monti il composer (Lexical editor). Aspetta esplicitamente che
            # uno qualsiasi dei selettori sia visibile, max 20s. Senza questo
            # tutti i selettori falliscono is_visible(2500ms) e torniamo
            # dm_input_not_found mentre la pagina e' ancora in skeleton grigio.
            try:
                composite = ", ".join(input_sels)
                await page.locator(composite).first.wait_for(state="visible", timeout=20000)
            except Exception:
                pass
            typed = False
            type_errors: list[str] = []
            for sel in input_sels:
                try:
                    if not await page.locator(sel).first.is_visible(timeout=2500):
                        continue
                except Exception as e:
                    type_errors.append(f"{sel} visible-check: {type(e).__name__}: {e}")
                    continue
                try:
                    await human_type(page, sel, message, profile=speed_profile)
                    typed = True
                    break
                except Exception as e:
                    type_errors.append(f"{sel} human_type: {type(e).__name__}: {e}")
                    continue
            if type_errors:
                log.warning("[facebook/%s] send_dm input attempts: %s", username_clean, " | ".join(type_errors))
            if not typed:
                try:
                    from datetime import datetime, timezone
                    from pathlib import Path
                    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
                    shot_dir = Path("data/sessions")
                    shot_dir.mkdir(parents=True, exist_ok=True)
                    base = shot_dir / f"dm_fail_{username_clean}_{ts}"
                    try:
                        await page.screenshot(path=str(base.with_suffix(".png")), full_page=False)
                    except Exception:
                        pass
                    try:
                        html = await page.evaluate("document.body && document.body.outerHTML")
                        base.with_suffix(".html").write_text(html or "", encoding="utf-8")
                    except Exception:
                        pass
                    try:
                        cur_url = page.url
                        base.with_suffix(".url.txt").write_text(cur_url, encoding="utf-8")
                    except Exception:
                        pass
                except Exception:
                    pass
                return DMResult(
                    ok=False, reason="dm_input_not_found",
                    target_username=username_clean,
                )
            await human_wait(1, 3, profile=speed_profile)
            # Send: bottone "Invia" / "Send" o Enter
            send_sels = [
                "[aria-label='Invia']",
                "[aria-label='Send']",
                "[aria-label='Premi Invio per inviare']",
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
            await human_wait(4, 7, profile=speed_profile)
            # Verifica delivery: il primo snippet del messaggio appare nel feed chat
            try:
                snippet = message[:30]
                await page.locator(f"text={snippet}").first.wait_for(timeout=5000)
                return DMResult(ok=True, target_username=username_clean, health=HealthStatus.OK)
            except Exception:
                return DMResult(
                    ok=False, reason="delivery_unconfirmed",
                    target_username=username_clean, health=HealthStatus.UNKNOWN,
                )
        except Exception as e:
            return DMResult(
                ok=False, reason=f"exception: {type(e).__name__}: {e}",
                target_username=username_clean,
            )

    async def _dismiss_messenger_modals(self, page: "Page") -> None:
        """Chiude modali di onboarding Messenger che bloccano l'accesso al
        textbox della chat. Loop fino a 3 modali consecutive (FB ne incatena
        spesso 2: la principale + un dialog di conferma).

        Conosciute al 2026-05-12:
        - "Invia un codice monouso per ripristinare la cronologia delle tue chat"
          → la X in alto-destra apre un dialog di conferma:
        - "Vuoi continuare senza ripristinare?" → bottone "Non ripristinare i
          messaggi" / "Don't restore messages" e' la chiusura definitiva.
        Si chiudono nell'ordine, quindi prima provo le conferme positive (che
        chiudono lo stack di modali), poi le X.
        """
        import asyncio, time
        from .humanize import human_wait

        # Bottoni che chiudono in modo "definitivo" la modale corrente senza
        # side-effect mobile/email. Ordinati: prima le risposte di conferma a
        # dialog secondari, poi le X di chiusura.
        confirm_labels = [
            "Non ripristinare i messaggi",
            "Don't restore messages",
            "Continua senza ripristinare",
            "Continue without restoring",
        ]
        close_labels = [
            "Chiudi", "Close",
            "Not now", "Non ora",
            "Forse più tardi", "Maybe later",
        ]

        async def _try_click_any() -> bool:
            # 1) Conferme positive (priorità: chiudono lo stack)
            for lbl in confirm_labels:
                try:
                    loc = page.get_by_role("button", name=lbl, exact=True).first
                    if await loc.is_visible(timeout=250):
                        await loc.click(delay=70)
                        await human_wait(0.5, 1.2)
                        return True
                except Exception:
                    pass
            # 2) X / Chiudi / Not now via accessibility tree
            for lbl in close_labels:
                try:
                    loc = page.get_by_role("button", name=lbl, exact=True).first
                    if await loc.is_visible(timeout=250):
                        await loc.click(delay=70)
                        await human_wait(0.5, 1.2)
                        return True
                except Exception:
                    pass
            # 3) Fallback CSS per X via aria-label
            for sel in (
                "div[aria-label='Chiudi']",
                "div[aria-label='Close']",
                "[role='button'][aria-label='Chiudi']",
                "[role='button'][aria-label='Close']",
            ):
                try:
                    loc = page.locator(sel).first
                    if await loc.is_visible(timeout=250):
                        await loc.click(delay=70)
                        await human_wait(0.5, 1.2)
                        return True
                except Exception:
                    pass
            return False

        # Polling per ~6s totali, max 3 modali consecutive
        deadline = time.time() + 6.0
        dismissed = 0
        while time.time() < deadline and dismissed < 3:
            if await _try_click_any():
                dismissed += 1
                # Dopo aver chiuso una modale, aspetta breve e ricontrolla
                # per il dialog di conferma che FB spesso incatena.
                await asyncio.sleep(0.6)
                continue
            await asyncio.sleep(0.4)

    async def _dismiss_e2e_continue_banner(self, page: "Page") -> None:
        """Chiude il banner E2E "Continua" di Messenger.

        Da quando FB ha rolled out end-to-end encryption di default su Messenger
        (rollout completato 2024-2025), aprendo una chat con messaggi pre-E2E
        compare un banner in basso con bottone "Continua" che blocca il textbox
        del messaggio finché non lo clicchi. Il textbox NON è renderizzato
        prima del click (verificato 2026-05-28 con dump DOM su @carlotta.castoro:
        zero `contenteditable='true'` nell'HTML).

        FB usa `<div aria-label="Continua">` (non `<button>`), quindi
        `get_by_role("button", name="Continua")` NON matcha — serve il selettore
        CSS diretto.
        """
        from .humanize import human_wait

        try:
            body = await page.evaluate("document.body && document.body.innerText")
        except Exception:
            return
        low = (body or "").lower()
        markers = (
            "crittografia end-to-end",
            "end-to-end encryption",
            "messaggi non sono stati inviati",
            "messages weren't sent",
        )
        if not any(m in low for m in markers):
            return
        for sel in (
            "div[aria-label='Continua']",
            "div[aria-label='Continue']",
        ):
            try:
                loc = page.locator(sel).first
                if await loc.is_visible(timeout=1500):
                    await loc.click(delay=80)
                    await human_wait(1.0, 2.0)
                    return
            except Exception:
                continue

    async def check_health(self, page: "Page") -> HealthStatus:
        url = page.url
        # Login required (URL hint)
        if "/checkpoint" in url:
            return HealthStatus.CHALLENGED
        if "/login" in url or "/recover" in url:
            return HealthStatus.LOGGED_OUT
        # Welcome page non-loggata: FB lascia URL su https://www.facebook.com/
        # ma mostra il form di login (input[name='email'] + input[name='pass']).
        # Senza questo check, check_health ritorna OK e login() salta il flusso.
        try:
            if await page.locator("input[name='pass']").first.is_visible(timeout=1500):
                return HealthStatus.LOGGED_OUT
        except Exception:
            pass
        # Cerca markers di ban / rate limit nel body
        try:
            for marker, status in (
                ("Account temporaneamente bloccato", HealthStatus.RATE_LIMITED),
                ("Account temporarily blocked", HealthStatus.RATE_LIMITED),
                ("temporarily restricted", HealthStatus.RATE_LIMITED),
                ("temporaneamente limitato", HealthStatus.RATE_LIMITED),
                ("Account disabled", HealthStatus.BANNED),
                ("Account disabilitato", HealthStatus.BANNED),
            ):
                if await page.locator(f"text=\"{marker}\"").first.is_visible(timeout=500):
                    return status
        except Exception:
            pass
        return HealthStatus.OK
