"""Realtime hub per i Fogli collaborativi.

Connection manager IN-MEMORY (per-worker): tiene le connessioni WebSocket
raggruppate per `sheet_id` ("stanze"), la presenza e fa il fan-out degli eventi
ai client locali.

Architettura a due livelli, pronta per Redis (Fase 4):
  - `_local_fanout(sheet_id, msg)` invia SOLO ai WebSocket di QUESTO worker.
  - `broadcast(sheet_id, msg)` = `_local_fanout` + (se Redis attivo) publish sul
    canale Redis del foglio, cosi' gli ALTRI worker ricevono e fanno il loro
    `_local_fanout`. In single-worker (default Argos) Redis e' assente e
    `broadcast == _local_fanout`.

Redis e' solo bus realtime: la fonte di verita' resta Postgres
(project_sheet_revisions). Se un evento live si perde, il client recupera al
reconnect via `hello`/`last_revision` (vedi WS handler + sheets_db.get_revisions_since).
"""
from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger(__name__)

_COLORS = ["#38bdf8", "#f59e0b", "#22c55e", "#ef4444", "#a855f7",
           "#ec4899", "#14b8a6", "#f97316", "#6366f1", "#84cc16"]


def color_for(uid: int | None) -> str:
    n = int(uid or 0)
    return _COLORS[n % len(_COLORS)]


class Connection:
    """Una connessione WebSocket a un foglio. `cid` la rende hashabile/unica
    anche se lo stesso utente apre piu' schede."""
    _counter = 0

    def __init__(self, ws, user_id: int, email: str):
        Connection._counter += 1
        self.cid = Connection._counter
        self.ws = ws
        self.user_id = user_id
        self.email = email

    async def send(self, msg: dict) -> None:
        await self.ws.send_json(msg)

    def __hash__(self):
        return self.cid

    def __eq__(self, other):
        return isinstance(other, Connection) and other.cid == self.cid


# sheet_id -> set[Connection]
_rooms: dict[int, set[Connection]] = {}


# ---------------------------------------------------------------------------
# Registrazione connessioni
# ---------------------------------------------------------------------------

async def register(sheet_id: int, conn: Connection) -> None:
    _rooms.setdefault(sheet_id, set()).add(conn)
    await _redis_touch(sheet_id, conn.user_id, conn.email)
    await broadcast_presence(sheet_id)
    log.debug("WS register sheet=%s user=%s (room=%d)", sheet_id, conn.user_id, len(_rooms[sheet_id]))


async def _redis_touch(sheet_id: int, user_id: int, email: str) -> None:
    try:
        from . import realtime_redis
        await realtime_redis.touch_presence(sheet_id, user_id, email)
    except Exception:
        pass


async def unregister(sheet_id: int, conn: Connection) -> None:
    room = _rooms.get(sheet_id)
    if not room:
        return
    room.discard(conn)
    if not room:
        _rooms.pop(sheet_id, None)
    else:
        await broadcast_presence(sheet_id)
    # avvisa gli altri che il cursore di questo utente non c'e' piu' (se nessuna
    # altra sua connessione resta nel foglio)
    if not _user_present(sheet_id, conn.user_id):
        await _local_fanout(sheet_id, {"type": "cursor_gone", "user_id": conn.user_id})


def _user_present(sheet_id: int, user_id: int) -> bool:
    return any(c.user_id == user_id for c in _rooms.get(sheet_id, ()))


def presence_users(sheet_id: int) -> list[dict[str, Any]]:
    """Lista utenti unici presenti nel foglio (dedup per user_id)."""
    seen: dict[int, dict] = {}
    for c in _rooms.get(sheet_id, ()):
        if c.user_id not in seen:
            seen[c.user_id] = {"user_id": c.user_id, "email": c.email, "color": color_for(c.user_id)}
    return list(seen.values())


# ---------------------------------------------------------------------------
# Fan-out
# ---------------------------------------------------------------------------

async def _local_fanout(sheet_id: int, msg: dict, exclude_cid: int | None = None) -> None:
    """Invia `msg` a tutte le connessioni LOCALI del foglio. Rimuove quelle morte."""
    room = _rooms.get(sheet_id)
    if not room:
        return
    dead: list[Connection] = []
    for conn in list(room):
        if exclude_cid is not None and conn.cid == exclude_cid:
            continue
        try:
            await conn.send(msg)
        except Exception:
            dead.append(conn)
    for d in dead:
        room.discard(d)
    if dead and not room:
        _rooms.pop(sheet_id, None)


async def broadcast(sheet_id: int, msg: dict, exclude_cid: int | None = None) -> None:
    """Fan-out locale + (Fase 4) publish su Redis per gli altri worker."""
    await _local_fanout(sheet_id, msg, exclude_cid=exclude_cid)
    # Fase 4: se Redis attivo, publish sul canale del foglio. Importato lazy per
    # non creare dipendenza dura su redis quando non configurato.
    try:
        from . import realtime_redis
        await realtime_redis.publish(sheet_id, msg)
    except Exception:
        pass


async def broadcast_revision(
    sheet_id: int,
    *,
    revision: int,
    actor_user_id: int | None,
    cells: list[dict],
    origin_patch_id: str | None = None,
) -> None:
    """Diffonde una patch applicata (dal WS handler o dal fallback HTTP)."""
    await broadcast(sheet_id, {
        "type": "revision_patch",
        "sheet_id": sheet_id,
        "revision": revision,
        "actor_user_id": actor_user_id,
        "patch_id": origin_patch_id,
        "cells": cells,
    })


async def broadcast_presence(sheet_id: int) -> None:
    await broadcast(sheet_id, {"type": "presence", "users": await _presence_for_broadcast(sheet_id)})


async def _presence_for_broadcast(sheet_id: int) -> list[dict[str, Any]]:
    """Presenza unificata cross-worker se Redis attivo, altrimenti locale."""
    try:
        from . import realtime_redis
        merged = await realtime_redis.merged_presence(sheet_id)
        if merged is not None:
            return merged
    except Exception:
        pass
    return presence_users(sheet_id)


async def broadcast_cursor(
    sheet_id: int, *, user_id: int, email: str, row: int, col: int, selection: Any = None,
    exclude_cid: int | None = None,
) -> None:
    await broadcast(sheet_id, {
        "type": "cursor",
        "user_id": user_id,
        "email": email,
        "row": row,
        "col": col,
        "selection": selection,
    }, exclude_cid=exclude_cid)


def room_size(sheet_id: int) -> int:
    return len(_rooms.get(sheet_id, ()))
