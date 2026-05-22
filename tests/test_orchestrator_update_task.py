"""Test del tool `update_task` esposto alla chat Orchestrator.

Pattern: il tool è una patch parziale che ricarica la config corrente con
`db.get_task`, applica solo i campi forniti e riscrive con `db.update_task`.
Speculare a quello che fa il form `POST /tasks/<id>`."""
from __future__ import annotations

import json

import pytest

from app import db
from app.routes.orchestrator import _tool_update_task


@pytest.fixture
def task_id() -> int:
    return db.create_task({
        "name": "Origin",
        "objective": "obiettivo iniziale",
        "agent_mode": "react",
        "model": "qwen3.5:latest",
        "max_iterations": 5,
        "seed_queries": ["q1"],
        "allowed_domains": ["a.test"],
        "target_cap_per_site": 30,
        "notes": "creato da test",
    })


def _call(args: dict) -> dict:
    return json.loads(_tool_update_task(args))


def test_patch_single_field_preserves_others(task_id):
    out = _call({"task_id": task_id, "max_iterations": 12})
    assert out["ok"] is True
    assert "max_iterations" in out["changed_fields"]
    t = db.get_task(task_id)
    assert t["max_iterations"] == 12
    assert t["name"] == "Origin"
    assert t["objective"] == "obiettivo iniziale"
    assert t["seed_queries"] == ["q1"]


def test_patch_multiple_fields_with_notes(task_id):
    out = _call({
        "task_id": task_id,
        "model": "ollama:llama3.1:8b",
        "objective": "obiettivo migliorato",
        "target_cap_per_site": 100,
        "notes": "auto-tune post job: cap alzato",
    })
    assert out["ok"] is True
    t = db.get_task(task_id)
    assert t["model"] == "llama3.1:8b"  # prefisso provider strippato
    assert t["objective"] == "obiettivo migliorato"
    assert t["target_cap_per_site"] == 100
    assert "auto-tune" in (t["notes"] or "")


def test_patch_replaces_lists(task_id):
    out = _call({"task_id": task_id, "seed_queries": ["q2", "q3"]})
    assert out["ok"] is True
    assert db.get_task(task_id)["seed_queries"] == ["q2", "q3"]


def test_patch_empty_list_clears(task_id):
    out = _call({"task_id": task_id, "allowed_domains": []})
    assert out["ok"] is True
    assert db.get_task(task_id)["allowed_domains"] == []


def test_patch_unknown_field_silently_ignored(task_id):
    out = _call({"task_id": task_id, "frobnicate": "yes", "max_iterations": 7})
    assert out["ok"] is True
    assert "frobnicate" not in out["changed_fields"]
    assert db.get_task(task_id)["max_iterations"] == 7


def test_patch_only_task_id_rejected(task_id):
    out = _call({"task_id": task_id})
    assert out["ok"] is False
    assert "nessun campo" in out["reason"]


def test_missing_task_id_rejected():
    out = _call({})
    assert out["ok"] is False


def test_task_not_found():
    out = _call({"task_id": 999_999, "max_iterations": 10})
    assert out["ok"] is False
    assert "non trovato" in out["reason"]


def test_invalid_int_value(task_id):
    out = _call({"task_id": task_id, "max_iterations": "abc"})
    assert out["ok"] is False
    assert "valore non valido" in out["reason"]
