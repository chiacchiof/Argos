"""B-008: cifratura at-rest trasparente per segreti in colonne TEXT.

Le credenziali "forti" (password social, token WhatsApp/Telegram, SMTP, vault
`llm_api_keys`) sono già cifrate Fernet in colonne BYTEA dedicate. Restavano in
chiaro le **LLM API key per-task** (`tasks.llm_api_key`, `discovery_llm_api_key`,
`browser_llm_api_key`), path legacy/fallback. Questo modulo le cifra at-rest
senza cambiare schema (il ciphertext base64 sta nella colonna TEXT) e senza
rompere nulla:

- `encrypt_secret`  — idempotente (non ri-cifra un valore già cifrato) e no-op
  se `ARGOS_SECRET` non è configurata (come fa già `social_accounts`).
- `decrypt_secret`  — con fallback: i valori legacy in chiaro vengono ritornati
  così come sono, quindi i task esistenti continuano a funzionare prima ancora
  della migrazione. Anche un ciphertext non decifrabile (chiave cambiata) non fa
  crashare il runner: si ritorna il grezzo.

Wrappa il Fernet di `app/agent/social/crypto_creds.py` (chiave da `ARGOS_SECRET`).
"""
from __future__ import annotations

from .agent.social.crypto_creds import decrypt as _fernet_decrypt
from .agent.social.crypto_creds import encrypt as _fernet_encrypt
from .agent.social.crypto_creds import is_configured

# Tutti i token Fernet (versione 0x80) iniziano con 'gAAAAA' una volta
# urlsafe-base64. Heuristica per riconoscere un valore già cifrato senza dover
# tentare una decrypt costosa. `decrypt_secret` comunque ritenta+fallback, quindi
# un falso negativo non corrompe nulla.
_FERNET_PREFIX = "gAAAAA"


def is_secret_configured() -> bool:
    """True se `ARGOS_SECRET` è settata (quindi la cifratura at-rest è attiva)."""
    return is_configured()


def looks_encrypted(value: object) -> bool:
    """True se il valore ha l'aspetto di un token Fernet (prefisso noto)."""
    return isinstance(value, str) and value.startswith(_FERNET_PREFIX)


def encrypt_secret(value: str | None) -> str | None:
    """Cifra un segreto per lo storage. Ritorna una stringa base64 (ciphertext).

    - `None`/vuoto → invariato.
    - già cifrato (prefisso Fernet) → invariato (idempotente: niente doppia cifratura).
    - `ARGOS_SECRET` assente → invariato (degrada a plaintext, come oggi: meglio
      funzionante-in-chiaro che task rotto).
    """
    if not value or not isinstance(value, str):
        return value
    if looks_encrypted(value):
        return value
    if not is_configured():
        return value
    try:
        return _fernet_encrypt(value).decode("ascii")
    except Exception:
        return value


def decrypt_secret(value: str | None) -> str | None:
    """Decifra un segreto letto dallo storage.

    - `None`/vuoto → invariato.
    - non cifrato (plaintext legacy) → invariato.
    - cifrato ma non decifrabile (chiave diversa/corrotto) → ritorna il grezzo
      (non solleva: il chiamante gestirà l'eventuale 'API key mancante').
    """
    if not value or not isinstance(value, str):
        return value
    if not looks_encrypted(value):
        return value
    try:
        return _fernet_decrypt(value)
    except Exception:
        return value
