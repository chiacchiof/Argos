"""Test UI/route portal_fill (Fase 2).

Copre, via authed_client (super-admin) e unit:
- MODE_SCHEMAS espone portal_fill con i 3 campi in allowed_fields.
- TaskIn accetta agent_mode='portal_fill' e coerce gli id ("" → None).
- Route macro CRUD: lista, new form, create, edit, update, delete.
- Route run: crea un task portal_fill e un job.
- Form task POST con agent_mode=portal_fill crea il task coi campi giusti.
- recorder: stato in-memory (is_active/stop) senza aprire browser; overlay JS ben formato.

I flussi che aprono un browser headed (login/record) NON sono testati qui: richiedono
display interattivo. Si testa la logica attorno (route che li lanciano rispondono 303).
"""
from __future__ import annotations

import pytest

from app import db
from app.agent.mode_schemas import MODE_SCHEMAS
from app.agent.portal import recorder
from app.models import TaskIn


# ---------------------------------------------------------------------------
# Unit: schema + modello
# ---------------------------------------------------------------------------

def test_portal_fill_in_mode_schemas():
    s = MODE_SCHEMAS["portal_fill"]
    assert s.family == "outreach"
    assert s.runner_status == "beta"
    af = s.allowed_fields()
    for f in ("portal_macro_id", "portal_sheet_id", "portal_auto_submit"):
        assert f in af


def test_taskin_accepts_portal_fill_and_coerces_ids():
    t = TaskIn(name="x", objective="y", agent_mode="portal_fill",
               portal_macro_id="", portal_sheet_id="5", portal_auto_submit=True)
    assert t.agent_mode == "portal_fill"
    assert t.portal_macro_id is None       # "" → None
    assert t.portal_sheet_id == 5
    assert t.portal_auto_submit is True


def test_create_task_portal_columns_via_model():
    """create_task da TaskIn.model_dump() preserva i campi portal."""
    t = TaskIn(name="pf", objective="o", agent_mode="portal_fill",
               portal_macro_id="3", portal_sheet_id="7", portal_auto_submit=True)
    tid = db.create_task(t.model_dump())
    row = db.get_task(tid)
    assert row["agent_mode"] == "portal_fill"
    assert row["portal_macro_id"] == 3
    assert row["portal_sheet_id"] == 7
    assert row["portal_auto_submit"] == 1


# ---------------------------------------------------------------------------
# Route macro CRUD (authed_client = super-admin)
# ---------------------------------------------------------------------------

def test_portal_macros_list_page(authed_client):
    r = authed_client.get("/strumenti/portal-macros")
    assert r.status_code == 200
    assert "Compilazione portali" in r.text


def test_portal_macro_new_form(authed_client):
    r = authed_client.get("/strumenti/portal-macros/new")
    assert r.status_code == 200


def test_portal_macro_create_edit_delete(authed_client):
    # create
    r = authed_client.post("/strumenti/portal-macros", data={
        "name": "Portale Test", "portal_url": "https://p.example/new",
        "submit_selector": "#salva", "auto_submit": "1",
        "fields_json": '[{"selector": "#nome", "column_name": "Nome"}]',
    }, follow_redirects=False)
    assert r.status_code == 303
    loc = r.headers["location"]
    macro_id = int(loc.split("/portal-macros/")[1].split("/")[0])

    m = db.get_portal_macro(macro_id)
    assert m["name"] == "Portale Test"
    assert m["auto_submit"] == 1

    # edit
    r = authed_client.post(f"/strumenti/portal-macros/{macro_id}/edit", data={
        "name": "Portale Test 2", "portal_url": "https://p.example/new",
        "submit_selector": "", "auto_submit": "",
        "fields_json": '[{"selector": "#nome2", "column_name": "Nome"}]',
    }, follow_redirects=False)
    assert r.status_code == 303
    m2 = db.get_portal_macro(macro_id)
    assert m2["name"] == "Portale Test 2"
    assert m2["auto_submit"] == 0

    # delete
    r = authed_client.post(f"/strumenti/portal-macros/{macro_id}/delete", follow_redirects=False)
    assert r.status_code == 303
    assert db.get_portal_macro(macro_id) is None


def test_portal_macro_run_creates_task_and_job(authed_client):
    # macro con un campo
    mid = db.create_portal_macro({
        "name": "M run", "portal_url": "https://p.example/f",
        "fields": [{"selector": "#a", "column_name": "Nome"}],
    })
    # foglio sorgente (serve un tenant: create_sheet rifiuta super_admin)
    from app import db_cloud
    from app.fascicoli import sheets_db
    tid_tenant = db_cloud.create_tenant("PF-run", "pf-run")
    sid = sheets_db.create_sheet(title="Dati", tenant_id=tid_tenant)

    r = authed_client.post(f"/strumenti/portal-macros/{mid}/run", data={
        "portal_sheet_id": str(sid), "portal_auto_submit": "",
    }, follow_redirects=False)
    assert r.status_code == 303
    # il redirect punta a /jobs/{job_id}
    assert "/jobs/" in r.headers["location"]

    # è stato creato un task portal_fill collegato a macro+foglio
    tasks = [t for t in db.list_tasks() if t.get("agent_mode") == "portal_fill"]
    assert any(t["portal_macro_id"] == mid and t["portal_sheet_id"] == sid for t in tasks)


def test_run_inherits_auto_submit_from_macro(authed_client):
    """auto_submit unico interruttore: una macro con auto_submit=ON crea task con
    portal_auto_submit=True anche se la checkbox del run non è spuntata (eredità)."""
    from app import db_cloud
    from app.fascicoli import sheets_db
    tid = db_cloud.create_tenant("PF-inh", "pf-inh")
    sid = sheets_db.create_sheet(title="D", tenant_id=tid)
    mid = db.create_portal_macro({
        "name": "MAuto", "portal_url": "u", "auto_submit": True,
        "fields": [{"selector": "#a", "column_name": "Nome"}],
    })
    r = authed_client.post(f"/strumenti/portal-macros/{mid}/run", data={
        "portal_sheet_id": str(sid), "portal_auto_submit": "",  # NON spuntato nel run
    }, follow_redirects=False)
    assert r.status_code == 303
    task = next(t for t in db.list_tasks()
                if t.get("agent_mode") == "portal_fill" and t["portal_macro_id"] == mid)
    assert task["portal_auto_submit"] == 1  # ereditato dalla macro


def test_portal_macro_run_without_sheet_redirects_error(authed_client):
    mid = db.create_portal_macro({
        "name": "M2", "portal_url": "u",
        "fields": [{"selector": "#a", "column_name": "Nome"}],
    })
    r = authed_client.post(f"/strumenti/portal-macros/{mid}/run", data={
        "portal_sheet_id": "", "portal_auto_submit": "",
    }, follow_redirects=False)
    assert r.status_code == 303
    assert "error" in r.headers["location"].lower()


# ---------------------------------------------------------------------------
# Form task POST con agent_mode=portal_fill
# ---------------------------------------------------------------------------

def test_task_form_post_portal_fill(authed_client):
    mid = db.create_portal_macro({"name": "MM", "portal_url": "u", "fields": [{"selector": "#x"}]})
    from app import db_cloud
    from app.fascicoli import sheets_db
    tid_tenant = db_cloud.create_tenant("PF-form", "pf-form")
    sid = sheets_db.create_sheet(title="S", tenant_id=tid_tenant)
    r = authed_client.post("/tasks", data={
        "name": "Task PF", "objective": "compila",
        "agent_mode": "portal_fill",
        "portal_macro_id": str(mid), "portal_sheet_id": str(sid),
        "portal_auto_submit": "1",
    }, follow_redirects=False)
    assert r.status_code == 303  # redirect a /tasks/{id}
    tid = int(r.headers["location"].rstrip("/").split("/")[-1])
    t = db.get_task(tid)
    assert t["agent_mode"] == "portal_fill"
    assert t["portal_macro_id"] == mid
    assert t["portal_sheet_id"] == sid
    assert t["portal_auto_submit"] == 1


# ---------------------------------------------------------------------------
# Recorder: stato in-memory + overlay JS (niente browser)
# ---------------------------------------------------------------------------

def test_recorder_state_default_inactive():
    assert recorder.is_active(999999) is False
    assert recorder.session_mode(999999) is None
    assert recorder.stop(999999) is False  # niente da fermare
    assert recorder.captured_fields(999999) == []


def test_overlay_js_is_wellformed():
    js = recorder._OVERLAY_JS
    assert "argosCaptureField" in js
    assert "robustSelector" in js
    assert "data-testid" in js  # gerarchia di robustezza
    assert "isSubmit" in js     # cattura anche il bottone Invia


def test_overlay_js_captures_submit_button():
    """Il recorder marca i bottoni di invio con action 'submit' (no JSON a mano)."""
    js = recorder._OVERLAY_JS
    assert "'submit'" in js  # action submit per i bottoni
    assert "__argos-hi-submit" in js  # highlight verde dedicato


def test_sheet_columns_endpoint(authed_client):
    """L'endpoint colonne ritorna i nomi della riga header del foglio."""
    from app import db_cloud
    from app.fascicoli import sheets_db
    tid = db_cloud.create_tenant("PF-cols", "pf-cols")
    sid = sheets_db.create_sheet(title="C", tenant_id=tid)
    sheets_db.apply_cell_patch(sid, [
        {"row": 0, "col": 0, "value": "Nome"},
        {"row": 0, "col": 1, "value": "Email"},
    ], tenant_id=tid, actor_user_id=None)
    mid = db.create_portal_macro({"name": "MC", "portal_url": "u", "fields": [{"selector": "#x"}]})
    r = authed_client.get(f"/strumenti/portal-macros/{mid}/columns?sheet_id={sid}")
    assert r.status_code == 200
    assert r.json()["columns"] == ["Nome", "Email"]


def test_sheet_column_names_helper():
    """sheet_column_names legge solo la riga header, in ordine di colonna."""
    from app import db_cloud
    from app.fascicoli import sheets_db
    tid = db_cloud.create_tenant("PF-h", "pf-h")
    sid = sheets_db.create_sheet(title="H", tenant_id=tid)
    sheets_db.apply_cell_patch(sid, [
        {"row": 0, "col": 0, "value": "A"},
        {"row": 0, "col": 2, "value": "C"},  # colonna 1 vuota → saltata
        {"row": 1, "col": 0, "value": "dato"},  # riga dati → ignorata
    ], tenant_id=tid, actor_user_id=None)
    assert sheets_db.sheet_column_names(sid, tenant_id=tid) == ["A", "C"]
