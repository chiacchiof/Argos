"""Watchdog per workflow_run.

Esecuzione: poll ogni N secondi. Exit code:
  0  = workflow ancora running, nessun problema
  10 = workflow terminato naturalmente (tutti job done/cancelled/error per cause naturali)
  20 = trigger di stop attivato (crash, stallo, o yield in caduta)

Quando il trigger scatta, invia control_signal='stop' a tutti i job running
correlati e scrive in data/results/_watchdog.log il motivo.

Lo stato per detect "stallo" (log non cresce) e' persistito in
data/results/_watchdog_state.json.
"""
from __future__ import annotations

import json
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = Path("data/agentscraper.db")
STATE_PATH = Path("data/results/_watchdog_state.json")
LOG_PATH = Path("data/results/_watchdog.log")

WORKFLOW_RUN_ID = int(sys.argv[1]) if len(sys.argv) > 1 else 16
MIN_JOB_ID = int(sys.argv[2]) if len(sys.argv) > 2 else 46  # primo job del run
STALLO_CYCLES = 2     # 2 cicli consecutivi senza cambio log = stallo
YIELD_FAIL_THRESHOLD = 50  # ⛔ consecutivi senza ✅


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def append_watchdog_log(line: str) -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(f"[{now_iso()}] {line}\n")


def load_state() -> dict:
    if not STATE_PATH.exists():
        return {}
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=2), encoding="utf-8")


def get_workflow_jobs() -> list[dict]:
    """Tutti i job correlati al workflow_run (main + sub-jobs creati da auto_extract).

    Catch-all: prendo OGNI job con id >= MIN_JOB_ID che soddisfi una di queste:
    - workflow_run_id == WORKFLOW_RUN_ID (job lanciati direttamente dal workflow)
    - workflow_run_id IS NULL (sub-job di auto_extract: ereditano task_id ma non
      workflow_run_id, perche' sono lanciati da `_execute_strategy` come standalone)

    Versione precedente filtrava per task_id IN (to_task_id ...), il che escludeva
    i sub-job del task root (`from_task_id`). Bug che generava falsi stalli.
    """
    with sqlite3.connect(DB_PATH) as con:
        con.row_factory = sqlite3.Row
        rows = con.execute(
            "SELECT id, task_id, status, log, error, workflow_run_id FROM jobs "
            "WHERE id >= ? AND (workflow_run_id = ? OR workflow_run_id IS NULL) "
            "ORDER BY id",
            (MIN_JOB_ID, WORKFLOW_RUN_ID),
        ).fetchall()
        return [dict(r) for r in rows]


def count_consecutive_fails(log: str) -> int:
    """Conta ⛔ consecutivi senza ✅ nella coda del log (ultime 1000 righe)."""
    if not log:
        return 0
    lines = log.splitlines()[-1000:]
    consec = 0
    for ln in reversed(lines):
        if "✅" in ln and "[runner" in ln:
            return 0  # ✅ recente azzera il contatore
        if "⛔" in ln and "[runner" in ln:
            consec += 1
    return consec


def send_stop_to_running(jobs: list[dict], reason: str) -> list[int]:
    """Invia control_signal='stop' a tutti i job running. Ritorna lista id."""
    stopped: list[int] = []
    with sqlite3.connect(DB_PATH) as con:
        for j in jobs:
            if j["status"] == "running":
                con.execute(
                    "UPDATE jobs SET control_signal = 'stop' WHERE id = ?",
                    (j["id"],),
                )
                stopped.append(j["id"])
        con.commit()
    append_watchdog_log(f"STOP signal inviato a {len(stopped)} job: {stopped}. Motivo: {reason}")
    return stopped


def main() -> int:
    state = load_state()
    jobs = get_workflow_jobs()
    if not jobs:
        append_watchdog_log("Nessun job trovato per workflow_run %d (MIN_JOB_ID %d). Exit." % (WORKFLOW_RUN_ID, MIN_JOB_ID))
        return 10

    running = [j for j in jobs if j["status"] == "running"]
    statuses = {j["status"]: 0 for j in jobs}
    for j in jobs:
        statuses[j["status"]] = statuses.get(j["status"], 0) + 1

    # === Trigger 1: crash ===
    errored = [j for j in jobs if j["status"] == "error"]
    if errored:
        msg = f"CRASH detectato: job {[j['id'] for j in errored]} in status=error. Errori: " + \
              "; ".join((j.get("error") or "")[:120] for j in errored)
        send_stop_to_running(jobs, msg)
        return 20

    # === Trigger 3: yield in caduta (50 ⛔ consecutivi senza ✅) ===
    for j in running:
        consec = count_consecutive_fails(j.get("log") or "")
        if consec >= YIELD_FAIL_THRESHOLD:
            msg = f"YIELD FAIL: job {j['id']} ha {consec} extract falliti consecutivi (soglia {YIELD_FAIL_THRESHOLD})"
            send_stop_to_running(jobs, msg)
            return 20

    # === Trigger 2: stallo (log AGGREGATO del workflow non cresce per 2 cicli) ===
    # Il parent auto_extract aspetta i sub-job senza scrivere log proprio, quindi
    # valutare i job singolarmente genera falsi positivi (un sub-job browser_use
    # da 33 min appare come "parent fermo"). Soluzione: aggrego le righe di TUTTI
    # i job correlati al workflow_run. Lo stallo scatta solo se nessuno di loro
    # produce nuovo output per 2 cicli.
    total_log_lines = sum(len((j.get("log") or "").splitlines()) for j in jobs)
    prev_total = state.get("_total_log_lines", -1)
    consec_no_change = state.get("_consec_no_change", 0)
    if total_log_lines == prev_total:
        consec_no_change += 1
    else:
        consec_no_change = 0
    new_state = {
        "_total_log_lines": total_log_lines,
        "_consec_no_change": consec_no_change,
        "_ts": now_iso(),
    }
    if consec_no_change >= STALLO_CYCLES:
        msg = (f"STALLO: log aggregato workflow fermo a {total_log_lines} righe "
               f"per {consec_no_change} cicli consecutivi (~{consec_no_change*10} min). "
               f"Running: {[j['id'] for j in running]}")
        send_stop_to_running(jobs, msg)
        return 20
    save_state(new_state)

    # Verifica se workflow e' finito naturalmente (nessun job running)
    if not running:
        append_watchdog_log(
            f"Workflow run {WORKFLOW_RUN_ID} terminato naturalmente. Jobs status: {statuses}"
        )
        return 10

    # Log periodico stato (per audit)
    summary = ", ".join(f"{s}:{c}" for s, c in statuses.items())
    append_watchdog_log(f"Polling OK. Workflow run {WORKFLOW_RUN_ID} jobs: {summary}. Running: {[j['id'] for j in running]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
