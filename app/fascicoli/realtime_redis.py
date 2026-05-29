"""Bus realtime Redis OPZIONALE per i Fogli collaborativi (Fase 4).

Argos gira di default a processo singolo: in quel caso il connection manager
in-memory (app/fascicoli/realtime.py) basta e Redis NON serve. Redis si attiva
solo se `REDIS_URL` e' configurato, per scenari multi-worker / multi-server:
fa da bus pub/sub tra i processi cosi' una patch applicata su un worker arriva
ai browser collegati agli ALTRI worker.

Garanzie:
  - Redis NON e' fonte di verita': solo notifica live. La sequenza autorevole
    delle revisioni vive in Postgres (project_sheet_revisions). Se Redis perde
    un evento, il client recupera al reconnect via hello/last_revision.
  - Tutto e' best-effort: ogni errore Redis e' silenziato e degrada al
    comportamento single-worker (in-memory). Una caduta di Redis non rompe mai
    il WebSocket ne' l'editing.

Canali / chiavi (vedi piano §Redis):
  - sheet:{id}:events                 -> pub/sub eventi (revision_patch, cursor, presence)
  - sheet:{id}:presence (hash)        -> presenza cross-worker, field=user_id, TTL refresh 10s
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid

from ..config import settings
from . import realtime

log = logging.getLogger(__name__)

_WORKER_ID = uuid.uuid4().hex      # identita' di questo worker (dedup eco pub/sub)
_PRESENCE_TTL = 30                 # secondi: presenza scade se non rinfrescata
_REFRESH_EVERY = 10                # secondi: intervallo refresh presenza locali

_client = None                     # redis.asyncio.Redis | None
_listen_task: asyncio.Task | None = None
_refresh_task: asyncio.Task | None = None


def is_enabled() -> bool:
    return bool((settings.redis_url or "").strip())


def is_active() -> bool:
    """True se il client Redis e' effettivamente connesso (dopo start())."""
    return _client is not None


# ---------------------------------------------------------------------------
# Lifecycle (chiamato dal lifespan di app.main)
# ---------------------------------------------------------------------------

async def start() -> None:
    global _client, _listen_task, _refresh_task
    if not is_enabled() or _client is not None:
        return
    try:
        import redis.asyncio as aioredis
    except ImportError:
        log.warning("[fogli] REDIS_URL impostato ma pacchetto 'redis' non installato: fan-out multi-worker disabilitato.")
        return
    try:
        client = aioredis.from_url(settings.redis_url, encoding="utf-8", decode_responses=True)
        await client.ping()
    except Exception as exc:
        log.warning("[fogli] Redis non raggiungibile (%s): resto in modalita' in-memory single-worker.", exc)
        return
    _client = client
    _listen_task = asyncio.create_task(_listen_loop())
    _refresh_task = asyncio.create_task(_refresh_loop())
    log.info("[fogli] Redis realtime attivo (worker=%s).", _WORKER_ID[:8])


async def stop() -> None:
    global _client, _listen_task, _refresh_task
    for t in (_listen_task, _refresh_task):
        if t is not None:
            t.cancel()
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
    _listen_task = _refresh_task = None
    if _client is not None:
        try:
            await _client.aclose()
        except Exception:
            pass
        _client = None


# ---------------------------------------------------------------------------
# Pub/sub eventi
# ---------------------------------------------------------------------------

async def publish(sheet_id: int, msg: dict) -> None:
    """Pubblica un evento per gli altri worker. No-op se Redis non attivo.
    L'envelope porta l'origine (worker id) per evitare che il publisher
    riprocessi il proprio messaggio (l'ha gia' fatto in locale)."""
    if _client is None:
        return
    try:
        await _client.publish(f"sheet:{sheet_id}:events",
                              json.dumps({"o": _WORKER_ID, "m": msg}))
    except Exception:
        pass


async def _listen_loop() -> None:
    assert _client is not None
    try:
        pubsub = _client.pubsub()
        await pubsub.psubscribe("sheet:*:events")
        async for message in pubsub.listen():
            if message.get("type") != "pmessage":
                continue
            try:
                channel = message["channel"]
                sheet_id = int(channel.split(":")[1])
                env = json.loads(message["data"])
                if env.get("o") == _WORKER_ID:
                    continue  # eco del nostro stesso messaggio: gia' fan-out locale
                await realtime._local_fanout(sheet_id, env.get("m") or {})
            except Exception:
                continue
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # pragma: no cover
        log.warning("[fogli] listener Redis terminato: %s", exc)


# ---------------------------------------------------------------------------
# Presenza cross-worker (chiavi con TTL)
# ---------------------------------------------------------------------------

async def touch_presence(sheet_id: int, user_id: int, email: str) -> None:
    """Marca/aggiorna la presenza di un utente nel foglio (chiamato al join e
    dal refresh loop)."""
    if _client is None:
        return
    try:
        key = f"sheet:{sheet_id}:presence"
        await _client.hset(key, str(user_id), json.dumps({"email": email, "ts": int(time.time())}))
        await _client.expire(key, _PRESENCE_TTL * 2)
    except Exception:
        pass


async def merged_presence(sheet_id: int):
    """Lista presenza unificata cross-worker, oppure None se Redis non attivo
    (il chiamante usera' la presenza locale in-memory)."""
    if _client is None:
        return None
    try:
        key = f"sheet:{sheet_id}:presence"
        data = await _client.hgetall(key)
    except Exception:
        return None
    now = int(time.time())
    out, stale = [], []
    for uid, val in (data or {}).items():
        try:
            d = json.loads(val)
        except Exception:
            stale.append(uid)
            continue
        if now - int(d.get("ts", 0)) <= _PRESENCE_TTL:
            out.append({"user_id": int(uid), "email": d.get("email"), "color": realtime.color_for(int(uid))})
        else:
            stale.append(uid)
    if stale:
        try:
            await _client.hdel(key, *stale)
        except Exception:
            pass
    return out


async def _refresh_loop() -> None:
    """Ogni 10s rinfresca la presenza Redis per gli utenti LOCALMENTE connessi
    (derivati dalle stanze in-memory). Chi si disconnette smette di essere
    rinfrescato e scade entro _PRESENCE_TTL (cleanup automatico anche su crash)."""
    try:
        while True:
            await asyncio.sleep(_REFRESH_EVERY)
            if _client is None:
                continue
            try:
                for sheet_id, room in list(realtime._rooms.items()):
                    seen = {}
                    for c in room:
                        seen[c.user_id] = c.email
                    for uid, email in seen.items():
                        await touch_presence(sheet_id, uid, email)
            except Exception:
                continue
    except asyncio.CancelledError:
        raise
