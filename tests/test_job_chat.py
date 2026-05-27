"""Test B-001: chat in-running (human-in-the-loop su job attivo).

Copre tre livelli:
  1. parser deterministico dei comandi (`parse_chat_input`) — puro, no DB.
  2. helper DB della coda chat (`consume_pending_chat`, `count_pending_chat`).
  3. route `POST/GET /jobs/{id}/chat` end-to-end via authed_client.
"""
from __future__ import annotations

import pytest

from app.agent.job_chat_commands import SETTABLE_FIELDS, parse_chat_input


# ---------------------------------------------------------------------------
# 1. Parser (puro)
# ---------------------------------------------------------------------------

def test_parse_free_text():
    r = parse_chat_input("correggi il numero di Chanel")
    assert r.kind == "free_text"
    assert r.command is None


def test_parse_empty_is_free_text_with_error():
    r = parse_chat_input("   ")
    assert r.kind == "free_text"
    assert r.error


@pytest.mark.parametrize("text,cmd", [
    ("/stop", "stop"),
    ("/pause", "pause"),
    ("/resume", "resume"),
    ("/skip", "skip"),
    ("/help", "help"),
    ("/STOP", "stop"),  # case-insensitive
])
def test_parse_nullary_commands(text, cmd):
    r = parse_chat_input(text)
    assert r.kind == "command"
    assert r.command == cmd
    assert r.error is None


def test_parse_note():
    r = parse_chat_input("/note ricordati di rallentare")
    assert r.command == "note"
    assert r.note == "ricordati di rallentare"


def test_parse_note_requires_text():
    r = parse_chat_input("/note")
    assert r.command == "note"
    assert r.error


def test_parse_set_ok():
    r = parse_chat_input("/set 4155 whatsapp +393331234567")
    assert r.command == "set"
    assert r.error is None
    assert r.asset_id == 4155
    assert r.field_name == "whatsapp"
    assert r.value == "+393331234567"


def test_parse_set_bad_id():
    r = parse_chat_input("/set abc whatsapp +39")
    assert r.command == "set"
    assert r.error and "asset_id" in r.error


def test_parse_set_field_not_whitelisted():
    r = parse_chat_input("/set 1 status qualified")
    assert r.command == "set"
    assert r.error
    assert "status" not in SETTABLE_FIELDS


def test_parse_unknown_command():
    r = parse_chat_input("/frobnicate")
    assert r.command == "unknown"
    assert r.error


# ---------------------------------------------------------------------------
# 2. Helper DB (usano il fixture autouse _isolate_test_db)
# ---------------------------------------------------------------------------

def _mk_job(agent_mode: str = "browser_use") -> tuple[int, int]:
    from app import db
    tid = db.create_task({
        "name": "task-chat-test",
        "objective": "obiettivo di test",
        "agent_mode": agent_mode,
        "model": "qwen3-coder:30b",
    })
    jid = db.create_job(tid)
    return tid, jid


def test_insert_and_list_messages():
    from app import db
    _, jid = _mk_job()
    db.insert_job_chat_message(jid, "user", "ciao", kind="suggestion", applied=0)
    db.insert_job_chat_message(jid, "assistant", "ricevuto", kind="reply", applied=1)
    msgs = db.list_job_chat_messages(jid)
    assert [m["direction"] for m in msgs] == ["user", "assistant"]
    assert msgs[0]["body"] == "ciao"


def test_consume_pending_marks_applied():
    from app import db
    _, jid = _mk_job()
    db.insert_job_chat_message(jid, "user", "salta il prossimo", kind="suggestion", applied=0)
    db.insert_job_chat_message(jid, "user", "/skip", kind="command", applied=0)
    # un ack assistant non deve essere consumato
    db.insert_job_chat_message(jid, "assistant", "ok", kind="reply", applied=1)

    assert db.count_pending_chat(jid) == 2
    consumed = db.consume_pending_chat(jid)
    assert {c["body"] for c in consumed} == {"salta il prossimo", "/skip"}
    # idempotente: una seconda consume non ritorna nulla
    assert db.consume_pending_chat(jid) == []
    assert db.count_pending_chat(jid) == 0


def test_consume_live_instructions_block():
    from app import db
    from app.agent.runner_control import consume_live_instructions
    _, jid = _mk_job()
    db.insert_job_chat_message(jid, "user", "usa un tono più formale", kind="suggestion", applied=0)
    db.insert_job_chat_message(jid, "user", "/skip", kind="command", applied=0)

    block = consume_live_instructions(jid, lambda _s: None)
    assert block is not None
    assert "ISTRUZIONI UTENTE LIVE" in block
    assert "tono più formale" in block
    assert "SALTARE" in block.upper()
    # ha postato un ack assistant e drenato la coda
    assert db.count_pending_chat(jid) == 0
    assert any(m["direction"] == "assistant" for m in db.list_job_chat_messages(jid))
    # niente in coda → None
    assert consume_live_instructions(jid, lambda _s: None) is None


# ---------------------------------------------------------------------------
# 3. Route end-to-end
# ---------------------------------------------------------------------------

def test_route_note_command(authed_client):
    from app import db
    _, jid = _mk_job("browser_use")
    db.update_job(jid, status="running")
    r = authed_client.post(f"/jobs/{jid}/chat", data={"body": "/note rallenta"})
    assert r.status_code == 200
    job = db.get_job(jid, tenant_id=None)
    assert "Nota operatore (chat): rallenta" in (job["log"] or "")


def test_route_free_text_queued_for_supporting_mode(authed_client):
    from app import db
    _, jid = _mk_job("browser_use")
    db.update_job(jid, status="running")
    authed_client.post(f"/jobs/{jid}/chat", data={"body": "estrai anche le email"})
    # il messaggio utente resta pending (applied=0) per il runner
    assert db.count_pending_chat(jid) == 1


def test_route_free_text_not_queued_for_unsupported_mode(authed_client):
    from app import db
    _, jid = _mk_job("bulk_extract")  # non in MODES_SUPPORTING_LIVE_CHAT
    db.update_job(jid, status="running")
    authed_client.post(f"/jobs/{jid}/chat", data={"body": "fai una cosa"})
    # non runner-consumabile → applied=1 subito, niente pending
    assert db.count_pending_chat(jid) == 0


def test_route_set_updates_asset(authed_client):
    from app import db
    _, jid = _mk_job("browser_use")
    db.update_job(jid, status="running")
    aid = db.upsert_asset({
        "asset_type": "contact_legacy",
        "title": "Tizio",
        "whatsapp": "+390000000000",
        "raw_json": "{}",
    })
    r = authed_client.post(
        f"/jobs/{jid}/chat",
        data={"body": f"/set {aid} whatsapp +393331234567"},
    )
    assert r.status_code == 200
    asset = db.get_asset(aid, tenant_id=None)
    assert asset["whatsapp"] == "+393331234567"


def test_route_unknown_command_acked(authed_client):
    from app import db
    _, jid = _mk_job("browser_use")
    db.update_job(jid, status="running")
    r = authed_client.post(f"/jobs/{jid}/chat", data={"body": "/nope"})
    assert r.status_code == 200
    assert any(
        m["direction"] == "assistant" and "sconosciuto" in m["body"].lower()
        for m in db.list_job_chat_messages(jid)
    )
