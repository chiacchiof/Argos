"""Calcolo dello stato dashboard di un job dal log + filesystem.

Pure-deterministic: non chiama LLM, non blocca, non muta nulla.
Estrae metriche utili a monitorare un run browser-use in tempo reale.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import db, jobs
from .config import RESULTS_DIR


# Riconoscitori sul testo del log
_RE_TS = re.compile(r"^\[(?P<ts>[^\]]+)\]\s*(?P<msg>.*)$")
_RE_SEED = re.compile(r"=== seed (?P<i>\d+)/(?P<n>\d+):\s*(?P<url>.*?)\s*===")
_RE_RUN_DIR = re.compile(r"Run dir:\s*(?P<path>.+)$")
_RE_MAX_STEPS = re.compile(r"max_steps=(\d+)")
_RE_BU_STEP = re.compile(r"Step (\d+):")
_RE_ACTION = re.compile(
    r"(Step \d+:|Clicked|Opened new tab|navigate|scroll|wait|write_file|"
    r"read_file|Plan updated|Eval:|Next goal|Final result)"
)


def _parse_iso(s: str) -> datetime | None:
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None


def _human_seconds(s: float) -> str:
    s = int(s)
    if s < 60:
        return f"{s}s"
    m, sec = divmod(s, 60)
    if m < 60:
        return f"{m}m {sec:02d}s"
    h, m = divmod(m, 60)
    return f"{h}h {m:02d}m"


_EMPTY_HEALTH: dict[str, Any] = {
    "score": None, "label": "mai eseguito", "color": "muted",
    "n_total": 0, "n_done": 0, "n_error": 0, "n_cancelled": 0,
    "last_status": None,
}


def _health_from_jobs(jobs_for_task: list[dict[str, Any]]) -> dict[str, Any]:
    n = len(jobs_for_task)
    if n == 0:
        return dict(_EMPTY_HEALTH)
    n_done = sum(1 for j in jobs_for_task if (j.get("status") or "") == "done")
    n_err = sum(1 for j in jobs_for_task if (j.get("status") or "") == "error")
    n_cancel = sum(1 for j in jobs_for_task if (j.get("status") or "") == "cancelled")
    score = int(round(100 * n_done / n))
    if score >= 80:
        label, color = "healthy", "ok"
    elif score >= 50:
        label, color = "flaky", "warn"
    else:
        label, color = "broken", "err"
    return {
        "score": score,
        "label": label,
        "color": color,
        "n_total": n,
        "n_done": n_done,
        "n_error": n_err,
        "n_cancelled": n_cancel,
        "last_status": (jobs_for_task[0].get("status") or None),
    }


def compute_task_health(task_id: int, last_n: int = 5) -> dict[str, Any]:
    """Calcola un health score 0-100 basato sugli ultimi N job del task.

    Per liste con MOLTI task usare `compute_task_health_batch(task_ids)` che
    ottiene gli stessi dati in una sola query (vs N query, pessimo su DB
    remoto).
    """
    return _health_from_jobs(db.list_jobs(task_id)[:last_n])


def compute_task_health_batch(
    task_ids: list[int], last_n: int = 5
) -> dict[int, dict[str, Any]]:
    """Versione batch di `compute_task_health`: 1 query SQL per N task.

    Su DB remoto (Neon/Azure) ogni round-trip costa 30-60 ms; sostituire
    N chiamate a `list_jobs` con una singola CTE-windowed query taglia la
    latenza di /tasks da O(N×RTT) a O(RTT). Vedi
    `db.list_recent_jobs_for_tasks`.
    """
    if not task_ids:
        return {}
    jobs_by_task = db.list_recent_jobs_for_tasks(task_ids, last_n=last_n)
    return {tid: _health_from_jobs(jobs_by_task.get(tid, [])) for tid in task_ids}


def compute_dashboard(job_id: int) -> dict[str, Any] | None:
    """Ritorna un dict con le metriche per il dashboard panel."""
    job = db.get_job(job_id)
    if not job:
        return None
    task = db.get_task(job["task_id"])

    log_text = job.get("log") or ""
    lines = log_text.splitlines()

    # parsing top-down ma conserviamo l'ULTIMO match per ogni informazione
    current_seed_idx: int | None = None
    total_seeds: int | None = None
    current_seed_url: str | None = None
    current_step: int | None = None
    max_steps: int | None = None
    run_dir_path: str | None = None
    last_ts: datetime | None = None
    first_ts: datetime | None = None
    recent_actions: list[str] = []  # le ultime N

    for raw in lines:
        m = _RE_TS.match(raw)
        if not m:
            continue
        ts = _parse_iso(m.group("ts"))
        msg = m.group("msg")
        if ts:
            last_ts = ts
            if first_ts is None:
                first_ts = ts

        ms = _RE_SEED.search(msg)
        if ms:
            current_seed_idx = int(ms.group("i"))
            total_seeds = int(ms.group("n"))
            current_seed_url = ms.group("url").strip()
            current_step = None  # reset al cambio seed

        rd = _RE_RUN_DIR.search(msg)
        if rd and run_dir_path is None:
            run_dir_path = rd.group("path").strip()

        mx = _RE_MAX_STEPS.search(msg)
        if mx:
            max_steps = int(mx.group(1))

        st = _RE_BU_STEP.search(msg)
        if st:
            current_step = int(st.group(1))

        if _RE_ACTION.search(msg):
            recent_actions.append(msg.strip())
            if len(recent_actions) > 20:
                recent_actions = recent_actions[-20:]

    # profile count = righe in profiles.jsonl consolidato
    profile_count = 0
    run_dir_name: str | None = None
    if run_dir_path:
        p = Path(run_dir_path)
        try:
            run_dir_name = p.name
        except Exception:
            run_dir_name = None
        consolidated = p / "profiles.jsonl"
        if consolidated.exists():
            try:
                profile_count = sum(
                    1
                    for line in consolidated.read_text(encoding="utf-8").splitlines()
                    if line.strip()
                )
            except Exception:
                pass

    # tempo trascorso e idle
    now = datetime.now(timezone.utc)
    elapsed_sec: float | None = None
    if first_ts:
        elapsed_sec = (now - first_ts).total_seconds()
    idle_sec: float | None = None
    if last_ts:
        idle_sec = (now - last_ts).total_seconds()

    # health
    status = job.get("status", "")
    runner_alive = jobs.is_runner_alive(job["id"])
    if status == "error":
        health = "error"
    elif status == "cancelled":
        health = "cancelled"
    elif status == "paused":
        health = "paused" if runner_alive else "dead"
    elif status == "done":
        health = "done"
    elif status in ("queued", "running"):
        if not runner_alive:
            # Il task asyncio non c'è più ma il DB dice "running" → processo morto
            health = "dead"
        elif idle_sec is not None and idle_sec > 90:
            health = "stuck"
        else:
            health = "ok"
    else:
        health = "unknown"

    is_active = status in ("queued", "running", "paused") and runner_alive

    from .agent.runner_control import MODES_SUPPORTING_PAUSE
    agent_mode = (task or {}).get("agent_mode") or ""
    return {
        "job_id": job["id"],
        "task_id": job["task_id"],
        "task_name": task["name"] if task else "",
        "agent_mode": agent_mode,
        "pause_supported": agent_mode in MODES_SUPPORTING_PAUSE,
        "status": status,
        "health": health,
        "is_active": is_active,
        "runner_alive": runner_alive,
        "control_signal": job.get("control_signal"),
        "started_at": job.get("started_at"),
        "finished_at": job.get("finished_at"),
        "elapsed_sec": elapsed_sec,
        "elapsed_human": _human_seconds(elapsed_sec) if elapsed_sec is not None else "—",
        "idle_sec": idle_sec,
        "idle_human": _human_seconds(idle_sec) if idle_sec is not None else "—",
        "current_seed_idx": current_seed_idx,
        "total_seeds": total_seeds,
        "current_seed_url": current_seed_url,
        "current_step": current_step,
        "max_steps_per_seed": max_steps,
        "step_pct": (
            int(100 * current_step / max_steps)
            if (current_step and max_steps)
            else None
        ),
        "seed_pct": (
            int(100 * (current_seed_idx - 1) / total_seeds)
            if (current_seed_idx and total_seeds)
            else None
        ),
        "profile_count": profile_count,
        "run_dir_name": run_dir_name,
        "recent_actions": recent_actions[-5:],
        "result_path": job.get("result_path"),
        "error": job.get("error"),
        "master_summary_text": _read_master_summary(job),
        "n_subjobs": _count_subjobs(job),
    }


def _read_master_summary(job: dict[str, Any]) -> str | None:
    """Legge `master_summary.md` dalla run_dir del job, se esiste.

    Generato da `app.agent.master_summary.generate_master_summary` al termine
    di un parent auto_extract con sub-job. Ritorna None se assente o non leggibile.
    """
    rp = job.get("result_path") or ""
    if not rp:
        return None
    from pathlib import Path as _P
    p = _P(rp)
    run_dir = p if p.is_dir() else p.parent
    summary = run_dir / "master_summary.md"
    if not summary.exists():
        return None
    try:
        return summary.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return None


def _count_subjobs(job: dict[str, Any]) -> int:
    """Numero di sub-job triggerati da questo job (auto_extract → site_explorer/browser_use/...)."""
    try:
        return len(db.list_subjobs(int(job["id"])))
    except Exception:
        return 0
