"""Test isolamento multi-tenant: Ferdinando (tenant A) e Mario (tenant B) vedono
solo i propri dati; il super-admin vede tutto.

Verifica che il ContextVar tenant_id settato dal middleware HTTP filtri
correttamente le query in db.tasks/jobs/workflows/assets/contacts/
orchestrator_messages.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import db, db_cloud
from app.auth import hash_password


@pytest.fixture
def populated_db():
    """Crea 2 tenant (A, B), 1 user per tenant, e 1 task per tenant + 1 task super-admin."""
    tenant_a = db_cloud.create_tenant("TenantA", "tenant-a")
    tenant_b = db_cloud.create_tenant("TenantB", "tenant-b")

    super_id = db_cloud.create_user(
        tenant_id=None, email="super", password_hash=hash_password("pwd-super"),
        role="super_admin",
    )
    alice_id = db_cloud.create_user(
        tenant_id=tenant_a, email="alice@a.it", password_hash=hash_password("pwd-alice"),
        role="tenant_architect",
    )
    bob_id = db_cloud.create_user(
        tenant_id=tenant_b, email="bob@b.it", password_hash=hash_password("pwd-bob"),
        role="tenant_architect",
    )

    # Task per ogni utente (via API db con contesto esplicito)
    task_alice = db.create_task(
        {"name": "Task Alice", "objective": "x"},
        tenant_id=tenant_a, created_by_user_id=alice_id,
    )
    task_bob = db.create_task(
        {"name": "Task Bob", "objective": "x"},
        tenant_id=tenant_b, created_by_user_id=bob_id,
    )
    task_super = db.create_task(
        {"name": "Task Super", "objective": "x"},
        tenant_id=None, created_by_user_id=super_id,
    )

    # Asset per Alice e Bob
    asset_alice = db.upsert_asset(
        {"asset_type": "test", "title": "Asset Alice", "raw_json": "{}",
         "source_url": "https://a.test/alice"},
        tenant_id=tenant_a, created_by_user_id=alice_id,
    )
    asset_bob = db.upsert_asset(
        {"asset_type": "test", "title": "Asset Bob", "raw_json": "{}",
         "source_url": "https://b.test/bob"},
        tenant_id=tenant_b, created_by_user_id=bob_id,
    )

    return {
        "tenant_a": tenant_a, "tenant_b": tenant_b,
        "super_id": super_id, "alice_id": alice_id, "bob_id": bob_id,
        "task_alice": task_alice, "task_bob": task_bob, "task_super": task_super,
        "asset_alice": asset_alice, "asset_bob": asset_bob,
    }


# ---------------------------------------------------------------------------
# Tasks
# ---------------------------------------------------------------------------

def test_alice_lists_only_her_tasks(populated_db):
    tasks = db.list_tasks(tenant_id=populated_db["tenant_a"])
    names = {t["name"] for t in tasks}
    assert names == {"Task Alice"}


def test_bob_lists_only_his_tasks(populated_db):
    tasks = db.list_tasks(tenant_id=populated_db["tenant_b"])
    names = {t["name"] for t in tasks}
    assert names == {"Task Bob"}


def test_super_admin_lists_all_tasks(populated_db):
    # tenant_id=None → no filter
    tasks = db.list_tasks(tenant_id=None)
    names = {t["name"] for t in tasks}
    assert names == {"Task Alice", "Task Bob", "Task Super"}


def test_alice_cannot_get_bobs_task_by_id(populated_db):
    # Anti-IDOR: get con tenant_id di Alice su task di Bob → None
    t = db.get_task(populated_db["task_bob"], tenant_id=populated_db["tenant_a"])
    assert t is None


def test_alice_cannot_delete_bobs_task(populated_db):
    db.delete_task(populated_db["task_bob"], tenant_id=populated_db["tenant_a"])
    # Bob può ancora vederlo (delete non ha effetto)
    t = db.get_task(populated_db["task_bob"], tenant_id=populated_db["tenant_b"])
    assert t is not None


def test_super_admin_can_get_any_task(populated_db):
    t = db.get_task(populated_db["task_bob"], tenant_id=None)
    assert t is not None and t["name"] == "Task Bob"


# ---------------------------------------------------------------------------
# Assets
# ---------------------------------------------------------------------------

def test_alice_lists_only_her_assets(populated_db):
    assets = db.list_assets(tenant_id=populated_db["tenant_a"])
    titles = {a["title"] for a in assets}
    assert titles == {"Asset Alice"}


def test_bob_lists_only_his_assets(populated_db):
    assets = db.list_assets(tenant_id=populated_db["tenant_b"])
    titles = {a["title"] for a in assets}
    assert titles == {"Asset Bob"}


def test_alice_cannot_get_bobs_asset(populated_db):
    a = db.get_asset(populated_db["asset_bob"], tenant_id=populated_db["tenant_a"])
    assert a is None


def test_count_assets_tenant_filtered(populated_db):
    assert db.count_assets(tenant_id=populated_db["tenant_a"]) == 1
    assert db.count_assets(tenant_id=populated_db["tenant_b"]) == 1
    assert db.count_assets(tenant_id=None) == 2  # super-admin


# ---------------------------------------------------------------------------
# ContextVar integration: middleware setta tenant_id automaticamente
# ---------------------------------------------------------------------------

def test_contextvar_drives_filtering(populated_db, monkeypatch, tmp_path):
    """Senza passare tenant_id esplicito, le funzioni leggono dal ContextVar."""
    from app import config, storage
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "RESULTS_DIR", tmp_path / "results")
    monkeypatch.setattr(storage, "RESULTS_DIR", tmp_path / "results")

    # Setta ContextVar a Alice → list_tasks() (senza param) deve filtrare per Alice
    token = db.set_current_tenant(populated_db["tenant_a"])
    try:
        tasks = db.list_tasks()  # NO tenant_id esplicito
        names = {t["name"] for t in tasks}
        assert names == {"Task Alice"}
    finally:
        db.reset_current_tenant(token)


def test_contextvar_resets_correctly(populated_db):
    """Dopo reset, list_tasks() torna a no-filter (super-admin)."""
    token = db.set_current_tenant(populated_db["tenant_a"])
    db.reset_current_tenant(token)
    tasks = db.list_tasks()  # context dopo reset = None
    assert len(tasks) == 3  # tutti


# ---------------------------------------------------------------------------
# End-to-end con TestClient: la route automaticamente filtra via middleware
# ---------------------------------------------------------------------------

def test_end_to_end_alice_sees_only_her_task(populated_db, tmp_path, monkeypatch):
    """Alice logga via HTTP → GET / → vede solo "Task Alice", NON "Task Bob" né "Task Super"."""
    from app import config, storage
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "RESULTS_DIR", tmp_path / "results")
    monkeypatch.setattr(storage, "RESULTS_DIR", tmp_path / "results")

    from app.main import app
    with TestClient(app) as client:
        r = client.post(
            "/login",
            data={"email": "alice@a.it", "password": "pwd-alice", "next": "/"},
            follow_redirects=False,
        )
        assert r.status_code == 303
        r = client.get("/")
        assert r.status_code == 200
        assert "Task Alice" in r.text
        assert "Task Bob" not in r.text
        assert "Task Super" not in r.text


def test_end_to_end_super_admin_sees_everything(populated_db, tmp_path, monkeypatch):
    from app import config, storage
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "RESULTS_DIR", tmp_path / "results")
    monkeypatch.setattr(storage, "RESULTS_DIR", tmp_path / "results")

    from app.main import app
    with TestClient(app) as client:
        r = client.post(
            "/login",
            data={"email": "super", "password": "pwd-super", "next": "/"},
            follow_redirects=False,
        )
        assert r.status_code == 303
        r = client.get("/")
        assert r.status_code == 200
        assert "Task Alice" in r.text
        assert "Task Bob" in r.text
        assert "Task Super" in r.text


# ---------------------------------------------------------------------------
# Social accounts + WhatsApp API config (Step D₄)
# ---------------------------------------------------------------------------

@pytest.fixture
def social_accounts_setup(populated_db):
    """Crea 1 social account per tenant_a e 1 per tenant_b."""
    a_id = db.create_social_account(
        {
            "uuid": "uuid-alice", "platform": "instagram", "username": "alice_ig",
            "encrypted_password": b"\x01\x02\x03",
        },
        tenant_id=populated_db["tenant_a"],
        created_by_user_id=populated_db["alice_id"],
    )
    b_id = db.create_social_account(
        {
            "uuid": "uuid-bob", "platform": "tiktok", "username": "bob_tt",
            "encrypted_password": b"\x04\x05\x06",
        },
        tenant_id=populated_db["tenant_b"],
        created_by_user_id=populated_db["bob_id"],
    )
    return {**populated_db, "sa_alice": a_id, "sa_bob": b_id}


def test_alice_lists_only_her_social_accounts(social_accounts_setup):
    sa = db.list_social_accounts(tenant_id=social_accounts_setup["tenant_a"])
    assert len(sa) == 1
    assert sa[0]["username"] == "alice_ig"


def test_bob_lists_only_his_social_accounts(social_accounts_setup):
    sa = db.list_social_accounts(tenant_id=social_accounts_setup["tenant_b"])
    assert len(sa) == 1
    assert sa[0]["username"] == "bob_tt"


def test_super_admin_lists_all_social_accounts(social_accounts_setup):
    sa = db.list_social_accounts(tenant_id=None)
    assert len(sa) == 2


def test_alice_cannot_get_bobs_social_account(social_accounts_setup):
    sa = db.get_social_account(
        social_accounts_setup["sa_bob"], tenant_id=social_accounts_setup["tenant_a"]
    )
    assert sa is None


def test_alice_cannot_delete_bobs_social_account(social_accounts_setup):
    db.delete_social_account(
        social_accounts_setup["sa_bob"], tenant_id=social_accounts_setup["tenant_a"]
    )
    sa = db.get_social_account(
        social_accounts_setup["sa_bob"], tenant_id=social_accounts_setup["tenant_b"]
    )
    assert sa is not None


def test_job_runner_propagates_tenant_id_in_writes(populated_db):
    """Simula il comportamento del job runner (`_run_job`): set_current_tenant
    dal task, poi le scritture db.* eredita automaticamente il tenant via
    ContextVar — senza dover passare tenant_id esplicito.

    Questo test cattura il bug pre-fix dove i runner agentici (lanciati da
    APScheduler cron o workflow downstream, fuori dal context HTTP) scrivevano
    su DB con tenant_id=NULL — gli asset/contacts creati erano invisibili al
    tenant proprietario del task.
    """
    # Simulo l'avvio di un job di Alice (tenant_a): _run_job legge il task e
    # setta il context. Da quel momento, ogni chiamata db.* dal runner deve
    # ereditare tenant_id=A.
    task = db.get_task(populated_db["task_alice"], tenant_id=None)
    assert task["tenant_id"] == populated_db["tenant_a"]

    tenant_token = db.set_current_tenant(task["tenant_id"])
    user_token = db.set_current_user(task["created_by_user_id"])
    try:
        # Il runner agentico crea asset SENZA passare tenant_id esplicito.
        # Deve essere automaticamente assegnato al tenant del task.
        asset_id = db.upsert_asset({
            "asset_type": "scraped", "title": "Auto from runner",
            "raw_json": "{}", "source_url": "https://example.com/auto",
            "source_task_id": task["id"],
        })
        # E un contact
        contact_id = db.upsert_contact({
            "email": "auto-from-runner@example.com",
            "source_task_id": task["id"],
            "display_name": "Auto Runner",
        })
    finally:
        db.reset_current_user(user_token)
        db.reset_current_tenant(tenant_token)

    # Verifica: asset e contact hanno tenant_id e created_by_user_id corretti
    with db.connect() as conn:
        a = conn.execute(
            "SELECT tenant_id, created_by_user_id FROM assets WHERE id = %s", (asset_id,)
        ).fetchone()
        assert a["tenant_id"] == populated_db["tenant_a"]
        assert a["created_by_user_id"] == populated_db["alice_id"]

        c = conn.execute(
            "SELECT tenant_id, created_by_user_id FROM contacts WHERE id = %s", (contact_id,)
        ).fetchone()
        assert c["tenant_id"] == populated_db["tenant_a"]
        assert c["created_by_user_id"] == populated_db["alice_id"]

    # Verifica isolamento: Bob non vede l'asset/contact appena creato
    assert db.get_asset(asset_id, tenant_id=populated_db["tenant_b"]) is None
    assert db.get_contact(contact_id, tenant_id=populated_db["tenant_b"]) is None


def test_whatsapp_api_config_isolation(populated_db):
    """WhatsApp API config: isolato per tenant."""
    a_id = db.insert_whatsapp_api_config(
        {
            "label": "WA Alice", "phone_number_id": "111",
            "business_account_id": "222", "encrypted_access_token": b"\x10",
        },
        tenant_id=populated_db["tenant_a"],
        created_by_user_id=populated_db["alice_id"],
    )
    b_id = db.insert_whatsapp_api_config(
        {
            "label": "WA Bob", "phone_number_id": "333",
            "business_account_id": "444", "encrypted_access_token": b"\x20",
        },
        tenant_id=populated_db["tenant_b"],
        created_by_user_id=populated_db["bob_id"],
    )
    # Alice vede solo la sua
    cfg_a = db.list_whatsapp_api_config(tenant_id=populated_db["tenant_a"])
    assert {c["label"] for c in cfg_a} == {"WA Alice"}
    # Bob vede solo la sua
    cfg_b = db.list_whatsapp_api_config(tenant_id=populated_db["tenant_b"])
    assert {c["label"] for c in cfg_b} == {"WA Bob"}
    # Anti-IDOR
    assert db.get_whatsapp_api_config(b_id, tenant_id=populated_db["tenant_a"]) is None


# ---------------------------------------------------------------------------
# IDOR contacts (vulnerabilita' fixate 2026-05-22): list_*, find_*, get_*_by_ids,
# update_* devono filtrare per tenant.
# ---------------------------------------------------------------------------

@pytest.fixture
def populated_contacts(populated_db):
    """Crea 1 contact per tenant con email + whatsapp + telegram_chat_id + social."""
    alice_c = db.upsert_contact(
        {
            "email": "shared@example.com", "display_name": "Alice's Mario",
            "whatsapp": "+39111", "telegram_chat_id": "111",
            "social_json": '[{"platform":"instagram","url":"https://ig.com/alice_mario"}]',
            "source_domain": "alice.test",
        },
        tenant_id=populated_db["tenant_a"], created_by_user_id=populated_db["alice_id"],
    )
    bob_c = db.upsert_contact(
        {
            "email": "shared@example.com", "display_name": "Bob's Mario",
            "whatsapp": "+39222", "telegram_chat_id": "222",
            "social_json": '[{"platform":"instagram","url":"https://ig.com/bob_mario"}]',
            "source_domain": "bob.test",
        },
        tenant_id=populated_db["tenant_b"], created_by_user_id=populated_db["bob_id"],
    )
    return {**populated_db, "alice_c": alice_c, "bob_c": bob_c}


def test_get_contacts_by_ids_filters_tenant(populated_contacts):
    """IDOR fix: get_contacts_by_ids non deve ritornare contact di altri tenant
    nemmeno se l'attaccante passa l'ID esatto."""
    ctx = populated_contacts
    # Alice chiede l'ID di Bob → ritorna [] (tenant filtra)
    results = db.get_contacts_by_ids([ctx["bob_c"]], tenant_id=ctx["tenant_a"])
    assert results == []
    # Bob chiede il suo → ritorna 1
    results = db.get_contacts_by_ids([ctx["bob_c"]], tenant_id=ctx["tenant_b"])
    assert len(results) == 1
    # Super-admin chiede entrambi → ritorna 2
    results = db.get_contacts_by_ids([ctx["alice_c"], ctx["bob_c"]], tenant_id=None)
    assert len(results) == 2


def test_list_contacts_with_whatsapp_filters_tenant(populated_contacts):
    ctx = populated_contacts
    # Alice vede solo il suo
    rows = db.list_contacts_with_whatsapp(tenant_id=ctx["tenant_a"])
    assert len(rows) == 1
    assert rows[0]["whatsapp"] == "+39111"
    # Bob vede solo il suo
    rows = db.list_contacts_with_whatsapp(tenant_id=ctx["tenant_b"])
    assert rows[0]["whatsapp"] == "+39222"


def test_list_contacts_with_social_platform_filters_tenant(populated_contacts):
    ctx = populated_contacts
    rows = db.list_contacts_with_social_platform("instagram", tenant_id=ctx["tenant_a"])
    assert len(rows) == 1
    assert "alice_mario" in rows[0]["_platform_url"]
    rows = db.list_contacts_with_social_platform("instagram", tenant_id=ctx["tenant_b"])
    assert "bob_mario" in rows[0]["_platform_url"]


def test_list_contact_source_domains_filters_tenant(populated_contacts):
    ctx = populated_contacts
    rows = db.list_contact_source_domains(tenant_id=ctx["tenant_a"])
    domains = {d for d, _ in rows}
    assert domains == {"alice.test"}
    rows = db.list_contact_source_domains(tenant_id=ctx["tenant_b"])
    assert {d for d, _ in rows} == {"bob.test"}


def test_find_contact_by_email_filters_tenant(populated_contacts):
    """CRITICO: inbound poisoning. Tenant A che riceve mail per shared@example.com
    NON deve matchare il contact di Bob con la stessa email."""
    ctx = populated_contacts
    # Alice cerca shared@example.com → trova IL SUO contact, non quello di Bob
    c = db.find_contact_by_email("shared@example.com", tenant_id=ctx["tenant_a"])
    assert c["display_name"] == "Alice's Mario"
    # Bob cerca lo stesso → trova IL SUO
    c = db.find_contact_by_email("shared@example.com", tenant_id=ctx["tenant_b"])
    assert c["display_name"] == "Bob's Mario"


def test_find_contact_by_telegram_chat_filters_tenant(populated_contacts):
    """Stesso pattern di find_contact_by_email: tenant deve restare isolato
    anche se chat_id collide accidentalmente."""
    ctx = populated_contacts
    # Alice cerca telegram_chat_id="222" (di Bob) → non trova nulla
    c = db.find_contact_by_telegram_chat("222", tenant_id=ctx["tenant_a"])
    assert c is None
    # Bob cerca lo stesso → trova il suo
    c = db.find_contact_by_telegram_chat("222", tenant_id=ctx["tenant_b"])
    assert c["display_name"] == "Bob's Mario"


def test_update_contact_filters_tenant(populated_contacts):
    """Write IDOR: Alice tenta UPDATE su contact_id di Bob → no-op silenzioso."""
    ctx = populated_contacts
    db.update_contact(ctx["bob_c"], {"display_name": "HACKED"}, tenant_id=ctx["tenant_a"])
    # Il contact di Bob NON e' stato modificato
    c = db.get_contact(ctx["bob_c"], tenant_id=ctx["tenant_b"])
    assert c["display_name"] == "Bob's Mario"


def test_update_contact_status_filters_tenant(populated_contacts):
    ctx = populated_contacts
    db.update_contact_status(ctx["bob_c"], "rejected", tenant_id=ctx["tenant_a"])
    c = db.get_contact(ctx["bob_c"], tenant_id=ctx["tenant_b"])
    assert c["status"] != "rejected"


# ---------------------------------------------------------------------------
# IDOR workflow edges (fixed 2026-05-22): get_edge + delete_edge + toggle_edge
# devono filtrare per tenant via JOIN sul workflow padre.
# ---------------------------------------------------------------------------

@pytest.fixture
def populated_edges(populated_db):
    """Crea 1 workflow per tenant con 1 edge ognuno."""
    ctx = populated_db
    # 2 task in piu' (servono per legare gli edge)
    task_a2 = db.create_task(
        {"name": "Alice T2", "objective": "x"},
        tenant_id=ctx["tenant_a"], created_by_user_id=ctx["alice_id"],
    )
    task_b2 = db.create_task(
        {"name": "Bob T2", "objective": "x"},
        tenant_id=ctx["tenant_b"], created_by_user_id=ctx["bob_id"],
    )
    wf_a = db.create_workflow(
        "WF Alice", tenant_id=ctx["tenant_a"], created_by_user_id=ctx["alice_id"]
    )
    wf_b = db.create_workflow(
        "WF Bob", tenant_id=ctx["tenant_b"], created_by_user_id=ctx["bob_id"]
    )
    edge_a = db.create_edge(
        from_task_id=ctx["task_alice"], to_task_id=task_a2, workflow_id=wf_a,
    )
    edge_b = db.create_edge(
        from_task_id=ctx["task_bob"], to_task_id=task_b2, workflow_id=wf_b,
    )
    return {**ctx, "wf_a": wf_a, "wf_b": wf_b, "edge_a": edge_a, "edge_b": edge_b}


def test_get_edge_filters_tenant(populated_edges):
    ctx = populated_edges
    # Alice non vede edge di Bob
    assert db.get_edge(ctx["edge_b"], tenant_id=ctx["tenant_a"]) is None
    # Bob vede il suo
    assert db.get_edge(ctx["edge_b"], tenant_id=ctx["tenant_b"]) is not None
    # Super-admin vede tutto
    assert db.get_edge(ctx["edge_b"], tenant_id=None) is not None


def test_delete_edge_filters_tenant(populated_edges):
    """Write IDOR: Alice tenta DELETE su edge di Bob → no-op."""
    ctx = populated_edges
    n = db.delete_edge(ctx["edge_b"], tenant_id=ctx["tenant_a"])
    assert n == 0  # niente cancellato
    # Edge di Bob ancora esiste
    assert db.get_edge(ctx["edge_b"], tenant_id=ctx["tenant_b"]) is not None


def test_toggle_edge_filters_tenant(populated_edges):
    ctx = populated_edges
    # Alice tenta toggle su edge di Bob → no-op
    n = db.toggle_edge(ctx["edge_b"], enabled=False, tenant_id=ctx["tenant_a"])
    assert n == 0
    # Edge di Bob ancora enabled
    edge = db.get_edge(ctx["edge_b"], tenant_id=ctx["tenant_b"])
    assert edge["enabled"] == 1


# ---------------------------------------------------------------------------
# IDOR site memory (fixed 2026-05-22): delete_site_pattern/playbook + flush.
# ---------------------------------------------------------------------------

@pytest.fixture
def populated_site_memory(populated_db):
    """1 pattern + 1 playbook per tenant."""
    ctx = populated_db
    # Crea pattern per Alice
    tok_a = db.set_current_tenant(ctx["tenant_a"])
    try:
        pat_a = db.upsert_site_pattern(
            registrable_domain="alice.test", pattern="/p/", regex="/p/", asset_type="x",
        )
        pb_a = db.upsert_site_playbook(
            registrable_domain="alice.test", asset_type="x",
            playbook='{"text":"alice"}', source_runner="test",
            source_job_id=None, transferable=True,
        )
    finally:
        db.reset_current_tenant(tok_a)
    # Crea pattern per Bob
    tok_b = db.set_current_tenant(ctx["tenant_b"])
    try:
        pat_b = db.upsert_site_pattern(
            registrable_domain="bob.test", pattern="/q/", regex="/q/", asset_type="x",
        )
        pb_b = db.upsert_site_playbook(
            registrable_domain="bob.test", asset_type="x",
            playbook='{"text":"bob"}', source_runner="test",
            source_job_id=None, transferable=True,
        )
    finally:
        db.reset_current_tenant(tok_b)
    return {**ctx, "pat_a": pat_a, "pat_b": pat_b, "pb_a": pb_a, "pb_b": pb_b}


def test_delete_site_pattern_filters_tenant(populated_site_memory):
    """Alice tenta DELETE su pattern di Bob → no-op."""
    ctx = populated_site_memory
    n = db.delete_site_pattern(ctx["pat_b"], tenant_id=ctx["tenant_a"])
    assert n == 0
    # Pattern di Bob ancora c'e' (visible al super-admin)
    rows = db.list_site_patterns(registrable_domain="bob.test")
    # list_site_patterns nel context senza tenant filtra: serve super-admin
    tok = db.set_current_tenant(None)
    try:
        rows = db.list_site_patterns(registrable_domain="bob.test")
        assert any(r["id"] == ctx["pat_b"] for r in rows)
    finally:
        db.reset_current_tenant(tok)


def test_delete_site_playbook_filters_tenant(populated_site_memory):
    ctx = populated_site_memory
    n = db.delete_site_playbook(ctx["pb_b"], tenant_id=ctx["tenant_a"])
    assert n == 0
    # Playbook di Bob ancora esiste
    tok = db.set_current_tenant(ctx["tenant_b"])
    try:
        pb = db.get_site_playbook("bob.test", "x")
        assert pb is not None
    finally:
        db.reset_current_tenant(tok)


def test_truncate_site_memory_filters_tenant(populated_site_memory):
    """truncate da tenant_user A svuota SOLO la memoria di A, non di B."""
    ctx = populated_site_memory
    res = db.truncate_site_memory(tenant_id=ctx["tenant_a"])
    assert res["site_patterns"] == 1
    assert res["site_playbooks"] == 1
    # Memoria di Bob intatta
    tok = db.set_current_tenant(ctx["tenant_b"])
    try:
        assert db.list_site_patterns(registrable_domain="bob.test")
        assert db.get_site_playbook("bob.test", "x") is not None
    finally:
        db.reset_current_tenant(tok)


def test_truncate_site_memory_superadmin_wipes_all(populated_site_memory):
    """Super-admin (tenant_id=None) puo' fare flush totale."""
    res = db.truncate_site_memory(tenant_id=None)
    assert res["site_patterns"] == 2
    assert res["site_playbooks"] == 2


# ---------------------------------------------------------------------------
# End-to-end route IDOR: Alice logged-in via HTTP tenta di toccare workflow
# edges di Bob via POST → atteso 404 (route fa get_edge tenant-filtered)
# ---------------------------------------------------------------------------

def test_e2e_route_delete_edge_idor_blocked(populated_edges, tmp_path, monkeypatch):
    """End-to-end: Alice logga via HTTP, tenta POST /workflow_edges/{edge_b}/delete
    via il proprio client → la route ritorna 404, l'edge di Bob NON e' cancellato.
    """
    from app import config, storage
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "RESULTS_DIR", tmp_path / "results")
    monkeypatch.setattr(storage, "RESULTS_DIR", tmp_path / "results")

    ctx = populated_edges
    from app.main import app
    with TestClient(app) as client:
        r = client.post(
            "/login",
            data={"email": "alice@a.it", "password": "pwd-alice", "next": "/"},
            follow_redirects=False,
        )
        assert r.status_code == 303
        r = client.post(
            f"/workflow_edges/{ctx['edge_b']}/delete",
            data={"redirect_to": "/workflows"},
            follow_redirects=False,
        )
        # Atteso: 404 (route fa get_edge che filtra per tenant di Alice)
        assert r.status_code == 404, f"IDOR exploit possibile: got {r.status_code}"

    # Verifica DB: edge di Bob ancora esiste
    edge = db.get_edge(ctx["edge_b"], tenant_id=None)
    assert edge is not None, "Edge di Bob cancellato — IDOR exploit successo!"


def test_list_subjobs_filters_tenant(populated_db):
    """IDOR fix: list_subjobs(parent_id) non deve ritornare sub-job di un
    parent appartenente ad altro tenant nemmeno se l'attaccante ne indovina l'id.
    """
    ctx = populated_db
    # Crea un parent job + 2 sub per tenant_a
    parent_a = db.create_job(ctx["task_alice"], tenant_id=ctx["tenant_a"], created_by_user_id=ctx["alice_id"])
    sub_a1 = db.create_job(ctx["task_alice"], triggered_by_job_id=parent_a, tenant_id=ctx["tenant_a"], created_by_user_id=ctx["alice_id"])
    sub_a2 = db.create_job(ctx["task_alice"], triggered_by_job_id=parent_a, tenant_id=ctx["tenant_a"], created_by_user_id=ctx["alice_id"])
    # Crea un parent job + 1 sub per tenant_b
    parent_b = db.create_job(ctx["task_bob"], tenant_id=ctx["tenant_b"], created_by_user_id=ctx["bob_id"])
    sub_b1 = db.create_job(ctx["task_bob"], triggered_by_job_id=parent_b, tenant_id=ctx["tenant_b"], created_by_user_id=ctx["bob_id"])

    # Alice vede i suoi sub
    subs = db.list_subjobs(parent_a, tenant_id=ctx["tenant_a"])
    assert {s["id"] for s in subs} == {sub_a1, sub_a2}
    # Alice NON vede i sub di Bob nemmeno passando il parent di Bob
    assert db.list_subjobs(parent_b, tenant_id=ctx["tenant_a"]) == []
    # Bob vede i suoi
    subs = db.list_subjobs(parent_b, tenant_id=ctx["tenant_b"])
    assert {s["id"] for s in subs} == {sub_b1}
    # Super-admin vede tutto
    assert len(db.list_subjobs(parent_a, tenant_id=None)) == 2
    assert len(db.list_subjobs(parent_b, tenant_id=None)) == 1


def test_list_subjobs_for_jobs_batch_filters_tenant(populated_db):
    """Variante batch: list_subjobs_for_jobs([...]) rispetta lo stesso filtro."""
    ctx = populated_db
    parent_a = db.create_job(ctx["task_alice"], tenant_id=ctx["tenant_a"], created_by_user_id=ctx["alice_id"])
    db.create_job(ctx["task_alice"], triggered_by_job_id=parent_a, tenant_id=ctx["tenant_a"], created_by_user_id=ctx["alice_id"])
    parent_b = db.create_job(ctx["task_bob"], tenant_id=ctx["tenant_b"], created_by_user_id=ctx["bob_id"])
    db.create_job(ctx["task_bob"], triggered_by_job_id=parent_b, tenant_id=ctx["tenant_b"], created_by_user_id=ctx["bob_id"])

    # Alice chiede entrambi gli ID: vede solo i suoi sub
    res = db.list_subjobs_for_jobs([parent_a, parent_b], tenant_id=ctx["tenant_a"])
    assert len(res.get(parent_a, [])) == 1
    assert res.get(parent_b, []) == []  # Bob's parent: zero sub visibili ad Alice
    # Super-admin vede tutto
    res = db.list_subjobs_for_jobs([parent_a, parent_b], tenant_id=None)
    assert len(res[parent_a]) == 1
    assert len(res[parent_b]) == 1


def test_upsert_contact_dedup_is_intra_tenant(populated_db):
    """REGRESSIONE 2026-05-22: pre-fix, upsert_contact con stessa email per
    tenant A e poi tenant B FACEVA UPDATE del record di A (cross-tenant write
    attack). Post-fix il dedup e' intra-tenant: B crea un record NUOVO."""
    ctx = populated_db
    a_id = db.upsert_contact(
        {"email": "shared@example.com", "display_name": "Alice's"},
        tenant_id=ctx["tenant_a"], created_by_user_id=ctx["alice_id"],
    )
    b_id = db.upsert_contact(
        {"email": "shared@example.com", "display_name": "Bob's"},
        tenant_id=ctx["tenant_b"], created_by_user_id=ctx["bob_id"],
    )
    # Devono essere 2 record DISTINTI
    assert a_id != b_id, "Cross-tenant dedup: B ha sovrascritto A!"
    # Ognuno appartiene al proprio tenant con i propri dati
    ca = db.get_contact(a_id, tenant_id=ctx["tenant_a"])
    cb = db.get_contact(b_id, tenant_id=ctx["tenant_b"])
    assert ca["display_name"] == "Alice's"
    assert cb["display_name"] == "Bob's"


def test_upsert_asset_dedup_is_intra_tenant(populated_db):
    """REGRESSIONE 2026-05-22: stesso pattern di upsert_contact su asset
    (dedup per source_url_canonical era cross-tenant)."""
    ctx = populated_db
    a_id = db.upsert_asset(
        {"asset_type": "lead", "title": "Alice's", "raw_json": "{}",
         "source_url": "https://shared.test/page"},
        tenant_id=ctx["tenant_a"], created_by_user_id=ctx["alice_id"],
    )
    b_id = db.upsert_asset(
        {"asset_type": "lead", "title": "Bob's", "raw_json": "{}",
         "source_url": "https://shared.test/page"},
        tenant_id=ctx["tenant_b"], created_by_user_id=ctx["bob_id"],
    )
    assert a_id != b_id, "Cross-tenant dedup: B ha sovrascritto A!"
    aa = db.get_asset(a_id, tenant_id=ctx["tenant_a"])
    ab = db.get_asset(b_id, tenant_id=ctx["tenant_b"])
    assert aa["title"] == "Alice's"
    assert ab["title"] == "Bob's"


def test_e2e_route_delete_site_pattern_idor_blocked(populated_site_memory, tmp_path, monkeypatch):
    """End-to-end: Alice tenta POST /site_memory/pattern/{pat_b}/delete → la
    funzione DB filtra per tenant, ritorna n=0 ma route ritorna 303 redirect.
    Verifica DB: pattern di Bob ancora esiste."""
    from app import config, storage
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "RESULTS_DIR", tmp_path / "results")
    monkeypatch.setattr(storage, "RESULTS_DIR", tmp_path / "results")

    ctx = populated_site_memory
    from app.main import app
    with TestClient(app) as client:
        r = client.post(
            "/login",
            data={"email": "alice@a.it", "password": "pwd-alice", "next": "/"},
            follow_redirects=False,
        )
        assert r.status_code == 303
        r = client.post(
            f"/site_memory/pattern/{ctx['pat_b']}/delete",
            follow_redirects=False,
        )
        # La route fa sempre 303 redirect (delete e' "silenzioso"), ma in DB
        # il filtro tenant non ha matchato → 0 righe cancellate.
        assert r.status_code in (303, 200)

    # Verifica DB: pattern di Bob ancora esiste
    tok = db.set_current_tenant(None)
    try:
        patterns = db.list_site_patterns(registrable_domain="bob.test")
        assert any(p["id"] == ctx["pat_b"] for p in patterns), \
            "Pattern di Bob cancellato da Alice — IDOR exploit successo!"
    finally:
        db.reset_current_tenant(tok)
