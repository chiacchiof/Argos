"""Gestione session_state Playwright per account social.

Una "session" = cookies + localStorage di un browser context. Persistirla su
disco permette di:
1. Saltare il login alla prossima esecuzione (login = MOMENTO PIU' RISCHIOSO)
2. Mantenere "trust score" cumulativo (account che browsa per giorni dalla
   stessa sessione e' meno sospetto di uno che fa fresh login ogni volta)

Storage: `data/sessions/<account_uuid>.json` (formato standard Playwright
`context.storage_state()`).

Sicurezza:
- File salvati con permessi user-only (chmod 0600 su Linux, default Windows ACL)
- Niente cifratura: le cookie/storage NON contengono password chiare. Se uno ha
  accesso al filesystem, puo' comunque assumere l'identita' — stesso problema
  di un cookie file di un browser normale.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING

from ...config import DATA_DIR

if TYPE_CHECKING:
    from playwright.async_api import BrowserContext

log = logging.getLogger(__name__)

SESSIONS_DIR = DATA_DIR / "sessions"
SESSIONS_DIR.mkdir(parents=True, exist_ok=True)


def session_path(account_uuid: str) -> Path:
    """Path al file di sessione di un account."""
    safe = "".join(c for c in account_uuid if c.isalnum() or c in "-_")
    return SESSIONS_DIR / f"{safe}.json"


async def save_session(context: "BrowserContext", account_uuid: str) -> Path:
    """Salva il `storage_state` del context su disco. Ritorna il path."""
    p = session_path(account_uuid)
    state = await context.storage_state()
    p.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    # Permessi user-only quando supportato
    try:
        os.chmod(p, 0o600)
    except (OSError, NotImplementedError):
        pass
    log.debug("session saved for %s -> %s", account_uuid, p)
    return p


def load_session_state(account_uuid: str) -> dict | None:
    """Carica lo state dal disco. None se non esiste o corrotto."""
    p = session_path(account_uuid)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        log.warning("session corrupt for %s: %s — sara' fatto fresh login", account_uuid, e)
        return None


def delete_session(account_uuid: str) -> None:
    """Cancella la sessione (es. dopo logout forzato o ban detection)."""
    p = session_path(account_uuid)
    if p.exists():
        try:
            p.unlink()
            log.info("session deleted for %s", account_uuid)
        except OSError as e:
            log.warning("delete session failed for %s: %s", account_uuid, e)


def has_session(account_uuid: str) -> bool:
    return session_path(account_uuid).exists()
