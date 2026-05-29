"""Test Fogli collaborativi — route HTTP (Fase 2).

End-to-end con TestClient: creazione, editor, snapshot, patch HTTP, e gating
permessi (operatore collaborativo vs viewer di progetto vs cross-tenant).
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import db, db_cloud
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
    db_cloud.create_user(tenant_id=ta, email="arch-a@a.it", password_hash=hash_password("pw"), role="tenant_architect")
    db_cloud.create_user(tenant_id=ta, email="op-a@a.it", password_hash=hash_password("pw"), role="tenant_user")
    db_cloud.create_user(tenant_id=ta, email="op-a2@a.it", password_hash=hash_password("pw"), role="tenant_user")
    db_cloud.create_user(tenant_id=tb, email="arch-b@b.it", password_hash=hash_password("pw"), role="tenant_architect")
    return {"ta": ta, "tb": tb}


def _login(client, email):
    r = client.post("/login", data={"email": email, "password": "pw", "next": "/"}, follow_redirects=False)
    assert r.status_code == 303, r.text[:200]


def test_operator_creates_and_edits_tenant_sheet(env):
    from app.main import app
    with TestClient(app) as client:
        _login(client, "op-a@a.it")
        # crea
        r = client.post("/sheets", data={"title": "Budget", "visibility": "tenant"}, follow_redirects=False)
        assert r.status_code == 303
        loc = r.headers["location"]
        assert loc.startswith("/sheets/")
        sid = int(loc.rsplit("/", 1)[1])
        # editor render
        r = client.get(loc)
        assert r.status_code == 200
        assert "Budget" in r.text
        assert "sheet-grid" in r.text  # griglia presente
        # snapshot
        r = client.get(f"/sheets/{sid}/snapshot")
        assert r.status_code == 200
        snap = r.json()
        assert snap["revision"] == 0 and snap["can_edit"] is True and snap["cells"] == []
        # patch
        r = client.post(f"/sheets/{sid}/patch", json={"patch_id": "x1", "cells": [{"row": 0, "col": 0, "value": "Acme"}]})
        assert r.status_code == 200
        assert r.json()["revision"] == 1
        # snapshot riflette
        snap = client.get(f"/sheets/{sid}/snapshot").json()
        assert snap["revision"] == 1
        assert snap["cells"][0]["value"] == "Acme"


def test_other_operator_can_edit_shared_tenant_sheet(env):
    from app.main import app
    with TestClient(app) as client:
        _login(client, "op-a@a.it")
        sid = int(client.post("/sheets", data={"title": "S", "visibility": "tenant"},
                              follow_redirects=False).headers["location"].rsplit("/", 1)[1])
    # altro operatore stesso tenant: modifica consentita (tenant-collaborativo)
    with TestClient(app) as client2:
        _login(client2, "op-a2@a.it")
        r = client2.post(f"/sheets/{sid}/patch", json={"cells": [{"row": 1, "col": 1, "value": "ok"}]})
        assert r.status_code == 200


def test_private_sheet_not_visible_to_other_operator(env):
    from app.main import app
    with TestClient(app) as client:
        _login(client, "op-a@a.it")
        sid = int(client.post("/sheets", data={"title": "Privato", "visibility": "user"},
                              follow_redirects=False).headers["location"].rsplit("/", 1)[1])
    with TestClient(app) as client2:
        _login(client2, "op-a2@a.it")
        assert client2.get(f"/sheets/{sid}").status_code == 404
        assert client2.get(f"/sheets/{sid}/snapshot").status_code == 404
        assert client2.post(f"/sheets/{sid}/patch", json={"cells": [{"row": 0, "col": 0, "value": "x"}]}).status_code == 404


def test_project_viewer_cannot_edit_attached_sheet(env):
    ctx = env
    # progetto user-visibility owned by op-a, con op-a2 come viewer
    op_a = db_cloud.get_user_by_email("op-a@a.it")["id"]
    op_a2 = db_cloud.get_user_by_email("op-a2@a.it")["id"]
    pid = fdb.create_project(title="P", visibility="user", tenant_id=ctx["ta"], owner_user_id=op_a)
    fdb.add_project_member(pid, op_a2, role="viewer")
    sid = sdb.create_sheet(title="Foglio", project_id=pid, tenant_id=ctx["ta"], created_by_user_id=op_a)

    from app.main import app
    with TestClient(app) as client:
        _login(client, "op-a2@a.it")
        # viewer puo' aprire e leggere
        assert client.get(f"/sheets/{sid}").status_code == 200
        assert client.get(f"/sheets/{sid}/snapshot").status_code == 200
        # ma NON modificare
        r = client.post(f"/sheets/{sid}/patch", json={"cells": [{"row": 0, "col": 0, "value": "x"}]})
        assert r.status_code == 403


def test_cross_tenant_sheet_not_accessible(env):
    ctx = env
    op_a = db_cloud.get_user_by_email("op-a@a.it")["id"]
    sid = sdb.create_sheet(title="Secret A", tenant_id=ctx["ta"], created_by_user_id=op_a)
    from app.main import app
    with TestClient(app) as client:
        _login(client, "arch-b@b.it")  # tenant B
        assert client.get(f"/sheets/{sid}").status_code == 404
        assert client.get(f"/sheets/{sid}/snapshot").status_code == 404
        assert client.post(f"/sheets/{sid}/patch", json={"cells": [{"row": 0, "col": 0, "value": "h"}]}).status_code == 404


def test_manage_gating_rename_archive(env):
    ctx = env
    op_a = db_cloud.get_user_by_email("op-a@a.it")["id"]
    # foglio condiviso creato da op-a
    sid = sdb.create_sheet(title="S", visibility="tenant", tenant_id=ctx["ta"], created_by_user_id=op_a)
    from app.main import app
    # op-a2 (non creatore, non architetto) NON puo' rinominare
    with TestClient(app) as client:
        _login(client, "op-a2@a.it")
        assert client.post(f"/sheets/{sid}/rename", data={"title": "X"}, follow_redirects=False).status_code == 403
    # architetto SI
    with TestClient(app) as client:
        _login(client, "arch-a@a.it")
        assert client.post(f"/sheets/{sid}/rename", data={"title": "Rinominato"}, follow_redirects=False).status_code == 303
    assert sdb.get_sheet(sid, tenant_id=ctx["ta"], current_user_id=op_a)["title"] == "Rinominato"


def test_sheets_list_page_renders(env):
    from app.main import app
    with TestClient(app) as client:
        _login(client, "op-a@a.it")
        client.post("/sheets", data={"title": "Mio foglio", "visibility": "tenant"}, follow_redirects=False)
        r = client.get("/sheets")
        assert r.status_code == 200
        assert "Mio foglio" in r.text
