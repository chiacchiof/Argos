"""Test conversazioni chat del fascicolo (chat multiple salvate, max 20)."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import db, db_cloud
from app.auth import hash_password
from app.fascicoli import db as fdb


@pytest.fixture
def proj():
    ta = db_cloud.create_tenant("TA", "ta")
    uid = db_cloud.create_user(tenant_id=ta, email="u@a.it", password_hash=hash_password("pw"), role="tenant_user")
    pid = fdb.create_project(title="P", tenant_id=ta, owner_user_id=uid)
    return {"ta": ta, "uid": uid, "pid": pid}


# ---- DB ----------------------------------------------------------------------

def test_create_list_messages_scoped(proj):
    c = proj
    a = fdb.create_conversation(c["pid"], title="Chat A", user_id=c["uid"])
    b = fdb.create_conversation(c["pid"], title="Chat B", user_id=c["uid"])
    fdb.add_chat_message(c["pid"], conversation_id=a, role="user", content="ciao A", user_id=c["uid"])
    fdb.add_chat_message(c["pid"], conversation_id=a, role="assistant", content="risposta A")
    fdb.add_chat_message(c["pid"], conversation_id=b, role="user", content="ciao B", user_id=c["uid"])
    # messaggi isolati per conversazione
    ma = fdb.list_chat_messages(c["pid"], conversation_id=a)
    mb = fdb.list_chat_messages(c["pid"], conversation_id=b)
    assert [m["content"] for m in ma] == ["ciao A", "risposta A"]
    assert [m["content"] for m in mb] == ["ciao B"]
    # list_conversations ordina per last_message_at desc (B ha l'ultimo messaggio... no, A)
    convs = fdb.list_conversations(c["pid"])
    assert {x["id"] for x in convs} == {a, b}
    by_id = {x["id"]: x for x in convs}
    assert by_id[a]["n_messages"] == 2 and by_id[b]["n_messages"] == 1


def test_rename_and_delete(proj):
    c = proj
    a = fdb.create_conversation(c["pid"], title="X", user_id=c["uid"])
    fdb.rename_conversation(a, "Rinominata", c["pid"])
    assert fdb.get_conversation(a, c["pid"])["title"] == "Rinominata"
    fdb.add_chat_message(c["pid"], conversation_id=a, role="user", content="m")
    assert fdb.delete_conversation(a, c["pid"]) is True
    # CASCADE: messaggi spariti
    assert fdb.list_chat_messages(c["pid"], conversation_id=a) == []
    assert fdb.get_conversation(a, c["pid"]) is None


def test_limit_20(proj):
    c = proj
    for i in range(fdb.MAX_CONVERSATIONS_PER_PROJECT):
        fdb.create_conversation(c["pid"], title=f"C{i}", user_id=c["uid"])
    assert fdb.count_conversations(c["pid"]) == 20
    with pytest.raises(fdb.ConversationLimitError):
        fdb.create_conversation(c["pid"], title="overflow", user_id=c["uid"])


def test_adopt_legacy_messages(proj):
    c = proj
    # messaggi legacy (conversation_id NULL)
    fdb.add_chat_message(c["pid"], role="user", content="vecchio 1", user_id=c["uid"])
    fdb.add_chat_message(c["pid"], role="assistant", content="vecchio 2")
    assert fdb.list_chat_messages(c["pid"], conversation_id=None)  # esistono come legacy
    cid = fdb.adopt_legacy_messages(c["pid"], user_id=c["uid"])
    assert cid is not None
    # ora appartengono alla conversazione 'Chat'
    msgs = fdb.list_chat_messages(c["pid"], conversation_id=cid)
    assert [m["content"] for m in msgs] == ["vecchio 1", "vecchio 2"]
    assert fdb.list_chat_messages(c["pid"], conversation_id=None) == []
    # idempotente: seconda chiamata non crea altro
    assert fdb.adopt_legacy_messages(c["pid"], user_id=c["uid"]) is None


def test_cross_project_isolation(proj):
    c = proj
    pid2 = fdb.create_project(title="P2", tenant_id=c["ta"], owner_user_id=c["uid"])
    a = fdb.create_conversation(c["pid"], title="A", user_id=c["uid"])
    # rename/delete con project sbagliato -> no-op
    fdb.rename_conversation(a, "HACK", pid2)
    assert fdb.get_conversation(a, c["pid"])["title"] == "A"
    assert fdb.delete_conversation(a, pid2) is False
    assert fdb.get_conversation(a, pid2) is None  # non visibile da pid2


# ---- Route -------------------------------------------------------------------

@pytest.fixture
def http(proj, tmp_path, monkeypatch):
    from app import config, storage
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "RESULTS_DIR", tmp_path / "results")
    monkeypatch.setattr(storage, "RESULTS_DIR", tmp_path / "results")
    return proj


def test_routes_create_rename_delete(http):
    c = http
    from app.main import app
    with TestClient(app) as client:
        client.post("/login", data={"email": "u@a.it", "password": "pw", "next": "/"}, follow_redirects=False)
        # crea
        r = client.post(f"/fascicoli/{c['pid']}/conversations", follow_redirects=False)
        assert r.status_code == 302 and "conv=" in r.headers["location"]
        cid = int(r.headers["location"].split("conv=")[1])
        # detail rende col selettore
        r = client.get(f"/fascicoli/{c['pid']}?conv={cid}")
        assert r.status_code == 200 and "fasc-conv-bar" in r.text
        # rinomina
        r = client.post(f"/fascicoli/{c['pid']}/conversations/{cid}/rename",
                        data={"title": "Mia chat"}, follow_redirects=False)
        assert r.status_code == 302
        assert fdb.get_conversation(cid, c["pid"])["title"] == "Mia chat"
        # elimina
        r = client.post(f"/fascicoli/{c['pid']}/conversations/{cid}/delete", follow_redirects=False)
        assert r.status_code == 302
        assert fdb.get_conversation(cid, c["pid"]) is None


def test_route_limit_blocks(http):
    c = http
    for i in range(fdb.MAX_CONVERSATIONS_PER_PROJECT):
        fdb.create_conversation(c["pid"], title=f"C{i}", user_id=c["uid"])
    from app.main import app
    with TestClient(app) as client:
        client.post("/login", data={"email": "u@a.it", "password": "pw", "next": "/"}, follow_redirects=False)
        r = client.post(f"/fascicoli/{c['pid']}/conversations", follow_redirects=False)
        assert r.status_code == 302 and "conv_err=" in r.headers["location"]
        assert fdb.count_conversations(c["pid"]) == 20  # non creata
