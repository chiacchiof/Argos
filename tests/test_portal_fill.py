"""Test portal_fill (compilazione assistita portali) — Fase 1 (motore).

Copre:
- sheets_db.rows_as_dicts: ricostruzione foglio → righe-dict, header row 0,
  righe vuote saltate, isolamento tenant.
- db CRUD portal_macros: create/get/list/update tenant-scoped + auto-riparazione
  (update_portal_macro con nuovi fields) + portal_fill_log.
- form_fill helper puri: MacroField (value_for/from_dict/to_dict), _parse_index,
  _css_for_dom_field.
- form_fill.locate + fill_form su DOM statico via Playwright (skippato se il
  browser Chromium non e' installato), con llm_remap mockato per l'auto-riparazione.
"""
from __future__ import annotations

import asyncio

import pytest

from app import db, db_cloud
from app.auth import hash_password
from app.fascicoli import sheets_db as sdb
from app.agent.portal import form_fill as ff
from app.agent.portal import recorder


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def two_tenants():
    ta = db_cloud.create_tenant("TenantA", "pf-tenant-a")
    tb = db_cloud.create_tenant("TenantB", "pf-tenant-b")
    op_a = db_cloud.create_user(
        tenant_id=ta, email="pf-a@a.it", password_hash=hash_password("x"),
        role="tenant_user",
    )
    op_b = db_cloud.create_user(
        tenant_id=tb, email="pf-b@b.it", password_hash=hash_password("x"),
        role="tenant_user",
    )
    return {"ta": ta, "tb": tb, "op_a": op_a, "op_b": op_b}


def _seed_sheet(tenant_id: int, user_id: int) -> int:
    """Foglio: header (riga 0) = Nome | Email; due righe dati + una vuota in mezzo."""
    sid = sdb.create_sheet(title="Anagrafiche", tenant_id=tenant_id, created_by_user_id=user_id)
    sdb.apply_cell_patch(sid, [
        {"row": 0, "col": 0, "value": "Nome"},
        {"row": 0, "col": 1, "value": "Email"},
        {"row": 1, "col": 0, "value": "Acme SRL"},
        {"row": 1, "col": 1, "value": "info@acme.it"},
        # riga 2 vuota (deve essere saltata)
        {"row": 3, "col": 0, "value": "Beta Spa"},
        {"row": 3, "col": 1, "value": "ciao@beta.it"},
    ], tenant_id=tenant_id, actor_user_id=user_id)
    return sid


# ---------------------------------------------------------------------------
# rows_as_dicts
# ---------------------------------------------------------------------------

def test_rows_as_dicts_basic(two_tenants):
    ctx = two_tenants
    sid = _seed_sheet(ctx["ta"], ctx["op_a"])
    rows = sdb.rows_as_dicts(sid, tenant_id=ctx["ta"])
    assert rows == [
        {"Nome": "Acme SRL", "Email": "info@acme.it"},
        {"Nome": "Beta Spa", "Email": "ciao@beta.it"},
    ]


def test_rows_as_dicts_tenant_isolation(two_tenants):
    """Un altro tenant non deve leggere le righe del foglio (anti-IDOR)."""
    ctx = two_tenants
    sid = _seed_sheet(ctx["ta"], ctx["op_a"])
    assert sdb.rows_as_dicts(sid, tenant_id=ctx["tb"]) == []


def test_rows_as_dicts_empty_or_no_header(two_tenants):
    ctx = two_tenants
    sid = sdb.create_sheet(title="Vuoto", tenant_id=ctx["ta"], created_by_user_id=ctx["op_a"])
    assert sdb.rows_as_dicts(sid, tenant_id=ctx["ta"]) == []


# ---------------------------------------------------------------------------
# CRUD portal_macros + log
# ---------------------------------------------------------------------------

def test_portal_macro_crud_and_tenant_scope(two_tenants):
    ctx = two_tenants
    mid = db.create_portal_macro(
        {
            "name": "Portale X",
            "portal_url": "https://portale.example/new",
            "fields": [{"selector": "#nome", "semantic_label": "Nome", "column_name": "Nome"}],
            "auto_submit": True,
            "submit_selector": "#salva",
        },
        tenant_id=ctx["ta"], created_by_user_id=ctx["op_a"],
    )
    m = db.get_portal_macro(mid, tenant_id=ctx["ta"])
    assert m is not None
    assert m["name"] == "Portale X"
    assert m["auto_submit"] == 1
    # tenant isolation
    assert db.get_portal_macro(mid, tenant_id=ctx["tb"]) is None
    assert [r["id"] for r in db.list_portal_macros(tenant_id=ctx["ta"])] == [mid]
    assert db.list_portal_macros(tenant_id=ctx["tb"]) == []


def test_portal_macro_self_heal_update(two_tenants):
    """update_portal_macro con nuovi fields = auto-riparazione persistita."""
    ctx = two_tenants
    mid = db.create_portal_macro(
        {"name": "M", "portal_url": "u", "fields": [{"selector": "#old", "column_name": "Nome"}]},
        tenant_id=ctx["ta"], created_by_user_id=ctx["op_a"],
    )
    db.update_portal_macro(
        mid, {"fields": [{"selector": "#new", "column_name": "Nome"}]},
        tenant_id=ctx["ta"],
    )
    import json
    m = db.get_portal_macro(mid, tenant_id=ctx["ta"])
    fields = json.loads(m["fields_json"])
    assert fields[0]["selector"] == "#new"


def test_portal_fill_log_roundtrip(two_tenants):
    ctx = two_tenants
    db.insert_portal_fill_log(
        job_id=999, macro_id=None, sheet_id=None, row_idx=0,
        status="ok", detail="compilato", tenant_id=ctx["ta"],
    )
    logs = db.list_portal_fill_log(999, tenant_id=ctx["ta"])
    assert len(logs) == 1 and logs[0]["status"] == "ok"
    # tenant isolation
    assert db.list_portal_fill_log(999, tenant_id=ctx["tb"]) == []


def test_task_portal_columns_roundtrip(two_tenants):
    """create_task + get_task preservano i campi portal_*."""
    ctx = two_tenants
    tid = db.create_task(
        {
            "name": "Compila portale", "objective": "x", "agent_mode": "portal_fill",
            "portal_macro_id": 7, "portal_sheet_id": 11, "portal_auto_submit": True,
        },
        tenant_id=ctx["ta"], created_by_user_id=ctx["op_a"],
    )
    t = db.get_task(tid, tenant_id=ctx["ta"])
    assert t["agent_mode"] == "portal_fill"
    assert t["portal_macro_id"] == 7
    assert t["portal_sheet_id"] == 11
    assert t["portal_auto_submit"] == 1


# ---------------------------------------------------------------------------
# Helper puri di form_fill (niente browser)
# ---------------------------------------------------------------------------

def test_macrofield_value_for():
    col = ff.MacroField(selector="#a", column_name="Nome", source="column")
    const = ff.MacroField(selector="#b", source="const", const_value="IT")
    row = {"Nome": "Acme", "Email": "x@y.it"}
    assert col.value_for(row) == "Acme"
    assert const.value_for(row) == "IT"
    assert ff.MacroField(selector="#c", column_name="Mancante").value_for(row) == ""


def test_macrofield_roundtrip_dict():
    d = {"selector": "#x", "semantic_label": "Partita IVA", "source": "column",
         "column_name": "PIVA", "strategy": "css", "action": "fill", "phase": "activity"}
    f = ff.MacroField.from_dict(d)
    assert f.to_dict() == {**d, "const_value": ""}


def test_macrofield_legacy_dict_defaults_to_activity():
    """Uno step legacy senza 'phase' deve risultare in fase 'activity'."""
    f = ff.MacroField.from_dict({"selector": "#x", "action": "fill"})
    assert f.phase == "activity"


def test_parse_index_tolerant():
    assert ff._parse_index('{"index": 3}') == 3
    assert ff._parse_index('<think>hmm</think>{"index": 0}') == 0
    assert ff._parse_index('blah "index": 5 blah') == 5
    assert ff._parse_index('{"index": null}') is None
    assert ff._parse_index('') is None


def test_css_for_dom_field():
    assert ff._css_for_dom_field({"id": "nome"}) == "#nome"
    assert ff._css_for_dom_field({"name": "email", "tag": "input"}) == 'input[name="email"]'
    assert ff._css_for_dom_field({"placeholder": "x"}) is None


# ---------------------------------------------------------------------------
# locate + fill_form su DOM statico (richiede il browser Chromium)
# ---------------------------------------------------------------------------

_FORM_HTML = """
<!doctype html><html><body>
<form>
  <label for="nome">Nome</label>
  <input id="nome" name="nome" type="text">
  <label for="email">Email</label>
  <input id="email" name="email" type="email">
  <button id="salva" type="button">Salva</button>
</form>
</body></html>
"""


async def _with_page(html: str, fn):
    try:
        from patchright.async_api import async_playwright as _ap
    except ImportError:
        from playwright.async_api import async_playwright as _ap
    p = await _ap().start()
    try:
        browser = await p.chromium.launch(headless=True)
    except Exception as e:  # browser binary non installato
        await p.stop()
        pytest.skip(f"Chromium non disponibile: {e}")
    try:
        page = await browser.new_page()
        await page.set_content(html)
        return await fn(page)
    finally:
        await browser.close()
        await p.stop()


def test_fill_form_fills_known_selectors():
    async def _run(page):
        fields = [
            ff.MacroField(selector="#nome", semantic_label="Nome", column_name="Nome"),
            ff.MacroField(selector="#email", semantic_label="Email", column_name="Email"),
        ]
        row = {"Nome": "Acme SRL", "Email": "info@acme.it"}
        res = await ff.fill_form(page, fields, row, llm_cfg=ff.LLMConfig(enabled=False))
        assert res.ok and not res.macro_updated
        assert await page.locator("#nome").input_value() == "Acme SRL"
        assert await page.locator("#email").input_value() == "info@acme.it"
    asyncio.run(_with_page(_FORM_HTML, _run))


def test_fill_form_self_heals_via_llm(monkeypatch):
    """Selettore stantio (#vecchio) → llm_remap ritorna #nome → campo riempito +
    macro_updated True (l'auto-riparazione scatta)."""
    async def fake_remap(dom_fields, semantic_label, *, cfg):
        return "#nome"
    monkeypatch.setattr(ff, "llm_remap", fake_remap)

    async def _run(page):
        fields = [ff.MacroField(selector="#vecchio", semantic_label="Nome", column_name="Nome")]
        row = {"Nome": "Acme SRL"}
        res = await ff.fill_form(page, fields, row, llm_cfg=ff.LLMConfig(enabled=True))
        assert res.ok
        assert res.macro_updated
        assert fields[0].selector == "#nome"  # selettore ri-mappato in place
        assert await page.locator("#nome").input_value() == "Acme SRL"
    asyncio.run(_with_page(_FORM_HTML, _run))


def test_fill_form_detects_challenge():
    challenge_html = "<html><body><div class='g-recaptcha'>verifica di non essere un robot</div></body></html>"

    async def _run(page):
        fields = [ff.MacroField(selector="#nome", column_name="Nome")]
        res = await ff.fill_form(page, fields, {"Nome": "x"}, llm_cfg=ff.LLMConfig(enabled=False))
        assert res.challenged
        assert not res.ok
    asyncio.run(_with_page(challenge_html, _run))


# ---------------------------------------------------------------------------
# Macro a fasi: _steps_by_phase (retro-compat) + run_steps (azioni miste)
# ---------------------------------------------------------------------------

def test_steps_by_phase_legacy_compat():
    """Macro legacy (fields senza phase) → tutto in 'activity'; submit_selector →
    step submit finale; auto_submit OFF rimuove gli step submit."""
    from app.agent.runner_portal_fill import _steps_by_phase
    legacy = [ff.MacroField(selector="#a", column_name="Nome"),
              ff.MacroField(selector="#b", column_name="Email")]
    on = _steps_by_phase(legacy, auto_submit=True, submit_selector="#invia")
    assert [s.action for s in on["activity"]] == ["fill", "fill", "submit"]
    assert on["warmup"] == [] and on["return"] == [] and on["closing"] == []
    off = _steps_by_phase(legacy, auto_submit=False, submit_selector="#invia")
    assert [s.action for s in off["activity"]] == ["fill", "fill"]  # submit rimosso


def test_steps_by_phase_keeps_nav_submit_off():
    """Con auto_submit OFF, i submit di NAVIGAZIONE (warmup/return/closing) NON
    devono essere rimossi — solo l'invio della fase attività. Regressione del bug
    'compila solo la seconda riga' (2026-05-31): il 'Nuovo' del warmup, registrato
    come submit, veniva tolto e il form non si apriva per la prima riga."""
    from app.agent.runner_portal_fill import _steps_by_phase
    steps = [
        ff.MacroField(selector="#nuovo", action="submit", phase="warmup"),
        ff.MacroField(selector="#nome", action="fill", column_name="Nome", phase="activity"),
        ff.MacroField(selector="#invia", action="submit", phase="activity"),
        ff.MacroField(selector="#nuovo2", action="submit", phase="return"),
    ]
    off = _steps_by_phase(steps, auto_submit=False, submit_selector="")
    assert [s.action for s in off["warmup"]] == ["submit"]   # navigazione preservata
    assert [s.action for s in off["return"]] == ["submit"]   # navigazione preservata
    assert [s.action for s in off["activity"]] == ["fill"]   # solo l'invio attività rimosso
    on = _steps_by_phase(steps, auto_submit=True, submit_selector="")
    assert [s.action for s in on["activity"]] == ["fill", "submit"]  # con ON resta


def test_steps_by_phase_groups_mixed():
    from app.agent.runner_portal_fill import _steps_by_phase
    mixed = [
        ff.MacroField(selector="#menu", action="click", phase="warmup"),
        ff.MacroField(selector="#nome", column_name="Nome", phase="activity"),
        ff.MacroField(selector="#nuovo", action="click", phase="return"),
        ff.MacroField(selector="#esci", action="click", phase="closing"),
    ]
    bp = _steps_by_phase(mixed, auto_submit=True, submit_selector="")
    assert len(bp["warmup"]) == 1 and len(bp["activity"]) == 1
    assert len(bp["return"]) == 1 and len(bp["closing"]) == 1


def test_macrofield_value_for_none_row():
    """value_for tollera row=None (fasi warmup/closing): column → '', const → valore."""
    col = ff.MacroField(selector="#a", column_name="Nome", source="column")
    const = ff.MacroField(selector="#b", source="const", const_value="IT")
    assert col.value_for(None) == ""
    assert const.value_for(None) == "IT"


# HTML con un form + un "link" che mostra/nasconde una seconda sezione (navigazione).
_NAV_HTML = """
<!doctype html><html><body>
<div id="page1">
  <a id="go" href="#">Vai al form</a>
</div>
<div id="page2" style="display:none">
  <input id="nome" name="nome" type="text">
  <button id="save" type="button">Salva</button>
</div>
<script>
  document.getElementById('go').addEventListener('click', e => {
    e.preventDefault();
    document.getElementById('page1').style.display='none';
    document.getElementById('page2').style.display='block';
  });
</script>
</body></html>
"""


def test_run_steps_mixed_click_and_fill():
    """run_steps esegue in ordine: click di navigazione (mostra il form) poi fill.
    Il fill mappato a colonna prende il valore della riga."""
    async def _run(page):
        steps = [
            ff.MacroField(selector="#go", action="click", phase="activity"),
            ff.MacroField(selector="#nome", action="fill", column_name="Nome", phase="activity"),
        ]
        row = {"Nome": "Beta Spa"}
        res = await ff.run_steps(page, steps, row, llm_cfg=ff.LLMConfig(enabled=False))
        assert res.ok, [s.detail for s in res.steps if not s.ok]
        assert await page.locator("#nome").input_value() == "Beta Spa"
    asyncio.run(_with_page(_NAV_HTML, _run))


def test_run_steps_const_value():
    """Uno step fill con source=const usa il valore fisso, anche con row=None."""
    async def _run(page):
        steps = [
            ff.MacroField(selector="#go", action="click", phase="warmup"),
            ff.MacroField(selector="#nome", action="fill", source="const",
                          const_value="FISSO", phase="warmup"),
        ]
        res = await ff.run_steps(page, steps, None, llm_cfg=ff.LLMConfig(enabled=False))
        assert res.ok
        assert await page.locator("#nome").input_value() == "FISSO"
    asyncio.run(_with_page(_NAV_HTML, _run))


def test_overlay_js_has_phase_bar():
    js = recorder._OVERLAY_JS
    assert "__argosPhase" in js
    for ph in ("warmup", "activity", "return", "closing"):
        assert ph in js
    assert "isNav" in js  # cattura click di navigazione


# HTML che riproduce il bug: un <button onclick> FUORI da un form (come "Nuovo
# fornitore") deve essere navigazione, non submit — e il recorder passivo deve
# lasciarlo procedere (aprire il form) registrandolo come click.
_NUOVO_HTML = """
<!doctype html><html><body>
<div style="height:160px"></div>
<button id="nuovo" onclick="document.getElementById('f').style.display='block'">Nuovo fornitore</button>
<div id="f" style="display:none"><input id="nome"><button id="invia" type="submit">Salva</button></div>
</body></html>
"""


def test_recorder_passive_nav_button_opens_and_records():
    """Il button di navigazione apre il form (recorder passivo) ed è registrato
    come action 'click' nella fase corrente. Regressione del bug 2026-05-31."""
    async def _run():
        try:
            from patchright.async_api import async_playwright as _ap
        except ImportError:
            from playwright.async_api import async_playwright as _ap
        captured = []
        p = await _ap().start()
        try:
            browser = await p.chromium.launch(headless=True)
        except Exception as e:
            await p.stop()
            pytest.skip(f"Chromium non disponibile: {e}")
        try:
            ctx = await browser.new_context()

            async def _binding(source, payload):
                captured.append(payload)
                return "ok"

            await ctx.expose_binding("argosCaptureField", _binding)
            page = await ctx.new_page()
            await page.set_content(_NUOVO_HTML)
            await page.evaluate(f"({recorder._OVERLAY_JS})()")
            await page.click("#nuovo")
            await page.wait_for_timeout(250)
            assert await page.locator("#f").is_visible()  # il form si è aperto
            assert captured and captured[0]["action"] == "click"
            assert "Nuovo fornitore" in captured[0]["label"]
        finally:
            await browser.close()
            await p.stop()
    asyncio.run(_run())
