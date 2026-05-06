"""Canale Telegram: send + getUpdates polling tramite Bot API REST.

Configurazione via tabella channel_config (channel='telegram'). Schema config:
{
  "bot_token": "...",   # opzionale, env TELEGRAM_BOT_TOKEN ha priorità
  "polling_offset": 0   # ultimo update_id processato
}

Vincolo Telegram: il bot riceve messaggi SOLO da utenti che gli hanno scritto
per primi (tramite il link t.me/<botname>). Outreach proattivo non è possibile
senza che il contatto avvii la conversazione.
"""
from __future__ import annotations

import logging
from typing import Any

import httpx

from .. import db
from .base import InboundMessage, get_config, resolve_secret


log = logging.getLogger(__name__)
API_BASE = "https://api.telegram.org/bot{token}/{method}"


def _bot_token(cfg: dict[str, Any] | None = None) -> str | None:
    cfg = cfg or get_config("telegram")
    return resolve_secret("TELEGRAM_BOT_TOKEN", cfg.get("bot_token"))


async def _call(method: str, **params: Any) -> dict[str, Any]:
    token = _bot_token()
    if not token:
        raise RuntimeError(
            "Bot token Telegram mancante: imposta TELEGRAM_BOT_TOKEN nell'env "
            "oppure compila il campo in /settings."
        )
    url = API_BASE.format(token=token, method=method)
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(url, json=params)
    data = r.json()
    if not data.get("ok"):
        raise RuntimeError(f"Telegram API error ({method}): {data}")
    return data.get("result") or {}


async def send_message(chat_id: str | int, body: str) -> str:
    """Invia un messaggio. Ritorna lo message_id come stringa."""
    res = await _call("sendMessage", chat_id=chat_id, text=body)
    return str(res.get("message_id") or "")


async def send_test_message(chat_id: str | int) -> str:
    return await send_message(
        chat_id,
        "🤖 Questo è un messaggio di test da AgentScraper.\n"
        "Se lo leggi, la configurazione del bot Telegram funziona.",
    )


async def get_me() -> dict[str, Any]:
    """Verifica che il token sia valido."""
    return await _call("getMe")


async def fetch_inbound(limit: int = 100) -> list[InboundMessage]:
    """Polling getUpdates. Aggiorna polling_offset nel DB.

    Ritorna SOLO messaggi nuovi (incrementali via offset).
    """
    cfg = get_config("telegram")
    if not cfg:
        return []
    if not _bot_token(cfg):
        return []

    offset = int(cfg.get("polling_offset") or 0)
    try:
        updates = await _call("getUpdates", offset=offset, timeout=0, limit=limit)
    except Exception as e:  # pragma: no cover
        log.warning("Telegram getUpdates failed: %s", e)
        return []

    if not isinstance(updates, list):
        return []

    out: list[InboundMessage] = []
    max_update_id = offset
    for upd in updates:
        max_update_id = max(max_update_id, int(upd.get("update_id") or 0) + 1)
        msg = upd.get("message") or upd.get("edited_message")
        if not msg:
            continue
        chat = msg.get("chat") or {}
        sender = msg.get("from") or {}
        body = msg.get("text") or msg.get("caption") or ""
        out.append(
            InboundMessage(
                channel="telegram",
                external_id=str(msg.get("message_id")),
                sender_address=str(chat.get("id") or sender.get("id") or ""),
                sender_name=" ".join(
                    filter(None, [sender.get("first_name"), sender.get("last_name")])
                ).strip() or None,
                sender_telegram_username=sender.get("username") or None,
                subject=None,
                body=body,
                in_reply_to=None,
            )
        )

    # persisti il nuovo offset
    if max_update_id != offset:
        cfg_clean = {k: v for k, v in cfg.items() if not k.startswith("_")}
        cfg_clean["polling_offset"] = max_update_id
        db.save_channel_config("telegram", cfg_clean, enabled=True)

    return out
