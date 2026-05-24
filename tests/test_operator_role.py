"""Test del ruolo `tenant_user` come operator: gating, redirect, dashboard."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import db, db_cloud
from app.auth import (
    CurrentUser,
    hash_password,
    require_architect_or_admin,
    require_operator,
)


@pytest.fixture
def setup_users():
    """Crea tenant + super_admin + tenant_architect + tenant_user (operator)."""
    tenant = db_cloud.create_tenant("OpTenant", "optnt")
    admin = db_cloud.create_user(
        tenant_id=None, email="admin@op", password_hash=hash_password("pw"),
        role="super_admin",
    )
    architect = db_cloud.create_user(
        tenant_id=tenant, email="arch@op", password_hash=hash_password("pw"),
        role="tenant_architect",
    )
    operator = db_cloud.create_user(
        tenant_id=tenant, email="op@op", password_hash=hash_password("pw"),
        role="tenant_user",
    )
    return {
        "tenant": tenant, "admin_id": admin,
        "architect_id": architect, "operator_id": operator,
    }


def test_role_check_three_roles_accepted(setup_users):
    """CHECK constraint accetta tutti e 3 i ruoli."""
    assert setup_users["admin_id"]
    assert setup_users["architect_id"]
    assert setup_users["operator_id"]


def test_role_check_rejects_invalid(setup_users):
    """Tentativo di creare utente con ruolo non valido fallisce."""
    with pytest.raises(ValueError):
        db_cloud.create_user(
            tenant_id=setup_users["tenant"], email="bad@op",
            password_hash=hash_password("pw"), role="hacker",
        )


def test_current_user_properties():
    """is_operator / is_architect / can_manage_architecture computati correttamente."""
    u_op = CurrentUser(id=1, email="x@y", role="tenant_user",
                      tenant_id=1, tenant_name="T", is_active=True)
    u_arch = CurrentUser(id=2, email="x@y", role="tenant_architect",
                        tenant_id=1, tenant_name="T", is_active=True)
    u_admin = CurrentUser(id=3, email="x@y", role="super_admin",
                         tenant_id=None, tenant_name=None, is_active=True)

    assert u_op.is_operator and not u_op.is_architect and not u_op.is_super_admin
    assert not u_op.can_manage_architecture

    assert u_arch.is_architect and not u_arch.is_operator and not u_arch.is_super_admin
    assert u_arch.can_manage_architecture

    assert u_admin.is_super_admin and not u_admin.is_architect and not u_admin.is_operator
    assert u_admin.can_manage_architecture


def test_operator_login_redirects_to_home(setup_users):
    """Login operator → redirect a /home (non a /)."""
    from app.main import app
    client = TestClient(app)
    with client:
        r = client.post("/login", data={"email": "op@op", "password": "pw"},
                       follow_redirects=False)
        assert r.status_code == 303
        assert r.headers["location"] == "/home"


def test_architect_login_redirects_to_root(setup_users):
    """Login architect → redirect a / (lista task)."""
    from app.main import app
    client = TestClient(app)
    with client:
        r = client.post("/login", data={"email": "arch@op", "password": "pw"},
                       follow_redirects=False)
        assert r.status_code == 303
        assert r.headers["location"] == "/"


def test_operator_blocked_from_architect_pages(setup_users):
    """Operator accede a /tasks/new → redirect a /home (gating gentile)."""
    from app.main import app
    client = TestClient(app)
    with client:
        client.post("/login", data={"email": "op@op", "password": "pw"},
                   follow_redirects=False)
        for path in ["/tasks/new", "/workflows/new", "/site_memory",
                    "/accounts/llm-keys", "/social/accounts"]:
            r = client.get(path, follow_redirects=False)
            assert r.status_code in (303, 307), f"Path {path}: expected redirect, got {r.status_code}"
            assert r.headers.get("location") == "/home", f"Path {path}: expected redirect to /home, got {r.headers.get('location')}"


def test_operator_can_access_home(setup_users):
    """Operator vede /home senza errori."""
    from app.main import app
    client = TestClient(app)
    with client:
        client.post("/login", data={"email": "op@op", "password": "pw"},
                   follow_redirects=False)
        r = client.get("/home")
        assert r.status_code == 200
        assert "Cosa posso fare per te" in r.text


def test_operator_can_access_messages(setup_users):
    """Operator vede /messages."""
    from app.main import app
    client = TestClient(app)
    with client:
        client.post("/login", data={"email": "op@op", "password": "pw"},
                   follow_redirects=False)
        r = client.get("/messages")
        assert r.status_code == 200
        assert "Messaggi" in r.text


def test_architect_blocked_from_operator_home(setup_users):
    """Architect su /home → 403 (UI semplificata e' per operator)."""
    from app.main import app
    client = TestClient(app)
    with client:
        client.post("/login", data={"email": "arch@op", "password": "pw"},
                   follow_redirects=False)
        r = client.get("/home")
        assert r.status_code == 403
