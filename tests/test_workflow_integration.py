"""Test B-007: integration end-to-end dei "seam" cross-cutting del runtime job/
workflow, SENZA runner reali (niente LLM/browser/external):

- recovery: reconcile_orphan_jobs (boot) + watchdog_zombie_jobs (runtime)
- workflow state machine: find_workflow_roots + _maybe_finalize_workflow_run policy
- artifact passing A→B (DAG edge con pass_artifact)

I runner reali (LLM/browser/WhatsApp) sono non-deterministici e richiedono servizi
esterni: l'esecuzione full-stack va verificata a mano/in staging (vedi skill verify).
Qui esercitiamo la logica di orchestrazione, che è dove si annidano le regressioni.
"""
from __future__ import annotations

import asyncio
import datetime as dt

import pytest

from app import db, jobs


def _run_status(run_id: int) -> str | None:
    with db.connect() as c:
        row = c.execute(
            "SELECT status FROM workflow_runs WHERE id = %s", (run_id,)
        ).fetchone()
    return row["status"] if row else None


def _mk_task(name: str = "t", mode: str = "bulk_extract") -> int:
    return db.create_task({"name": name, "objective": "o", "agent_mode": mode, "model": "m"})


# ---------------------------------------------------------------------------
# Recovery
# ---------------------------------------------------------------------------

def test_reconcile_orphan_jobs_marks_active_as_error():
    """Al boot, ogni job rimasto attivo (il processo che lo eseguiva è morto)
    va riconciliato a 'error'. Simula il crash/riavvio del server."""
    t = _mk_task()
    j_running = db.create_job(t)
    db.update_job(j_running, status="running", started_at=db.now_iso())
    j_queued = db.create_job(t)  # resta 'queued'
    j_done = db.create_job(t)
    db.update_job(j_done, status="done")

    n = jobs.reconcile_orphan_jobs()
    assert n >= 2
    assert db.get_job(j_running, tenant_id=None)["status"] == "error"
    assert db.get_job(j_queued, tenant_id=None)["status"] == "error"
    # i job già terminali non vengono toccati
    assert db.get_job(j_done, tenant_id=None)["status"] == "done"


def test_reconcile_finalizes_orphan_workflow_run():
    """Un workflow_run con job orfani viene finalizzato a 'error' (non resta
    'running' per sempre nella UL)."""
    t = _mk_task()
    wf = db.create_workflow("wf-orphan")
    run = db.create_workflow_run(wf)
    j = db.create_job(t, workflow_run_id=run)
    db.update_job(j, status="running", started_at=db.now_iso())

    jobs.reconcile_orphan_jobs()
    assert db.get_job(j, tenant_id=None)["status"] == "error"
    assert _run_status(run) == "error"


def test_watchdog_marks_zombie_but_respects_grace():
    """watchdog_zombie_jobs: un job 'running' senza task asyncio vivo e oltre il
    grace period → 'error'; uno appena partito (entro grace) → intoccato."""
    t = _mk_task()
    # zombie: started_at 5 minuti fa, nessun runner registrato in-process
    old = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=5)).isoformat()
    j_zombie = db.create_job(t)
    db.update_job(j_zombie, status="running", started_at=old)
    # fresco: appena partito → nel grace period
    j_fresh = db.create_job(t)
    db.update_job(j_fresh, status="running", started_at=db.now_iso())

    jobs.watchdog_zombie_jobs()
    assert db.get_job(j_zombie, tenant_id=None)["status"] == "error"
    assert db.get_job(j_fresh, tenant_id=None)["status"] == "running"


# ---------------------------------------------------------------------------
# Workflow state machine
# ---------------------------------------------------------------------------

def test_find_workflow_roots():
    a, b, c = _mk_task("A"), _mk_task("B"), _mk_task("C")
    wf = db.create_workflow("abc")
    db.create_edge(a, b, workflow_id=wf)
    db.create_edge(b, c, workflow_id=wf)
    assert db.find_workflow_roots(wf) == [a]


@pytest.mark.parametrize("statuses,expected", [
    (["done", "done"], "done"),
    (["done", "error"], "error"),
    (["done", "cancelled"], "cancelled"),
    (["error", "cancelled"], "error"),       # error prevale su cancelled
])
def test_finalize_workflow_run_policy(statuses, expected):
    t = _mk_task()
    wf = db.create_workflow("wf-pol")
    run = db.create_workflow_run(wf)
    for s in statuses:
        j = db.create_job(t, workflow_run_id=run)
        db.update_job(j, status=s)
    jobs._maybe_finalize_workflow_run(run)
    assert _run_status(run) == expected


def test_finalize_noop_while_jobs_pending():
    """Se almeno un job non è terminale, il run resta 'running' (non finalizza)."""
    t = _mk_task()
    wf = db.create_workflow("wf-pending")
    run = db.create_workflow_run(wf)
    j1 = db.create_job(t, workflow_run_id=run)
    db.update_job(j1, status="done")
    j2 = db.create_job(t, workflow_run_id=run)
    db.update_job(j2, status="running", started_at=db.now_iso())
    jobs._maybe_finalize_workflow_run(run)
    assert _run_status(run) == "running"


# ---------------------------------------------------------------------------
# Artifact passing (A→B) — senza runner reale (monkeypatch _run_job)
# ---------------------------------------------------------------------------

def test_artifact_passing_to_downstream(monkeypatch, tmp_path):
    a = _mk_task("upstream", "bulk_extract")
    b = _mk_task("downstream", "qualifier")
    wf = db.create_workflow("wf-art")
    db.create_edge(a, b, workflow_id=wf, pass_artifact="profiles.jsonl")
    run = db.create_workflow_run(wf)

    # job upstream 'done' con result_path = <run_dir>/report.md e l'artifact accanto
    run_dir = tmp_path / "run1"
    run_dir.mkdir()
    (run_dir / "profiles.jsonl").write_text('{"id": 1}\n', encoding="utf-8")
    report = run_dir / "report.md"
    report.write_text("ok", encoding="utf-8")
    j_up = db.create_job(a, workflow_run_id=run)
    db.update_job(j_up, status="done", result_path=str(report))

    # stub del runner: _after_job_done lancia asyncio.create_task(_run_job(...)).
    launched: list[tuple[int, int]] = []

    async def _fake_run_job(job_id, task_id):
        launched.append((job_id, task_id))
        db.update_job(job_id, status="done")

    monkeypatch.setattr(jobs, "_run_job", _fake_run_job)

    async def _drive():
        jobs._after_job_done(j_up, a, workflow_run_id=run)
        await asyncio.sleep(0.1)  # lascia girare il task downstream stubbed

    asyncio.run(_drive())

    # 1) l'artifact è stato passato al task downstream
    b_row = db.get_task(b)
    assert (b_row["input_artifact_path"] or "").endswith("profiles.jsonl")
    # 2) è stato lanciato un job per il task downstream
    assert any(tid == b for _jid, tid in launched)
    assert len(db.list_jobs(b)) >= 1
