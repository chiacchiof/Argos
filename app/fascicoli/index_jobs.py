"""Gestione background dell'indicizzazione fascicoli.

Storage in-memory di processo (`_JOBS: dict[project_id -> status]`) + worker
thread che chiama `rag.index_project` con callback di progresso.

Se uvicorn restarta, lo stato in memoria si perde — gli embedding gia' scritti
sul disco restano (sono persistenti in `.argos/embeddings.json`). L'utente
puo' semplicemente rilanciare l'indicizzazione.

In v1 supportiamo un solo job concorrente per progetto (il secondo POST viene
ignorato finche' il primo non termina). Non c'e' coda globale: thread pool
dell'OS gestisce il resto.
"""
from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger(__name__)

_LOCK = threading.Lock()
_JOBS: dict[int, dict[str, Any]] = {}


def get_status(project_id: int) -> dict[str, Any] | None:
    """Snapshot dello stato del job di indicizzazione per `project_id`.
    None se non c'e' mai stato un job."""
    with _LOCK:
        s = _JOBS.get(project_id)
        return dict(s) if s else None


def is_running(project_id: int) -> bool:
    with _LOCK:
        s = _JOBS.get(project_id)
        return bool(s and s.get("status") == "running")


def clear(project_id: int) -> None:
    """Cancella lo stato (es. quando l'utente clicca 'reset')."""
    with _LOCK:
        _JOBS.pop(project_id, None)


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def start(project_id: int, folder: Path, *, actor_user_id: int | None = None) -> bool:
    """Avvia il job di indicizzazione in un thread daemon.

    Ritorna True se il job e' stato avviato, False se ce n'era gia' uno in
    corso per lo stesso progetto.
    """
    with _LOCK:
        cur = _JOBS.get(project_id)
        if cur and cur.get("status") == "running":
            return False
        _JOBS[project_id] = {
            "status": "running",
            "started_at": _now_iso(),
            "finished_at": None,
            "phase": "init",
            "current_file": None,
            "files_done": 0,
            "files_total": 0,
            "chunks_done": 0,
            "chunks_total": 0,
            "message": "Avvio…",
            "error": None,
            "summary": None,
        }

    def _update(**kw: Any) -> None:
        with _LOCK:
            cur = _JOBS.get(project_id)
            if cur is not None:
                cur.update(kw)

    def _worker() -> None:
        from . import rag as frag  # late import per evitare cicli a import-time
        try:
            summary = frag.index_project(
                project_id, folder, progress=_update,
            )
            msg = (
                f"Indicizzato {summary['chunks']} chunk da "
                f"{summary['files_processed']} file"
            )
            if summary.get("skipped_unsupported"):
                msg += f" · {summary['skipped_unsupported']} skip non supportati"
            if summary.get("skipped_empty"):
                msg += f" · {summary['skipped_empty']} skip vuoti"
            _update(
                status="done",
                phase="done",
                message=msg,
                summary=summary,
                finished_at=_now_iso(),
            )
        except Exception as exc:
            log.exception("index job failed for project=%s", project_id)
            _update(
                status="error",
                phase="error",
                error=str(exc),
                message=f"Errore: {exc}",
                finished_at=_now_iso(),
            )

    t = threading.Thread(target=_worker, daemon=True, name=f"argos-idx-{project_id}")
    t.start()
    log.info("started index job for project=%s by user=%s", project_id, actor_user_id)
    return True
