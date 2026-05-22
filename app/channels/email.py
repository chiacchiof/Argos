"""Canale email: SMTP send (async) + IMAP fetch (in thread executor).

Fonte unica di config: tabella `email_accounts` (multi-account, tenant-scoped,
password cifrata Fernet con ARGOS_SECRET). Gestita da /accounts/email.

Le funzioni accettano un parametro opzionale `account`:
 - se passato (row di email_accounts) la usa direttamente
 - se None fa lookup del primo account `active` del tenant corrente

Pre-2026-05-22 esisteva una fallback `channel_config('email')` (singleton legacy)
con env vars `SMTP_PASSWORD` / `IMAP_PASSWORD`. Rimosso — la migration al boot
(`db.migrate_legacy_channels_to_accounts`) ha gia' creato un account
`legacy-default` per chi aveva quella config.
"""
from __future__ import annotations

import asyncio
import logging
import re
from email.message import EmailMessage
from email.utils import make_msgid, parseaddr
from typing import Any

import aiosmtplib

from .. import db
from .base import InboundMessage


log = logging.getLogger(__name__)


def _resolve_account(account: dict[str, Any] | None) -> dict[str, Any]:
    """Ritorna l'account passato o il primo `active` del tenant. RuntimeError se
    nessun account configurato."""
    if account is not None:
        return account
    accounts = db.list_email_accounts(status="active")
    if not accounts:
        raise RuntimeError(
            "Nessun email account configurato. Aggiungi un account su /accounts/email."
        )
    return accounts[0]


def _account_to_cfg(account: dict[str, Any]) -> dict[str, Any]:
    """Converte una row `email_accounts` (con `encrypted_smtp_password` BYTEA) in
    un dict cfg con password Fernet-decifrate inline."""
    from ..agent.social.crypto_creds import decrypt

    smtp_pwd = decrypt(account["encrypted_smtp_password"]) if account.get("encrypted_smtp_password") else None
    imap_pwd = decrypt(account["encrypted_imap_password"]) if account.get("encrypted_imap_password") else None
    return {
        "smtp_host": account.get("smtp_host"),
        "smtp_port": account.get("smtp_port") or 587,
        "smtp_user": account.get("smtp_user"),
        "smtp_password": smtp_pwd,
        "smtp_use_tls": bool(account.get("smtp_use_tls", 1)),
        "imap_host": account.get("imap_host"),
        "imap_port": account.get("imap_port") or 993,
        "imap_user": account.get("imap_user") or account.get("smtp_user"),
        "imap_password": imap_pwd,
        "imap_folder": account.get("imap_folder") or "INBOX",
        "from_address": account.get("from_address"),
        "reply_to": account.get("reply_to"),
        "rate_limit_per_minute": account.get("rate_limit_per_minute") or 10,
    }


async def send_email(
    to_address: str,
    subject: str,
    body: str,
    in_reply_to: str | None = None,
    references: str | None = None,
    account: dict[str, Any] | None = None,
) -> str:
    """Invia un'email tramite SMTP. Ritorna il Message-ID generato.

    Se `account` non viene passato usa il primo email_account attivo del tenant.
    """
    cfg = _account_to_cfg(_resolve_account(account))

    host = cfg.get("smtp_host")
    port = int(cfg.get("smtp_port") or 587)
    user = cfg.get("smtp_user")
    password = cfg.get("smtp_password")
    from_addr = cfg.get("from_address") or user
    use_tls = bool(cfg.get("smtp_use_tls", True))

    if not (host and user and password and from_addr):
        raise RuntimeError(
            "Config email account incompleta: smtp_host, smtp_user, "
            "smtp_password e from_address sono richiesti. Modifica su /accounts/email."
        )

    msg = EmailMessage()
    msg["From"] = from_addr
    msg["To"] = to_address
    msg["Subject"] = subject
    if cfg.get("reply_to"):
        msg["Reply-To"] = cfg["reply_to"]
    message_id = make_msgid(domain=from_addr.split("@", 1)[-1] if "@" in from_addr else "argos.local")
    msg["Message-ID"] = message_id
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
        msg["References"] = references or in_reply_to
    msg.set_content(body)

    # aiosmtplib: STARTTLS se port=587, SSL diretto se port=465
    smtp_kwargs: dict[str, Any] = {
        "hostname": host,
        "port": port,
        "username": user,
        "password": password,
        "timeout": 30,
    }
    if port == 465:
        smtp_kwargs["use_tls"] = True
    else:
        smtp_kwargs["start_tls"] = use_tls

    log.info("Sending email to %s subj=%s via %s:%s", to_address, subject[:40], host, port)
    await aiosmtplib.send(msg, **smtp_kwargs)
    return message_id


async def send_test_email(
    to_address: str,
    subject: str = "Argos test",
    account: dict[str, Any] | None = None,
) -> str:
    return await send_email(
        to_address,
        subject,
        "Questa è una email di test inviata da Argos.\n\n"
        "Se l'hai ricevuta, la configurazione SMTP funziona.",
        account=account,
    )


# --------------------------------------------------------------------------
# IMAP fetch (sync via imap-tools, eseguito in executor)
# --------------------------------------------------------------------------

_PROCESSED_UIDS_KEY = "_processed_uids"  # cache in cfg in-memory


def _fetch_inbound_sync(cfg: dict[str, Any], limit: int = 50) -> list[InboundMessage]:
    from imap_tools import MailBox, AND

    host = cfg.get("imap_host")
    port = int(cfg.get("imap_port") or 993)
    user = cfg.get("imap_user") or cfg.get("smtp_user")
    password = cfg.get("imap_password") or cfg.get("smtp_password")
    folder = cfg.get("imap_folder") or "INBOX"

    if not (host and user and password):
        raise RuntimeError("Config IMAP incompleta")

    out: list[InboundMessage] = []
    with MailBox(host, port=port).login(user, password, initial_folder=folder) as mb:
        # filtro: solo email NON lette (UNSEEN). Le segniamo come SEEN dopo aver processato.
        for m in mb.fetch(AND(seen=False), limit=limit, mark_seen=True):
            sender_email = ""
            sender_name = None
            if m.from_:
                sender_name, sender_email = parseaddr(m.from_)
                if not sender_email:
                    sender_email = m.from_
            out.append(
                InboundMessage(
                    channel="email",
                    external_id=m.uid or m.headers.get("message-id", [""])[0] or f"unknown-{m.date}",
                    sender_address=sender_email,
                    sender_name=sender_name or None,
                    sender_telegram_username=None,
                    subject=(m.subject or "").strip() or None,
                    body=(m.text or m.html or "").strip(),
                    in_reply_to=(m.headers.get("in-reply-to") or [None])[0],
                )
            )
    return out


async def fetch_inbound(
    limit: int = 50,
    account: dict[str, Any] | None = None,
) -> list[InboundMessage]:
    """Polling IMAP — ritorna nuove email come InboundMessage. Se `account` non
    viene passato usa il primo email_account attivo del tenant (o ritorna []
    se non ce ne sono).
    """
    if account is None:
        accounts = db.list_email_accounts(status="active")
        if not accounts:
            return []
        account = accounts[0]
    cfg = _account_to_cfg(account)
    try:
        return await asyncio.to_thread(_fetch_inbound_sync, cfg, limit)
    except Exception as e:  # pragma: no cover
        log.warning("IMAP fetch failed: %s", e)
        return []


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")


def is_valid_email(s: str | None) -> bool:
    return bool(s and EMAIL_RE.match(s.strip()))
