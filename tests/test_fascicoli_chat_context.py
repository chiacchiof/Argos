"""La chat del fascicolo deve includere i FOGLI (contenuto su DB) tra le fonti."""
from __future__ import annotations

import pytest

from app import db_cloud
from app.auth import CurrentUser, hash_password
from app.fascicoli import db as fdb, sheets_db as sdb, sheets_export as sx
from app.routes.fascicoli import _sheet_context_chunks


def _user(uid, tid):
    return CurrentUser(id=uid, email="u@x.it", role="tenant_user", tenant_id=tid, tenant_name=None, is_active=True)


def test_to_prompt_text_renders_grid():
    cells = [
        {"row": 0, "col": 0, "value": "Voce", "formula": None},
        {"row": 0, "col": 1, "value": "Importo", "formula": None},
        {"row": 1, "col": 0, "value": "Affitto", "formula": None},
        {"row": 1, "col": 1, "value": "1200", "formula": None},
    ]
    txt = sx.to_prompt_text(cells)
    assert "A\tB" in txt          # intestazioni colonna
    assert "Affitto" in txt and "1200" in txt
    assert sx.to_prompt_text([]) == "(foglio vuoto)"


def test_sheet_context_chunks_includes_attached_sheets():
    ta = db_cloud.create_tenant("TA", "ta")
    uid = db_cloud.create_user(tenant_id=ta, email="u@x.it", password_hash=hash_password("x"), role="tenant_user")
    pid = fdb.create_project(title="P", tenant_id=ta, owner_user_id=uid)
    sid = sdb.create_sheet(title="Budget", project_id=pid, tenant_id=ta, created_by_user_id=uid)
    sdb.apply_cell_patch(sid, [
        {"row": 0, "col": 0, "value": "Voce"}, {"row": 0, "col": 1, "value": "Importo"},
        {"row": 1, "col": 0, "value": "Affitto"}, {"row": 1, "col": 1, "value": "1200"},
    ], tenant_id=ta, actor_user_id=uid)
    # foglio vuoto: non deve comparire
    sdb.create_sheet(title="Vuoto", project_id=pid, tenant_id=ta, created_by_user_id=uid)

    chunks = _sheet_context_chunks(pid, _user(uid, ta))
    assert len(chunks) == 1
    c = chunks[0]
    assert c["file"] == "Foglio «Budget»"
    assert "Affitto" in c["text"] and "1200" in c["text"]
    assert "idx" in c and "text" in c and "score" in c  # forma compatibile con _build_messages


def test_sheet_context_chunks_empty_when_no_sheets():
    ta = db_cloud.create_tenant("TB", "tb")
    uid = db_cloud.create_user(tenant_id=ta, email="v@x.it", password_hash=hash_password("x"), role="tenant_user")
    pid = fdb.create_project(title="P2", tenant_id=ta, owner_user_id=uid)
    assert _sheet_context_chunks(pid, _user(uid, ta)) == []
