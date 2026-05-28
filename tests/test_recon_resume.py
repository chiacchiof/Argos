"""Test B-015: chiusura R3 di recon_social — checkpoint + resume mid-job.

Esercita gli helper DB (`insert_recon_run`, `find_resumable_recon_run`,
`resume_recon_run`, `list_visited_urls`, `mark_recon_run_paused`,
`save_recon_checkpoint`, `latest_recon_checkpoint`) + il lifecycle completo
"run → stop graceful → resume" sui dati di DB (senza browser reale).

Il runner full-stack (con Playwright + social account loggato) richiede un account
sender e un dominio target reali: per quello c'è `verify` skill / manuale.
"""
from __future__ import annotations

import datetime as dt

import pytest

from app import db, db_cloud
from app.auth import hash_password


# ---------------------------------------------------------------------------
# Setup helpers
# ---------------------------------------------------------------------------

def _mk_task() -> tuple[int, int]:
    """Tenant + task recon_social. Restituisce (tenant_id, task_id)."""
    tid = db_cloud.create_tenant("RecT", "rect")
    uid = db_cloud.create_user(
        tenant_id=tid, email="u@rect", password_hash=hash_password("p"),
        role="tenant_user",
    )
    task_id = db.create_task(
        {"name": "rec", "objective": "o", "agent_mode": "recon_social",
         "model": "qwen3-coder:30b", "recon_mode": "url_driven"},
        tenant_id=tid, created_by_user_id=uid,
    )
    return tid, task_id


def _mk_job(task_id: int) -> int:
    jid = db.create_job(task_id)
    db.update_job(jid, status="running", started_at=db.now_iso())
    return jid


def _mk_social_account(tenant_id: int) -> int:
    """social_account fittizio (solo i campi NOT NULL)."""
    import secrets
    return db.create_social_account({
        "uuid": f"sa-{secrets.token_hex(6)}",
        "platform": "facebook",
        "username": f"acc-{secrets.token_hex(3)}",
        "encrypted_password": b"x",  # BYTEA NOT NULL — valore fittizio
    }, tenant_id=tenant_id)


# ---------------------------------------------------------------------------
# Test helpers DB
# ---------------------------------------------------------------------------

def test_insert_recon_run_returns_id_fixes_lastrowid_bug():
    """Verifica il fix del bug pre-esistente `cur.lastrowid` (None su psycopg)."""
    tid, task_id = _mk_task()
    jid = _mk_job(task_id)
    sa = _mk_social_account(tid)
    run_id = db.insert_recon_run(task_id, jid, sa)
    assert isinstance(run_id, int) and run_id > 0
    # status='running' di default
    with db.connect() as c:
        row = c.execute(
            "SELECT status, task_id, job_id, social_account_id FROM recon_runs WHERE id=%s",
            (run_id,),
        ).fetchone()
    assert row["status"] == "running"
    assert row["task_id"] == task_id and row["job_id"] == jid


def test_find_resumable_recon_run_only_paused_and_recent():
    tid, task_id = _mk_task()
    jid = _mk_job(task_id)
    sa = _mk_social_account(tid)

    # 1) run 'running' → non resumable
    rid_running = db.insert_recon_run(task_id, jid, sa)
    assert db.find_resumable_recon_run(task_id) is None

    # 2) run 'paused' recente → resumable, ritorna proprio quello
    rid_paused = db.insert_recon_run(task_id, jid, sa)
    db.mark_recon_run_paused(rid_paused)
    got = db.find_resumable_recon_run(task_id)
    assert got is not None and got["id"] == rid_paused

    # 3) run 'paused' VECCHIO (≥24h) → non resumable
    old_iso = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=48)).isoformat()
    with db.connect() as c:
        c.execute("UPDATE recon_runs SET last_active_at=%s WHERE id=%s",
                  (old_iso, rid_paused))
    assert db.find_resumable_recon_run(task_id) is None
    # custom window 72h lo include di nuovo
    assert db.find_resumable_recon_run(task_id, max_age_hours=72)["id"] == rid_paused


def test_list_visited_urls_per_run_isolation():
    tid, task_id = _mk_task()
    jid = _mk_job(task_id)
    sa = _mk_social_account(tid)
    r1 = db.insert_recon_run(task_id, jid, sa)
    r2 = db.insert_recon_run(task_id, jid, sa)
    with db.connect() as c:
        for run, url in [(r1, "https://fb.com/a"), (r1, "https://fb.com/b"),
                         (r2, "https://fb.com/c")]:
            c.execute(
                "INSERT INTO recon_visited (run_id, target_url, target_platform, "
                "visited_at) VALUES (%s, %s, 'facebook', %s)",
                (run, url, db.now_iso()),
            )
    assert db.list_visited_urls(r1) == {"https://fb.com/a", "https://fb.com/b"}
    assert db.list_visited_urls(r2) == {"https://fb.com/c"}


def test_checkpoint_save_and_retrieve_roundtrip():
    tid, task_id = _mk_task()
    jid = _mk_job(task_id)
    sa = _mk_social_account(tid)
    r = db.insert_recon_run(task_id, jid, sa)

    assert db.latest_recon_checkpoint(r) is None  # niente all'inizio

    db.save_recon_checkpoint(r, {"n_ok": 3, "n_fail": 1, "current_index": 4})
    db.save_recon_checkpoint(r, {"n_ok": 12, "n_fail": 2, "current_index": 14})  # piu' recente

    latest = db.latest_recon_checkpoint(r)
    assert latest is not None
    assert latest["snapshot"]["n_ok"] == 12  # ritorna il più recente
    assert latest["snapshot"]["current_index"] == 14


def test_resume_recon_run_flips_paused_to_running_and_relinks_job():
    tid, task_id = _mk_task()
    jid_old = _mk_job(task_id)
    sa = _mk_social_account(tid)
    r = db.insert_recon_run(task_id, jid_old, sa)
    db.mark_recon_run_paused(r)

    # Nuovo job → resume
    jid_new = _mk_job(task_id)
    db.resume_recon_run(r, jid_new)

    with db.connect() as c:
        row = c.execute(
            "SELECT status, job_id FROM recon_runs WHERE id=%s", (r,),
        ).fetchone()
    assert row["status"] == "running"
    assert row["job_id"] == jid_new  # relinkato al nuovo job


# ---------------------------------------------------------------------------
# Lifecycle integration: simula stop → resume
# ---------------------------------------------------------------------------

def test_full_lifecycle_stop_then_resume_skips_visited():
    """Scenario completo: 1° job processa 2/4 target, viene stoppato →
    paused; 2° job parte, trova il run paused, lista visited → salta i 2 già
    fatti e ne processa gli altri 2 → done."""
    tid, task_id = _mk_task()
    sa = _mk_social_account(tid)

    # --- Run #1: processa 2 target poi stop graceful ---
    jid1 = _mk_job(task_id)
    r = db.insert_recon_run(task_id, jid1, sa)
    targets = [f"https://fb.com/p{i}" for i in range(4)]
    with db.connect() as c:
        for url in targets[:2]:
            c.execute(
                "INSERT INTO recon_visited (run_id, target_url, target_platform, "
                "visited_at) VALUES (%s, %s, 'facebook', %s)",
                (r, url, db.now_iso()),
            )
    db.mark_recon_run_paused(r)
    db.save_recon_checkpoint(r, {"n_ok": 2, "current_index": 2, "total": 4})

    # --- Run #2: trova il paused, fa resume ---
    jid2 = _mk_job(task_id)
    resumed = db.find_resumable_recon_run(task_id)
    assert resumed is not None and resumed["id"] == r
    visited = db.list_visited_urls(r)
    assert visited == {targets[0], targets[1]}
    db.resume_recon_run(r, jid2)

    # simula processing dei restanti 2 (con dedup automatico)
    with db.connect() as c:
        for url in targets:
            if url in visited:
                continue  # SKIP_RESUMED
            c.execute(
                "INSERT INTO recon_visited (run_id, target_url, target_platform, "
                "visited_at) VALUES (%s, %s, 'facebook', %s) "
                "ON CONFLICT (run_id, target_url) DO NOTHING",
                (r, url, db.now_iso()),
            )

    # ora tutti e 4 sono visited; finalize done
    assert len(db.list_visited_urls(r)) == 4
    with db.connect() as c:
        c.execute(
            "UPDATE recon_runs SET status='done', finished_at=%s, target_count=%s "
            "WHERE id=%s", (db.now_iso(), 4, r),
        )
    # Non più resumable (status='done')
    assert db.find_resumable_recon_run(task_id) is None
