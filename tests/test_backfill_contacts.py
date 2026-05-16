"""Test del backfill contacts → assets (Fase 2B).

Verifica i 3 percorsi del script `scripts/backfill_contacts_to_assets.py`:
- contact con asset_id → copia campi su asset esistente (COALESCE)
- contact orfano con match per source_url → link + copia
- contact orfano senza match → crea shadow asset
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Permetti import dello script
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

import backfill_contacts_to_assets as backfill  # noqa: E402

from app import db, db_cloud  # noqa: E402
from app.auth import hash_password  # noqa: E402


@pytest.fixture
def backfill_setup():
    """Crea 1 tenant + 1 user + 3 scenari di contact."""
    tenant = db_cloud.create_tenant("T", "t")
    user = db_cloud.create_user(
        tenant_id=tenant, email="u@t", password_hash=hash_password("pwd"),
        role="tenant_user",
    )

    # Scenario 1: asset esistente + contact linkato
    asset1 = db.upsert_asset(
        {"asset_type": "profile", "title": "A1", "raw_json": "{}",
         "source_url": "https://test/a1"},
        tenant_id=tenant, created_by_user_id=user,
    )
    contact1 = db.upsert_contact(
        {"display_name": "Mario Linked", "email": "linked@x.it",
         "whatsapp": "+39111", "asset_id": asset1, "status": "contacted"},
        tenant_id=tenant, created_by_user_id=user,
    )

    # Scenario 2: asset esistente, contact orfano con stesso source_url
    asset2 = db.upsert_asset(
        {"asset_type": "profile", "title": "A2", "raw_json": "{}",
         "source_url": "https://test/a2"},
        tenant_id=tenant, created_by_user_id=user,
    )
    contact2 = db.upsert_contact(
        {"display_name": "Mario Orphan-Matched", "email": "matched@x.it",
         "telegram_username": "matched_tg",
         "source_url": "https://test/a2", "status": "replied"},
        tenant_id=tenant, created_by_user_id=user,
    )

    # Scenario 3: contact orfano senza match (source_url unico) → shadow asset
    contact3 = db.upsert_contact(
        {"display_name": "Mario Shadow", "email": "shadow@x.it",
         "source_url": "https://test/shadow-only", "status": "optedout",
         "qualifier_score": 8},
        tenant_id=tenant, created_by_user_id=user,
    )

    return {
        "tenant": tenant, "user": user,
        "asset1": asset1, "asset2": asset2,
        "contact1": contact1, "contact2": contact2, "contact3": contact3,
    }


def test_dry_run_makes_no_changes(backfill_setup):
    """--dry-run NON deve toccare il DB."""
    orig_assets = db.count_assets()
    with db.connect() as con:
        orig_contact3 = con.execute(
            "SELECT asset_id FROM contacts WHERE id = %s",
            (backfill_setup["contact3"],),
        ).fetchone()
    assert orig_contact3["asset_id"] is None

    # Run dry-run via sys.argv
    sys.argv = ["backfill", "--dry-run"]
    rc = backfill.main()
    assert rc == 0

    # Verifico nessuna change
    assert db.count_assets() == orig_assets
    with db.connect() as con:
        post = con.execute(
            "SELECT asset_id FROM contacts WHERE id = %s",
            (backfill_setup["contact3"],),
        ).fetchone()
    assert post["asset_id"] is None


def test_linked_contact_backfills_asset_fields(backfill_setup):
    """Contact1 ha asset1 linkato + email/whatsapp → asset1 deve riceverli."""
    sys.argv = ["backfill"]
    backfill.main()

    asset1 = db.get_asset(backfill_setup["asset1"], tenant_id=None)
    assert asset1["email"] == "linked@x.it"
    assert asset1["whatsapp"] == "+39111"
    assert asset1["display_name"] == "Mario Linked"
    assert asset1["outreach_status"] == "contacted"


def test_orphan_matched_by_url_links_and_backfills(backfill_setup):
    """Contact2 è orfano ma source_url matcha asset2 → linkato + campi copiati."""
    sys.argv = ["backfill"]
    backfill.main()

    with db.connect() as con:
        c2 = con.execute(
            "SELECT asset_id FROM contacts WHERE id = %s",
            (backfill_setup["contact2"],),
        ).fetchone()
    # Il contact2 ora deve avere asset_id = asset2
    assert c2["asset_id"] == backfill_setup["asset2"]

    asset2 = db.get_asset(backfill_setup["asset2"], tenant_id=None)
    assert asset2["email"] == "matched@x.it"
    assert asset2["telegram_username"] == "matched_tg"
    assert asset2["outreach_status"] == "replied"


def test_orphan_without_match_creates_shadow(backfill_setup):
    """Contact3 senza match → nuovo asset shadow + tag qualifier_legacy."""
    sys.argv = ["backfill"]
    backfill.main()

    with db.connect() as con:
        c3 = con.execute(
            "SELECT asset_id FROM contacts WHERE id = %s",
            (backfill_setup["contact3"],),
        ).fetchone()
    new_aid = c3["asset_id"]
    assert new_aid is not None
    assert new_aid != backfill_setup["asset1"]
    assert new_aid != backfill_setup["asset2"]

    shadow = db.get_asset(new_aid, tenant_id=None)
    assert shadow["asset_type"] == "contact_legacy"
    assert shadow["email"] == "shadow@x.it"
    assert shadow["display_name"] == "Mario Shadow"
    assert shadow["outreach_status"] == "optedout"
    # Tag qualifier_legacy preservata
    assert "qualifier_legacy" in shadow["tags"]
    assert "qualifier_score_legacy" in shadow["tags"]
    assert shadow["tags"]["qualifier_score_legacy"] == ["8"]


def test_idempotent_double_run(backfill_setup):
    """Eseguire il backfill due volte non deve creare duplicati."""
    sys.argv = ["backfill"]
    backfill.main()
    assets_after_first = db.count_assets()
    orfani_after_first = db.count_contacts() - sum(
        1 for c in db.list_contacts() if c.get("asset_id")
    )

    sys.argv = ["backfill"]
    backfill.main()
    assets_after_second = db.count_assets()

    # Nessuna duplicazione
    assert assets_after_second == assets_after_first
    assert orfani_after_first == 0


def test_outreach_status_does_not_downgrade(backfill_setup):
    """Se asset ha già outreach_status != 'pending' (es. settato a mano),
    il backfill non deve sovrascrivere a 'pending' anche se contact è 'new'."""
    # Setto outreach_status='contacted' su asset1 a mano
    with db.connect() as con:
        con.execute(
            "UPDATE assets SET outreach_status = 'contacted' WHERE id = %s",
            (backfill_setup["asset1"],),
        )
        con.commit()

    # Cambio contact1.status a 'new' (regressione semantica)
    with db.connect() as con:
        con.execute(
            "UPDATE contacts SET status = 'new' WHERE id = %s",
            (backfill_setup["contact1"],),
        )
        con.commit()

    sys.argv = ["backfill"]
    backfill.main()

    asset1 = db.get_asset(backfill_setup["asset1"], tenant_id=None)
    # Deve rimanere 'contacted' (non downgrade a 'pending')
    assert asset1["outreach_status"] == "contacted"
