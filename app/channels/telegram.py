"""Canale Telegram: send + getUpdates polling tramite Bot API REST.

Due fonti di config supportate:
  1) Multi-bot (preferito): tabella `telegram_bots` con Fernet-encrypted token.
     Passa `bot=db.get_telegram_bot(id)` (o dict analogo). `last_update_id` per
     polling è memorizzato per-bot.
  2) Legacy singleton: tabella `channel_config` channel='telegram' (back-compat).
     Usato se nessun `bot` viene passato.

Vincolo Telegram: il bot riceve messaggi SOLO da utenti che gli hanno scritto
per primi (tramite link t.me/<botname>). Outreach proattivo non è possibile
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


def _bot_token_from_dict(bot: dict[str, Any]) -> str:
    """Decifra `encrypted_bot_token` di una row telegram_bots."""
    from ..agent.social.crypto_creds import decrypt
    return decrypt(bot["encrypted_bot_token"])


def _legacy_bot_token(cfg: dict[str, Any] | None = None) -> str | None:
    cfg = cfg or get_config("telegram")
    if not cfg:
        return None
    return resolve_secret("TELEGRAM_BOT_TOKEN", cfg.get("bot_token"))


def _resolve_token(bot: dict[str, Any] | None) -> str:
    """Restituisce il token: bot dict ha priorità, fallback al singleton."""
    if bot is not None:
        token = _bot_token_from_dict(bot)
        if not token:
            raise RuntimeError(
                f"telegram_bot id={bot.get('id')} ha token vuoto/non decifrabile."
            )
        return token
    token = _legacy_bot_token()
    if not token:
        raise RuntimeError(
            "Bot token Telegram mancante: configura un bot in /accounts/messaging "
            "oppure imposta TELEGRAM_BOT_TOKEN nell'env."
        )
    return token


async def _call(method: str, *, bot: dict[str, Any] | None = None, **params: Any) -> dict[str, Any]:
    token = _resolve_token(bot)
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
    """Polling getUpdates. Aggiorna `last_update_id` (per-bot) o `polling_offset`
    (singleton legacy).

    Ritorna SOLO messaggi nuovi (incrementali via offset).
    """
    if bot is not None:
        offset = int(bot.get("last_update_id") or 0)
        token_ok = True
    else:
        cfg = get_config("telegram")
        if not cfg:
            return []
        if not _legacy_bot_token(cfg):
            return []
        offset = int(cfg.get("polling_offset") or 0)
        token_ok = True

    if not token_ok:
        return []

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

    # persisti il nuovo offset
    if max_update_id != offset:
        if bot is not None:
            try:
                db.update_telegram_bot(int(bot["id"]), last_update_id=max_update_id)
            except Exception as e:
                log.warning("update_telegram_bot.last_update_id failed: %s", e)
        else:
            cfg = get_config("telegram") or {}
            cfg_clean = {k: v for k, v in cfg.items() if not k.startswith("_")}
            cfg_clean["polling_offset"] = max_update_id
            db.save_channel_config("telegram", cfg_clean, enabled=True)

    return out
