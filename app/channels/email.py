"""Canale email: SMTP send (async) + IMAP fetch (in thread executor).

Configurazione via tabella channel_config (channel='email'). Schema config:
{
  "smtp_host": "smtp.gmail.com",
  "smtp_port": 587,
  "smtp_user": "...",
  "smtp_password": "..."  # opzionale, env SMTP_PASSWORD ha priorità
  "smtp_use_tls": true,
  "imap_host": "imap.gmail.com",
  "imap_port": 993,
  "imap_user": "...",
  "imap_password": "..."  # opzionale, env IMAP_PASSWORD ha priorità
  "imap_folder": "INBOX",
  "from_address": "...",
  "reply_to": null,
  "rate_limit_per_minute": 10
}
"""
from __future__ import annotations

import asyncio
import logging
import re
from email.message import EmailMessage
from email.utils import make_msgid, parseaddr
from typing import Any

import aiosmtplib

from .base import InboundMessage, get_config, resolve_secret


log = logging.getLogger(__name__)


def _smtp_password(cfg: dict[str, Any]) -> str | None:
    return resolve_secret("SMTP_PASSWORD", cfg.get("smtp_password"))


def _imap_password(cfg: dict[str, Any]) -> str | None:
    return resolve_secret("IMAP_PASSWORD", cfg.get("imap_password"))


async def send_email(
    to_address: str,
    subject: str,
    body: str,
    in_reply_to: str | None = None,
    references: str | None = None,
) -> str:
    """Invia un'email tramite SMTP. Ritorna il Message-ID generato.

    Solleva RuntimeError se la config non è completa.
    """
    cfg = get_config("email")
    if not cfg:
        raise RuntimeError("Canale email non configurato (vai su /settings).")

    host = cfg.get("smtp_host")
    port = int(cfg.get("smtp_port") or 587)
    user = cfg.get("smtp_user")
    password = _smtp_password(cfg)
    from_addr = cfg.get("from_address") or user
    use_tls = bool(cfg.get("smtp_use_tls", True))

    if not (host and user and password and from_addr):
        raise RuntimeError(
            "Config email incompleta: serve smtp_host, smtp_user, smtp_password (o "
            "SMTP_PASSWORD env), from_address."
        )

    msg = EmailMessage()
    msg["From"] = from_addr
    msg["To"] = to_address
    msg["Subject"] = subject
    if cfg.get("reply_to"):
        msg["Reply-To"] = cfg["reply_to"]
    message_id = make_msgid(domain=from_addr.split("@", 1)[-1] if "@" in from_addr else "agentscraper.local")
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


async def send_test_email(to_address: str, subject: str = "AgentScraper test") -> str:
    return await send_email(
        to_address,
        subject,
        "Questa è una email di test inviata da AgentScraper.\n\n"
        "Se l'hai ricevuta, la configurazione SMTP funziona.",
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
    password = _imap_password(cfg) or _smtp_password(cfg)
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


async def fetch_inbound(limit: int = 50) -> list[InboundMessage]:
    """Polling IMAP — ritorna nuove email come InboundMessage."""
    cfg = get_config("email")
    if not cfg:
        return []
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
