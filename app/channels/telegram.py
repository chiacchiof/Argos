"""Canale Telegram: send + getUpdates polling tramite Bot API REST.

Fonte unica di config: tabella `telegram_bots` (multi-bot, tenant-scoped, token
cifrato Fernet con ARGOS_SECRET). Gestita da /accounts/messaging.

Le funzioni accettano un parametro opzionale `bot`:
 - se passato (row di telegram_bots) la usa direttamente
 - se None fa lookup del primo bot `active` del tenant corrente

Vincolo Telegram: il bot riceve messaggi SOLO da utenti che gli hanno scritto
per primi (tramite link t.me/<botname>). Outreach proattivo non è possibile
senza che il contatto avvii la conversazione.

Pre-2026-05-22 esisteva una fallback `channel_config('telegram')` (singleton
legacy) con env var `TELEGRAM_BOT_TOKEN`. Rimosso — la migration al boot
(`db.migrate_legacy_channels_to_accounts`) ha gia' creato un bot
`legacy-default` per chi aveva quella config.
"""
from __future__ import annotations

import logging
from typing import Any

import httpx

from .. import db
from .base import InboundMessage


log = logging.getLogger(__name__)
API_BASE = "https://api.telegram.org/bot{token}/{method}"


def _resolve_bot(bot: dict[str, Any] | None) -> dict[str, Any]:
    """Ritorna il bot passato o il primo `active` del tenant. RuntimeError se
    nessun bot configurato."""
    if bot is not None:
        return bot
    bots = db.list_telegram_bots(status="active")
    if not bots:
        raise RuntimeError(
            "Nessun bot Telegram configurato. Aggiungine uno su /accounts/messaging."
        )
    return bots[0]


def _bot_token(bot: dict[str, Any]) -> str:
    """Decifra `encrypted_bot_token` di una row telegram_bots."""
    from ..agent.social.crypto_creds import decrypt
    token = decrypt(bot["encrypted_bot_token"]) if bot.get("encrypted_bot_token") else None
    if not token:
        raise RuntimeError(
            f"telegram_bot id={bot.get('id')} ha token vuoto/non decifrabile."
        )
    return token


async def _call(method: str, *, bot: dict[str, Any] | None = None, **params: Any) -> dict[str, Any]:
    token = _bot_token(_resolve_bot(bot))
    url = API_BASE.format(token=token, method=method)
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(url, json=params)
    data = r.json()
    if not data.get("ok"):
        raise RuntimeError(f"Telegram API error ({method}): {data}")
    return data.get("result") or {}


async def send_message(
    chat_id: str | int,
    body: str,
    bot: dict[str, Any] | None = None,
) -> str:
    """Invia un messaggio. Ritorna lo message_id come stringa."""
    res = await _call("sendMessage", bot=bot, chat_id=chat_id, text=body)
    return str(res.get("message_id") or "")


async def send_test_message(
    chat_id: str | int,
    bot: dict[str, Any] | None = None,
) -> str:
    return await send_message(
        chat_id,
        "🤖 Questo è un messaggio di test da Argos.\n"
        "Se lo leggi, la configurazione del bot Telegram funziona.",
        bot=bot,
    )


async def get_me(bot: dict[str, Any] | None = None) -> dict[str, Any]:
    """Verifica che il token sia valido."""
    return await _call("getMe", bot=bot)


async def fetch_inbound(
    limit: int = 100,
    bot: dict[str, Any] | None = None,
) -> list[InboundMessage]:
    """Polling getUpdates. Aggiorna `last_update_id` per-bot.

    Ritorna SOLO messaggi nuovi (incrementali via offset). Se nessun bot
    attivo, ritorna [].
    """
    if bot is None:
        bots = db.list_telegram_bots(status="active")
        if not bots:
            return []
        bot = bots[0]

    offset = int(bot.get("last_update_id") or 0)

    try:
        updates = await _call("getUpdates", bot=bot, offset=offset, timeout=0, limit=limit)
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

    # persisti il nuovo offset per-bot
    if max_update_id != offset:
        try:
            db.update_telegram_bot(int(bot["id"]), last_update_id=max_update_id)
        except Exception as e:
            log.warning("update_telegram_bot.last_update_id failed: %s", e)

    return out
