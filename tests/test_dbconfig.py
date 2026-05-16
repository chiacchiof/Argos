"""Test pagina /dbconfig: login DBadmin + save/clear DSN.

La pagina ha un sistema di auth indipendente da quello dei tenant_user/super_admin.
Verifichiamo:
- Login form servito a chi non è autenticato come DBadmin
- POST /dbconfig/login OK/KO
- Save DSN scrive il file cifrato
- Clear rimuove il file
- Mascheramento password nella visualizzazione
- Validazione DSN (deve iniziare con postgresql://)
"""
from __future__ import annotations

import os
import secrets

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def env_with_secret(monkeypatch, tmp_path):
    """Imposta AGENTSCRAPER_SECRET + isola il file db_config.enc in tmp_path.

    Pulisce DATABASE_URL prima e dopo: `apply_override()` la setta direttamente
    in os.environ (non via monkeypatch), quindi serve cleanup manuale per non
    inquinare i test successivi.
    """
    monkeypatch.setenv("AGENTSCRAPER_SECRET", "test-secret-" + secrets.token_hex(8))

    from app import _runtime_db_override
    test_file = tmp_path / "db_config.enc"
    monkeypatch.setattr(_runtime_db_override, "_CONFIG_FILE", test_file)

    saved_db_url = os.environ.pop("DATABASE_URL", None)
    try:
        yield test_file
    finally:
        os.environ.pop("DATABASE_URL", None)
        if saved_db_url is not None:
            os.environ["DATABASE_URL"] = saved_db_url


@pytest.fixture
def client(env_with_secret, monkeypatch, tmp_path):
    from app import config, db, storage
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "RESULTS_DIR", tmp_path / "results")
    monkeypatch.setattr(storage, "RESULTS_DIR", tmp_path / "results")

    from app.main import app
    with TestClient(app) as c:
        yield c


# ---------------------------------------------------------------------------
# Routing + login form
# ---------------------------------------------------------------------------

def test_dbconfig_shows_login_when_anonymous(client):
    r = client.get("/dbconfig")
    assert r.status_code == 200
    assert "DB Config" in r.text
    assert 'name="username"' in r.text
    assert 'name="password"' in r.text
    # Non deve mostrare il panel (no DSN form)
    assert 'name="database_url"' not in r.text


def test_dbconfig_login_wrong_credentials(client):
    r = client.post(
        "/dbconfig/login",
        data={"username": "DBadmin", "password": "wrong"},
        follow_redirects=False,
    )
    assert r.status_code == 401
    assert "Credenziali non valide" in r.text


def test_dbconfig_login_wrong_username(client):
    r = client.post(
        "/dbconfig/login",
        data={"username": "edgAdmin", "password": "Entra123!"},
        follow_redirects=False,
    )
    # edgAdmin è l'altro super-admin, non valido per /dbconfig
    assert r.status_code == 401


def test_dbconfig_login_success_shows_panel(client):
    r = client.post(
        "/dbconfig/login",
        data={"username": "DBadmin", "password": "Entra123!"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/dbconfig"

    # Ora /dbconfig deve mostrare il panel
    r2 = client.get("/dbconfig")
    assert r2.status_code == 200
    assert 'name="database_url"' in r2.text
    assert "Stato attuale" in r2.text


def test_dbconfig_logout_clears_session(client):
    # Login
    client.post(
        "/dbconfig/login",
        data={"username": "DBadmin", "password": "Entra123!"},
        follow_redirects=False,
    )
    # Logout
    r = client.get("/dbconfig/logout", follow_redirects=False)
    assert r.status_code == 303
    # /dbconfig torna a mostrare login
    r2 = client.get("/dbconfig")
    assert 'name="username"' in r2.text
    assert 'name="database_url"' not in r2.text


# ---------------------------------------------------------------------------
# Save / Clear DSN
# ---------------------------------------------------------------------------

def _login(client: TestClient) -> None:
    r = client.post(
        "/dbconfig/login",
        data={"username": "DBadmin", "password": "Entra123!"},
        follow_redirects=False,
    )
    assert r.status_code == 303


def test_save_writes_encrypted_file(client, env_with_secret):
    _login(client)
    assert not env_with_secret.exists()
    r = client.post(
        "/dbconfig/save",
        data={
            "database_url": "postgresql://user:secret@localhost:5432/agentscraper_dev",
            "active_label": "Locale dev",
        },
    )
    assert r.status_code == 200
    assert "RIAVVIA" in r.text or "riavvia" in r.text.lower()
    assert env_with_secret.exists()

    # Il contenuto su disco non deve contenere la password in chiaro
    raw = env_with_secret.read_bytes()
    assert b"secret" not in raw
    assert b"agentscraper_dev" not in raw
    # Ma deve essere decifrabile
    from app import _runtime_db_override
    data = _runtime_db_override.read_override()
    assert data is not None
    assert data["database_url"] == "postgresql://user:secret@localhost:5432/agentscraper_dev"
    assert data["active_label"] == "Locale dev"


def test_save_rejects_empty_dsn(client, env_with_secret):
    _login(client)
    r = client.post(
        "/dbconfig/save",
        data={"database_url": "", "active_label": ""},
    )
    assert r.status_code == 400
    assert not env_with_secret.exists()


def test_save_rejects_non_postgres_dsn(client, env_with_secret):
    _login(client)
    r = client.post(
        "/dbconfig/save",
        data={"database_url": "mysql://user:pwd@localhost/db", "active_label": "x"},
    )
    assert r.status_code == 400
    assert not env_with_secret.exists()


def test_save_accepts_postgres_short_scheme(client, env_with_secret):
    _login(client)
    r = client.post(
        "/dbconfig/save",
        data={"database_url": "postgres://user:pwd@localhost/db", "active_label": "x"},
    )
    assert r.status_code == 200
    assert env_with_secret.exists()


def test_clear_removes_file(client, env_with_secret):
    _login(client)
    client.post(
        "/dbconfig/save",
        data={"database_url": "postgresql://u:p@h/db", "active_label": "x"},
    )
    assert env_with_secret.exists()
    r = client.post("/dbconfig/clear")
    assert r.status_code == 200
    assert not env_with_secret.exists()


def test_save_requires_authentication(client, env_with_secret):
    """Senza login, save non deve scrivere niente (redirect a /dbconfig)."""
    r = client.post(
        "/dbconfig/save",
        data={"database_url": "postgresql://u:p@h/db", "active_label": "x"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/dbconfig"
    assert not env_with_secret.exists()


def test_clear_requires_authentication(client, env_with_secret, monkeypatch):
    """Senza login, clear non deve fare niente."""
    # Pre-popolo il file via API low-level
    monkeypatch.setenv("AGENTSCRAPER_SECRET", os.environ["AGENTSCRAPER_SECRET"])
    from app import _runtime_db_override
    _runtime_db_override.write_override("postgresql://u:p@h/db", "test")
    assert env_with_secret.exists()

    r = client.post("/dbconfig/clear", follow_redirects=False)
    assert r.status_code == 303
    # File ancora presente: clear è stato bloccato
    assert env_with_secret.exists()


# ---------------------------------------------------------------------------
# Mascheramento password nella visualizzazione
# ---------------------------------------------------------------------------

def test_password_masked_in_panel(client, env_with_secret, monkeypatch):
    """Quando il panel mostra la DSN attiva, la password deve essere mascherata."""
    monkeypatch.setenv("DATABASE_URL", "postgresql://myuser:my-secret-pwd@my-host:5432/mydb")
    _login(client)
    r = client.get("/dbconfig")
    assert r.status_code == 200
    assert "my-secret-pwd" not in r.text
    assert "myuser" in r.text
    assert "****" in r.text


# ---------------------------------------------------------------------------
# Public path: /dbconfig accessibile anche con cloud DB attivo + utente non loggato
# ---------------------------------------------------------------------------

def test_dbconfig_accessible_with_cloud_active_no_user_login(client, monkeypatch):
    """Con DATABASE_URL settata, l'auth utente è attiva ma /dbconfig deve
    restare raggiungibile (ha il proprio gate interno)."""
    monkeypatch.setenv("DATABASE_URL", "postgresql://x:y@127.0.0.1:1/none")
    # Senza fare login utente, GET /dbconfig deve servire la login form di dbconfig
    r = client.get("/dbconfig", follow_redirects=False)
    assert r.status_code == 200
    assert "DB Config" in r.text


# ---------------------------------------------------------------------------
# Apply override all'avvio
# ---------------------------------------------------------------------------

def test_apply_override_sets_env(env_with_secret, monkeypatch):
    """Se il file cifrato esiste, apply_override deve popolare os.environ['DATABASE_URL']."""
    from app import _runtime_db_override

    target_dsn = "postgresql://override:secret@override-host/override_db"
    _runtime_db_override.write_override(target_dsn, "Override test")
    monkeypatch.delenv("DATABASE_URL", raising=False)

    _runtime_db_override.apply_override()
    assert os.environ.get("DATABASE_URL") == target_dsn


def test_apply_override_no_file_does_nothing(env_with_secret, monkeypatch):
    """Senza file, apply_override non tocca os.environ."""
    from app import _runtime_db_override
    monkeypatch.delenv("DATABASE_URL", raising=False)
    _runtime_db_override.apply_override()
    assert "DATABASE_URL" not in os.environ
