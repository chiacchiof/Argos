"""Tipi e helper comuni per i canali di messaggistica."""
from __future__ import annotations

from dataclasses import dataclass

from .. import db


@dataclass
class InboundMessage:
    """Messaggio ricevuto da un canale, pronto per essere inserito in DB."""
    channel: str          # "email" | "telegram"
    external_id: str      # Message-ID o telegram message_id
    sender_address: str   # email mittente o telegram chat_id
    sender_name: str | None
    sender_telegram_username: str | None
    subject: str | None
    body: str
    in_reply_to: str | None = None  # solo email: header In-Reply-To


def is_enabled(channel: str) -> bool:
    """True se il canale ha almeno un account/bot `active` per il tenant.

    Per `email` controlla `email_accounts`, per `telegram` controlla
    `telegram_bots`. Pre-2026-05-22 leggeva `channel_config(channel).enabled`;
    da quando la fonte canonica e' la tabella multi-account quel campo non
    serve piu' (vedi `db.migrate_legacy_channels_to_accounts`).
    """
    if channel == "email":
        try:
            return len(db.list_email_accounts(status="active")) > 0
        except Exception:
            return False
    if channel == "telegram":
        try:
            return len(db.list_telegram_bots(status="active")) > 0
        except Exception:
            return False
    # Altri canali (es. orchestrator) restano su channel_config
    row = db.get_channel_config(channel)
    return bool(row and row.get("enabled"))
