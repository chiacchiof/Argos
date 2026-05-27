"""Test B-002: mini-CLI CRUD contatti (/inbox/contacts).

Tre livelli: parser puro, apply (con DB) e route end-to-end via authed_client.
"""
from __future__ import annotations

import pytest

from app.contact_cli import (
    UPDATABLE_FIELDS,
    parse_contact_command,
)


# ---------------------------------------------------------------------------
# 1. Parser (puro)
# ---------------------------------------------------------------------------

def test_parse_optout():
    p = parse_contact_command("optout 4155")
    assert p.action == "optout"
    assert p.contact_id == 4155
    assert p.error is None


def test_parse_optout_requires_id():
    p = parse_contact_command("optout")
    assert p.action == "optout"
    assert p.error


def test_parse_update_ok():
    p = parse_contact_command("update 4155 whatsapp=+393331234567")
    assert p.action == "update"
    assert p.error is None
    assert p.contact_id == 4155
    assert p.field_name == "whatsapp"
    assert p.value == "+393331234567"


def test_parse_update_value_with_spaces():
    p = parse_contact_command("update 7 display_name=Mario Rossi")
    assert p.action == "update"
    assert p.field_name == "display_name"
    assert p.value == "Mario Rossi"


def test_parse_update_field_not_whitelisted():
    p = parse_contact_command("update 7 status=qualified")
    assert p.action == "update"
    assert p.error
    assert "status" not in UPDATABLE_FIELDS


def test_parse_qualify_ok():
    p = parse_contact_command("qualify 4155 score=8")
    assert p.action == "qualify"
    assert p.contact_id == 4155
    assert p.score == 8
    assert p.error is None


def test_parse_qualify_score_out_of_range():
    p = parse_contact_command("qualify 4155 score=99")
    assert p.action == "qualify"
    assert p.error


def test_parse_bulk_optout():
    p = parse_contact_command("bulk-optout 4155,4156, 4157")
    assert p.action == "bulk-optout"
    assert p.contact_ids == [4155, 4156, 4157]


def test_parse_help_and_unknown():
    assert parse_contact_command("help").action == "help"
    u = parse_contact_command("frobnicate 1")
    assert u.action == "unknown"
    assert u.error


# ---------------------------------------------------------------------------
# 2. apply (con DB, fixture autouse _isolate_test_db)
# ---------------------------------------------------------------------------

def _mk_contact(**over) -> int:
    from app import db
    data = {"display_name": "Tizio", "whatsapp": "+390000000000", "raw_json": "{}"}
    data.update(over)
    return db.upsert_contact(data)


def test_apply_optout():
    from app import db
    from app.contact_cli import apply_contact_command
    cid = _mk_contact(email="a@example.com")
    ok, msg = apply_contact_command(f"optout {cid}")
    assert ok
    assert db.get_contact(cid)["status"] == "optedout"


def test_apply_update_whatsapp():
    from app import db
    from app.contact_cli import apply_contact_command
    cid = _mk_contact(email="b@example.com")
    ok, msg = apply_contact_command(f"update {cid} whatsapp=+393331234567")
    assert ok
    assert db.get_contact(cid)["whatsapp"] == "+393331234567"


def test_apply_qualify_sets_score_and_status():
    from app import db
    from app.contact_cli import apply_contact_command
    cid = _mk_contact(email="c@example.com")
    ok, msg = apply_contact_command(f"qualify {cid} score=8")
    assert ok
    c = db.get_contact(cid)
    assert c["qualifier_score"] == 8
    assert c["status"] == "qualified"


def test_apply_bulk_optout():
    from app import db
    from app.contact_cli import apply_contact_command
    a = _mk_contact(email="d1@example.com")
    b = _mk_contact(email="d2@example.com")
    ok, msg = apply_contact_command(f"bulk-optout {a},{b}")
    assert ok
    assert db.get_contact(a)["status"] == "optedout"
    assert db.get_contact(b)["status"] == "optedout"


def test_apply_missing_contact():
    from app.contact_cli import apply_contact_command
    ok, msg = apply_contact_command("optout 999999")
    assert not ok
    assert "non trovato" in msg


# ---------------------------------------------------------------------------
# 3. Route end-to-end
# ---------------------------------------------------------------------------

def test_route_cli_optout(authed_client):
    from app import db
    cid = _mk_contact(email="route@example.com")
    r = authed_client.post("/inbox/contacts/cli", data={"command": f"optout {cid}"},
                           follow_redirects=False)
    assert r.status_code == 303
    assert "/inbox/contacts?flash=" in r.headers["location"]
    assert db.get_contact(cid, tenant_id=None)["status"] == "optedout"


def test_route_cli_update(authed_client):
    from app import db
    cid = _mk_contact(email="route2@example.com")
    authed_client.post("/inbox/contacts/cli",
                       data={"command": f"update {cid} email=new@example.com"},
                       follow_redirects=False)
    assert db.get_contact(cid, tenant_id=None)["email"] == "new@example.com"
