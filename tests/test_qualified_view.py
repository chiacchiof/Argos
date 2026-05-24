"""Test del tab /qualified (Fase 1): asset-centric con multi-select qualifier.

Verifica:
- db.list_distinct_qualifier_slugs() ritorna i qualifier eseguiti + count.
- db.list_qualified_assets() applica filtro AND su multi-qualifier.
- db.count_qualified_assets() coerente con list_*.
- Score min filtering (anche su tag value as numeric string).
- Multi-tenant isolation (alice non vede qualifier di bob).
- Endpoint GET /qualified ritorna 200 con i filtri applicati.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import db, db_cloud
from app.auth import hash_password


@pytest.fixture
def qualifier_setup():
    """Crea 2 tenant, 1 user per tenant, 3 asset per Alice con 2 qualifier
    distinti applicati con diverse decision/score, 1 asset per Bob.

    Layout:
      Alice / tenant_a:
        asset_p1 → qualifier_palestra=qualified (score 8)
        asset_p2 → qualifier_palestra=qualified (score 5), qualifier_yoga=qualified (score 7)
        asset_p3 → qualifier_palestra=rejected (score 2)
      Bob / tenant_b:
        asset_b1 → qualifier_palestra=qualified (score 9)
    """
    tenant_a = db_cloud.create_tenant("TA", "ta")
    tenant_b = db_cloud.create_tenant("TB", "tb")

    alice = db_cloud.create_user(
        tenant_id=tenant_a, email="alice@ta", password_hash=hash_password("pwd"),
        role="tenant_architect",
    )
    bob = db_cloud.create_user(
        tenant_id=tenant_b, email="bob@tb", password_hash=hash_password("pwd"),
        role="tenant_architect",
    )

    # Crea task qualifier (per friendly_name lookup)
    task_palestra = db.create_task(
        {"name": "Qualifica Palestra", "objective": "x", "agent_mode": "qualifier"},
        tenant_id=tenant_a, created_by_user_id=alice,
    )
    task_yoga = db.create_task(
        {"name": "Qualifica Yoga", "objective": "x", "agent_mode": "qualifier"},
        tenant_id=tenant_a, created_by_user_id=alice,
    )

    p1 = db.upsert_asset(
        {"asset_type": "profile", "title": "Persona 1", "raw_json": "{}",
         "source_url": "https://test/p1"},
        tenant_id=tenant_a, created_by_user_id=alice,
    )
    p2 = db.upsert_asset(
        {"asset_type": "profile", "title": "Persona 2", "raw_json": "{}",
         "source_url": "https://test/p2"},
        tenant_id=tenant_a, created_by_user_id=alice,
    )
    p3 = db.upsert_asset(
        {"asset_type": "profile", "title": "Persona 3", "raw_json": "{}",
         "source_url": "https://test/p3"},
        tenant_id=tenant_a, created_by_user_id=alice,
    )
    b1 = db.upsert_asset(
        {"asset_type": "profile", "title": "Bob Persona", "raw_json": "{}",
         "source_url": "https://test/b1"},
        tenant_id=tenant_b, created_by_user_id=bob,
    )

    # Tag qualifier (mimica runner_qualifier)
    db.set_asset_tag(p1, "qualifier_qualifica_palestra", "qualified")
    db.set_asset_tag(p1, "qualifier_score_qualifica_palestra", "8")

    db.set_asset_tag(p2, "qualifier_qualifica_palestra", "qualified")
    db.set_asset_tag(p2, "qualifier_score_qualifica_palestra", "5")
    db.set_asset_tag(p2, "qualifier_qualifica_yoga", "qualified")
    db.set_asset_tag(p2, "qualifier_score_qualifica_yoga", "7")

    db.set_asset_tag(p3, "qualifier_qualifica_palestra", "rejected")
    db.set_asset_tag(p3, "qualifier_score_qualifica_palestra", "2")

    db.set_asset_tag(b1, "qualifier_qualifica_palestra", "qualified")
    db.set_asset_tag(b1, "qualifier_score_qualifica_palestra", "9")

    return {
        "tenant_a": tenant_a, "tenant_b": tenant_b,
        "alice": alice, "bob": bob,
        "p1": p1, "p2": p2, "p3": p3, "b1": b1,
    }


# ---------------------------------------------------------------------------
# db.list_distinct_qualifier_slugs
# ---------------------------------------------------------------------------

def test_distinct_qualifier_slugs_alice_sees_only_her_qualifiers(qualifier_setup):
    slugs = db.list_distinct_qualifier_slugs(tenant_id=qualifier_setup["tenant_a"])
    by_slug = {s["slug"]: s for s in slugs}
    # Alice ha 2 qualifier: palestra (2 qualified + 1 rejected) e yoga (1 qualified)
    assert "qualifica_palestra" in by_slug
    assert "qualifica_yoga" in by_slug
    assert by_slug["qualifica_palestra"]["count_qualified"] == 2
    assert by_slug["qualifica_palestra"]["count_rejected"] == 1
    assert by_slug["qualifica_yoga"]["count_qualified"] == 1


def test_distinct_qualifier_slugs_bob_sees_only_his(qualifier_setup):
    slugs = db.list_distinct_qualifier_slugs(tenant_id=qualifier_setup["tenant_b"])
    by_slug = {s["slug"]: s for s in slugs}
    assert "qualifica_palestra" in by_slug
    # Bob ha 1 qualified per palestra, 0 rejected, 0 yoga
    assert by_slug["qualifica_palestra"]["count_qualified"] == 1
    assert "qualifica_yoga" not in by_slug


def test_distinct_qualifier_slugs_friendly_name_from_task(qualifier_setup):
    slugs = db.list_distinct_qualifier_slugs(tenant_id=qualifier_setup["tenant_a"])
    by_slug = {s["slug"]: s for s in slugs}
    # Il task "Qualifica Palestra" deve essere matchato come friendly_name
    assert by_slug["qualifica_palestra"]["friendly_name"] == "Qualifica Palestra"


# ---------------------------------------------------------------------------
# db.list_qualified_assets — multi-qualifier AND
# ---------------------------------------------------------------------------

def test_list_single_qualifier_qualified(qualifier_setup):
    rows = db.list_qualified_assets(
        qualifier_slugs=["qualifica_palestra"], status_filter="qualified",
        tenant_id=qualifier_setup["tenant_a"],
    )
    ids = {r["id"] for r in rows}
    assert ids == {qualifier_setup["p1"], qualifier_setup["p2"]}


def test_list_single_qualifier_rejected(qualifier_setup):
    rows = db.list_qualified_assets(
        qualifier_slugs=["qualifica_palestra"], status_filter="rejected",
        tenant_id=qualifier_setup["tenant_a"],
    )
    ids = {r["id"] for r in rows}
    assert ids == {qualifier_setup["p3"]}


def test_list_multi_qualifier_AND(qualifier_setup):
    """Solo p2 ha entrambi qualifier_palestra=qualified E qualifier_yoga=qualified."""
    rows = db.list_qualified_assets(
        qualifier_slugs=["qualifica_palestra", "qualifica_yoga"],
        status_filter="qualified",
        tenant_id=qualifier_setup["tenant_a"],
    )
    ids = {r["id"] for r in rows}
    assert ids == {qualifier_setup["p2"]}


def test_list_score_min_filter(qualifier_setup):
    """score_min=7 esclude p2 (score 5) ma include p1 (score 8)."""
    rows = db.list_qualified_assets(
        qualifier_slugs=["qualifica_palestra"], status_filter="qualified",
        score_min=7, tenant_id=qualifier_setup["tenant_a"],
    )
    ids = {r["id"] for r in rows}
    assert ids == {qualifier_setup["p1"]}


def test_list_no_qualifier_filter_returns_only_qualified(qualifier_setup):
    """Senza filtro qualifier slug, la pagina /qualified mostra SOLO asset con
    almeno un tag `qualifier_*:qualified` (fix 2026-05-23: prima ritornava
    tutti gli asset del tenant — vedi `_qualified_assets_where_clause`,
    parametro `require_qualified=True` settato da list/count_qualified_assets).
    """
    rows = db.list_qualified_assets(
        qualifier_slugs=[], status_filter="qualified",
        tenant_id=qualifier_setup["tenant_a"],
    )
    ids = {r["id"] for r in rows}
    # Solo p1 e p2 hanno tag qualifier_*:qualified (p3 e' rejected).
    assert ids == {qualifier_setup["p1"], qualifier_setup["p2"]}


def test_list_no_qualifier_filter_rejected(qualifier_setup):
    """Senza slug ma status=rejected → solo asset taggati :rejected (no :qualified)."""
    rows = db.list_qualified_assets(
        qualifier_slugs=[], status_filter="rejected",
        tenant_id=qualifier_setup["tenant_a"],
    )
    ids = {r["id"] for r in rows}
    assert ids == {qualifier_setup["p3"]}


def test_list_no_qualifier_filter_both(qualifier_setup):
    """Senza slug ma status=both → asset taggati :qualified OR :rejected."""
    rows = db.list_qualified_assets(
        qualifier_slugs=[], status_filter="both",
        tenant_id=qualifier_setup["tenant_a"],
    )
    ids = {r["id"] for r in rows}
    assert ids == {qualifier_setup["p1"], qualifier_setup["p2"], qualifier_setup["p3"]}


# ---------------------------------------------------------------------------
# count_qualified_assets
# ---------------------------------------------------------------------------

def test_count_matches_list(qualifier_setup):
    rows = db.list_qualified_assets(
        qualifier_slugs=["qualifica_palestra"], status_filter="qualified",
        tenant_id=qualifier_setup["tenant_a"],
    )
    n = db.count_qualified_assets(
        qualifier_slugs=["qualifica_palestra"], status_filter="qualified",
        tenant_id=qualifier_setup["tenant_a"],
    )
    assert n == len(rows) == 2


def test_count_status_both(qualifier_setup):
    """status='both' → qualified + rejected (3 asset totali palestra-taggati)."""
    n = db.count_qualified_assets(
        qualifier_slugs=["qualifica_palestra"], status_filter="both",
        tenant_id=qualifier_setup["tenant_a"],
    )
    assert n == 3  # p1, p2, p3


# ---------------------------------------------------------------------------
# Multi-tenant isolation
# ---------------------------------------------------------------------------

def test_alice_does_not_see_bob_qualified(qualifier_setup):
    rows = db.list_qualified_assets(
        qualifier_slugs=["qualifica_palestra"], status_filter="qualified",
        tenant_id=qualifier_setup["tenant_a"],
    )
    ids = {r["id"] for r in rows}
    assert qualifier_setup["b1"] not in ids


def test_bob_sees_only_his(qualifier_setup):
    rows = db.list_qualified_assets(
        qualifier_slugs=["qualifica_palestra"], status_filter="qualified",
        tenant_id=qualifier_setup["tenant_b"],
    )
    ids = {r["id"] for r in rows}
    assert ids == {qualifier_setup["b1"]}


# ---------------------------------------------------------------------------
# Endpoint HTTP GET /qualified
# ---------------------------------------------------------------------------

def test_qualified_endpoint_returns_200(qualifier_setup, tmp_path, monkeypatch):
    from app import config, storage
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "RESULTS_DIR", tmp_path / "results")
    monkeypatch.setattr(storage, "RESULTS_DIR", tmp_path / "results")

    from app.main import app
    with TestClient(app) as client:
        r = client.post(
            "/login",
            data={"email": "alice@ta", "password": "pwd", "next": "/"},
            follow_redirects=False,
        )
        assert r.status_code == 303

        # GET /qualified senza filtri → mostra TUTTI gli asset di Alice (3)
        r = client.get("/qualified")
        assert r.status_code == 200
        assert "Asset qualified" in r.text
        # Menu qualifier deve mostrare entrambi i qualifier di Alice
        assert "Qualifica Palestra" in r.text
        assert "Qualifica Yoga" in r.text


def test_qualified_endpoint_filter_by_qualifier(qualifier_setup, tmp_path, monkeypatch):
    from app import config, storage
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "RESULTS_DIR", tmp_path / "results")
    monkeypatch.setattr(storage, "RESULTS_DIR", tmp_path / "results")

    from app.main import app
    with TestClient(app) as client:
        client.post("/login", data={"email": "alice@ta", "password": "pwd", "next": "/"},
                    follow_redirects=False)
        r = client.get("/qualified?qualifiers=qualifica_palestra&status=qualified&score_min=7")
        assert r.status_code == 200
        # Solo p1 ha score >= 7
        assert "Persona 1" in r.text
        assert "Persona 2" not in r.text  # score 5
        assert "Persona 3" not in r.text  # rejected


def test_qualified_endpoint_accepts_empty_form_values(qualifier_setup, tmp_path, monkeypatch):
    """Il form HTML invia `?score_min=&source_task_id=` quando i campi sono vuoti.
    FastAPI di default 422 sul cast `"" → int`. Verifichiamo che il route handler
    castigi manualmente questi parametri come "None" trasparente."""
    from app import config, storage
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "RESULTS_DIR", tmp_path / "results")
    monkeypatch.setattr(storage, "RESULTS_DIR", tmp_path / "results")

    from app.main import app
    with TestClient(app) as client:
        client.post("/login", data={"email": "alice@ta", "password": "pwd", "next": "/"},
                    follow_redirects=False)
        # URL identica a quella che produce il form HTML con tutti i campi vuoti
        r = client.get(
            "/qualified?qualifiers=qualifica_palestra&status=rejected"
            "&score_min=&asset_type=&source_task_id=&q="
            "&tag_key__0=&tag_value__0=&tag_key__1=&tag_value__1="
            "&tag_key__2=&tag_value__2="
        )
        assert r.status_code == 200, f"got {r.status_code}: {r.text[:300]}"


def test_qualified_endpoint_isolation(qualifier_setup, tmp_path, monkeypatch):
    """Alice loggata non deve vedere asset di Bob nemmeno dall'endpoint."""
    from app import config, storage
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "RESULTS_DIR", tmp_path / "results")
    monkeypatch.setattr(storage, "RESULTS_DIR", tmp_path / "results")

    from app.main import app
    with TestClient(app) as client:
        client.post("/login", data={"email": "alice@ta", "password": "pwd", "next": "/"},
                    follow_redirects=False)
        r = client.get("/qualified?qualifiers=qualifica_palestra")
        assert r.status_code == 200
        assert "Bob Persona" not in r.text
