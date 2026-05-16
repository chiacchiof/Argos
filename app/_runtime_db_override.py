"""Override runtime della `DATABASE_URL` via pagina /dbconfig.

Salva la connection string scelta dall'utente in un file CIFRATO con
`AGENTSCRAPER_SECRET` (Fernet). All'avvio dell'app, se il file esiste,
il suo contenuto sovrascrive la variabile d'ambiente `DATABASE_URL`.

Sicurezza:
- La cifratura impedisce a chi legge `data/db_config.enc` di vedere la DSN
  in chiaro (utile se il file finisce in backup o log).
- NON protegge da un utente con accesso fisico al PC: chi cancella il file
  fa tornare l'app alla DSN di `.env`. Per le credenziali della pagina vedi
  `app/routes/dbconfig.py`.

Vedi anche: SETUP_CLOUD_DB_TENANT.md sezione "Pagina /dbconfig".
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
from pathlib import Path

log = logging.getLogger(__name__)


_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_CONFIG_FILE = _PROJECT_ROOT / "data" / "db_config.enc"
_SECRET_ENV = "AGENTSCRAPER_SECRET"


def _derive_fernet_key(secret: str) -> bytes:
    """AGENTSCRAPER_SECRET può non essere già in formato Fernet (32-byte b64).
    Lo derivamo via SHA-256 + base64 urlsafe per ottenere una chiave valida.
    """
    return base64.urlsafe_b64encode(hashlib.sha256(secret.encode("utf-8")).digest())


def _get_fernet():
    from cryptography.fernet import Fernet

    secret = os.environ.get(_SECRET_ENV)
    if not secret:
        raise RuntimeError(
            f"{_SECRET_ENV} non impostata: impossibile cifrare/decifrare la DSN. "
            "Settala in .env (vedi .env.example)."
        )
    return Fernet(_derive_fernet_key(secret))


def read_override() -> dict | None:
    """Legge il file cifrato. Ritorna `{database_url, active_label}` o None.

    `None` se: file non esiste, secret mancante, cifratura corrotta.
    """
    if not _CONFIG_FILE.exists():
        return None
    try:
        f = _get_fernet()
        data = f.decrypt(_CONFIG_FILE.read_bytes())
        parsed = json.loads(data)
        if isinstance(parsed, dict) and parsed.get("database_url"):
            return parsed
        return None
    except Exception as exc:
        log.warning("Override DB illeggibile (%s) — uso .env come fallback.", exc)
        return None


def write_override(database_url: str, active_label: str = "") -> None:
    """Scrive il file cifrato con la DSN scelta dall'UI."""
    _CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    f = _get_fernet()
    payload = json.dumps(
        {
            "database_url": database_url.strip(),
            "active_label": active_label.strip(),
        }
    ).encode("utf-8")
    _CONFIG_FILE.write_bytes(f.encrypt(payload))
    log.info("Override DSN scritta in %s (label=%r).", _CONFIG_FILE.name, active_label)


def clear_override() -> None:
    """Rimuove il file di override, tornando alla DSN di `.env`."""
    if _CONFIG_FILE.exists():
        _CONFIG_FILE.unlink()
        log.info("Override DSN rimosso. L'app userà nuovamente .env::DATABASE_URL al prossimo boot.")


def apply_override() -> None:
    """All'avvio: se esiste un override, sovrascrive `DATABASE_URL` in `os.environ`.

    Chiamato da `app/config.py` DOPO `load_dotenv`, in modo che `Settings()` veda
    già il valore overridato quando istanziato.
    """
    data = read_override()
    if data and data.get("database_url"):
        os.environ["DATABASE_URL"] = data["database_url"]
        log.info("DATABASE_URL caricata da %s (label=%r).", _CONFIG_FILE.name, data.get("active_label", ""))


def override_active() -> bool:
    return _CONFIG_FILE.exists()
