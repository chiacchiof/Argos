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
    1. Importa `app` PRIMA del monkeypatch — perche' l'import esegue
       `app.config._apply_db_override()` che sovrascrive DATABASE_URL leggendo
       il file cifrato `data/db_config.enc`. Se monkeypatchassimo prima,
       l'import lo annullerebbe e dropperemmo lo schema sul DB sbagliato.
       (Bug storico 2026-05-17: lo abbiamo wipato 'agentscraper_dev' diverse volte.)
    2. Forza DATABASE_URL al DB di test (con monkeypatch, sovrascrive l'override).
    3. Cintura di sicurezza: verifica che il DB target abbia "test" nel nome.
       Se no, ABORT — meglio test failure che dati di sviluppo distrutti.
    4. Reset pool, DROP SCHEMA public, ricrea schema.
    """
    from app import db, db_cloud  # PRIMA del monkeypatch: vedi docstring

    test_dsn = os.environ.get("TEST_DATABASE_URL", DEFAULT_TEST_DATABASE_URL)
    monkeypatch.setenv("DATABASE_URL", test_dsn)

    # Safety net: il DSN target deve contenere "test". Se ad es. ci ritroviamo
    # con `agentscraper_dev` nell'env (regressione del fix), aborto subito.
    if "test" not in test_dsn.lower():
        raise RuntimeError(
            f"REFUSING TO RUN TESTS: target DSN non contiene 'test': {test_dsn!r}. "
            "Setta TEST_DATABASE_URL o verifica DEFAULT_TEST_DATABASE_URL."
        )

    db.reset_pool()
    db_cloud.close_pool()

    # DROP + CREATE schema (più rapido di drop di ogni tabella).
    # Doppia verifica: chiediamo a Postgres il nome del DB corrente prima di droppare.
    with db.connect() as conn:
        row = conn.execute("SELECT current_database() AS db").fetchone()
        current_db = (row["db"] if isinstance(row, dict) else row[0])
        if "test" not in str(current_db).lower():
            raise RuntimeError(
                f"REFUSING TO DROP SCHEMA: current_database()={current_db!r} non e' un DB di test. "
                "Verifica che _apply_db_override non abbia rimesso un DSN production."
            )
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
