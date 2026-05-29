"""Test WebSocket realtime (Fase 3) via TestClient.

Copre: auth (close 4401), hello->snapshot, cell_patch->revision_patch echo,
recupero incrementale via last_revision, gating edit (viewer forbidden),
isolamento cross-tenant (close 4404), ping/pong.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app import db_cloud
from app.auth import hash_password
from app.fascicoli import db as fdb, sheets_db as sdb


@pytest.fixture
def env(tmp_path, monkeypatch):
    from app import config, storage
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "RESULTS_DIR", tmp_path / "results")
    monkeypatch.setattr(storage, "RESULTS_DIR", tmp_path / "results")
    ta = db_cloud.create_tenant("TenantA", "tenant-a")
    tb = db_cloud.create_tenant("TenantB", "tenant-b")
    db_cloud.create_user(tenant_id=ta, email="op-a@a.it", password_hash=hash_password("pw"), role="tenant_user")
    db_cloud.create_user(tenant_id=ta, email="op-a2@a.it", password_hash=hash_password("pw"), role="tenant_user")
    db_cloud.create_user(tenant_id=tb, email="op-b@b.it", password_hash=hash_password("pw"), role="tenant_user")
    return {"ta": ta, "tb": tb}


def _login(client, email):
    r = client.post("/login", data={"email": email, "password": "pw", "next": "/"}, follow_redirects=False)
    assert r.status_code == 303


def _recv_until(ws, wanted, limit=12):
    """Riceve fino a trovare un messaggio del tipo in `wanted` (set/str)."""
    if isinstance(wanted, str):
        wanted = {wanted}
    for _ in range(limit):
        m = ws.receive_json()
        if m.get("type") in wanted:
            return m
    raise AssertionError("messaggio atteso non ricevuto: " + str(wanted))


def test_ws_requires_auth(env):
    from app.main import app
    with TestClient(app) as client:
        # nessun login -> handshake accettato poi close 4401
        with client.websocket_connect("/ws/sheets/1") as ws:
            with pytest.raises(WebSocketDisconnect) as exc:
                ws.receive_json()
            assert exc.value.code == 4401


def test_ws_hello_snapshot_and_patch_echo(env):
    ctx = env
    op_a = db_cloud.get_user_by_email("op-a@a.it")["id"]
    sid = sdb.create_sheet(title="S", tenant_id=ctx["ta"], created_by_user_id=op_a)
    sdb.apply_cell_patch(sid, [{"row": 0, "col": 0, "value": "seed"}], tenant_id=ctx["ta"], actor_user_id=op_a)

    from app.main import app
    with TestClient(app) as client:
        _login(client, "op-a@a.it")
        with client.websocket_connect(f"/ws/sheets/{sid}") as ws:
            ws.send_json({"type": "hello", "last_revision": -1})
            snap = _recv_until(ws, "snapshot")
            assert snap["revision"] == 1
            assert snap["cells"][0]["value"] == "seed"
            # invia una patch -> ricevi revision_patch (echo a se stesso) con patch_id
            ws.send_json({"type": "cell_patch", "patch_id": "pid1",
                          "cells": [{"row": 1, "col": 1, "value": "hello"}]})
            rp = _recv_until(ws, "revision_patch")
            assert rp["revision"] == 2
            assert rp["patch_id"] == "pid1"
            assert rp["cells"][0]["value"] == "hello"


def test_ws_incremental_recovery_via_last_revision(env):
    ctx = env
    op_a = db_cloud.get_user_by_email("op-a@a.it")["id"]
    sid = sdb.create_sheet(title="S", tenant_id=ctx["ta"], created_by_user_id=op_a)
    for v in ["a", "b", "c"]:
        sdb.apply_cell_patch(sid, [{"row": 0, "col": 0, "value": v}], tenant_id=ctx["ta"], actor_user_id=op_a)
    from app.main import app
    with TestClient(app) as client:
        _login(client, "op-a@a.it")
        with client.websocket_connect(f"/ws/sheets/{sid}") as ws:
            # client fermo a rev 1 -> deve ricevere le revision 2 e 3 (no snapshot)
            ws.send_json({"type": "hello", "last_revision": 1})
            r2 = _recv_until(ws, {"revision_patch", "snapshot"})
            assert r2["type"] == "revision_patch" and r2["revision"] == 2
            r3 = _recv_until(ws, "revision_patch")
            assert r3["revision"] == 3


def test_ws_ping_pong(env):
    ctx = env
    op_a = db_cloud.get_user_by_email("op-a@a.it")["id"]
    sid = sdb.create_sheet(title="S", tenant_id=ctx["ta"], created_by_user_id=op_a)
    from app.main import app
    with TestClient(app) as client:
        _login(client, "op-a@a.it")
        with client.websocket_connect(f"/ws/sheets/{sid}") as ws:
            ws.send_json({"type": "ping"})
            assert _recv_until(ws, "pong")["type"] == "pong"


def test_ws_viewer_cannot_patch(env):
    ctx = env
    op_a = db_cloud.get_user_by_email("op-a@a.it")["id"]
    op_a2 = db_cloud.get_user_by_email("op-a2@a.it")["id"]
    pid = fdb.create_project(title="P", visibility="user", tenant_id=ctx["ta"], owner_user_id=op_a)
    fdb.add_project_member(pid, op_a2, role="viewer")
    sid = sdb.create_sheet(title="F", project_id=pid, tenant_id=ctx["ta"], created_by_user_id=op_a)
    from app.main import app
    with TestClient(app) as client:
        _login(client, "op-a2@a.it")  # viewer
        with client.websocket_connect(f"/ws/sheets/{sid}") as ws:
            ws.send_json({"type": "hello", "last_revision": -1})
            _recv_until(ws, "snapshot")
            ws.send_json({"type": "cell_patch", "cells": [{"row": 0, "col": 0, "value": "x"}]})
            err = _recv_until(ws, "error")
            assert err["code"] == "forbidden"
    # nessuna cella scritta
    assert sdb.get_cells(sid, tenant_id=ctx["ta"]) == []


def test_ws_cross_tenant_closed(env):
    ctx = env
    op_a = db_cloud.get_user_by_email("op-a@a.it")["id"]
    sid = sdb.create_sheet(title="Secret A", tenant_id=ctx["ta"], created_by_user_id=op_a)
    from app.main import app
    with TestClient(app) as client:
        _login(client, "op-b@b.it")  # tenant B
        with client.websocket_connect(f"/ws/sheets/{sid}") as ws:
            # error not_found poi close 4404
            err = ws.receive_json()
            assert err["type"] == "error" and err["code"] == "not_found"
            with pytest.raises(WebSocketDisconnect) as exc:
                ws.receive_json()
            assert exc.value.code == 4404
