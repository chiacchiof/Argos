"""Cifratura simmetrica per credenziali social account.

Usa Fernet (cryptography lib) — AES128-CBC + HMAC-SHA256, derivata da master
key in env `AGENTSCRAPER_SECRET`. Senza la chiave, niente decryption: anche
con accesso al DB, le credenziali restano illeggibili.

Razionale: le credenziali social (Instagram/TikTok) sono target di alto
valore. Salvarle in plaintext nel DB e' un rischio inaccettabile. Anche se
SQLite e' locale, il DB potrebbe finire in backup, screenshot, ecc.

Uso:
    from app.agent.social.crypto_creds import encrypt, decrypt

    enc = encrypt("mypassword")  # bytes (memorizzabile come BLOB o base64)
    pw = decrypt(enc)            # ritorna "mypassword"
"""
from __future__ import annotations

import base64
import hashlib
import os
from typing import Final

from cryptography.fernet import Fernet, InvalidToken


_FERNET: Final[Fernet | None] = None  # populated lazy
_ENV_KEY: Final[str] = "AGENTSCRAPER_SECRET"


def _derive_key(master: str) -> bytes:
    """Deriva una chiave Fernet (32 byte base64-encoded) da una stringa master."""
    h = hashlib.sha256(master.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(h)


def _get_fernet() -> Fernet:
    """Lazy init del Fernet singleton. Solleva RuntimeError se la chiave manca."""
    global _FERNET  # noqa: PLW0603
    if _FERNET is not None:
        return _FERNET
    master = os.environ.get(_ENV_KEY, "").strip()
    if not master:
        raise RuntimeError(
            f"Variabile d'ambiente {_ENV_KEY} non impostata. Le credenziali social "
            f"non possono essere cifrate/decifrate. Aggiungi al file .env: "
            f"{_ENV_KEY}=<stringa-segreta-lunga-30+-caratteri>"
        )
    if len(master) < 16:
        raise RuntimeError(
            f"{_ENV_KEY} troppo corta ({len(master)} char). Minimo 16 caratteri "
            f"raccomandato per sicurezza."
        )
    key = _derive_key(master)
    _FERNET = Fernet(key)
    return _FERNET


def encrypt(plaintext: str) -> bytes:
    """Cifra una stringa, ritorna bytes cifrati."""
    if not isinstance(plaintext, str):
        raise TypeError("plaintext deve essere str")
    return _get_fernet().encrypt(plaintext.encode("utf-8"))


def decrypt(ciphertext: bytes | str) -> str:
    """Decifra bytes/str cifrati. Solleva InvalidToken se la chiave non corrisponde."""
    if isinstance(ciphertext, str):
        ciphertext = ciphertext.encode("utf-8")
    try:
        return _get_fernet().decrypt(ciphertext).decode("utf-8")
    except InvalidToken:
        raise RuntimeError(
            "Decryption fallita: la chiave master non corrisponde a quella usata "
            "per cifrare. Hai cambiato AGENTSCRAPER_SECRET?"
        )


def is_configured() -> bool:
    """True se la chiave master e' settata e accessibile."""
    try:
        _get_fernet()
        return True
    except RuntimeError:
        return False
