"""B-016: Test asset dedup cross-task + merge.

Copre:
  - Normalizzazione phone/email/telegram/social handle.
  - find_dedup_candidates: rileva match su chiavi forti (whatsapp/email/
    social handle/telegram) e medie (url canonical).
  - Tenant isolation: asset di tenant diversi NON matchano.
  - merge_assets: redirect FK (tasks.target_asset_ids, social_dm_log,
    asset_tags), union campi vuoti, marca candidate.
  - Idempotenza: re-scan stesso asset non duplica rows.
  - reject_candidate: marca rejected.
"""
from __future__ import annotations

import pytest

from app import db, db_cloud
from app.agent import asset_dedup
from app.auth import hash_password


@pytest.fixture
def dedup_setup():
    tenant_a = db_cloud.create_tenant("DedupA", "ddpa")
    tenant_b = db_cloud.create_tenant("DedupB", "ddpb")
    user_a = db_cloud.create_user(
        tenant_id=tenant_a, email="a@ddp", password_hash=hash_password("p"),
        role="tenant_user",
    )
    return {"tenant_a": tenant_a, "tenant_b": tenant_b, "user_a": user_a}


# ---------------------------------------------------------------------------
# Normalizers
# ---------------------------------------------------------------------------

def test_normalize_phone_italian_formats():
    assert asset_dedup.normalize_phone("+39 333 1234567") == "+393331234567"
    assert asset_dedup.normalize_phone("0039 333 1234567") == "+393331234567"
    assert asset_dedup.normalize_phone("3331234567") == "+393331234567"
    assert asset_dedup.normalize_phone("+393331234567") == "+393331234567"
    assert asset_dedup.normalize_phone("") is None
    assert asset_dedup.normalize_phone(None) is None
    assert asset_dedup.normalize_phone("abc") is None
    # Solo 9 digit → invalido
    assert asset_dedup.normalize_phone("333123456") is None


def test_normalize_email():
    assert asset_dedup.normalize_email("  Foo@Bar.COM ") == "foo@bar.com"
    assert asset_dedup.normalize_email("not_an_email") is None
    assert asset_dedup.normalize_email("") is None
    assert asset_dedup.normalize_email("a@b") is None  # no TLD


def test_normalize_telegram():
    assert asset_dedup.normalize_telegram("@MarioRossi", None) == "@mariorossi"
    assert asset_dedup.normalize_telegram("MarioRossi", None) == "@mariorossi"
    assert asset_dedup.normalize_telegram(None, "123456") == "id:123456"
    assert asset_dedup.normalize_telegram("", "") is None


def test_extract_social_handles():
    sj = [
        {"platform": "instagram", "url": "https://www.instagram.com/MarioRossi/"},
        {"platform": "tiktok", "url": "https://tiktok.com/@mariorossi.official"},
        {"platform": "facebook", "url": "https://facebook.com/profile.php?id=12345"},
    ]
    out = asset_dedup.extract_social_handles(sj)
    assert out["instagram"] == "mariorossi"
    assert out["tiktok"] == "mariorossi.official"
    assert "facebook" in out


# ---------------------------------------------------------------------------
# find_dedup_candidates
# ---------------------------------------------------------------------------

def _mk_asset(tenant_id: int, user_id: int, **kw) -> int:
    base = {
        "asset_type": "profile_contacts",
        "title": "X",
        "raw_json": "{}",
    }
    base.update(kw)
    return db.upsert_asset(
        base, tenant_id=tenant_id, created_by_user_id=user_id,
    )


def test_find_candidates_whatsapp_strong_match(dedup_setup):
    """Due asset stesso tenant con stesso whatsapp → candidate row creata."""
    ta, ua = dedup_setup["tenant_a"], dedup_setup["user_a"]
    a = _mk_asset(ta, ua, title="Mario A", whatsapp="+393331234567",
                  source_url="https://a.test/mario")
    b = _mk_asset(ta, ua, title="M. Rossi", whatsapp="+39 333 1234567",
                  source_url="https://b.test/mario")
    # find_dedup_candidates viene chiamata automaticamente in upsert_asset.
    # Verifica che ci sia una riga candidate.
    candidates = asset_dedup.list_pending_candidates(tenant_id=ta)
    pair_ids = {(c["primary_asset_id"], c["candidate_asset_id"]) for c in candidates}
    expected = (min(a, b), max(a, b))
    assert expected in pair_ids
    # Score >= 0.5 (whatsapp = strong = 1.0)
    target = next(c for c in candidates if (c["primary_asset_id"], c["candidate_asset_id"]) == expected)
    assert target["match_score"] >= 1.0


def test_find_candidates_email_strong_match(dedup_setup):
    ta, ua = dedup_setup["tenant_a"], dedup_setup["user_a"]
    a = _mk_asset(ta, ua, title="A", email="Foo@Bar.com",
                  source_url="https://x.test/1")
    b = _mk_asset(ta, ua, title="B", email="foo@bar.com",
                  source_url="https://x.test/2")
    candidates = asset_dedup.list_pending_candidates(tenant_id=ta)
    pair = (min(a, b), max(a, b))
    assert any((c["primary_asset_id"], c["candidate_asset_id"]) == pair for c in candidates)


def test_find_candidates_social_handle_match(dedup_setup):
    import json
    ta, ua = dedup_setup["tenant_a"], dedup_setup["user_a"]
    sj1 = json.dumps([{"platform": "instagram", "url": "https://instagram.com/mario_x/"}])
    sj2 = json.dumps([{"platform": "instagram", "url": "https://www.instagram.com/Mario_X"}])
    a = _mk_asset(ta, ua, title="A", social_json=sj1, source_url="https://q.test/a")
    b = _mk_asset(ta, ua, title="B", social_json=sj2, source_url="https://q.test/b")
    candidates = asset_dedup.list_pending_candidates(tenant_id=ta)
    pair = (min(a, b), max(a, b))
    assert any((c["primary_asset_id"], c["candidate_asset_id"]) == pair for c in candidates)


def test_tenant_isolation_no_cross_tenant_match(dedup_setup):
    """Asset di tenant diversi con stesso whatsapp NON devono matchare."""
    ta, tb, ua = dedup_setup["tenant_a"], dedup_setup["tenant_b"], dedup_setup["user_a"]
    a = _mk_asset(ta, ua, title="A", whatsapp="+393331111111",
                  source_url="https://t.test/a")
    b = _mk_asset(tb, None, title="B", whatsapp="+393331111111",
                  source_url="https://t.test/b")
    # In tenant A: nessun candidate (b e' in tenant B)
    candidates_a = asset_dedup.list_pending_candidates(tenant_id=ta)
    assert not any(c["primary_asset_id"] == a or c["candidate_asset_id"] == a
                   for c in candidates_a)


def test_dedup_idempotent_re_run(dedup_setup):
    """Riesegui find_dedup_candidates sullo stesso asset → no duplicate rows."""
    ta, ua = dedup_setup["tenant_a"], dedup_setup["user_a"]
    a = _mk_asset(ta, ua, title="A", whatsapp="+393332222222",
                  source_url="https://z.test/a")
    b = _mk_asset(ta, ua, title="B", whatsapp="+393332222222",
                  source_url="https://z.test/b")
    initial = asset_dedup.list_pending_candidates(tenant_id=ta)
    n0 = len(initial)
    # Riesegui detection manuale
    asset_dedup.find_dedup_candidates(b, tenant_id=ta)
    asset_dedup.find_dedup_candidates(a, tenant_id=ta)
    second = asset_dedup.list_pending_candidates(tenant_id=ta)
    assert len(second) == n0


# ---------------------------------------------------------------------------
# merge_assets
# ---------------------------------------------------------------------------

def test_merge_redirects_target_asset_ids(dedup_setup):
    """Merge: tasks.target_asset_ids riferito al candidate → riscritto al primary."""
    ta, ua = dedup_setup["tenant_a"], dedup_setup["user_a"]
    a = _mk_asset(ta, ua, title="A", whatsapp="+393333333333",
                  source_url="https://m.test/a")
    b = _mk_asset(ta, ua, title="B", whatsapp="+393333333333",
                  source_url="https://m.test/b")
    # Task con audience contenente entrambi
    tid = db.create_task({
        "name": "T", "objective": "o", "agent_mode": "outreach",
        "target_asset_ids": [a, b],
    }, tenant_id=ta, created_by_user_id=ua)
    # Merge b in a
    result = asset_dedup.merge_assets(a, b, tenant_id=ta)
    assert "candidate marked merged_into" in result["merged_fields"]
    # target_asset_ids deve essere [a] (dedup)
    task = db.get_task(tid, tenant_id=None)
    assert task["target_asset_ids"] == [a]


def test_merge_unions_empty_fields(dedup_setup):
    """Primary vuoto su email, candidate ha email → primary la eredita."""
    ta, ua = dedup_setup["tenant_a"], dedup_setup["user_a"]
    a = _mk_asset(ta, ua, title="A", whatsapp="+393334444444",
                  source_url="https://u.test/a")
    b = _mk_asset(ta, ua, title="B", whatsapp="+393334444444",
                  email="b@b.com", source_url="https://u.test/b")
    asset_dedup.merge_assets(a, b, tenant_id=ta)
    primary = db.get_asset(a, tenant_id=None)
    assert primary["email"] == "b@b.com"


def test_merge_marks_candidate_merged(dedup_setup):
    ta, ua = dedup_setup["tenant_a"], dedup_setup["user_a"]
    a = _mk_asset(ta, ua, title="A", whatsapp="+393335555555",
                  source_url="https://v.test/a")
    b = _mk_asset(ta, ua, title="B", whatsapp="+393335555555",
                  source_url="https://v.test/b")
    asset_dedup.merge_assets(a, b, tenant_id=ta)
    candidate = db.get_asset(b, tenant_id=None)
    assert candidate["dedup_status"] == f"merged_into:{a}"
    assert candidate["dedup_canonical_id"] == a


def test_merge_resolves_candidate_row_status(dedup_setup):
    """Dopo merge, la riga in asset_dedup_candidates ha status='merged'."""
    ta, ua = dedup_setup["tenant_a"], dedup_setup["user_a"]
    a = _mk_asset(ta, ua, title="A", whatsapp="+393336666666",
                  source_url="https://w.test/a")
    b = _mk_asset(ta, ua, title="B", whatsapp="+393336666666",
                  source_url="https://w.test/b")
    asset_dedup.merge_assets(a, b, tenant_id=ta)
    pending = asset_dedup.list_pending_candidates(tenant_id=ta)
    # Quella coppia non e' piu' pending
    pair = (min(a, b), max(a, b))
    assert not any((c["primary_asset_id"], c["candidate_asset_id"]) == pair
                   for c in pending)


def test_merge_tenant_mismatch_fails(dedup_setup):
    """Merge cross-tenant deve sollevare ValueError."""
    ta, tb, ua = dedup_setup["tenant_a"], dedup_setup["tenant_b"], dedup_setup["user_a"]
    a = _mk_asset(ta, ua, title="A", source_url="https://k.test/a")
    b = _mk_asset(tb, None, title="B", source_url="https://k.test/b")
    with pytest.raises(ValueError, match="tenant mismatch"):
        asset_dedup.merge_assets(a, b, tenant_id=ta)


def test_reject_candidate_marks_rejected(dedup_setup):
    ta, ua = dedup_setup["tenant_a"], dedup_setup["user_a"]
    a = _mk_asset(ta, ua, title="A", email="r1@x.com",
                  source_url="https://r.test/a")
    b = _mk_asset(ta, ua, title="B", email="r1@x.com",
                  source_url="https://r.test/b")
    pending = asset_dedup.list_pending_candidates(tenant_id=ta)
    pair = (min(a, b), max(a, b))
    target = next(c for c in pending if (c["primary_asset_id"], c["candidate_asset_id"]) == pair)
    asset_dedup.reject_candidate(target["id"])
    pending2 = asset_dedup.list_pending_candidates(tenant_id=ta)
    assert not any((c["primary_asset_id"], c["candidate_asset_id"]) == pair
                   for c in pending2)
