"""Test Fogli collaborativi — livello DB/ACL (Fase 1).

Copre: CRUD foglio, revisioni monotone, snapshot celle, validazione payload,
isolamento tenant (anti-IDOR), visibilita' standalone/agganciato, e la matrice
permessi operatore vs architetto.

I test di route/WebSocket end-to-end stanno in test_sheets_routes.py.
"""
from __future__ import annotations

import pytest

from app import db, db_cloud
from app.auth import CurrentUser, hash_password
from app.fascicoli import acl, db as fdb, sheets_db as sdb


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def two_tenants():
    """2 tenant con 1 architetto + 1 operatore ciascuno."""
    ta = db_cloud.create_tenant("TenantA", "tenant-a")
    tb = db_cloud.create_tenant("TenantB", "tenant-b")
    arch_a = db_cloud.create_user(
        tenant_id=ta, email="arch-a@a.it", password_hash=hash_password("x"),
        role="tenant_architect",
    )
    op_a = db_cloud.create_user(
        tenant_id=ta, email="op-a@a.it", password_hash=hash_password("x"),
        role="tenant_user",
    )
    op_a2 = db_cloud.create_user(
        tenant_id=ta, email="op-a2@a.it", password_hash=hash_password("x"),
        role="tenant_user",
    )
    arch_b = db_cloud.create_user(
        tenant_id=tb, email="arch-b@b.it", password_hash=hash_password("x"),
        role="tenant_architect",
    )
    super_id = db_cloud.create_user(
        tenant_id=None, email="super", password_hash=hash_password("x"),
        role="super_admin",
    )
    return {
        "ta": ta, "tb": tb,
        "arch_a": arch_a, "op_a": op_a, "op_a2": op_a2,
        "arch_b": arch_b, "super_id": super_id,
    }


def _user(uid, role, tenant_id, email="u@x.it"):
    return CurrentUser(id=uid, email=email, role=role, tenant_id=tenant_id,
                       tenant_name=None, is_active=True)


# ---------------------------------------------------------------------------
# CRUD + revisioni
# ---------------------------------------------------------------------------

def test_create_and_get_standalone_sheet(two_tenants):
    ctx = two_tenants
    sid = sdb.create_sheet(title="Budget", tenant_id=ctx["ta"], created_by_user_id=ctx["op_a"])
    s = sdb.get_sheet(sid, tenant_id=ctx["ta"], current_user_id=ctx["op_a"])
    assert s is not None
    assert s["title"] == "Budget"
    assert s["project_id"] is None
    assert s["visibility"] == "tenant"
    assert s["revision"] == 0
    assert s["n_rows"] == sdb.DEFAULT_ROWS and s["n_cols"] == sdb.DEFAULT_COLS


def test_apply_patch_increments_revision_and_stores_cells(two_tenants):
    ctx = two_tenants
    sid = sdb.create_sheet(title="S", tenant_id=ctx["ta"], created_by_user_id=ctx["op_a"])
    r1 = sdb.apply_cell_patch(
        sid, [{"row": 0, "col": 0, "value": "Acme SRL"}],
        tenant_id=ctx["ta"], actor_user_id=ctx["op_a"],
    )
    assert r1["revision"] == 1
    r2 = sdb.apply_cell_patch(
        sid, [{"row": 1, "col": 2, "value": "42"}, {"row": 0, "col": 0, "value": "Acme spa"}],
        tenant_id=ctx["ta"], actor_user_id=ctx["op_a"],
    )
    assert r2["revision"] == 2

    cells = sdb.get_cells(sid, tenant_id=ctx["ta"])
    by_pos = {(c["row"], c["col"]): c["value"] for c in cells}
    assert by_pos[(0, 0)] == "Acme spa"  # ultima patch vince
    assert by_pos[(1, 2)] == "42"
    assert sdb.get_head_revision(sid, tenant_id=ctx["ta"]) == 2


def test_get_revisions_since_for_reconnect(two_tenants):
    ctx = two_tenants
    sid = sdb.create_sheet(title="S", tenant_id=ctx["ta"], created_by_user_id=ctx["op_a"])
    sdb.apply_cell_patch(sid, [{"row": 0, "col": 0, "value": "a"}], tenant_id=ctx["ta"], actor_user_id=ctx["op_a"])
    sdb.apply_cell_patch(sid, [{"row": 0, "col": 1, "value": "b"}], tenant_id=ctx["ta"], actor_user_id=ctx["op_a"])
    sdb.apply_cell_patch(sid, [{"row": 0, "col": 2, "value": "c"}], tenant_id=ctx["ta"], actor_user_id=ctx["op_a"])
    # Client fermo a rev 1 → deve ricevere rev 2 e 3
    revs = sdb.get_revisions_since(sid, 1, tenant_id=ctx["ta"])
    assert [r["revision"] for r in revs] == [2, 3]
    assert revs[0]["patch"]["cells"][0]["value"] == "b"


def test_rename_archive_delete(two_tenants):
    ctx = two_tenants
    sid = sdb.create_sheet(title="Old", tenant_id=ctx["ta"], created_by_user_id=ctx["op_a"])
    sdb.rename_sheet(sid, "New", tenant_id=ctx["ta"])
    assert sdb.get_sheet(sid, tenant_id=ctx["ta"], current_user_id=ctx["op_a"])["title"] == "New"
    sdb.set_sheet_archived(sid, True, tenant_id=ctx["ta"])
    # Non compare nelle liste non-archiviate
    listed = sdb.list_sheets(tenant_id=ctx["ta"], current_user_id=ctx["op_a"])
    assert sid not in {s["id"] for s in listed}
    assert sdb.delete_sheet(sid, tenant_id=ctx["ta"]) is True
    assert sdb.get_sheet(sid, tenant_id=ctx["ta"], current_user_id=ctx["op_a"]) is None


# ---------------------------------------------------------------------------
# Validazione payload (piano §Validazione Payload)
# ---------------------------------------------------------------------------

def test_patch_rejects_too_many_cells(two_tenants):
    ctx = two_tenants
    sid = sdb.create_sheet(title="S", tenant_id=ctx["ta"], created_by_user_id=ctx["op_a"],
                           n_rows=1000, n_cols=100)
    big = [{"row": i // 100, "col": i % 100, "value": "x"} for i in range(sdb.MAX_CELLS_PER_PATCH + 1)]
    with pytest.raises(sdb.SheetValidationError):
        sdb.apply_cell_patch(sid, big, tenant_id=ctx["ta"], actor_user_id=ctx["op_a"])


def test_patch_rejects_out_of_range_and_negative(two_tenants):
    ctx = two_tenants
    sid = sdb.create_sheet(title="S", tenant_id=ctx["ta"], created_by_user_id=ctx["op_a"])  # 100x26
    with pytest.raises(sdb.SheetValidationError):
        sdb.apply_cell_patch(sid, [{"row": 0, "col": 26, "value": "x"}], tenant_id=ctx["ta"], actor_user_id=ctx["op_a"])
    with pytest.raises(sdb.SheetValidationError):
        sdb.apply_cell_patch(sid, [{"row": -1, "col": 0, "value": "x"}], tenant_id=ctx["ta"], actor_user_id=ctx["op_a"])
    with pytest.raises(sdb.SheetValidationError):
        sdb.apply_cell_patch(sid, [{"row": 100, "col": 0, "value": "x"}], tenant_id=ctx["ta"], actor_user_id=ctx["op_a"])


def test_patch_rejects_oversized_value_and_duplicates(two_tenants):
    ctx = two_tenants
    sid = sdb.create_sheet(title="S", tenant_id=ctx["ta"], created_by_user_id=ctx["op_a"])
    with pytest.raises(sdb.SheetValidationError):
        sdb.apply_cell_patch(sid, [{"row": 0, "col": 0, "value": "x" * (sdb.MAX_VALUE_LEN + 1)}],
                             tenant_id=ctx["ta"], actor_user_id=ctx["op_a"])
    with pytest.raises(sdb.SheetValidationError):
        sdb.apply_cell_patch(sid, [{"row": 0, "col": 0, "value": "a"}, {"row": 0, "col": 0, "value": "b"}],
                             tenant_id=ctx["ta"], actor_user_id=ctx["op_a"])


def test_failed_patch_does_not_bump_revision(two_tenants):
    ctx = two_tenants
    sid = sdb.create_sheet(title="S", tenant_id=ctx["ta"], created_by_user_id=ctx["op_a"])
    with pytest.raises(sdb.SheetValidationError):
        sdb.apply_cell_patch(sid, [{"row": 0, "col": 999, "value": "x"}], tenant_id=ctx["ta"], actor_user_id=ctx["op_a"])
    assert sdb.get_head_revision(sid, tenant_id=ctx["ta"]) == 0


# ---------------------------------------------------------------------------
# Isolamento tenant (anti-IDOR)
# ---------------------------------------------------------------------------

def test_sheet_not_visible_cross_tenant(two_tenants):
    ctx = two_tenants
    sid = sdb.create_sheet(title="Secret A", tenant_id=ctx["ta"], created_by_user_id=ctx["op_a"])
    # Tenant B non lo vede
    assert sdb.get_sheet(sid, tenant_id=ctx["tb"], current_user_id=ctx["arch_b"]) is None
    listed_b = sdb.list_sheets(tenant_id=ctx["tb"], current_user_id=ctx["arch_b"])
    assert sid not in {s["id"] for s in listed_b}


def test_apply_patch_cross_tenant_forbidden(two_tenants):
    ctx = two_tenants
    sid = sdb.create_sheet(title="Secret A", tenant_id=ctx["ta"], created_by_user_id=ctx["op_a"])
    with pytest.raises(sdb.SheetForbidden):
        sdb.apply_cell_patch(sid, [{"row": 0, "col": 0, "value": "hack"}],
                             tenant_id=ctx["tb"], actor_user_id=ctx["arch_b"])
    # Nessuna cella scritta
    assert sdb.get_cells(sid, tenant_id=ctx["ta"]) == []


def test_get_cells_cross_tenant_returns_empty(two_tenants):
    ctx = two_tenants
    sid = sdb.create_sheet(title="A", tenant_id=ctx["ta"], created_by_user_id=ctx["op_a"])
    sdb.apply_cell_patch(sid, [{"row": 0, "col": 0, "value": "a"}], tenant_id=ctx["ta"], actor_user_id=ctx["op_a"])
    # Tenant B non legge le celle
    assert sdb.get_cells(sid, tenant_id=ctx["tb"]) == []
    assert sdb.get_revisions_since(sid, 0, tenant_id=ctx["tb"]) == []


def test_rename_cross_tenant_noop(two_tenants):
    ctx = two_tenants
    sid = sdb.create_sheet(title="A", tenant_id=ctx["ta"], created_by_user_id=ctx["op_a"])
    sdb.rename_sheet(sid, "HACKED", tenant_id=ctx["tb"])  # tenant sbagliato → no-op
    assert sdb.get_sheet(sid, tenant_id=ctx["ta"], current_user_id=ctx["op_a"])["title"] == "A"


def test_create_sheet_cross_tenant_project_forbidden(two_tenants):
    ctx = two_tenants
    # Progetto del tenant A
    pid = fdb.create_project(title="ProjA", tenant_id=ctx["ta"], owner_user_id=ctx["op_a"])
    # Tenant B prova ad agganciare un foglio al progetto di A → SheetForbidden
    with pytest.raises(sdb.SheetForbidden):
        sdb.create_sheet(title="X", project_id=pid, tenant_id=ctx["tb"], created_by_user_id=ctx["arch_b"])


# ---------------------------------------------------------------------------
# Visibilita' standalone (tenant vs user) + architect_view
# ---------------------------------------------------------------------------

def test_standalone_user_visibility_only_creator(two_tenants):
    ctx = two_tenants
    sid = sdb.create_sheet(title="Privato", visibility="user",
                           tenant_id=ctx["ta"], created_by_user_id=ctx["op_a"])
    # Il creatore lo vede
    assert sdb.get_sheet(sid, tenant_id=ctx["ta"], current_user_id=ctx["op_a"]) is not None
    # Un altro operatore dello stesso tenant NO
    assert sdb.get_sheet(sid, tenant_id=ctx["ta"], current_user_id=ctx["op_a2"]) is None
    # L'architetto SI (architect_view)
    assert sdb.get_sheet(sid, tenant_id=ctx["ta"], current_user_id=ctx["arch_a"], architect_view=True) is not None


def test_standalone_tenant_visibility_all_members(two_tenants):
    ctx = two_tenants
    sid = sdb.create_sheet(title="Condiviso", visibility="tenant",
                           tenant_id=ctx["ta"], created_by_user_id=ctx["op_a"])
    # Un altro operatore dello stesso tenant lo vede
    assert sdb.get_sheet(sid, tenant_id=ctx["ta"], current_user_id=ctx["op_a2"]) is not None


# ---------------------------------------------------------------------------
# Matrice permessi ACL (operatore vs architetto)
# ---------------------------------------------------------------------------

def test_acl_standalone_tenant_sheet(two_tenants):
    ctx = two_tenants
    sid = sdb.create_sheet(title="T", visibility="tenant",
                           tenant_id=ctx["ta"], created_by_user_id=ctx["op_a"])
    sheet = sdb.get_sheet(sid, tenant_id=ctx["ta"], current_user_id=ctx["op_a"])
    op = _user(ctx["op_a2"], "tenant_user", ctx["ta"])
    arch = _user(ctx["arch_a"], "tenant_architect", ctx["ta"])
    creator = _user(ctx["op_a"], "tenant_user", ctx["ta"])
    # tenant-collaborativo: ogni operatore del tenant apre e modifica
    assert acl.can_open_sheet(sheet, None, op) is True
    assert acl.can_edit_sheet_cells(sheet, None, op) is True
    # ma NON gestisce (rename/archive) se non e' creatore/architetto
    assert acl.can_manage_sheet(sheet, None, op) is False
    assert acl.can_manage_sheet(sheet, None, creator) is True
    assert acl.can_manage_sheet(sheet, None, arch) is True


def test_acl_standalone_user_sheet(two_tenants):
    ctx = two_tenants
    sid = sdb.create_sheet(title="P", visibility="user",
                           tenant_id=ctx["ta"], created_by_user_id=ctx["op_a"])
    sheet = sdb.get_sheet(sid, tenant_id=ctx["ta"], current_user_id=ctx["op_a"])
    other_op = _user(ctx["op_a2"], "tenant_user", ctx["ta"])
    arch = _user(ctx["arch_a"], "tenant_architect", ctx["ta"])
    # foglio privato: un altro operatore non apre/modifica
    assert acl.can_open_sheet(sheet, None, other_op) is False
    assert acl.can_edit_sheet_cells(sheet, None, other_op) is False
    # architetto sempre
    assert acl.can_open_sheet(sheet, None, arch) is True
    assert acl.can_edit_sheet_cells(sheet, None, arch) is True


def test_acl_project_attached_follows_project(two_tenants):
    ctx = two_tenants
    # Progetto user-visibility, owner op_a, con op_a2 come viewer
    pid = fdb.create_project(title="P", visibility="user",
                             tenant_id=ctx["ta"], owner_user_id=ctx["op_a"])
    fdb.add_project_member(pid, ctx["op_a2"], role="viewer")
    sid = sdb.create_sheet(title="Foglio progetto", project_id=pid,
                           tenant_id=ctx["ta"], created_by_user_id=ctx["op_a"])
    sheet = sdb.get_sheet(sid, tenant_id=ctx["ta"], current_user_id=ctx["op_a"])
    project = fdb.get_project(pid, tenant_id=ctx["ta"], current_user_id=ctx["op_a2"])
    viewer = _user(ctx["op_a2"], "tenant_user", ctx["ta"])
    owner = _user(ctx["op_a"], "tenant_user", ctx["ta"])
    # viewer del progetto: apre ma NON modifica le celle
    assert acl.can_open_sheet(sheet, project, viewer) is True
    assert acl.can_edit_sheet_cells(sheet, project, viewer) is False
    # owner del progetto: modifica
    assert acl.can_edit_sheet_cells(sheet, project, owner) is True
