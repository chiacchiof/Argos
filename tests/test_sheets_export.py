"""Test export Fogli: CSV anti formula-injection + XLSX valido (Fase 6)."""
from __future__ import annotations

import io
import zipfile

import pytest
from fastapi.testclient import TestClient

from app import db_cloud
from app.auth import hash_password
from app.fascicoli import sheets_db as sdb, sheets_export as sx


# ---- unit: csv_safe ----------------------------------------------------------

def test_csv_safe_neutralizes_formulas():
    assert sx.csv_safe("=SUM(A1)") == "'=SUM(A1)"
    assert sx.csv_safe("+cmd") == "'+cmd"
    assert sx.csv_safe("@x") == "'@x"
    assert sx.csv_safe("=2+5") == "'=2+5"


def test_csv_safe_preserves_numbers_and_text():
    assert sx.csv_safe("-5") == "-5"          # negativo: numero, non formula
    assert sx.csv_safe("+3.2") == "+3.2"
    assert sx.csv_safe("hello") == "hello"
    assert sx.csv_safe("") == ""


def test_to_csv_has_bom_and_escapes():
    cells = [
        {"row": 0, "col": 0, "value": "=HACK()", "formula": None},
        {"row": 0, "col": 1, "value": "ok", "formula": None},
        {"row": 1, "col": 0, "value": "-3", "formula": None},
    ]
    data = sx.to_csv(cells).decode("utf-8")
    assert data.startswith("﻿")          # BOM Excel-friendly
    assert "'=HACK()" in data
    assert "-3" in data and "'-3" not in data


# ---- unit: xlsx --------------------------------------------------------------

def test_to_xlsx_is_valid_zip_with_inline_string_and_number():
    cells = [
        {"row": 0, "col": 0, "value": "=HACK()", "formula": None},
        {"row": 0, "col": 1, "value": "42", "formula": None},
    ]
    blob = sx.to_xlsx(cells)
    z = zipfile.ZipFile(io.BytesIO(blob))
    names = z.namelist()
    assert "[Content_Types].xml" in names
    assert "xl/worksheets/sheet1.xml" in names
    sheet = z.read("xl/worksheets/sheet1.xml").decode("utf-8")
    # la formula e' scritta come inline string (NON valutata) -> safe
    assert "inlineStr" in sheet and "=HACK()" in sheet
    # il numero come cella numerica
    assert "<v>42</v>" in sheet


# ---- route -------------------------------------------------------------------

@pytest.fixture
def env(tmp_path, monkeypatch):
    from app import config, storage
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "RESULTS_DIR", tmp_path / "results")
    monkeypatch.setattr(storage, "RESULTS_DIR", tmp_path / "results")
    ta = db_cloud.create_tenant("TenantA", "tenant-a")
    db_cloud.create_user(tenant_id=ta, email="op-a@a.it", password_hash=hash_password("pw"), role="tenant_user")
    return {"ta": ta}


def test_export_routes(env):
    ctx = env
    op_a = db_cloud.get_user_by_email("op-a@a.it")["id"]
    sid = sdb.create_sheet(title="Budget 2026", tenant_id=ctx["ta"], created_by_user_id=op_a)
    sdb.apply_cell_patch(sid, [{"row": 0, "col": 0, "value": "=DANGER()"}, {"row": 0, "col": 1, "value": "100"}],
                         tenant_id=ctx["ta"], actor_user_id=op_a)
    from app.main import app
    with TestClient(app) as client:
        client.post("/login", data={"email": "op-a@a.it", "password": "pw", "next": "/"}, follow_redirects=False)
        # CSV
        r = client.get(f"/sheets/{sid}/export.csv")
        assert r.status_code == 200
        assert "text/csv" in r.headers["content-type"]
        assert "Budget 2026.csv" in r.headers["content-disposition"]
        assert "'=DANGER()" in r.text
        # XLSX
        r = client.get(f"/sheets/{sid}/export.xlsx")
        assert r.status_code == 200
        assert "spreadsheetml.sheet" in r.headers["content-type"]
        z = zipfile.ZipFile(io.BytesIO(r.content))
        assert "xl/worksheets/sheet1.xml" in z.namelist()
