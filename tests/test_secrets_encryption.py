"""Test B-008: cifratura at-rest delle LLM API key per-task.

Livelli: helper puro (secrets_util) + integrazione DB (create/update/get task +
migrazione one-time). Le asserzioni "il valore grezzo è cifrato" girano solo se
ARGOS_SECRET è configurata (in test lo è, caricata da .env all'import di app);
l'invariante di round-trip (get_task ritorna il plaintext) vale sempre.
"""
from __future__ import annotations

# Carica .env PRIMA di importare i moduli app, così ARGOS_SECRET è già in env sia
# a collection-time (per gli skipif) sia a runtime. In CI senza .env è un no-op e
# gli skipif scattano correttamente.
from dotenv import load_dotenv

load_dotenv()

import pytest

from app.secrets_util import (
    decrypt_secret,
    encrypt_secret,
    is_secret_configured,
    looks_encrypted,
)


# ---------------------------------------------------------------------------
# 1. Helper puro
# ---------------------------------------------------------------------------

def test_none_and_empty_passthrough():
    assert encrypt_secret(None) is None
    assert encrypt_secret("") == ""
    assert decrypt_secret(None) is None


def test_legacy_plaintext_decrypt_passthrough():
    # un valore non cifrato (legacy) torna invariato
    assert decrypt_secret("sk-legacy-plaintext") == "sk-legacy-plaintext"


@pytest.mark.skipif(not is_secret_configured(), reason="ARGOS_SECRET non configurata")
def test_roundtrip_and_idempotent():
    k = "sk-proj-Secret123"
    enc = encrypt_secret(k)
    assert enc != k
    assert looks_encrypted(enc)
    assert decrypt_secret(enc) == k
    # idempotente: ri-cifrare un valore già cifrato non cambia nulla
    assert encrypt_secret(enc) == enc


# ---------------------------------------------------------------------------
# 2. Integrazione DB (fixture autouse _isolate_test_db)
# ---------------------------------------------------------------------------

def _raw_key(task_id: int, field: str = "llm_api_key") -> str | None:
    """Legge il valore GREZZO della colonna (bypassa _row_to_task / decrypt)."""
    from app import db
    with db.connect() as con:
        row = con.execute(
            f"SELECT {field} FROM tasks WHERE id = %s", (task_id,)
        ).fetchone()
    return row[field] if row else None


def _mk(**over) -> int:
    from app import db
    data = {
        "name": "t-sec", "objective": "o", "agent_mode": "browser_use",
        "model": "gpt-4o-mini", "llm_provider": "openai",
        "llm_api_key": "sk-PLAINTEXT-create",
    }
    data.update(over)
    return db.create_task(data)


def test_create_task_encrypts_at_rest_and_reads_plaintext():
    from app import db
    tid = _mk()
    # lettura applicativa → plaintext
    assert db.get_task(tid)["llm_api_key"] == "sk-PLAINTEXT-create"
    # storage grezzo → cifrato (se la cifratura è attiva)
    raw = _raw_key(tid)
    if is_secret_configured():
        assert looks_encrypted(raw), f"valore grezzo non cifrato: {raw!r}"
        assert raw != "sk-PLAINTEXT-create"


def test_update_task_encrypts_at_rest():
    from app import db
    tid = _mk(llm_api_key=None)
    # update_task è full-replace: carico il task, modifico la chiave, ri-salvo
    # (stesso pattern del form di edit).
    t = db.get_task(tid)
    t["llm_api_key"] = "sk-PLAINTEXT-update"
    db.update_task(tid, t)
    assert db.get_task(tid)["llm_api_key"] == "sk-PLAINTEXT-update"
    if is_secret_configured():
        assert looks_encrypted(_raw_key(tid))


def test_all_three_key_fields():
    from app import db
    tid = _mk(
        llm_api_key="sk-main",
        discovery_llm_api_key="sk-disc",
        browser_llm_api_key="sk-brow",
    )
    t = db.get_task(tid)
    assert t["llm_api_key"] == "sk-main"
    assert t["discovery_llm_api_key"] == "sk-disc"
    assert t["browser_llm_api_key"] == "sk-brow"
    if is_secret_configured():
        assert looks_encrypted(_raw_key(tid, "discovery_llm_api_key"))
        assert looks_encrypted(_raw_key(tid, "browser_llm_api_key"))


@pytest.mark.skipif(not is_secret_configured(), reason="ARGOS_SECRET non configurata")
def test_encrypt_legacy_task_keys_migration():
    from app import db
    tid = _mk(llm_api_key=None)
    # simulo un valore LEGACY in chiaro scritto direttamente nel DB
    with db.connect() as con:
        con.execute("UPDATE tasks SET llm_api_key = %s WHERE id = %s",
                    ("sk-LEGACY-plain", tid))
    assert not looks_encrypted(_raw_key(tid))  # davvero in chiaro

    n = db.encrypt_legacy_task_keys()
    assert n >= 1
    assert looks_encrypted(_raw_key(tid))             # ora cifrato a riposo
    assert db.get_task(tid)["llm_api_key"] == "sk-LEGACY-plain"  # leggibile

    # idempotente: una seconda passata non ri-cifra nulla
    assert db.encrypt_legacy_task_keys() == 0
