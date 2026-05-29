"""Condivisione fascicoli (project_users) via modale: ruolo per-utente, rimozione,
ricerca/aggiunta, e semantica 'editor = upload + creazione fogli'."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import db_cloud
from app.auth import hash_password
from app.fascicoli import db as fdb


@pytest.fixture
def env(tmp_path, monkeypatch):
    from app import config, storage
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "RESULTS_DIR", tmp_path / "results")
    monkeypatch.setattr(storage, "RESULTS_DIR", tmp_path / "results")
    ta = db_cloud.create_tenant("TA", "ta")
    tb = db_cloud.create_tenant("TB", "tb")
    owner = db_cloud.create_user(tenant_id=ta, email="own@a.it", password_hash=hash_password("pw"), role="tenant_user")
    other = db_cloud.create_user(tenant_id=ta, email="oth@a.it", password_hash=hash_password("pw"), role="tenant_user")
    bob = db_cloud.create_user(tenant_id=tb, email="bob@b.it", password_hash=hash_password("pw"), role="tenant_user")
    pid = fdb.create_project(title="ProjShare", visibility="user", tenant_id=ta, owner_user_id=owner)
    return {"ta": ta, "tb": tb, "owner": owner, "other": other, "bob": bob, "pid": pid}


def _login(client, email):
    r = client.post("/login", data={"email": email, "password": "pw", "next": "/"}, follow_redirects=False)
    assert r.status_code == 303


def test_project_share_flow(env):
    c = env
    from app.main import app
    with TestClient(app) as client:
        _login(client, "own@a.it")
        # modale: owner come Proprietario, 'other' aggiungibile
        r = client.get(f"/fascicoli/{c['pid']}/share")
        assert r.status_code == 200
        assert "Proprietario" in r.text and "oth@a.it" in r.text
        # aggiungi 'other' come editor (Modifica)
        r = client.post(f"/fascicoli/{c['pid']}/share", data={"user_id": c["other"], "role": "editor"})
        assert r.status_code == 200
        members = {m["user_id"]: m["role"] for m in fdb.list_project_members(c["pid"])}
        assert members.get(c["other"]) == "editor"
        # cambia a viewer
        client.post(f"/fascicoli/{c['pid']}/share", data={"user_id": c["other"], "role": "viewer"})
        assert fdb.list_project_members(c["pid"])[0]["role"] == "viewer"
        # rimuovi (none)
        client.post(f"/fascicoli/{c['pid']}/share", data={"user_id": c["other"], "role": "none"})
        assert fdb.list_project_members(c["pid"]) == []
        # cambia visibilita'
        r = client.post(f"/fascicoli/{c['pid']}/share/visibility", data={"visibility": "tenant"})
        assert r.status_code == 200
        assert fdb.get_project(c["pid"], current_user_id=c["owner"])["visibility"] == "tenant"


def test_project_share_cross_tenant_blocked(env):
    c = env
    from app.main import app
    with TestClient(app) as client:
        _login(client, "own@a.it")
        r = client.post(f"/fascicoli/{c['pid']}/share", data={"user_id": c["bob"], "role": "editor"})
        assert r.status_code == 400
        assert fdb.list_project_members(c["pid"]) == []


def test_project_share_requires_manage(env):
    c = env
    # 'other' e' solo viewer -> non puo' gestire la condivisione
    fdb.add_project_member(c["pid"], c["other"], role="viewer")
    from app.main import app
    with TestClient(app) as client:
        _login(client, "oth@a.it")
        assert client.get(f"/fascicoli/{c['pid']}/share").status_code == 403


def test_editor_can_create_sheet_viewer_cannot(env):
    c = env
    from app.main import app
    # viewer non puo' creare fogli nel progetto
    fdb.add_project_member(c["pid"], c["other"], role="viewer")
    with TestClient(app) as client:
        _login(client, "oth@a.it")
        r = client.post("/sheets", data={"title": "X", "visibility": "tenant", "project_id": c["pid"]},
                        follow_redirects=False)
        assert r.status_code == 403
    # promosso a editor: puo' creare
    fdb.add_project_member(c["pid"], c["other"], role="editor")
    with TestClient(app) as client:
        _login(client, "oth@a.it")
        r = client.post("/sheets", data={"title": "X", "visibility": "tenant", "project_id": c["pid"]},
                        follow_redirects=False)
        assert r.status_code == 303
