"""Test first_name/last_name su utenti + display_name helper + admin edit.

Copre:
  - create_user con first_name+last_name
  - get_user/list_users restituiscono i nuovi campi
  - user_display_name helper: nome+cognome > email fallback
  - POST /admin/users/{id}/edit aggiorna first_name+last_name
  - POST /admin/users/{id}/toggle disabilita is_active
  - is_active=False blocca il login
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import db_cloud
from app.auth import hash_password


@pytest.fixture
def name_setup():
    tenant = db_cloud.create_tenant("NameT", "namet")
    # super-admin per autenticarsi su /admin
    if not db_cloud.get_user_by_email("nadmin"):
        db_cloud.create_user(
            tenant_id=None, email="nadmin",
            password_hash=hash_password("npwd"), role="super_admin",
        )
    return {"tenant": tenant}


def test_create_user_with_first_last_name(name_setup):
    """create_user accetta first_name + last_name."""
    uid = db_cloud.create_user(
        tenant_id=name_setup["tenant"], email="mario@nt",
        password_hash=hash_password("p"), role="tenant_user",
        first_name="Mario", last_name="Rossi",
    )
    u = db_cloud.get_user(uid)
    assert u["first_name"] == "Mario"
    assert u["last_name"] == "Rossi"


def test_user_display_name_helper(name_setup):
    """Helper: nome+cognome > solo nome > solo cognome > email."""
    u_full = {"email": "x@y", "first_name": "Mario", "last_name": "Rossi"}
    u_fn_only = {"email": "x@y", "first_name": "Mario", "last_name": None}
    u_ln_only = {"email": "x@y", "first_name": "", "last_name": "Rossi"}
    u_email = {"email": "fallback@y", "first_name": None, "last_name": None}
    u_none = None
    assert db_cloud.user_display_name(u_full) == "Mario Rossi"
    assert db_cloud.user_display_name(u_fn_only) == "Mario"
    assert db_cloud.user_display_name(u_ln_only) == "Rossi"
    assert db_cloud.user_display_name(u_email) == "fallback@y"
    assert db_cloud.user_display_name(u_none) == ""


def test_admin_users_edit_first_last(name_setup):
    """POST /admin/users/<id>/edit aggiorna nome/cognome."""
    from app.main import app
    uid = db_cloud.create_user(
        tenant_id=name_setup["tenant"], email="empty_name@nt",
        password_hash=hash_password("p"), role="tenant_user",
    )
    client = TestClient(app)
    with client:
        client.post("/login", data={"email": "nadmin", "password": "npwd"})
        r = client.post(
            f"/admin/users/{uid}/edit",
            data={"first_name": "Luca", "last_name": "Bianchi"},
            follow_redirects=False,
        )
        assert r.status_code == 303
    u = db_cloud.get_user(uid)
    assert u["first_name"] == "Luca"
    assert u["last_name"] == "Bianchi"


def test_admin_users_toggle_disables_login(name_setup):
    """Toggle is_active=False → utente non puo' piu' loggare."""
    from app.main import app
    uid = db_cloud.create_user(
        tenant_id=name_setup["tenant"], email="disabletest@nt",
        password_hash=hash_password("pwd"), role="tenant_user",
    )
    client = TestClient(app)
    with client:
        # Login funziona inizialmente
        r = client.post(
            "/login", data={"email": "disabletest@nt", "password": "pwd"},
            follow_redirects=False,
        )
        assert r.status_code == 303
        client.get("/logout")

        # Admin disabilita
        client.post("/login", data={"email": "nadmin", "password": "npwd"})
        r = client.post(f"/admin/users/{uid}/toggle", follow_redirects=False)
        assert r.status_code == 303
        client.get("/logout")

        # Login bloccato
        r = client.post(
            "/login", data={"email": "disabletest@nt", "password": "pwd"},
            follow_redirects=False,
        )
        # Re-login deve fallire (200 con errore, NON 303)
        assert r.status_code != 303 or "login" in (r.headers.get("location") or "")


def test_admin_users_create_with_names(name_setup):
    """POST /admin/users con first_name+last_name persiste i campi."""
    from app.main import app
    client = TestClient(app)
    with client:
        client.post("/login", data={"email": "nadmin", "password": "npwd"})
        r = client.post(
            "/admin/users",
            data={
                "email": "mariocreato@nt",
                "password": "password123",
                "role": "tenant_user",
                "tenant_id": str(name_setup["tenant"]),
                "first_name": "Mario",
                "last_name": "Creato",
            },
            follow_redirects=False,
        )
        assert r.status_code == 303
    u = db_cloud.get_user_by_email("mariocreato@nt")
    assert u is not None
    assert u["first_name"] == "Mario"
    assert u["last_name"] == "Creato"
