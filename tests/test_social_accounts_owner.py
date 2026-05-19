"""Filtro owner + dropdown su /social/accounts.

Copre:
  - db.list_social_accounts(created_by_user_id=N): filtra per autore.
  - JOIN owner_email: ogni riga ha owner_email popolata.
  - Route /social/accounts: ?author=mine default per tenant_user, ?author=tenant override.
  - POST /social/accounts: super_admin puo' settare owner_user_id; tenant_user e' forzato.
"""
from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

from app import db, db_cloud
from app.auth import hash_password


# AGENTSCRAPER_SECRET deve essere settata per cifrare le password
os.environ.setdefault("AGENTSCRAPER_SECRET", "test-secret-key-12345678901234567890")


@pytest.fixture
def sa_setup():
    """1 tenant + 2 utenti (Alice + Bob). Ognuno crea 1 account."""
    tenant = db_cloud.create_tenant("SA_Tenant", "satnt")
    u_alice = db_cloud.create_user(
        tenant_id=tenant, email="alice@sa", password_hash=hash_password("pw"),
        role="tenant_user",
    )
    u_bob = db_cloud.create_user(
        tenant_id=tenant, email="bob@sa", password_hash=hash_password("pw"),
        role="tenant_user",
    )
    if not db_cloud.get_user_by_email("saadmin"):
        db_cloud.create_user(
            tenant_id=None, email="saadmin",
            password_hash=hash_password("sapwd"), role="super_admin",
        )
    sa_alice = db.create_social_account(
        {
            "uuid": "alice-ig-1",
            "platform": "instagram",
            "username": "alice_handle",
            "encrypted_password": b"\x00",  # placeholder bytes
            "daily_dm_cap": 10,
            "status": "warming_up",
        },
        tenant_id=tenant, created_by_user_id=u_alice,
    )
    sa_bob = db.create_social_account(
        {
            "uuid": "bob-tt-1",
            "platform": "tiktok",
            "username": "bob_handle",
            "encrypted_password": b"\x00",
            "daily_dm_cap": 10,
            "status": "warming_up",
        },
        tenant_id=tenant, created_by_user_id=u_bob,
    )
    return {
        "tenant": tenant,
        "u_alice": u_alice, "u_bob": u_bob,
        "sa_alice": sa_alice, "sa_bob": sa_bob,
    }


def test_list_social_accounts_filter_by_author(sa_setup):
    """db.list_social_accounts(created_by_user_id=N) → solo account di N."""
    s = sa_setup
    rows = db.list_social_accounts(
        tenant_id=s["tenant"], created_by_user_id=s["u_alice"],
    )
    ids = {r["id"] for r in rows}
    assert s["sa_alice"] in ids
    assert s["sa_bob"] not in ids

    rows_all = db.list_social_accounts(tenant_id=s["tenant"], created_by_user_id=None)
    ids_all = {r["id"] for r in rows_all}
    assert {s["sa_alice"], s["sa_bob"]}.issubset(ids_all)


def test_list_social_accounts_owner_email_populated(sa_setup):
    """JOIN users → ogni riga ha owner_email."""
    s = sa_setup
    rows = db.list_social_accounts(tenant_id=s["tenant"])
    by_id = {r["id"]: r for r in rows}
    assert by_id[s["sa_alice"]]["owner_email"] == "alice@sa"
    assert by_id[s["sa_bob"]]["owner_email"] == "bob@sa"


def test_route_default_mine_for_tenant_user(sa_setup):
    """Login come Alice → vede solo il suo account."""
    from app.main import app
    client = TestClient(app)
    with client:
        r = client.post(
            "/login", data={"email": "alice@sa", "password": "pw"},
            follow_redirects=False,
        )
        assert r.status_code == 303
        r = client.get("/social/accounts")
        assert r.status_code == 200
        assert "alice_handle" in r.text
        assert "bob_handle" not in r.text


def test_route_author_tenant_shows_all(sa_setup):
    """?author=tenant → Alice vede anche l'account di Bob."""
    from app.main import app
    client = TestClient(app)
    with client:
        client.post("/login", data={"email": "alice@sa", "password": "pw"})
        r = client.get("/social/accounts?author=tenant")
        assert r.status_code == 200
        assert "alice_handle" in r.text
        assert "bob_handle" in r.text


def test_route_super_admin_default_tenant(sa_setup):
    """Super_admin: default = tenant (overview)."""
    from app.main import app
    client = TestClient(app)
    with client:
        client.post("/login", data={"email": "saadmin", "password": "sapwd"})
        r = client.get("/social/accounts")
        assert r.status_code == 200
        # Vede entrambi senza dover passare ?author=tenant
        assert "alice_handle" in r.text
        assert "bob_handle" in r.text


def test_post_tenant_user_owner_forced_to_self(sa_setup):
    """Tenant_user che tenta di settare owner_user_id altrui → ignorato,
    forced a se stesso."""
    from app.main import app
    s = sa_setup
    client = TestClient(app)
    with client:
        client.post("/login", data={"email": "alice@sa", "password": "pw"})
        # Alice prova a settare Bob come owner: il backend deve ignorare
        r = client.post(
            "/social/accounts",
            data={
                "platform": "facebook",
                "username": "test_fb_alice",
                "password": "pw_to_encrypt",
                "daily_dm_cap": 10,
                "owner_user_id": str(s["u_bob"]),  # tentativo override
            },
            follow_redirects=False,
        )
        assert r.status_code == 303
    # Verifica DB: l'account creato ha owner = Alice (non Bob)
    rows = db.list_social_accounts(
        tenant_id=s["tenant"], created_by_user_id=s["u_alice"],
    )
    fb_accounts = [r for r in rows if r["username"] == "test_fb_alice"]
    assert len(fb_accounts) == 1
    assert fb_accounts[0]["created_by_user_id"] == s["u_alice"]


def test_post_super_admin_can_assign_owner(sa_setup):
    """Super_admin puo' assegnare owner_user_id a qualunque utente del tenant."""
    from app.main import app
    s = sa_setup
    # Setta il tenant context per super_admin via login (super_admin ha
    # tenant_id=None, ma create_social_account riceve tenant_id da chiamante).
    # In assenza di tenant nel context del super_admin, l'account viene
    # creato con tenant_id NULL. Per testare l'assegnazione owner servono
    # entrambi i path. Qui verifichiamo solo che il param venga rispettato.
    client = TestClient(app)
    with client:
        client.post("/login", data={"email": "saadmin", "password": "sapwd"})
        r = client.post(
            "/social/accounts",
            data={
                "platform": "instagram",
                "username": "assigned_to_bob",
                "password": "pw_x",
                "daily_dm_cap": 10,
                "owner_user_id": str(s["u_bob"]),
            },
            follow_redirects=False,
        )
        assert r.status_code == 303
    # Cerca l'account creato (può non avere tenant_id, quindi list senza filtro)
    all_rows = db.list_social_accounts()
    target = [r for r in all_rows if r["username"] == "assigned_to_bob"]
    assert len(target) == 1
    assert target[0]["created_by_user_id"] == s["u_bob"]
