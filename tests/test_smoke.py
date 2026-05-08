"""Smoke test: boot app + CRUD progetti via TestClient (no Ollama richiesto)."""
from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app


def test_boot_and_crud(monkeypatch, tmp_path):
    # isola DB e RESULTS in tmp_path per non sporcare data/ reale
    from app import config, db, storage
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "RESULTS_DIR", tmp_path / "results")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setattr(db, "DATA_DIR", tmp_path)
    monkeypatch.setattr(storage, "RESULTS_DIR", tmp_path / "results")

    db.init_db()

    with TestClient(app) as client:
        # lista vuota
        r = client.get("/")
        assert r.status_code == 200
        assert "AgentScraper" in r.text

        # form nuovo (potrebbe fallire la list_models se Ollama non è up: tolleriamo)
        r = client.get("/tasks/new")
        assert r.status_code == 200
        assert "Nuovo task" in r.text

        # crea progetto
        r = client.post(
            "/tasks",
            data={
                "name": "Test",
                "description": "smoke",
                "objective": "Trova info di test",
                "seed_queries": "test query 1\ntest query 2",
                "allowed_domains": "",
                "blocked_domains": "",
                "max_iterations": "5",
                "model": "qwen3.5:latest",
                "output_format": "txt",
                "cron": "",
            },
            follow_redirects=False,
        )
        assert r.status_code == 303
        loc = r.headers["location"]
        assert loc.startswith("/tasks/")
        task_id = int(loc.rsplit("/", 1)[-1])

        # detail
        r = client.get(f"/tasks/{task_id}")
        assert r.status_code == 200
        assert "Test" in r.text
        assert "Trova info di test" in r.text

        # update
        r = client.post(
            f"/tasks/{task_id}",
            data={
                "name": "Test rinominato",
                "description": "",
                "objective": "Obiettivo aggiornato",
                "seed_queries": "",
                "allowed_domains": "",
                "blocked_domains": "",
                "max_iterations": "8",
                "model": "qwen3.5:latest",
                "output_format": "md",
                "cron": "",
            },
            follow_redirects=False,
        )
        assert r.status_code == 303

        r = client.get(f"/tasks/{task_id}")
        assert "Test rinominato" in r.text
        assert "Obiettivo aggiornato" in r.text

        # results vuoti
        r = client.get(f"/tasks/{task_id}/results")
        assert r.status_code == 200

        # delete
        r = client.post(f"/tasks/{task_id}/delete", follow_redirects=False)
        assert r.status_code == 303

        r = client.get(f"/tasks/{task_id}")
        assert r.status_code == 404


def test_new_tables_created(monkeypatch, tmp_path):
    """Verifica che le nuove tabelle operative siano create da init_db()."""
    from app import config, db, storage
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "RESULTS_DIR", tmp_path / "results")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setattr(db, "DATA_DIR", tmp_path)
    monkeypatch.setattr(storage, "RESULTS_DIR", tmp_path / "results")

    db.init_db()

    with db.connect() as con:
        names = {r["name"] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
    expected = {
        "workflow_edges", "contacts", "threads", "messages",
        "channel_config", "orchestrator_messages",
    }
    missing = expected - names
    assert not missing, f"tabelle mancanti: {missing}"

    # verifica colonne nuove su projects
    with db.connect() as con:
        cols = {r["name"] for r in con.execute("PRAGMA table_info(tasks)").fetchall()}
    new_cols = {
        "input_artifact_path", "message_template", "message_subject",
        "message_channels", "responder_system_prompt",
    }
    missing_cols = new_cols - cols
    assert not missing_cols, f"colonne tasks mancanti: {missing_cols}"

    # contacts CRUD round-trip
    cid = db.upsert_contact({
        "source_url": "https://test.com/u/x",
        "source_domain": "test.com",
        "display_name": "Test User",
        "email": "test@example.com",
        "raw_json": '{"foo":"bar"}',
    })
    assert cid > 0
    c = db.get_contact(cid)
    assert c["email"] == "test@example.com"

    # upsert idempotente per email
    cid2 = db.upsert_contact({"email": "TEST@example.com", "display_name": "Updated"})
    assert cid2 == cid  # stesso record

    # thread + messaggi
    tid = db.get_or_create_thread(cid, "email", external_id="abc@server", subject="hi")
    db.insert_message(tid, "out", "hello there", status="sent")
    db.insert_message(tid, "in", "thanks", status="received")
    msgs = db.list_messages(tid)
    assert len(msgs) == 2

    # find_unprocessed_inbound trova quello in
    pending = db.find_unprocessed_inbound()
    # il msg "in" è dopo l'ultimo "out" → counts as unprocessed
    assert any(m["id"] == msgs[1]["id"] for m in pending)

    # chat persistente orchestrator
    db.add_orchestrator_message("user", "ciao")
    db.add_orchestrator_message("assistant", "dimmi pure")
    chat = db.list_orchestrator_messages()
    assert [m["role"] for m in chat[-2:]] == ["user", "assistant"]
    db.clear_orchestrator_messages()
    assert db.list_orchestrator_messages() == []


def test_workflow_dag_create_and_cycle(monkeypatch, tmp_path):
    from app import config, db, storage
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "RESULTS_DIR", tmp_path / "results")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setattr(db, "DATA_DIR", tmp_path)
    monkeypatch.setattr(storage, "RESULTS_DIR", tmp_path / "results")
    db.init_db()

    pid_a = db.create_task({"name": "A", "objective": "scrape"})
    pid_b = db.create_task({"name": "B", "objective": "qualify"})
    pid_c = db.create_task({"name": "C", "objective": "outreach"})

    eid_ab = db.create_edge(pid_a, pid_b, pass_artifact="profiles.jsonl")
    eid_bc = db.create_edge(pid_b, pid_c, pass_artifact="qualified.jsonl")
    assert eid_ab > 0 and eid_bc > 0

    edges_from_a = db.list_edges(from_task_id=pid_a)
    assert len(edges_from_a) == 1
    assert edges_from_a[0]["to_task_id"] == pid_b

    # Self-edge proibito
    import pytest
    with pytest.raises(ValueError):
        db.create_edge(pid_a, pid_a)

    # Ciclo proibito: C → A creerebbe A→B→C→A
    with pytest.raises(ValueError):
        db.create_edge(pid_c, pid_a)


def test_ingest_profiles_to_contacts(monkeypatch, tmp_path):
    """Lo scraper ingesta profiles.jsonl in tabella contacts con status='new',
    preservando lo status di contatti già esistenti (es. 'optedout')."""
    import json
    from app import config, db, storage
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "RESULTS_DIR", tmp_path / "results")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setattr(db, "DATA_DIR", tmp_path)
    monkeypatch.setattr(storage, "RESULTS_DIR", tmp_path / "results")
    db.init_db()

    from app.agent.runner_browseruse import _ingest_to_contacts

    # Pre-esistente: questo email è già optedout, NON deve tornare indietro a 'new'
    pre_id = db.upsert_contact({"email": "old@example.com", "display_name": "Old"})
    db.update_contact_status(pre_id, "optedout", "test")

    # Crea task + job fittizi per i FK source_task_id / source_job_id
    task_id = db.create_task({"name": "scraper", "objective": "x"})
    job_id = db.create_job(task_id)

    # profiles.jsonl con 4 righe (2 valide, 1 senza contatti, 1 invalida)
    jsonl = tmp_path / "profiles.jsonl"
    with jsonl.open("w", encoding="utf-8") as f:
        f.write(json.dumps({"url": "https://a.com/u/1", "display_name": "Alice", "email": "alice@a.com"}) + "\n")
        f.write(json.dumps({"url": "https://b.com/u/2", "display_name": "Bob", "telegram_username": "@bob"}) + "\n")
        f.write(json.dumps({"url": "https://c.com/u/3", "display_name": "NoContact"}) + "\n")  # no email/telegram
        f.write(json.dumps({"url": "https://d.com/u/4", "email": "old@example.com"}) + "\n")  # già optedout
        f.write("not-json\n")  # invalido

    n = _ingest_to_contacts(jsonl, task_id, job_id, lambda s: None)
    assert n == 3  # 3 ingestiti (Alice, Bob, Old re-touched), 1 skip (NoContact), 1 invalid

    contacts = db.list_contacts()
    emails = {c["email"] for c in contacts if c.get("email")}
    assert "alice@a.com" in emails
    assert "old@example.com" in emails

    # Verifica preservation status
    pre = db.get_contact(pre_id)
    assert pre["status"] == "optedout"  # NON degradato a 'new'
    # Verifica nuovi record sono 'new'
    alice = db.find_contact_by_email("alice@a.com")
    assert alice["status"] == "new"


def test_extract_json_dicts_robust():
    """Verifica le 3 strategie progressive di _extract_json_dicts."""
    from app.agent.runner_browseruse import _extract_json_dicts

    # Strategia 1: JSON puro (dict)
    out = _extract_json_dicts('{"url": "x", "name": "Y"}')
    assert len(out) == 1 and out[0]["url"] == "x"

    # Strategia 1: JSON puro (lista di dict)
    out = _extract_json_dicts('[{"a": 1}, {"b": 2}]')
    assert len(out) == 2

    # Strategia 2: blocco markdown ```json
    out = _extract_json_dicts(
        'Ho trovato:\n```json\n{"url": "x", "name": "Y"}\n```\nfine.'
    )
    assert len(out) == 1 and out[0]["name"] == "Y"

    # Strategia 3: scan greedy di {} bilanciati
    out = _extract_json_dicts(
        'Ecco i dati: {"url": "a", "n": 1} e poi {"url": "b", "n": 2}. Fine.'
    )
    assert len(out) == 2
    assert out[0]["n"] == 1 and out[1]["n"] == 2

    # Niente JSON validi
    out = _extract_json_dicts("nessun JSON qui")
    assert out == []

    # Dict troppo povero (1 campo) viene comunque restituito (filtraggio è nel chiamante)
    out = _extract_json_dicts('{"x": 1}')
    assert len(out) == 1


def test_bulk_extract_url_collection_filters():
    """Verifica che il runner bulk_extract raccolga URL da seed+artifact e applichi filtri."""
    from app.agent.runner_bulk_extract import (
        _normalize_url, _domain_allowed, _load_urls_from_artifact,
    )
    import json
    import tempfile
    from pathlib import Path

    # _normalize_url: aggiunge https://, gestisce vuoto
    assert _normalize_url("https://a.com/x") == "https://a.com/x"
    assert _normalize_url("a.com/x") == "https://a.com/x"
    assert _normalize_url("  ") is None
    assert _normalize_url("") is None

    # _domain_allowed con whitelist
    assert _domain_allowed("https://example.com/p", ["example.com"], [])
    assert not _domain_allowed("https://other.com/p", ["example.com"], [])
    assert _domain_allowed("https://sub.example.com/p", ["example.com"], [])  # subdomain

    # blacklist
    assert not _domain_allowed("https://bad.com/p", [], ["bad.com"])

    # _load_urls_from_artifact: jsonl con url
    with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False, encoding="utf-8") as f:
        f.write(json.dumps({"url": "https://a.com/1", "name": "x"}) + "\n")
        f.write(json.dumps({"url": "https://b.com/2"}) + "\n")
        f.write(json.dumps({"name": "noURL"}) + "\n")  # ignored
        f.write("https://c.com/3\n")  # plain text URL
        path = f.name

    urls = _load_urls_from_artifact(path)
    assert "https://a.com/1" in urls
    assert "https://b.com/2" in urls
    assert "https://c.com/3" in urls
    assert len(urls) == 3

    Path(path).unlink()


def test_optout_detection():
    from app.agent.runner_responder import _is_opt_out
    assert _is_opt_out("STOP")
    assert _is_opt_out("ti prego rimuovimi dalla lista")
    assert _is_opt_out("Please unsubscribe me")
    assert _is_opt_out("non contattarmi più")
    assert not _is_opt_out("ciao, mi interessa la proposta")
    assert not _is_opt_out("rispondo subito grazie")


def test_validation_error_returns_form(monkeypatch, tmp_path):
    from app import config, db, storage
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "RESULTS_DIR", tmp_path / "results")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setattr(db, "DATA_DIR", tmp_path)
    monkeypatch.setattr(storage, "RESULTS_DIR", tmp_path / "results")
    db.init_db()

    with TestClient(app) as client:
        r = client.post(
            "/tasks",
            data={
                "name": "",  # invalido (min_length=1 in ProjectIn)
                "description": "",
                "objective": "x",
                "seed_queries": "",
                "allowed_domains": "",
                "blocked_domains": "",
                "max_iterations": "5",
                "model": "qwen3.5:latest",
                "output_format": "txt",
                "cron": "",
            },
        )
        assert r.status_code == 400, f"body={r.text[:300]}"
        assert "Errori di validazione" in r.text


def test_orchestrator_plan_and_execute(monkeypatch, tmp_path):
    """L'orchestrator genera un piano locale e crea task/workflow dopo conferma."""
    import re
    from html import unescape

    from app import config, db, storage
    from app.routes import orchestrator as orchestrator_route

    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "RESULTS_DIR", tmp_path / "results")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setattr(db, "DATA_DIR", tmp_path)
    monkeypatch.setattr(storage, "RESULTS_DIR", tmp_path / "results")
    monkeypatch.setattr(orchestrator_route, "UPLOADS_DIR", tmp_path / "uploads")
    db.init_db()

    async def fake_chat_reply(latest_user_message, *, file_info=None, chat_options=None):
        assert "Allegato" in latest_user_message
        assert file_info and file_info["filename"] == "brief.md"
        assert "contenuto allegato" in file_info["context_text"]
        assert chat_options["web_enabled"] is True
        assert chat_options["files_enabled"] is True
        return "Ho letto il file allegato.", {"fake": True}

    async def fake_stream_reply(
        latest_user_message,
        *,
        file_info=None,
        chat_options=None,
        metadata_out=None,
    ):
        assert "Risposta breve" in latest_user_message
        assert chat_options["web_enabled"] is False
        if metadata_out is not None:
            metadata_out.update({"fake_stream": True})
        yield "Certo, "
        yield "breve."

    monkeypatch.setattr(orchestrator_route, "_generate_chat_reply", fake_chat_reply)
    monkeypatch.setattr(orchestrator_route, "_stream_chat_reply", fake_stream_reply)

    with TestClient(app) as client:
        r = client.post(
            "/settings/orchestrator",
            data={
                "use_llm": "on",
                "llm_provider": "custom",
                "planner_model": "planner-test",
                "llm_base_url": "https://llm.example/v1",
                "llm_api_key": "secret-test",
            },
            follow_redirects=False,
        )
        assert r.status_code == 303
        saved = db.get_channel_config("orchestrator")
        assert saved["enabled"] == 1
        assert saved["config"]["llm_provider"] == "custom"
        assert saved["config"]["llm_api_key"] == "secret-test"
        assert "chat_web_enabled" not in saved["config"]
        assert "chat_files_enabled" not in saved["config"]

        r = client.get("/orchestrator")
        assert r.status_code == 200
        assert "Orchestrator" in r.text
        assert "planner-test" in r.text
        assert 'name="chat_web_enabled"' in r.text
        assert 'name="chat_files_enabled"' in r.text

        r = client.post(
            "/orchestrator/chat",
            data={
                "message": "Leggi questo file.",
                "chat_web_enabled": "on",
                "chat_files_enabled": "on",
            },
            files={"attachment": ("brief.md", b"contenuto allegato", "text/markdown")},
            follow_redirects=False,
        )
        assert r.status_code == 303
        chat = db.list_orchestrator_messages()
        assert chat[-2]["role"] == "user"
        assert "brief.md" in chat[-2]["body"]
        assert chat[-2]["metadata"]["attachment"]["filename"] == "brief.md"
        assert chat[-1]["body"] == "Ho letto il file allegato."

        r = client.post(
            "/orchestrator/chat/stream",
            data={"message": "Risposta breve"},
        )
        assert r.status_code == 200
        assert '"type": "token"' in r.text
        assert "Certo, " in r.text
        assert db.list_orchestrator_messages()[-1]["metadata"]["fake_stream"] is True

        r = client.post(
            "/orchestrator/plan",
            data={
                "brief": "estrai contatti da https://example.com e qualificali",
                "autonomy_level": "builder",
                "llm_provider": "ollama",
                "planner_model": "qwen3.5:latest",
            },
        )
        assert r.status_code == 200
        assert "Task proposti" in r.text
        assert "Estrazione dati" in r.text
        assert "Qualifica contatti" in r.text

        m = re.search(r'name="plan_b64" value="([^"]+)"', r.text)
        assert m, r.text[:500]
        plan_b64 = unescape(m.group(1))

        r = client.post(
            "/orchestrator/execute",
            data={"plan_b64": plan_b64},
            follow_redirects=False,
        )
        assert r.status_code == 303
        assert r.headers["location"].startswith("/workflows/")

    assert len(db.list_tasks()) == 2
    assert len(db.list_workflows()) == 1
