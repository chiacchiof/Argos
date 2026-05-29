"""Test del bus Redis opzionale (Fase 4), path DISATTIVO.

Garanzia critica per il default single-worker: con REDIS_URL vuoto, tutte le
operazioni Redis sono no-op e il realtime resta in-memory senza errori.
(Il path multi-worker con Redis reale richiede un server e si testa in staging.)
"""
from __future__ import annotations

import asyncio

from app.fascicoli import realtime_redis as rr


def test_disabled_when_no_url(monkeypatch):
    from app import config
    monkeypatch.setattr(config.settings, "redis_url", "", raising=False)
    assert rr.is_enabled() is False


def test_enabled_flag_reads_url(monkeypatch):
    from app import config
    monkeypatch.setattr(config.settings, "redis_url", "redis://localhost:6379/0", raising=False)
    assert rr.is_enabled() is True


def test_noops_when_inactive():
    async def go():
        # _client e' None (start mai chiamato): tutte le op sono no-op safe
        await rr.publish(1, {"type": "x"})
        await rr.touch_presence(1, 5, "a@x.it")
        assert await rr.merged_presence(1) is None
    asyncio.run(go())


def test_start_noop_without_url(monkeypatch):
    from app import config
    monkeypatch.setattr(config.settings, "redis_url", "", raising=False)

    async def go():
        await rr.start()      # deve uscire subito, niente client
        assert rr.is_active() is False
        await rr.stop()       # idempotente
    asyncio.run(go())


def test_broadcast_presence_uses_local_when_redis_off():
    """realtime.broadcast_presence non deve esplodere quando Redis e' off:
    merged_presence ritorna None -> fallback alla presenza locale."""
    from app.fascicoli import realtime as rt

    class FakeWS:
        def __init__(self): self.sent = []
        async def send_json(self, m): self.sent.append(m)

    async def go():
        rt._rooms.clear()
        c = rt.Connection(FakeWS(), 3, "c@x.it")
        await rt.register(99, c)
        # almeno un messaggio presence con l'utente locale
        pres = [m for m in c.ws.sent if m["type"] == "presence"]
        assert pres and any(u["user_id"] == 3 for u in pres[-1]["users"])
        rt._rooms.clear()
    asyncio.run(go())
