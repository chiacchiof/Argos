"""Test di sicurezza: i guard impediscono ai test di toccare un DB di produzione.

Contesto (2026-05-31): Neon si svuotava "ogni tanto" perché l'override /dbconfig
(`_runtime_db_override.apply_override`) poteva rimettere la DSN di Neon in
`os.environ["DATABASE_URL"]` a ogni import di `app`, anche DOPO il monkeypatch del
conftest. Una `connect()` successiva poteva quindi colpire Neon, e il DROP SCHEMA
del fixture lo svuotava.

Difese verificate qui:
1. `db._assert_dsn_safe_under_pytest` rifiuta DSN remote/non-test sotto pytest
   (chiamata da `_resolve_dsn`, quindi protegge OGNI connessione).
2. `_runtime_db_override.apply_override` è no-op sotto pytest.
"""
from __future__ import annotations

import pytest

from app import db
from app import _runtime_db_override as ovr


NEON_DSN = (
    "postgresql://neondb_owner:secret@ep-delicate-leaf-alnrwlji.c-3."
    "eu-central-1.aws.neon.tech/neondb?sslmode=require"
)
LOCAL_TEST_DSN = "postgresql://postgres:postgres@localhost:5432/agentscraper_test?sslmode=disable"
LOCAL_PROD_DSN = "postgresql://postgres:postgres@localhost:5432/agentscraper_dev"


def test_resolve_dsn_rejects_neon_under_pytest(monkeypatch):
    """Una DSN Neon risolta durante un test deve far ABORTIRE (no connessione)."""
    monkeypatch.setenv("DATABASE_URL", NEON_DSN)
    with pytest.raises(RuntimeError, match="REFUSING DB CONNECTION UNDER PYTEST"):
        db._resolve_dsn()


def test_resolve_dsn_rejects_local_nontest_db_under_pytest(monkeypatch):
    """Anche un DB locale ma SENZA 'test' nel nome (es. agentscraper_dev) è rifiutato."""
    monkeypatch.setenv("DATABASE_URL", LOCAL_PROD_DSN)
    with pytest.raises(RuntimeError, match="REFUSING DB CONNECTION UNDER PYTEST"):
        db._resolve_dsn()


def test_resolve_dsn_allows_local_test_db(monkeypatch):
    """Il DB di test locale passa (è il caso normale)."""
    monkeypatch.setenv("DATABASE_URL", LOCAL_TEST_DSN)
    assert db._resolve_dsn() == LOCAL_TEST_DSN


def test_resolve_dsn_bypass_flag_allows_remote(monkeypatch):
    """Il bypass esplicito (per promote/copy deliberati) consente la DSN remota."""
    monkeypatch.setenv("DATABASE_URL", NEON_DSN)
    monkeypatch.setenv("ARGOS_ALLOW_REMOTE_DB_UNDER_PYTEST", "1")
    assert db._resolve_dsn() == NEON_DSN


def test_apply_override_is_noop_under_pytest(monkeypatch, tmp_path):
    """apply_override NON deve rimettere la DSN dell'override in env durante i test."""
    monkeypatch.setenv("DATABASE_URL", LOCAL_TEST_DSN)
    # Anche se read_override ritornasse Neon, apply_override deve ignorarlo sotto pytest.
    monkeypatch.setattr(ovr, "read_override", lambda: {"database_url": NEON_DSN, "active_label": "neon"})
    ovr.apply_override()
    import os
    assert os.environ["DATABASE_URL"] == LOCAL_TEST_DSN  # invariato


def test_host_dbname_parser():
    h, d = db._dsn_host_dbname(NEON_DSN)
    assert "neon.tech" in h and d == "neondb"
    h2, d2 = db._dsn_host_dbname(LOCAL_TEST_DSN)
    assert h2 == "localhost" and d2 == "agentscraper_test"
