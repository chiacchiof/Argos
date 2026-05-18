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


def test_post_tasks_from_qualified_with_explicit_asset_ids(audience_setup):
    """POST /tasks/from_qualified con asset_ids[] usa quelli, ignorando i filtri.

    Use case: utente seleziona 2 di 3 asset filtrati via checkbox e crea
    un task con solo quei 2.
    """
    from app.main import app
    if not db_cloud.get_user_by_email("testadmin"):
        db_cloud.create_user(
            tenant_id=None, email="testadmin",
            password_hash=hash_password("testpwd"), role="super_admin",
        )
    aids = audience_setup["asset_ids"]
    client = TestClient(app)
    with client:
        client.post("/login", data={"email": "testadmin", "password": "testpwd"})

        # Submit con asset_ids espliciti (subset) + filtri che matchano tutti
        r = client.post(
            "/tasks/from_qualified",
            data={
                "name": "Selezione esplicita",
                "agent_mode": "outreach",
                "qualifiers": "q_test",
                "status": "qualified",
                "asset_ids": [str(aids[0]), str(aids[2])],
            },
            follow_redirects=False,
        )
        assert r.status_code == 303
        loc = r.headers["location"]
        task_id = int(loc.split("/tasks/")[1].split("/")[0])
        task = db.get_task(task_id)
        # Esattamente i 2 selezionati, NON tutti e 3 dai filtri.
        assert sorted(task["target_asset_ids"]) == sorted([aids[0], aids[2]])
        assert len(task["target_asset_ids"]) == 2


def test_post_tasks_from_qualified_select_all_filtered_flag(audience_setup):
    """POST /tasks/from_qualified con asset_ids[] MA `select_all_filtered=1`:
    il flag vince e il backend usa i filtri (espande a tutti i filtrati)."""
    from app.main import app
    if not db_cloud.get_user_by_email("testadmin"):
        db_cloud.create_user(
            tenant_id=None, email="testadmin",
            password_hash=hash_password("testpwd"), role="super_admin",
        )
    aids = audience_setup["asset_ids"]
    client = TestClient(app)
    with client:
        client.post("/login", data={"email": "testadmin", "password": "testpwd"})

        r = client.post(
            "/tasks/from_qualified",
            data={
                "name": "All filtered",
                "agent_mode": "outreach",
                "qualifiers": "q_test",
                "status": "qualified",
                "select_all_filtered": "1",
                # asset_ids esplicito ma sara' ignorato per via del flag
                "asset_ids": [str(aids[0])],
            },
            follow_redirects=False,
        )
        assert r.status_code == 303
        loc = r.headers["location"]
        task_id = int(loc.split("/tasks/")[1].split("/")[0])
        task = db.get_task(task_id)
        # Tutti e 3 (dai filtri), non solo aids[0].
        assert len(task["target_asset_ids"]) == 3


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


def test_has_contacts_filter(audience_setup):
    """has_contacts=True ritorna SOLO asset con almeno un canale outreach."""
    aids = audience_setup["asset_ids"]
    # Setup: 3 asset hanno email (fixture); ne marchiamo 1 senza email
    with db.connect() as c:
        c.execute("UPDATE assets SET email = NULL WHERE id = %s", (aids[0],))
        c.commit()
    rows_all = db.list_qualified_assets(qualifier_slugs=["q_test"], status_filter="qualified")
    rows_with = db.list_qualified_assets(
        qualifier_slugs=["q_test"], status_filter="qualified", has_contacts=True,
    )
    assert len(rows_all) == 3
    assert len(rows_with) == 2
    assert aids[0] not in [r["id"] for r in rows_with]


def test_has_social_filter(audience_setup):
    """has_social=True ritorna SOLO asset con social_json popolato."""
    aids = audience_setup["asset_ids"]
    # Aggiungi social_json a 1 asset
    with db.connect() as c:
        c.execute(
            "UPDATE assets SET social_json = %s WHERE id = %s",
            ('[{"platform":"instagram","url":"https://ig.com/x"}]', aids[1]),
        )
        c.commit()
    rows = db.list_qualified_assets(
        qualifier_slugs=["q_test"], status_filter="qualified", has_social=True,
    )
    assert len(rows) == 1
    assert rows[0]["id"] == aids[1]


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


def test_gap_between_dms_roundtrip():
    """B-011: gap_between_dms_min/max persistito e recuperato (clamp + None)."""
    tid = db.create_task({
        "name": "X", "objective": "y", "agent_mode": "outreach_whatsapp",
        "gap_between_dms_min": 0.15,
        "gap_between_dms_max": 0.35,
    })
    fetched = db.get_task(tid)
    assert abs(fetched["gap_between_dms_min"] - 0.15) < 1e-6
    assert abs(fetched["gap_between_dms_max"] - 0.35) < 1e-6
    # String input → float
    db.update_task(tid, {**fetched, "gap_between_dms_min": "1.5", "gap_between_dms_max": "3,0"})
    fetched2 = db.get_task(tid)
    assert abs(fetched2["gap_between_dms_min"] - 1.5) < 1e-6
    assert abs(fetched2["gap_between_dms_max"] - 3.0) < 1e-6
    # Empty string → None
    db.update_task(tid, {**fetched, "gap_between_dms_min": "", "gap_between_dms_max": ""})
    fetched3 = db.get_task(tid)
    assert fetched3.get("gap_between_dms_min") is None
    assert fetched3.get("gap_between_dms_max") is None
    # Clamp eccesso → 60.0
    db.update_task(tid, {**fetched, "gap_between_dms_min": 9999, "gap_between_dms_max": 0.001})
    fetched4 = db.get_task(tid)
    assert fetched4["gap_between_dms_min"] == 60.0
    assert abs(fetched4["gap_between_dms_max"] - 0.05) < 1e-6  # clamp basso


def test_pick_gap_minutes_defaults_and_override():
    """B-011: pick_gap_minutes onora task override; fallback su default platform."""
    from app.agent.social.humanize import pick_gap_minutes, default_gap_range_min

    # Default WA: range stretto 0.15-0.35
    lo, hi = default_gap_range_min("whatsapp_browser")
    assert (lo, hi) == (0.15, 0.35)
    for _ in range(20):
        g = pick_gap_minutes("whatsapp_browser")
        assert lo <= g <= hi

    # Default IG: range largo 8-30
    for _ in range(20):
        g = pick_gap_minutes("instagram")
        assert 8.0 <= g <= 30.0

    # Override task: uniform fra i due
    for _ in range(20):
        g = pick_gap_minutes("instagram", task_min=0.5, task_max=1.0)
        assert 0.5 <= g <= 1.0

    # Override solo min → fix point (no jitter)
    assert pick_gap_minutes("instagram", task_min=2.0) == 2.0

    # Override invertito (min > max) → swap
    for _ in range(10):
        g = pick_gap_minutes("instagram", task_min=5.0, task_max=1.0)
        assert 1.0 <= g <= 5.0

    # Platform sconosciuta → fallback legacy 8-30
    for _ in range(20):
        g = pick_gap_minutes("unknown_platform")
        assert 8.0 <= g <= 30.0


def test_full_form_update_preserves_target_asset_ids(audience_setup):
    """Bug 2026-05-17: salvataggio full form NON deve toccare target_asset_ids.
    Scenario: utente apre /qualified in tab nuova, aggiunge asset via
    append_qualified_set (DB aggiornato), torna a tab originale (hidden input
    stale), modifica altri campi e salva. La lista target_asset_ids deve
    restare quella nel DB, non quella stale del form."""
    from app.main import app
    if not db_cloud.get_user_by_email("testadmin"):
        db_cloud.create_user(
            tenant_id=None, email="testadmin",
            password_hash=hash_password("testpwd"), role="super_admin",
        )

    aids = audience_setup["asset_ids"]
    # Task con audience iniziale di 1 asset (simula stato render pagina form)
    tid = db.create_task({
        "name": "X", "objective": "o", "agent_mode": "outreach",
        "target_asset_ids": [aids[0]],
    })

    client = TestClient(app)
    with client:
        client.post("/login", data={"email": "testadmin", "password": "testpwd"})

        # DB intanto si aggiorna fuori dal form (simula picker in nuova tab)
        db.update_task_target_asset_ids(tid, aids)
        assert db.get_task(tid)["target_asset_ids"] == aids

        # Utente salva il form dalla tab originale: hidden input STALE = [aids[0]]
        # (oppure non c'e' piu' affatto). In entrambi i casi, deve PRESERVARE
        # il valore corrente del DB (aids).
        r = client.post(
            f"/tasks/{tid}",
            data={
                "name": "X edited",
                "objective": "o",
                "agent_mode": "outreach",
                "max_iterations": 10,
                "model": "qwen3.5:latest",
                "output_format": "txt",
                "llm_provider": "ollama",
                # invio stale (vecchia lista)
                "target_asset_ids": str(aids[0]),
            },
            follow_redirects=False,
        )
        assert r.status_code == 303, r.text[:300]
        # PRESERVE: DB rimane con 3 asset, non sovrascritto a 1
        assert db.get_task(tid)["target_asset_ids"] == aids
        # E il name e' aggiornato (per verificare che il save sia avvenuto)
        assert db.get_task(tid)["name"] == "X edited"


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
