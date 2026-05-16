"""Fixture pytest comuni: gestione DB di test Postgres.

Tutti i test usano lo stesso container Docker (`agentscraper-postgres-dev`)
ma su DB separato `agentscraper_test`. A inizio di OGNI test droppiamo e
ricreiamo lo schema pubblico per garantire isolamento.

`TEST_DATABASE_URL` può essere overridato via env per puntare a un altro
Postgres (es. in CI).
"""
from __future__ import annotations

import os

import pytest


DEFAULT_TEST_DATABASE_URL = (
    "postgresql://postgres:postgres@localhost:5432/agentscraper_test?sslmode=disable"
)


@pytest.fixture(autouse=True)
def _isolate_test_db(monkeypatch):
    """Per ogni test:
    1. Forza DATABASE_URL al DB di test (sovrascrive eventuali override locali).
    2. Reset del pool psycopg di app.db (apre di nuovo con la TEST_DATABASE_URL).
    3. DROP SCHEMA public CASCADE; CREATE SCHEMA public; → tabula rasa.
    4. Reapplico lo schema di app.db (business) e app.db_cloud (tenants/users).
    5. Cleanup post-test: pool chiuso, DATABASE_URL ripristinata.
    """
    test_dsn = os.environ.get("TEST_DATABASE_URL", DEFAULT_TEST_DATABASE_URL)
    monkeypatch.setenv("DATABASE_URL", test_dsn)

    from app import db, db_cloud
    db.reset_pool()
    db_cloud.close_pool()

    # DROP + CREATE schema (più rapido di drop di ogni tabella)
    with db.connect() as conn:
        conn.execute("DROP SCHEMA IF EXISTS public CASCADE")
        conn.execute("CREATE SCHEMA public")
        conn.commit()

    # Reset di nuovo per essere sicuri che il pool veda lo schema vuoto
    db.reset_pool()
    db_cloud.close_pool()

    # Reapplico schema: db_cloud PRIMA (crea tenants/users), poi db (FK)
    db_cloud.init_db()
    db.init_db()

    yield

    # Cleanup
    db.reset_pool()
    db_cloud.close_pool()


@pytest.fixture
def authed_client(tmp_path, monkeypatch):
    """TestClient già loggato come super-admin di test.

    Crea un super-admin `testadmin / testpwd` ad ogni test (il DB è isolato
    dal fixture autouse `_isolate_test_db`), poi effettua POST /login per
    settare il cookie session sul client.

    Isola anche DATA_DIR/RESULTS_DIR su tmp_path per evitare di sporcare
    data/results/ del repo reale.
    """
    from fastapi.testclient import TestClient

    from app import config, db_cloud, storage
    from app.auth import hash_password

    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "RESULTS_DIR", tmp_path / "results")
    monkeypatch.setattr(storage, "RESULTS_DIR", tmp_path / "results")

    # Bootstrap super-admin di test (se non esiste già)
    if not db_cloud.get_user_by_email("testadmin"):
        db_cloud.create_user(
            tenant_id=None,
            email="testadmin",
            password_hash=hash_password("testpwd"),
            role="super_admin",
        )

    from app.main import app

    client = TestClient(app)
    # ENTER context manually so lifespan runs (init_db etc. — già fatti dal conftest ma idempotenti)
    client.__enter__()
    try:
        r = client.post(
            "/login",
            data={"email": "testadmin", "password": "testpwd", "next": "/"},
            follow_redirects=False,
        )
        assert r.status_code == 303, f"login failed: {r.status_code} {r.text[:200]}"
        yield client
    finally:
        client.__exit__(None, None, None)
