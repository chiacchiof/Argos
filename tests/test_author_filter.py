"""Filtro autore su /tasks e /workflows.

Default: l'utente vede solo i propri task/workflow del tenant. Via toggle
'Tutti del tenant' (?author=tenant) vede anche quelli creati da altri
utenti dello stesso tenant.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import db, db_cloud
from app.auth import hash_password


@pytest.fixture
def author_setup():
    """Setup 1 tenant + 2 utenti. Ogni utente crea 1 task + 1 workflow."""
    tenant = db_cloud.create_tenant("AuthFilter", "afsetup")
    u_alice = db_cloud.create_user(
        tenant_id=tenant, email="alice@af", password_hash=hash_password("pw"),
        role="tenant_architect",
    )
    u_bob = db_cloud.create_user(
        tenant_id=tenant, email="bob@af", password_hash=hash_password("pw"),
        role="tenant_architect",
    )
    t_alice = db.create_task(
        {"name": "Alice task", "objective": "x", "agent_mode": "react"},
        tenant_id=tenant, created_by_user_id=u_alice,
    )
    t_bob = db.create_task(
        {"name": "Bob task", "objective": "x", "agent_mode": "react"},
        tenant_id=tenant, created_by_user_id=u_bob,
    )
    w_alice = db.create_workflow(
        "Alice WF",
        tenant_id=tenant, created_by_user_id=u_alice,
    )
    w_bob = db.create_workflow(
        "Bob WF",
        tenant_id=tenant, created_by_user_id=u_bob,
    )
    return {
        "tenant": tenant,
        "u_alice": u_alice, "u_bob": u_bob,
        "t_alice": t_alice, "t_bob": t_bob,
        "w_alice": w_alice, "w_bob": w_bob,
    }


def test_list_tasks_filter_by_author(author_setup):
    """db.list_tasks(created_by_user_id=N) ritorna solo task creati da N."""
    s = author_setup
    rows = db.list_tasks(tenant_id=s["tenant"], created_by_user_id=s["u_alice"])
    ids = {r["id"] for r in rows}
    assert s["t_alice"] in ids
    assert s["t_bob"] not in ids

    rows_all = db.list_tasks(tenant_id=s["tenant"], created_by_user_id=None)
    ids_all = {r["id"] for r in rows_all}
    assert {s["t_alice"], s["t_bob"]}.issubset(ids_all)


def test_list_workflows_filter_by_author(author_setup):
    s = author_setup
    rows = db.list_workflows(tenant_id=s["tenant"], created_by_user_id=s["u_bob"])
    ids = {r["id"] for r in rows}
    assert s["w_bob"] in ids
    assert s["w_alice"] not in ids


def test_route_tasks_default_mine(author_setup):
    """GET / con login Alice → solo task Alice di default."""
    from app.main import app
    s = author_setup
    client = TestClient(app)
    with client:
        r = client.post("/login", data={"email": "alice@af", "password": "pw"},
                        follow_redirects=False)
        assert r.status_code == 303
        r = client.get("/")
        assert r.status_code == 200
        assert "Alice task" in r.text
        assert "Bob task" not in r.text


def test_route_tasks_author_tenant_shows_all(author_setup):
    """GET /?author=tenant con login Alice → vede entrambi i task."""
    from app.main import app
    s = author_setup
    client = TestClient(app)
    with client:
        client.post("/login", data={"email": "alice@af", "password": "pw"})
        r = client.get("/?author=tenant")
        assert r.status_code == 200
        assert "Alice task" in r.text
        assert "Bob task" in r.text


def test_route_workflows_default_mine(author_setup):
    from app.main import app
    s = author_setup
    client = TestClient(app)
    with client:
        client.post("/login", data={"email": "bob@af", "password": "pw"})
        r = client.get("/workflows")
        assert r.status_code == 200
        assert "Bob WF" in r.text
        assert "Alice WF" not in r.text


def test_route_workflows_author_tenant_shows_all(author_setup):
    from app.main import app
    s = author_setup
    client = TestClient(app)
    with client:
        client.post("/login", data={"email": "bob@af", "password": "pw"})
        r = client.get("/workflows?author=tenant")
        assert r.status_code == 200
        assert "Bob WF" in r.text
        assert "Alice WF" in r.text


def test_invalid_author_value_falls_back_to_mine(author_setup):
    """?author=garbage → comportamento come 'mine' (sicurezza by default)."""
    from app.main import app
    s = author_setup
    client = TestClient(app)
    with client:
        client.post("/login", data={"email": "alice@af", "password": "pw"})
        r = client.get("/?author=garbage_xyz")
        assert r.status_code == 200
        assert "Alice task" in r.text
        assert "Bob task" not in r.text
