"""Test condivisione fogli (ACL per-utente project_sheet_users)."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import db_cloud
from app.auth import CurrentUser, hash_password
from app.fascicoli import acl, sheets_db as sdb


@pytest.fixture
def env():
    ta = db_cloud.create_tenant("TA", "ta")
    tb = db_cloud.create_tenant("TB", "tb")
    owner = db_cloud.create_user(tenant_id=ta, email="own@a.it", password_hash=hash_password("x"), role="tenant_user")
    other = db_cloud.create_user(tenant_id=ta, email="oth@a.it", password_hash=hash_password("x"), role="tenant_user")
    bob = db_cloud.create_user(tenant_id=tb, email="bob@b.it", password_hash=hash_password("x"), role="tenant_user")
    return {"ta": ta, "tb": tb, "owner": owner, "other": other, "bob": bob}


def _u(uid, tid, role="tenant_user"):
    return CurrentUser(id=uid, email="u", role=role, tenant_id=tid, tenant_name=None, is_active=True)


# ---- DB + ACL ----------------------------------------------------------------

def test_share_grants_open_then_edit(env):
    c = env
    sid = sdb.create_sheet(title="P", visibility="user", tenant_id=c["ta"], created_by_user_id=c["owner"])
    sheet = sdb.get_sheet(sid, tenant_id=c["ta"], current_user_id=c["owner"])
    other = _u(c["other"], c["ta"])
    # prima della condivisione: niente accesso
    assert acl.can_open_sheet(sheet, None, other) is False
    assert sdb.get_sheet(sid, tenant_id=c["ta"], current_user_id=c["other"]) is None
    # condividi come viewer -> apre ma non modifica
    sdb.add_sheet_member(sid, c["other"], "viewer", tenant_id=c["ta"])
    assert acl.can_open_sheet(sheet, None, other) is True
    assert acl.can_edit_sheet_cells(sheet, None, other) is False
    assert sdb.get_sheet(sid, tenant_id=c["ta"], current_user_id=c["other"]) is not None  # ora visibile
    # promuovi a editor -> modifica
    sdb.add_sheet_member(sid, c["other"], "editor", tenant_id=c["ta"])
    assert acl.can_edit_sheet_cells(sheet, None, other) is True
    # rimuovi -> niente accesso
    sdb.remove_sheet_member(sid, c["other"], tenant_id=c["ta"])
    assert acl.can_open_sheet(sheet, None, other) is False


def test_shared_sheet_appears_in_list(env):
    c = env
    sid = sdb.create_sheet(title="P", visibility="user", tenant_id=c["ta"], created_by_user_id=c["owner"])
    assert sid not in {s["id"] for s in sdb.list_sheets(tenant_id=c["ta"], current_user_id=c["other"])}
    sdb.add_sheet_member(sid, c["other"], "viewer", tenant_id=c["ta"])
    assert sid in {s["id"] for s in sdb.list_sheets(tenant_id=c["ta"], current_user_id=c["other"])}


def test_cannot_share_cross_tenant(env):
    c = env
    sid = sdb.create_sheet(title="P", visibility="user", tenant_id=c["ta"], created_by_user_id=c["owner"])
    with pytest.raises(sdb.SheetForbidden):
        sdb.add_sheet_member(sid, c["bob"], "viewer", tenant_id=c["ta"])  # bob e' del tenant B


def test_list_members(env):
    c = env
    sid = sdb.create_sheet(title="P", visibility="user", tenant_id=c["ta"], created_by_user_id=c["owner"])
    sdb.add_sheet_member(sid, c["other"], "editor", tenant_id=c["ta"])
    members = sdb.list_sheet_members(sid, tenant_id=c["ta"])
    assert len(members) == 1 and members[0]["user_id"] == c["other"] and members[0]["role"] == "editor"
    # cross-tenant non vede i membri
    assert sdb.list_sheet_members(sid, tenant_id=c["tb"]) == []


# ---- Route end-to-end --------------------------------------------------------

@pytest.fixture
def http_env(env, tmp_path, monkeypatch):
    from app import config, storage
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "RESULTS_DIR", tmp_path / "results")
    monkeypatch.setattr(storage, "RESULTS_DIR", tmp_path / "results")
    return env


def _login(client, email):
    r = client.post("/login", data={"email": email, "password": "x", "next": "/"}, follow_redirects=False)
    assert r.status_code == 303


def test_share_route_flow(http_env):
    c = http_env
    sid = sdb.create_sheet(title="P", visibility="user", tenant_id=c["ta"], created_by_user_id=c["owner"])
    from app.main import app
    # owner apre la modale e condivide con 'other' come editor
    with TestClient(app) as client:
        _login(client, "own@a.it")
        r = client.get(f"/sheets/{sid}/share")
        assert r.status_code == 200 and "oth@a.it" in r.text
        r = client.post(f"/sheets/{sid}/share", data={"user_id": c["other"], "role": "editor"})
        assert r.status_code == 200
    # 'other' ora apre e modifica
    with TestClient(app) as client:
        _login(client, "oth@a.it")
        assert client.get(f"/sheets/{sid}").status_code == 200
        assert client.post(f"/sheets/{sid}/patch", json={"cells": [{"row": 0, "col": 0, "value": "x"}]}).status_code == 200
    # owner revoca
    with TestClient(app) as client:
        _login(client, "own@a.it")
        client.post(f"/sheets/{sid}/share", data={"user_id": c["other"], "role": "none"})
    with TestClient(app) as client:
        _login(client, "oth@a.it")
        assert client.get(f"/sheets/{sid}").status_code == 404


def test_non_manager_cannot_open_share(http_env):
    c = http_env
    # foglio tenant creato da owner; 'other' lo vede ma non lo gestisce
    sid = sdb.create_sheet(title="T", visibility="tenant", tenant_id=c["ta"], created_by_user_id=c["owner"])
    from app.main import app
    with TestClient(app) as client:
        _login(client, "oth@a.it")
        assert client.get(f"/sheets/{sid}/share").status_code == 403
