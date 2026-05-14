"""SafeBrowser — wrapper Playwright anti-azioni-invasive per recon_social.

Il problema: per R2 (exploration goal-driven) l'agente ReAct deve navigare
profili reali con il MIO account loggato. Una `page.click()` accidentale su
"Mi piace" / "Aggiungi amico" / "Invia messaggio" produrrebbe una notifica
visibile al target — esattamente quello che il modo "non invasivo" deve
evitare.

Soluzione: ogni interazione DOM passa da questo wrapper che:
  1. Verifica il selettore target NON sia (o non sovrapponga) un selettore
     blacklistato → altrimenti `BlockedActionError`
  2. Scrive in audit log JSONL ogni azione tentata (riuscita o bloccata)
  3. Opzionalmente cattura screenshot ogni N step (R2 audit completo)

Kill-switch globale: env `RECON_SOCIAL_DISABLED=1` → ogni recon task rifiuta
di partire (vedi `is_recon_disabled()`).

Used by:
- runner_recon_social.py (R1: solo audit log + screenshot, no validate click)
- runner_recon_social.py (R2: validate + audit + screenshot, hard mode)
"""
from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from playwright.async_api import Locator, Page

log = logging.getLogger(__name__)


# === Selettori blacklist (azioni invasive che generano notifica al target) ===
#
# Lista cumulativa cross-platform. Il wrapper verifica ad ogni click se il
# target è uno di questi (o overlap del bounding box). Se sì, raise.
BLACKLIST_SELECTORS: list[str] = [
    # --- Facebook (italiano + inglese) ---
    '[aria-label*="Mi piace" i]',
    '[aria-label*="Like" i]',
    '[aria-label*="Commenta" i]',
    '[aria-label*="Comment" i]',
    '[aria-label*="Condividi" i]',
    '[aria-label*="Share" i]',
    '[aria-label*="Invia messaggio" i]',
    '[aria-label*="Send message" i]',
    '[aria-label*="Aggiungi amico" i]',
    '[aria-label*="Add friend" i]',
    '[aria-label*="Segui" i]',
    '[aria-label*="Follow" i]',
    'div[role="button"]:has-text("Mi piace")',
    'div[role="button"]:has-text("Like")',
    'div[role="button"]:has-text("Commenta")',
    'div[role="button"]:has-text("Comment")',
    'div[role="button"]:has-text("Aggiungi")',

    # --- Instagram ---
    'svg[aria-label="Like"]',
    'svg[aria-label="Mi piace"]',
    'svg[aria-label="Comment"]',
    'svg[aria-label="Commenta"]',
    'svg[aria-label="Send"]',
    'svg[aria-label="Invia"]',
    'button:has-text("Segui")',
    'button:has-text("Follow")',

    # --- TikTok ---
    'button[data-e2e="follow-button"]',
    'button[data-e2e="like-icon"]',
    'button[data-e2e="comment-icon"]',
    'button[data-e2e="share-icon"]',
    'button:has-text("Following")',
    '[data-e2e="message-icon"]',

    # --- Cross-platform generici (last resort) ---
    'button:has-text("Send")',
    'button:has-text("Invia")',
    'button:has-text("Iscriviti")',
    'button:has-text("Subscribe")',
    'button[type="submit"]:has-text("Pubblica")',
    'button[type="submit"]:has-text("Post")',
]


class BlockedActionError(RuntimeError):
    """Sollevato quando un click viene bloccato dal SafeBrowser."""


def is_recon_disabled() -> bool:
    """Kill-switch globale via env."""
    v = (os.environ.get("RECON_SOCIAL_DISABLED") or "").strip().lower()
    return v in ("1", "true", "yes", "on")


class ReconAudit:
    """Audit log JSONL + screenshots periodici per una run recon_social.

    File scritti in `run_dir`:
      - recon_audit_log.jsonl  → un record per ogni step LLM + ogni click DOM
      - screenshots/step-NNNN.png (opzionale, ogni `screenshot_every_n` step)
    """

    def __init__(
        self,
        run_dir: Path,
        screenshot_every_n: int = 5,
        enabled_screenshots: bool = True,
    ):
        self.run_dir = Path(run_dir)
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.audit_path = self.run_dir / "recon_audit_log.jsonl"
        self.screenshots_dir = self.run_dir / "screenshots"
        self.screenshots_dir.mkdir(parents=True, exist_ok=True)
        self.screenshot_every_n = max(1, int(screenshot_every_n))
        self.enabled_screenshots = enabled_screenshots
        self._step_counter = 0
        # Apri il file in append (potrebbero coesistere più ReconAudit per resume)
        self._fp = self.audit_path.open("a", encoding="utf-8")

    def close(self) -> None:
        try:
            self._fp.close()
        except Exception:
            pass

    def log_event(self, event: str, **fields: Any) -> None:
        """Scrive un evento generico nell'audit log."""
        rec = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "event": event,
            **fields,
        }
        try:
            self._fp.write(json.dumps(rec, ensure_ascii=False) + "\n")
            self._fp.flush()
        except Exception as e:
            log.warning("recon audit write fail: %s", e)

    async def capture_step(self, page: "Page | None", step_label: str = "") -> None:
        """Increment counter, capture screenshot se step % N == 0."""
        self._step_counter += 1
        if not self.enabled_screenshots or page is None:
            return
        if self._step_counter % self.screenshot_every_n != 0:
            return
        path = self.screenshots_dir / f"step-{self._step_counter:04d}.png"
        try:
            await page.screenshot(path=str(path), full_page=False)
            self.log_event("SCREENSHOT", step=self._step_counter, path=str(path), label=step_label)
        except Exception as e:
            log.debug("screenshot failed: %s", e)


class SafeBrowser:
    """Wrapper Playwright con validazione anti-click-invasivi.

    Usato in modalità R2 (exploration). Per R1 (url_driven) il wrapper è
    comunque utile per audit log, ma `strict=False` permette il click
    senza validate (R1 fa solo `goto` + `evaluate`, niente click).
    """

    def __init__(self, page: "Page", audit: ReconAudit, *, strict: bool = True):
        self.page = page
        self.audit = audit
        self.strict = strict  # True = enforce blacklist (R2). False = solo audit (R1).

    async def safe_click(self, selector: "str | Locator", *, label: str = "") -> None:
        """Click validato. Solleva BlockedActionError se selector matcha blacklist.

        Per ora la validazione è "string-based": se il selector stesso matcha
        una entry blacklist (substring case-insensitive), blocchiamo subito.
        Per R2 avanzato si potrebbe aggiungere validation via bounding-box
        overlap (più sicuro ma più lento).
        """
        sel_str = str(selector) if not hasattr(selector, "click") else "<Locator>"

        if self.strict:
            for blocked in BLACKLIST_SELECTORS:
                if _selector_matches_blacklist(sel_str, blocked):
                    self.audit.log_event(
                        "BLOCKED_CLICK",
                        selector=sel_str,
                        matched_pattern=blocked,
                        label=label,
                    )
                    raise BlockedActionError(
                        f"Click bloccato: '{sel_str}' matcha pattern blacklist '{blocked}'"
                    )

        self.audit.log_event("CLICK", selector=sel_str, label=label)
        try:
            if hasattr(selector, "click"):
                await selector.click()  # type: ignore[union-attr]
            else:
                await self.page.click(selector)  # type: ignore[arg-type]
        except Exception as e:
            self.audit.log_event("CLICK_ERROR", selector=sel_str, error=str(e))
            raise

    async def safe_goto(self, url: str, *, label: str = "") -> None:
        """Navigation è sempre permessa (read-only). Solo audit."""
        self.audit.log_event("GOTO", url=url, label=label)
        try:
            await self.page.goto(url, wait_until="domcontentloaded", timeout=30_000)
        except Exception as e:
            self.audit.log_event("GOTO_ERROR", url=url, error=str(e))
            raise

    async def safe_scroll(self, dy: int = 800, *, label: str = "") -> None:
        """Scroll è sempre permesso (read-only).

        dy = pixel verticali; positivo = giù.
        """
        self.audit.log_event("SCROLL", dy=dy, label=label)
        try:
            await self.page.mouse.wheel(0, dy)
        except Exception as e:
            self.audit.log_event("SCROLL_ERROR", error=str(e))
            raise


def _selector_matches_blacklist(sel: str, blacklist_entry: str) -> bool:
    """Match approssimativo: se il selector contiene un frammento "rivelatore"
    della blacklist entry, ritorna True.

    Strategia conservativa: estrai i frammenti distintivi (attr=value, has-text)
    e match case-insensitive substring.
    """
    sl = sel.lower()
    bl = blacklist_entry.lower()
    # Estrai sottostringhe "has-text" e i contenuti di aria-label / data-e2e
    import re
    # has-text("X") → estrai X
    m_has = re.search(r'has-text\([\'"]([^\'"]+)[\'"]\)', bl)
    if m_has:
        txt = m_has.group(1).lower()
        if f'has-text("{txt}"' in sl or f"has-text('{txt}'" in sl or f">{txt}<" in sl:
            return True
    m_aria = re.search(r'aria-label[*~^|$]?=[\'"]([^\'"]+)[\'"]', bl)
    if m_aria:
        val = m_aria.group(1).lower()
        if f'aria-label="{val}"' in sl or f"aria-label='{val}'" in sl or f"aria-label*={val}" in sl:
            return True
    m_e2e = re.search(r'data-e2e=[\'"]([^\'"]+)[\'"]', bl)
    if m_e2e:
        val = m_e2e.group(1).lower()
        if f'data-e2e="{val}"' in sl or f"data-e2e='{val}'" in sl:
            return True
    # fallback: substring del blacklist nel selector
    return bl in sl
