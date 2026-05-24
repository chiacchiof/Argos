"""Test del sistema "agenti pubblicati" — schema + helper + UI."""
from __future__ import annotations

import pytest

from app import db, db_cloud
from app.auth import hash_password


@pytest.fixture
def setup_publish():
    """Tenant + 2 task + 1 workflow per testare publish/list."""
    tenant = db_cloud.create_tenant("PubTnt", "pubtnt")
    user = db_cloud.create_user(
        tenant_id=tenant, email="user@pub", password_hash=hash_password("pw"),
        role="tenant_architect",
    )
    t1 = db.create_task(
        {"name": "Task A", "objective": "x", "agent_mode": "browser_use"},
        tenant_id=tenant, created_by_user_id=user,
    )
    t2 = db.create_task(
        {"name": "Task B", "objective": "y", "agent_mode": "qualifier"},
        tenant_id=tenant, created_by_user_id=user,
    )
    w1 = db.create_workflow("WF X", tenant_id=tenant, created_by_user_id=user)
    return {"tenant": tenant, "user": user, "t1": t1, "t2": t2, "w1": w1}


def test_default_not_published(setup_publish):
    """Nuovi task/workflow hanno is_published_agent=FALSE per default."""
    t = db.get_task(setup_publish["t1"], tenant_id=setup_publish["tenant"])
    assert t["is_published_agent"] is False
    w = db.get_workflow(setup_publish["w1"], tenant_id=setup_publish["tenant"])
    assert w["is_published_agent"] is False


def test_set_agent_publication_task(setup_publish):
    """set_agent_publication scrive correttamente tutti i campi."""
    db.set_agent_publication(
        kind="task",
        agent_id=setup_publish["t1"],
        is_published=True,
        display_name="Find pharmacies",
        description="Cerca farmacie in una citta",
        category="lead-discovery",
        icon="op-i-target",
        input_schema=[{"name": "city", "type": "text", "label": "Citta", "required": True}],
        tenant_id=setup_publish["tenant"],
    )
    agent = db.get_published_agent("task", setup_publish["t1"],
                                   tenant_id=setup_publish["tenant"])
    assert agent is not None
    assert agent["display_name"] == "Find pharmacies"
    assert agent["category"] == "lead-discovery"
    assert agent["icon"] == "op-i-target"
    assert len(agent["input_schema"]) == 1
    assert agent["input_schema"][0]["name"] == "city"


def test_list_published_agents_combines_tasks_workflows(setup_publish):
    """list_published_agents include task + workflow pubblicati, in ordine."""
    s = setup_publish
    db.set_agent_publication(
        kind="task", agent_id=s["t1"], is_published=True,
        display_name="A Find leads", category="lead-discovery",
        tenant_id=s["tenant"],
    )
    db.set_agent_publication(
        kind="workflow", agent_id=s["w1"], is_published=True,
        display_name="B Workflow", category="lead-discovery",
        tenant_id=s["tenant"],
    )
    # t2 NON pubblicato — non deve apparire
    agents = db.list_published_agents(tenant_id=s["tenant"])
    assert len(agents) == 2
    names = [a["display_name"] for a in agents]
    assert "A Find leads" in names
    assert "B Workflow" in names
    # task non pubblicato non presente
    assert all(a["display_name"] != "Task B" for a in agents)
    # ordine: alphabetic within category
    assert names == sorted(names, key=str.lower)


def test_unpublish_via_set_agent_publication(setup_publish):
    """is_published=False ritira la pubblicazione."""
    s = setup_publish
    db.set_agent_publication(
        kind="task", agent_id=s["t1"], is_published=True,
        display_name="x", tenant_id=s["tenant"],
    )
    assert db.get_published_agent("task", s["t1"], tenant_id=s["tenant"]) is not None
    db.set_agent_publication(
        kind="task", agent_id=s["t1"], is_published=False,
        tenant_id=s["tenant"],
    )
    assert db.get_published_agent("task", s["t1"], tenant_id=s["tenant"]) is None


def test_published_agents_tenant_scoped(setup_publish):
    """Tenant A pubblica → tenant B non vede."""
    s = setup_publish
    tenant_b = db_cloud.create_tenant("OtherTnt", "othertnt")
    db.set_agent_publication(
        kind="task", agent_id=s["t1"], is_published=True,
        display_name="A-only", tenant_id=s["tenant"],
    )
    agents_a = db.list_published_agents(tenant_id=s["tenant"])
    agents_b = db.list_published_agents(tenant_id=tenant_b)
    assert len(agents_a) == 1
    assert len(agents_b) == 0


def test_publish_endpoint_via_http(setup_publish):
    """POST /tasks/{id}/publish-agent flippa il flag."""
    from fastapi.testclient import TestClient
    from app.main import app
    s = setup_publish
    client = TestClient(app)
    with client:
        # Login come architect
        client.post("/login", data={"email": "user@pub", "password": "pw"},
                   follow_redirects=False)
        r = client.post(
            f"/tasks/{s['t1']}/publish-agent",
            data={
                "is_published": "1",
                "display_name": "HTTP test",
                "description": "via HTTP",
                "category": "outreach",
                "icon": "op-i-megaphone",
                "input_schema_json": '[{"name":"target","type":"text","label":"Target","required":true}]',
            },
            follow_redirects=False,
        )
        assert r.status_code == 303
    agent = db.get_published_agent("task", s["t1"], tenant_id=s["tenant"])
    assert agent is not None
    assert agent["display_name"] == "HTTP test"
    assert agent["category"] == "outreach"
