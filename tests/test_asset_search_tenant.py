"""Test isolation tenant sulla route `/assets/search`.

Bug rilevato 2026-05-28: la query inline non filtrava per `tenant_id` → un
architect/operator vedeva nei suggerimenti asset di altri tenant. Fix in
`app/routes/assets.py:asset_search_htmx`: `current_tenant_id()` aggiunge
`AND a.tenant_id = %s` (super_admin → None → niente filter).
"""
from __future__ import annotations

import secrets

import pytest
from fastapi.testclient import TestClient

from app import db, db_cloud
from app.auth import hash_password
from app.main import app


_UNIQUE = "isolXYZ-" + secrets.token_hex(4)


@pytest.fixture
def setup_tenants_assets():
    """Crea 2 tenant con 1 architect e 1 asset ciascuno (title condiviso 'isolXYZ-…')."""
    t_a = db_cloud.create_tenant("TA", "ta")
    t_b = db_cloud.create_tenant("TB", "tb")
    arch_a = db_cloud.create_user(
        tenant_id=t_a, email="arch_a@x", password_hash=hash_password("p"),
        role="tenant_architect",
    )
    arch_b = db_cloud.create_user(
        tenant_id=t_b, email="arch_b@x", password_hash=hash_password("p"),
        role="tenant_architect",
    )
    super_admin = db_cloud.create_user(
        tenant_id=None, email="sa@x", password_hash=hash_password("p"),
        role="super_admin",
    )
    asset_a = db.upsert_asset(
        {"asset_type": "contact_legacy", "title": f"{_UNIQUE} A",
         "whatsapp": "+3911111", "raw_json": "{}"},
        tenant_id=t_a, created_by_user_id=arch_a,
    )
    asset_b = db.upsert_asset(
        {"asset_type": "contact_legacy", "title": f"{_UNIQUE} B",
         "whatsapp": "+3922222", "raw_json": "{}"},
        tenant_id=t_b, created_by_user_id=arch_b,
    )
    return {
        "t_a": t_a, "t_b": t_b, "asset_a": asset_a, "asset_b": asset_b,
        "arch_a_email": "arch_a@x", "arch_b_email": "arch_b@x",
        "sa_email": "sa@x", "pwd": "p",
    }


def _login(c: TestClient, email: str, pwd: str) -> None:
    r = c.post("/login", data={"email": email, "password": pwd, "next": "/"},
               follow_redirects=False)
    assert r.status_code == 303, f"login failed for {email}: {r.status_code} {r.text[:200]}"


def _search(c: TestClient, q: str) -> str:
    r = c.get(f"/assets/search?q={q}&callback=audienceAddAsset",
              headers={"HX-Request": "true"})
    assert r.status_code == 200
    return r.text


def test_architect_sees_only_own_tenant_assets(setup_tenants_assets):
    s = setup_tenants_assets

    # Architect tenant A → solo asset A
    with TestClient(app) as c:
        _login(c, s["arch_a_email"], s["pwd"])
        body = _search(c, _UNIQUE)
        assert f"audienceAddAsset({s['asset_a']})" in body, "architect A non vede il proprio asset"
        assert f"audienceAddAsset({s['asset_b']})" not in body, "LEAK: architect A vede asset di B"

    # Architect tenant B → solo asset B
    with TestClient(app) as c:
        _login(c, s["arch_b_email"], s["pwd"])
        body = _search(c, _UNIQUE)
        assert f"audienceAddAsset({s['asset_b']})" in body, "architect B non vede il proprio asset"
        assert f"audienceAddAsset({s['asset_a']})" not in body, "LEAK: architect B vede asset di A"


def test_super_admin_sees_all_tenants(setup_tenants_assets):
    s = setup_tenants_assets
    with TestClient(app) as c:
        _login(c, s["sa_email"], s["pwd"])
        body = _search(c, _UNIQUE)
        assert f"audienceAddAsset({s['asset_a']})" in body
        assert f"audienceAddAsset({s['asset_b']})" in body


def test_htmx_401_on_expired_session():
    """Senza auth + HX-Request: il middleware ritorna 401 + HX-Redirect (HTMX
    1.9 lo rispetta lato client). Documenta il path già funzionante."""
    with TestClient(app) as c:
        r = c.get("/assets/search?q=foo&callback=audienceAddAsset",
                  headers={"HX-Request": "true"}, follow_redirects=False)
    assert r.status_code == 401
    assert "/login" in (r.headers.get("HX-Redirect") or "")
