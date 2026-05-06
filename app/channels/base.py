"""Tipi e helper comuni per i canali di messaggistica."""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

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


def get_config(channel: str) -> dict[str, Any]:
    """Carica la config del canale dal DB; ritorna {} se non c'è.

    Le credenziali sensibili (password, token) seguono il pattern env-first:
    se la env var corrispondente è impostata, ha priorità sul valore in DB.
    """
    row = db.get_channel_config(channel)
    if not row:
        return {}
    cfg = row.get("config") or {}
    cfg["_enabled"] = bool(row.get("enabled"))
    return cfg


def is_enabled(channel: str) -> bool:
    cfg = get_config(channel)
    return bool(cfg.get("_enabled"))


def resolve_secret(env_var: str, db_value: str | None) -> str | None:
    """env var prima, valore DB come fallback."""
    val = os.environ.get(env_var)
    if val:
        return val
    return db_value or None
