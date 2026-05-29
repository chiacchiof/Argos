"""Sincronizzazione cartella fisica <-> registro DB `project_files`.

In v1 NON c'e' un watcher event-driven: facciamo scansione on-demand (e al
boot dell'app come riconciliazione). Quando in v2 useremo `watchdog`, il
dispatcher chiamera' le stesse funzioni esposte qui.

Principio (design §2): il filesystem e' la fonte di verita'. Quando il DB e
il disco divergono, vince il disco.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from . import db as fdb
from . import fs

log = logging.getLogger(__name__)


def sync_project_from_disk(
    project_id: int,
    project_folder: Path,
    *,
    compute_hash: bool = True,
    actor_user_id: Any = None,
) -> dict[str, int]:
    """Reconcilia il registro DB con il filesystem.

    Per ogni file fisico: upsert in `project_files`. Per ogni record DB non
    presente fisicamente: delete. Hash ricalcolato solo se la dimensione e'
    cambiata (o `compute_hash=False` per skip totale del hashing).

    Ritorna summary: {"upserts", "deletes", "total_files", "total_bytes"}.
    """
    if not project_folder.is_dir():
        log.warning("sync_project_from_disk: cartella %s non esiste", project_folder)
        return {"upserts": 0, "deletes": 0, "total_files": 0, "total_bytes": 0}

    # Snapshot registro DB per confronto rapido.
    current = {
        r["relative_path"]: r
        for r in fdb.list_project_files(project_id)
    }

    seen: list[str] = []
    upserts = 0
    total_bytes = 0
    for info in fs.iter_project_files(project_folder):
        rel = info["relative_path"]
        seen.append(rel)
        total_bytes += info["size_bytes"]
        existing = current.get(rel)
        # Reuse hash se size invariato (cheap check). Modifiche reali in-place
        # con stessa size sono rare; quando capitano verranno colte alla prossima
        # reconciliation con compute_hash=True forzato dall'utente.
        if (
            existing
            and existing["size_bytes"] == info["size_bytes"]
            and existing.get("content_hash")
        ):
            content_hash = existing["content_hash"]
        else:
            content_hash = None
            if compute_hash:
                try:
                    content_hash = fs.compute_file_hash(info["_abs_path"])
                except OSError as exc:
                    log.warning("hash failed for %s: %s", info["_abs_path"], exc)
        fdb.upsert_project_file(
            project_id=project_id,
            relative_path=rel,
            name=info["name"],
            size_bytes=info["size_bytes"],
            content_hash=content_hash,
            mime_type=info["mime_type"],
            mtime=info["mtime"],
            added_by_user_id=actor_user_id,
        )
        upserts += 1

    deletes = fdb.delete_project_files_not_in(project_id, seen)
    return {
        "upserts": upserts,
        "deletes": deletes,
        "total_files": len(seen),
        "total_bytes": total_bytes,
    }


def locate_project_folder(root_project_path: Path | None, folder_uuid) -> Path | None:
    """Cerca la cartella che ha `.argos/manifest.json` con `argos_project_uuid == folder_uuid`,
    tra le sottocartelle dirette di `root_project_path`. None se non trovata.

    `folder_uuid` accetta `str` o `uuid.UUID` (i record postgres arrivano come UUID).
    """
    if not root_project_path:
        return None
    return fs.find_project_folder_by_uuid(Path(root_project_path), folder_uuid)


def annotate_projects_with_local_state(
    projects: list[dict[str, Any]],
    root_project_path: Path | None,
) -> list[dict[str, Any]]:
    """Per ogni progetto in lista, aggiunge:
      - `local_folder`: str path o None
      - `local_state`: 'completo' | 'monco'

    `monco` = nessuna cartella locale corrispondente all'UUID.
    Se `root_project_path` None / inesistente, tutti i progetti risultano 'monco'.
    """
    out = []
    for p in projects:
        folder = locate_project_folder(root_project_path, p["folder_uuid"]) if root_project_path else None
        annotated = dict(p)
        annotated["local_folder"] = str(folder) if folder else None
        annotated["local_state"] = "completo" if folder else "monco"
        out.append(annotated)
    return out
