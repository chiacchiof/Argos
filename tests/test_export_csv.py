"""Test export CSV (helper render + endpoint /qualified/export.csv e
/assets/export.csv).

Copre:
  - render_assets_csv: default fields + tags_mode flat/columns/none
  - BOM UTF-8 + header
  - endpoint /qualified/export.csv: filtri, asset_ids esplicito, select_all
  - endpoint /assets/export.csv: filtri, asset_ids esplicito
"""
from __future__ import annotations

import csv
import io

import pytest
from fastapi.testclient import TestClient

from app import db, db_cloud, export_csv
from app.auth import hash_password


@pytest.fixture
def export_setup():
    tenant = db_cloud.create_tenant("ExportT", "expt")
    user = db_cloud.create_user(
        tenant_id=tenant, email="u@expt", password_hash=hash_password("pw"),
        role="tenant_user",
    )
    # Super-admin per test endpoint
    if not db_cloud.get_user_by_email("expadmin"):
        db_cloud.create_user(
            tenant_id=None, email="expadmin",
            password_hash=hash_password("expw"), role="super_admin",
        )
    # 3 asset qualified con email/whatsapp diversi
    asset_ids = []
    for i, (title, email, wa) in enumerate([
        ("Mario", "mario@x.com", "+393331111111"),
        ("Luca",  "luca@y.com",  "+393332222222"),
        ("Sara",  "sara@z.com",  "+393333333333"),
    ], start=1):
        aid = db.upsert_asset(
            {
                "asset_type": "ig_profile",
                "title": title,
                "raw_json": "{}",
                "source_url": f"https://x.test/{i}",
                "email": email,
                "whatsapp": wa,
                "social_json": (
                    '[{"platform":"instagram","url":"https://instagram.com/'
                    + title.lower() + '"}]'
                ),
            },
            tenant_id=tenant, created_by_user_id=user,
        )
        db.set_asset_tag(aid, "qualifier_exp_test", "qualified")
        db.set_asset_tag(aid, "qualifier_score_exp_test", str(5 + i))
        db.set_asset_tag(aid, "interest", "fitness" if i < 3 else "cucina")
        db.update_asset_qualifier(aid, 5 + i, "qualified")
        asset_ids.append(aid)
    return {"tenant": tenant, "user": user, "asset_ids": asset_ids}


# ---------------------------------------------------------------------------
# Helper render
# ---------------------------------------------------------------------------

def _parse_csv(chunks):
    """Concatena byte chunks, strippa BOM, parse CSV. Ritorna (header, rows)."""
    raw = b"".join(chunks)
    assert raw.startswith(b"\xef\xbb\xbf"), "BOM UTF-8 mancante"
    text = raw[3:].decode("utf-8")
    reader = csv.reader(io.StringIO(text))
    rows = list(reader)
    return rows[0], rows[1:]


def test_render_assets_csv_default_fields(export_setup):
    aids = export_setup["asset_ids"]
    assets = [db.get_asset(aid, tenant_id=None) for aid in aids]
    # Default fields (DEFAULT_FIELDS set)
    chunks = list(export_csv.render_assets_csv(assets, list(export_csv.DEFAULT_FIELDS)))
    header, rows = _parse_csv(chunks)
    assert "ID" in header
    assert "Titolo" in header
    assert "Email" in header
    assert "WhatsApp" in header
    assert "Instagram URL" in header
    assert len(rows) == 3
    # Verifica che email/wa siano popolati nelle righe
    titles_col = header.index("Titolo")
    email_col = header.index("Email")
    titles = sorted(r[titles_col] for r in rows)
    assert titles == ["Luca", "Mario", "Sara"]
    emails = sorted(r[email_col] for r in rows)
    assert emails == ["luca@y.com", "mario@x.com", "sara@z.com"]


def test_render_assets_csv_tags_mode_flat(export_setup):
    aids = export_setup["asset_ids"]
    assets = [db.get_asset(aid, tenant_id=None) for aid in aids]
    chunks = list(export_csv.render_assets_csv(
        assets, ["id", "title"], tags_mode="flat",
    ))
    header, rows = _parse_csv(chunks)
    assert header == ["ID", "Titolo", "Tags"]
    # Ogni riga ha la colonna Tags con "k=v;..." popolata
    for r in rows:
        assert "qualifier_exp_test=qualified" in r[2]
        assert "interest=" in r[2]


def test_render_assets_csv_tags_mode_columns(export_setup):
    aids = export_setup["asset_ids"]
    assets = [db.get_asset(aid, tenant_id=None) for aid in aids]
    chunks = list(export_csv.render_assets_csv(
        assets, ["id", "title"], tags_mode="columns",
    ))
    header, rows = _parse_csv(chunks)
    # Le tag_key distinte: qualifier_exp_test, qualifier_score_exp_test, interest
    expected_tag_cols = {
        "tag:qualifier_exp_test",
        "tag:qualifier_score_exp_test",
        "tag:interest",
    }
    assert expected_tag_cols.issubset(set(header))
    # Una riga deve avere "qualified" nella colonna tag:qualifier_exp_test
    col_qual = header.index("tag:qualifier_exp_test")
    assert all(r[col_qual] == "qualified" for r in rows)


def test_render_assets_csv_qualifier_fields(export_setup):
    aids = export_setup["asset_ids"]
    assets = [db.get_asset(aid, tenant_id=None) for aid in aids]
    chunks = list(export_csv.render_assets_csv(
        assets, ["id", "qualifier_slugs", "qualifier_scores"], tags_mode="none",
    ))
    header, rows = _parse_csv(chunks)
    assert header == ["ID", "Qualifier (slugs)", "Qualifier (scores)"]
    for r in rows:
        assert "exp_test" in r[1]
        assert "exp_test=" in r[2]


def test_render_assets_csv_unknown_field_skipped(export_setup):
    aids = export_setup["asset_ids"]
    assets = [db.get_asset(aid, tenant_id=None) for aid in aids]
    chunks = list(export_csv.render_assets_csv(
        assets, ["id", "FAKE_FIELD", "title"], tags_mode="none",
    ))
    header, _ = _parse_csv(chunks)
    # FAKE_FIELD silently skipped
    assert header == ["ID", "Titolo"]


# ---------------------------------------------------------------------------
# Endpoint /qualified/export.csv
# ---------------------------------------------------------------------------

def test_qualified_export_csv_with_filters(export_setup):
    from app.main import app
    client = TestClient(app)
    with client:
        client.post("/login", data={"email": "expadmin", "password": "expw"})
        r = client.post(
            "/qualified/export.csv",
            data={
                "qualifiers": "exp_test",
                "status": "qualified",
                "fields": ["id", "title", "email"],
                "tags_mode": "none",
            },
        )
        assert r.status_code == 200
        assert "text/csv" in r.headers["content-type"]
        assert "attachment" in r.headers["content-disposition"]
        body = r.content
        assert body.startswith(b"\xef\xbb\xbf")
        text = body[3:].decode("utf-8")
        # Header + 3 righe
        lines = [ln for ln in text.split("\n") if ln.strip()]
        assert len(lines) == 4  # header + 3
        assert "ID,Titolo,Email" in lines[0]
        for t in ("Mario", "Luca", "Sara"):
            assert t in text


def test_qualified_export_csv_with_explicit_asset_ids(export_setup):
    """Selezione esplicita: solo 2 dei 3 qualified."""
    from app.main import app
    aids = export_setup["asset_ids"]
    client = TestClient(app)
    with client:
        client.post("/login", data={"email": "expadmin", "password": "expw"})
        r = client.post(
            "/qualified/export.csv",
            data={
                "qualifiers": "exp_test",  # filtro presente ma ignorato per asset_ids
                "fields": ["id", "title"],
                "asset_ids": [str(aids[0]), str(aids[2])],
            },
        )
        assert r.status_code == 200
        text = r.content[3:].decode("utf-8")
        # Solo 2 righe (Mario + Sara, non Luca)
        lines = [ln for ln in text.split("\n") if ln.strip()]
        assert len(lines) == 3  # header + 2
        assert "Mario" in text
        assert "Sara" in text
        assert "Luca" not in text


def test_qualified_export_csv_select_all_filtered_overrides_ids(export_setup):
    """Flag select_all_filtered=1 vince su asset_ids: tutti i filtrati."""
    from app.main import app
    aids = export_setup["asset_ids"]
    client = TestClient(app)
    with client:
        client.post("/login", data={"email": "expadmin", "password": "expw"})
        r = client.post(
            "/qualified/export.csv",
            data={
                "qualifiers": "exp_test",
                "fields": ["id", "title"],
                "asset_ids": [str(aids[0])],
                "select_all_filtered": "1",
            },
        )
        assert r.status_code == 200
        text = r.content[3:].decode("utf-8")
        lines = [ln for ln in text.split("\n") if ln.strip()]
        assert len(lines) == 4  # header + 3 (tutti)


def test_qualified_export_csv_empty_audience_400(export_setup):
    from app.main import app
    client = TestClient(app)
    with client:
        client.post("/login", data={"email": "expadmin", "password": "expw"})
        r = client.post(
            "/qualified/export.csv",
            data={
                "qualifiers": "qualifier_che_non_esiste",
                "fields": ["id"],
            },
        )
        assert r.status_code == 400


# ---------------------------------------------------------------------------
# Endpoint /assets/export.csv
# ---------------------------------------------------------------------------

def test_assets_export_csv_with_filters(export_setup):
    from app.main import app
    client = TestClient(app)
    with client:
        client.post("/login", data={"email": "expadmin", "password": "expw"})
        r = client.post(
            "/assets/export.csv",
            data={
                "asset_type": "ig_profile",
                "fields": ["id", "title", "email"],
                "tags_mode": "none",
            },
        )
        assert r.status_code == 200
        text = r.content[3:].decode("utf-8")
        lines = [ln for ln in text.split("\n") if ln.strip()]
        assert len(lines) >= 4  # header + >= 3
        for t in ("Mario", "Luca", "Sara"):
            assert t in text


def test_assets_export_csv_with_explicit_asset_ids(export_setup):
    from app.main import app
    aids = export_setup["asset_ids"]
    client = TestClient(app)
    with client:
        client.post("/login", data={"email": "expadmin", "password": "expw"})
        r = client.post(
            "/assets/export.csv",
            data={
                "fields": ["id", "title"],
                "asset_ids": [str(aids[1])],  # solo Luca
            },
        )
        assert r.status_code == 200
        text = r.content[3:].decode("utf-8")
        lines = [ln for ln in text.split("\n") if ln.strip()]
        assert len(lines) == 2  # header + 1
        assert "Luca" in text
