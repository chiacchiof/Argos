"""Test isolamento multi-tenant: Ferdinando (tenant A) e Mario (tenant B) vedono
solo i propri dati; il super-admin vede tutto.

Verifica che il ContextVar tenant_id settato dal middleware HTTP filtri
correttamente le query in db.tasks/jobs/workflows/assets/contacts/
orchestrator_messages.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import db, db_cloud
from app.auth import hash_password


@pytest.fixture
def populated_db():
    """Crea 2 tenant (A, B), 1 user per tenant, e 1 task per tenant + 1 task super-admin."""
    tenant_a = db_cloud.create_tenant("TenantA", "tenant-a")
    tenant_b = db_cloud.create_tenant("TenantB", "tenant-b")

    super_id = db_cloud.create_user(
        tenant_id=None, email="super", password_hash=hash_password("pwd-super"),
        role="super_admin",
    )
    alice_id = db_cloud.create_user(
        tenant_id=tenant_a, email="alice@a.it", password_hash=hash_password("pwd-alice"),
        role="tenant_user",
    )
    bob_id = db_cloud.create_user(
        tenant_id=tenant_b, email="bob@b.it", password_hash=hash_password("pwd-bob"),
        role="tenant_user",
    )

    # Task per ogni utente (via API db con contesto esplicito)
    task_alice = db.create_task(
        {"name": "Task Alice", "objective": "x"},
        tenant_id=tenant_a, created_by_user_id=alice_id,
    )
    task_bob = db.create_task(
        {"name": "Task Bob", "objective": "x"},
        tenant_id=tenant_b, created_by_user_id=bob_id,
    )
    task_super = db.create_task(
        {"name": "Task Super", "objective": "x"},
        tenant_id=None, created_by_user_id=super_id,
    )

    # Asset per Alice e Bob
    asset_alice = db.upsert_asset(
        {"asset_type": "test", "title": "Asset Alice", "raw_json": "{}",
         "source_url": "https://a.test/alice"},
        tenant_id=tenant_a, created_by_user_id=alice_id,
    )
    asset_bob = db.upsert_asset(
        {"asset_type": "test", "title": "Asset Bob", "raw_json": "{}",
         "source_url": "https://b.test/bob"},
        tenant_id=tenant_b, created_by_user_id=bob_id,
    )

    return {
        "tenant_a": tenant_a, "tenant_b": tenant_b,
        "super_id": super_id, "alice_id": alice_id, "bob_id": bob_id,
        "task_alice": task_alice, "task_bob": task_bob, "task_super": task_super,
        "asset_alice": asset_alice, "asset_bob": asset_bob,
    }


# ---------------------------------------------------------------------------
# Tasks
# ---------------------------------------------------------------------------

def test_alice_lists_only_her_tasks(populated_db):
    tasks = db.list_tasks(tenant_id=populated_db["tenant_a"])
    names = {t["name"] for t in tasks}
    assert names == {"Task Alice"}


def test_bob_lists_only_his_tasks(populated_db):
    tasks = db.list_tasks(tenant_id=populated_db["tenant_b"])
    names = {t["name"] for t in tasks}
    assert names == {"Task Bob"}


def test_super_admin_lists_all_tasks(populated_db):
    # tenant_id=None → no filter
    tasks = db.list_tasks(tenant_id=None)
    names = {t["name"] for t in tasks}
    assert names == {"Task Alice", "Task Bob", "Task Super"}


def test_alice_cannot_get_bobs_task_by_id(populated_db):
    # Anti-IDOR: get con tenant_id di Alice su task di Bob → None
    t = db.get_task(populated_db["task_bob"], tenant_id=populated_db["tenant_a"])
    assert t is None


def test_alice_cannot_delete_bobs_task(populated_db):
    db.delete_task(populated_db["task_bob"], tenant_id=populated_db["tenant_a"])
    # Bob può ancora vederlo (delete non ha effetto)
    t = db.get_task(populated_db["task_bob"], tenant_id=populated_db["tenant_b"])
    assert t is not None


def test_super_admin_can_get_any_task(populated_db):
    t = db.get_task(populated_db["task_bob"], tenant_id=None)
    assert t is not None and t["name"] == "Task Bob"


# ---------------------------------------------------------------------------
# Assets
# ---------------------------------------------------------------------------

def test_alice_lists_only_her_assets(populated_db):
    assets = db.list_assets(tenant_id=populated_db["tenant_a"])
    titles = {a["title"] for a in assets}
    assert titles == {"Asset Alice"}


def test_bob_lists_only_his_assets(populated_db):
    assets = db.list_assets(tenant_id=populated_db["tenant_b"])
    titles = {a["title"] for a in assets}
    assert titles == {"Asset Bob"}


def test_alice_cannot_get_bobs_asset(populated_db):
    a = db.get_asset(populated_db["asset_bob"], tenant_id=populated_db["tenant_a"])
    assert a is None


def test_count_assets_tenant_filtered(populated_db):
    assert db.count_assets(tenant_id=populated_db["tenant_a"]) == 1
    assert db.count_assets(tenant_id=populated_db["tenant_b"]) == 1
    assert db.count_assets(tenant_id=None) == 2  # super-admin


# ---------------------------------------------------------------------------
# ContextVar integration: middleware setta tenant_id automaticamente
# ---------------------------------------------------------------------------

def test_contextvar_drives_filtering(populated_db, monkeypatch, tmp_path):
    """Senza passare tenant_id esplicito, le funzioni leggono dal ContextVar."""
    from app import config, storage
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "RESULTS_DIR", tmp_path / "results")
    monkeypatch.setattr(storage, "RESULTS_DIR", tmp_path / "results")

    # Setta ContextVar a Alice → list_tasks() (senza param) deve filtrare per Alice
    token = db.set_current_tenant(populated_db["tenant_a"])
    try:
        tasks = db.list_tasks()  # NO tenant_id esplicito
        names = {t["name"] for t in tasks}
        assert names == {"Task Alice"}
    finally:
        db.reset_current_tenant(token)


def test_contextvar_resets_correctly(populated_db):
    """Dopo reset, list_tasks() torna a no-filter (super-admin)."""
    token = db.set_current_tenant(populated_db["tenant_a"])
    db.reset_current_tenant(token)
    tasks = db.list_tasks()  # context dopo reset = None
    assert len(tasks) == 3  # tutti


# ---------------------------------------------------------------------------
# End-to-end con TestClient: la route automaticamente filtra via middleware
# ---------------------------------------------------------------------------

def test_end_to_end_alice_sees_only_her_task(populated_db, tmp_path, monkeypatch):
    """Alice logga via HTTP → GET / → vede solo "Task Alice", NON "Task Bob" né "Task Super"."""
    from app import config, storage
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "RESULTS_DIR", tmp_path / "results")
    monkeypatch.setattr(storage, "RESULTS_DIR", tmp_path / "results")

    from app.main import app
    with TestClient(app) as client:
        r = client.post(
            "/login",
            data={"email": "alice@a.it", "password": "pwd-alice", "next": "/"},
            follow_redirects=False,
        )
        assert r.status_code == 303
        r = client.get("/")
        assert r.status_code == 200
        assert "Task Alice" in r.text
        assert "Task Bob" not in r.text
        assert "Task Super" not in r.text


def test_end_to_end_super_admin_sees_everything(populated_db, tmp_path, monkeypatch):
    from app import config, storage
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "RESULTS_DIR", tmp_path / "results")
    monkeypatch.setattr(storage, "RESULTS_DIR", tmp_path / "results")

    from app.main import app
    with TestClient(app) as client:
        r = client.post(
            "/login",
            data={"email": "super", "password": "pwd-super", "next": "/"},
            follow_redirects=False,
        )
        assert r.status_code == 303
        r = client.get("/")
        assert r.status_code == 200
        assert "Task Alice" in r.text
        assert "Task Bob" in r.text
        assert "Task Super" in r.text


# ---------------------------------------------------------------------------
# Social accounts + WhatsApp API config (Step D₄)
# ---------------------------------------------------------------------------

@pytest.fixture
def social_accounts_setup(populated_db):
    """Crea 1 social account per tenant_a e 1 per tenant_b."""
    a_id = db.create_social_account(
        {
            "uuid": "uuid-alice", "platform": "instagram", "username": "alice_ig",
            "encrypted_password": b"\x01\x02\x03",
        },
        tenant_id=populated_db["tenant_a"],
        created_by_user_id=populated_db["alice_id"],
    )
    b_id = db.create_social_account(
        {
            "uuid": "uuid-bob", "platform": "tiktok", "username": "bob_tt",
            "encrypted_password": b"\x04\x05\x06",
        },
        tenant_id=populated_db["tenant_b"],
        created_by_user_id=populated_db["bob_id"],
    )
    return {**populated_db, "sa_alice": a_id, "sa_bob": b_id}


def test_alice_lists_only_her_social_accounts(social_accounts_setup):
    sa = db.list_social_accounts(tenant_id=social_accounts_setup["tenant_a"])
    assert len(sa) == 1
    assert sa[0]["username"] == "alice_ig"


def test_bob_lists_only_his_social_accounts(social_accounts_setup):
    sa = db.list_social_accounts(tenant_id=social_accounts_setup["tenant_b"])
    assert len(sa) == 1
    assert sa[0]["username"] == "bob_tt"


def test_super_admin_lists_all_social_accounts(social_accounts_setup):
    sa = db.list_social_accounts(tenant_id=None)
    assert len(sa) == 2


def test_alice_cannot_get_bobs_social_account(social_accounts_setup):
    sa = db.get_social_account(
        social_accounts_setup["sa_bob"], tenant_id=social_accounts_setup["tenant_a"]
    )
    assert sa is None


def test_alice_cannot_delete_bobs_social_account(social_accounts_setup):
    db.delete_social_account(
        social_accounts_setup["sa_bob"], tenant_id=social_accounts_setup["tenant_a"]
    )
    sa = db.get_social_account(
        social_accounts_setup["sa_bob"], tenant_id=social_accounts_setup["tenant_b"]
    )
    assert sa is not None


def test_whatsapp_api_config_isolation(populated_db):
    """WhatsApp API config: isolato per tenant."""
    a_id = db.insert_whatsapp_api_config(
        {
            "label": "WA Alice", "phone_number_id": "111",
            "business_account_id": "222", "encrypted_access_token": b"\x10",
        },
        tenant_id=populated_db["tenant_a"],
        created_by_user_id=populated_db["alice_id"],
    )
    b_id = db.insert_whatsapp_api_config(
        {
            "label": "WA Bob", "phone_number_id": "333",
            "business_account_id": "444", "encrypted_access_token": b"\x20",
        },
        tenant_id=populated_db["tenant_b"],
        created_by_user_id=populated_db["bob_id"],
    )
    # Alice vede solo la sua
    cfg_a = db.list_whatsapp_api_config(tenant_id=populated_db["tenant_a"])
    assert {c["label"] for c in cfg_a} == {"WA Alice"}
    # Bob vede solo la sua
    cfg_b = db.list_whatsapp_api_config(tenant_id=populated_db["tenant_b"])
    assert {c["label"] for c in cfg_b} == {"WA Bob"}
    # Anti-IDOR
    assert db.get_whatsapp_api_config(b_id, tenant_id=populated_db["tenant_a"]) is None
