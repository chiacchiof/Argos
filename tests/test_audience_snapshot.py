"""Test audience snapshot (target_asset_ids) per task outreach.

Verifica:
  - Roundtrip schema: create_task/get_task/update_task_target_asset_ids.
  - POST /tasks/from_qualified crea task con target_asset_ids snapshot.
  - POST /tasks/<id>/append_asset_id (HTMX) aggiunge un asset (dedup).
  - POST /tasks/<id>/remove_asset_id rimuove un asset.
  - POST /tasks/<id>/append_qualified_set fa UNION + dedup.
  - PRESERVE: POST /tasks/<id> (form full update) senza target_asset_ids
    mantiene quello esistente (non azzera).
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import db, db_cloud
from app.auth import hash_password


@pytest.fixture
def audience_setup():
    """Crea 1 tenant, 1 user, 3 asset con qualifier_test=qualified + score."""
    tenant = db_cloud.create_tenant("AudT", "audt")
    user = db_cloud.create_user(
        tenant_id=tenant, email="u@audt", password_hash=hash_password("pwd"),
        role="tenant_user",
    )
    task_qual = db.create_task(
        {"name": "Q Test", "objective": "x", "agent_mode": "qualifier"},
        tenant_id=tenant, created_by_user_id=user,
    )

    asset_ids = []
    for i, title in enumerate(["A1", "A2", "A3"], start=1):
        aid = db.upsert_asset(
            {"asset_type": "profile", "title": title, "raw_json": "{}",
             "source_url": f"https://x.test/{i}",
             "email": f"a{i}@x.test"},  # serve email per runner email
            tenant_id=tenant, created_by_user_id=user,
        )
        db.set_asset_tag(aid, "qualifier_q_test", "qualified")
        db.set_asset_tag(aid, "qualifier_score_q_test", str(5 + i))  # 6,7,8
        # update_asset_qualifier serve a settare assets.status='qualified'
        db.update_asset_qualifier(aid, 5 + i, "qualified")
        asset_ids.append(aid)

    return {"tenant": tenant, "user": user, "task_qual": task_qual, "asset_ids": asset_ids}


def test_create_task_with_target_asset_ids_roundtrip(audience_setup):
    aids = audience_setup["asset_ids"]
    tid = db.create_task({
        "name": "X", "objective": "o", "agent_mode": "outreach",
        "target_asset_ids": aids,
    })
    fetched = db.get_task(tid)
    assert fetched["target_asset_ids"] == aids


def test_update_task_target_asset_ids_patch(audience_setup):
    aids = audience_setup["asset_ids"]
    tid = db.create_task({
        "name": "X", "objective": "o", "agent_mode": "outreach",
        "target_asset_ids": aids[:1],
    })
    # Patch
    db.update_task_target_asset_ids(tid, aids)
    assert db.get_task(tid)["target_asset_ids"] == aids
    # Patch to empty
    db.update_task_target_asset_ids(tid, [])
    assert db.get_task(tid)["target_asset_ids"] == []


def test_post_tasks_from_qualified_creates_task(audience_setup):
    """POST /tasks/from_qualified estrae set qualified e crea task."""
    from app.main import app
    # bootstrap super-admin per autenticarci
    if not db_cloud.get_user_by_email("testadmin"):
        db_cloud.create_user(
            tenant_id=None, email="testadmin",
            password_hash=hash_password("testpwd"), role="super_admin",
        )
    client = TestClient(app)
    with client:
        r = client.post(
            "/login",
            data={"email": "testadmin", "password": "testpwd", "next": "/"},
            follow_redirects=False,
        )
        assert r.status_code == 303

        r = client.post(
            "/tasks/from_qualified",
            data={
                "name": "From qual test",
                "agent_mode": "outreach",
                "qualifiers": "q_test",
                "status": "qualified",
            },
            follow_redirects=False,
        )
        assert r.status_code == 303, r.text[:300]
        # Estrai task_id dal redirect URL
        loc = r.headers["location"]
        assert "/tasks/" in loc and "/edit" in loc
        task_id = int(loc.split("/tasks/")[1].split("/")[0])
        task = db.get_task(task_id)
        assert task["agent_mode"] == "outreach"
        assert len(task["target_asset_ids"]) == 3  # tutti e 3 i qualified


def test_post_tasks_from_qualified_empty_audience_returns_400(audience_setup):
    """Se i filtri producono 0 asset, ritorna 400."""
    from app.main import app
    if not db_cloud.get_user_by_email("testadmin"):
        db_cloud.create_user(
            tenant_id=None, email="testadmin",
            password_hash=hash_password("testpwd"), role="super_admin",
        )
    client = TestClient(app)
    with client:
        client.post(
            "/login",
            data={"email": "testadmin", "password": "testpwd"},
            follow_redirects=False,
        )
        r = client.post(
            "/tasks/from_qualified",
            data={
                "name": "X",
                "agent_mode": "outreach",
                "qualifiers": "qualifier_che_non_esiste",
            },
            follow_redirects=False,
        )
        assert r.status_code == 400


def test_append_and_remove_asset_id_htmx(audience_setup):
    """POST /tasks/<id>/append_asset_id e remove_asset_id via HTMX."""
    from app.main import app
    if not db_cloud.get_user_by_email("testadmin"):
        db_cloud.create_user(
            tenant_id=None, email="testadmin",
            password_hash=hash_password("testpwd"), role="super_admin",
        )

    tid = db.create_task({"name": "X", "objective": "o", "agent_mode": "outreach"})
    aids = audience_setup["asset_ids"]

    client = TestClient(app)
    with client:
        client.post("/login", data={"email": "testadmin", "password": "testpwd"})

        # Append 1
        r = client.post(f"/tasks/{tid}/append_asset_id", params={"asset_id": aids[0]})
        assert r.status_code == 200
        assert db.get_task(tid)["target_asset_ids"] == [aids[0]]

        # Append duplicato (dedup, lista identica)
        r = client.post(f"/tasks/{tid}/append_asset_id", params={"asset_id": aids[0]})
        assert r.status_code == 200
        assert db.get_task(tid)["target_asset_ids"] == [aids[0]]

        # Append altri 2
        client.post(f"/tasks/{tid}/append_asset_id", params={"asset_id": aids[1]})
        client.post(f"/tasks/{tid}/append_asset_id", params={"asset_id": aids[2]})
        assert db.get_task(tid)["target_asset_ids"] == aids

        # Remove uno
        r = client.post(f"/tasks/{tid}/remove_asset_id", params={"asset_id": aids[1]})
        assert r.status_code == 200
        assert db.get_task(tid)["target_asset_ids"] == [aids[0], aids[2]]


def test_post_tasks_qualifier_from_qualified_creates_task(audience_setup):
    """POST /tasks/qualifier_from_qualified crea task qualifier con snapshot.
    Use case "qualifier of qualified": multi-qualifier additivo."""
    from app.main import app
    if not db_cloud.get_user_by_email("testadmin"):
        db_cloud.create_user(
            tenant_id=None, email="testadmin",
            password_hash=hash_password("testpwd"), role="super_admin",
        )
    client = TestClient(app)
    with client:
        client.post("/login", data={"email": "testadmin", "password": "testpwd"})

        r = client.post(
            "/tasks/qualifier_from_qualified",
            data={
                "name": "Qualifier raffinato",
                "objective": "Tra i qualified, identifica i lead caldi (score 7-10).",
                "qualifiers": "q_test",
                "status": "qualified",
            },
            follow_redirects=False,
        )
        assert r.status_code == 303, r.text[:300]
        loc = r.headers["location"]
        assert "/tasks/" in loc and "/edit" in loc
        task_id = int(loc.split("/tasks/")[1].split("/")[0])
        task = db.get_task(task_id)
        assert task["agent_mode"] == "qualifier"
        assert len(task["target_asset_ids"]) == 3
        assert "lead caldi" in (task.get("objective") or "")


def test_post_tasks_qualifier_from_qualified_requires_objective(audience_setup):
    """L'objective e' obbligatorio per il qualifier (criterio LLM)."""
    from app.main import app
    if not db_cloud.get_user_by_email("testadmin"):
        db_cloud.create_user(
            tenant_id=None, email="testadmin",
            password_hash=hash_password("testpwd"), role="super_admin",
        )
    client = TestClient(app)
    with client:
        client.post("/login", data={"email": "testadmin", "password": "testpwd"})
        r = client.post(
            "/tasks/qualifier_from_qualified",
            data={
                "name": "X",
                "objective": "",  # vuoto
                "qualifiers": "q_test",
            },
            follow_redirects=False,
        )
        assert r.status_code == 400


def test_target_asset_ids_validator_handles_json_envelope():
    """Bug 2026-05-17: form.getlist() ritorna ['[1,2,3]'] dall'hidden field.
    Il validator deve sciogliere l'envelope JSON invece di provare int('[1,2,3]')."""
    from app.models import TaskIn
    t = TaskIn(name="x", objective="y", target_asset_ids=["[10,20,30]"])
    assert t.target_asset_ids == [10, 20, 30]
    # Caso vuoto JSON
    t2 = TaskIn(name="x", objective="y", target_asset_ids=["[]"])
    assert t2.target_asset_ids == []


def test_social_account_id_roundtrip():
    tid = db.create_task({
        "name": "X", "objective": "y", "agent_mode": "outreach_social",
        "social_platform": "instagram",
        "social_account_id": 42,
    })
    fetched = db.get_task(tid)
    assert fetched.get("social_account_id") == 42
    # Update a None
    db.update_task(tid, {**fetched, "social_account_id": None})
    assert db.get_task(tid).get("social_account_id") is None


def test_append_qualified_set_unions_dedup(audience_setup):
    """POST /tasks/<id>/append_qualified_set fa UNION dei nuovi ID con esistenti."""
    from app.main import app
    if not db_cloud.get_user_by_email("testadmin"):
        db_cloud.create_user(
            tenant_id=None, email="testadmin",
            password_hash=hash_password("testpwd"), role="super_admin",
        )

    aids = audience_setup["asset_ids"]
    # task pre-popolato con solo aids[0]
    tid = db.create_task({
        "name": "X", "objective": "o", "agent_mode": "outreach",
        "target_asset_ids": [aids[0]],
    })

    client = TestClient(app)
    with client:
        client.post("/login", data={"email": "testadmin", "password": "testpwd"})

        # Append set qualified (3 asset, aids[0] gia' presente -> dedup)
        r = client.post(
            f"/tasks/{tid}/append_qualified_set",
            data={"qualifiers": "q_test", "status": "qualified"},
            follow_redirects=False,
        )
        assert r.status_code == 303
        final = db.get_task(tid)["target_asset_ids"]
        # Ordine: aids[0] originale + nuovi (dedup)
        assert len(final) == 3
        assert set(final) == set(aids)
        # aids[0] resta in prima posizione
        assert final[0] == aids[0]
