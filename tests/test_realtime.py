"""Test del connection manager realtime in-memory (app/fascicoli/realtime.py).

Pure async, niente DB: usa connessioni fake che catturano i messaggi inviati.
"""
from __future__ import annotations

import asyncio

import pytest

from app.fascicoli import realtime as rt


class FakeWS:
    def __init__(self):
        self.sent = []

    async def send_json(self, msg):
        self.sent.append(msg)


@pytest.fixture(autouse=True)
def _clear_rooms():
    rt._rooms.clear()
    yield
    rt._rooms.clear()


def _conn(uid, email):
    return rt.Connection(FakeWS(), uid, email)


def test_register_broadcasts_presence_and_dedups():
    async def go():
        a1 = _conn(7, "a@x.it")
        a2 = _conn(7, "a@x.it")  # stesso utente, due schede
        b = _conn(9, "b@x.it")
        await rt.register(12, a1)
        await rt.register(12, a2)
        await rt.register(12, b)
        users = rt.presence_users(12)
        # dedup per user_id: 2 utenti distinti (7 e 9)
        assert {u["user_id"] for u in users} == {7, 9}
        # ogni connessione ha ricevuto almeno un messaggio presence
        assert any(m["type"] == "presence" for m in a1.ws.sent)
    asyncio.run(go())


def test_broadcast_revision_reaches_all():
    async def go():
        a = _conn(1, "a@x.it")
        b = _conn(2, "b@x.it")
        await rt.register(5, a)
        await rt.register(5, b)
        await rt.broadcast_revision(5, revision=3, actor_user_id=1,
                                    cells=[{"row": 0, "col": 0, "value": "x"}], origin_patch_id="p1")
        for c in (a, b):
            rp = [m for m in c.ws.sent if m["type"] == "revision_patch"]
            assert rp and rp[-1]["revision"] == 3
            assert rp[-1]["cells"][0]["value"] == "x"
    asyncio.run(go())


def test_cursor_excludes_self():
    async def go():
        a = _conn(1, "a@x.it")
        b = _conn(2, "b@x.it")
        await rt.register(5, a)
        await rt.register(5, b)
        await rt.broadcast_cursor(5, user_id=1, email="a@x.it", row=4, col=2, selection=None, exclude_cid=a.cid)
        # b riceve il cursore, a no
        assert any(m["type"] == "cursor" for m in b.ws.sent)
        assert not any(m["type"] == "cursor" for m in a.ws.sent)
    asyncio.run(go())


def test_unregister_emits_cursor_gone_when_user_leaves():
    async def go():
        a = _conn(1, "a@x.it")
        b = _conn(2, "b@x.it")
        await rt.register(5, a)
        await rt.register(5, b)
        await rt.unregister(5, a)
        # b riceve cursor_gone per l'utente 1
        assert any(m.get("type") == "cursor_gone" and m.get("user_id") == 1 for m in b.ws.sent)
        assert rt.room_size(5) == 1
    asyncio.run(go())


def test_dead_connection_is_pruned():
    async def go():
        class DeadWS:
            async def send_json(self, msg):
                raise RuntimeError("client gone")
        a = rt.Connection(DeadWS(), 1, "a@x.it")
        b = _conn(2, "b@x.it")
        rt._rooms.setdefault(5, set()).update({a, b})
        await rt._local_fanout(5, {"type": "x"})
        # la connessione morta e' stata rimossa, b resta
        assert a not in rt._rooms.get(5, set())
        assert b in rt._rooms.get(5, set())
    asyncio.run(go())
