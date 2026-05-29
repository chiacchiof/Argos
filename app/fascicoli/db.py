"""CRUD per fascicoli (projects, project_users, project_files).

Tutte le funzioni sono tenant-scoped seguendo il pattern di `app/db.py`:
- `tenant_id: Any = _UNSET` legge dal ContextVar settato dal middleware HTTP.
- `None` = no filter (super_admin).
- `int` = WHERE tenant_id = %s (isolamento per tenant_user/architect).

Regola di visibilita' di un progetto verso un utente del tenant:
  - visibility = 'tenant'  -> tutti gli utenti del tenant
  - visibility = 'user'    -> owner_user_id == user OR riga in project_users

`architect_view=True` sui list/get bypassa il filtro di visibilita' (l'architect
ha "supervisione architetturale" su tutto il tenant; vedi design doc §7).
"""
from __future__ import annotations

import uuid as uuid_lib
from datetime import datetime
from typing import Any, Iterable

from ..db import _UNSET, _resolve_tenant, _resolve_user, connect


# ---------------------------------------------------------------------------
# Projects: CRUD
# ---------------------------------------------------------------------------

def _visibility_clause_for_user(user_id: int) -> tuple[str, list[Any]]:
    """Sub-clause SQL da aggiungere in WHERE per filtrare i progetti visibili a `user_id`.

    Restituisce (clause, args) dove `clause` referenzia `p.id` / `p.visibility` /
    `p.owner_user_id`. Il caller la appende con AND.
    """
    clause = (
        "(p.visibility = 'tenant' "
        " OR p.owner_user_id = %s "
        " OR EXISTS (SELECT 1 FROM project_users pu "
        "            WHERE pu.project_id = p.id AND pu.user_id = %s))"
    )
    return clause, [user_id, user_id]


def list_projects(
    *,
    tenant_id: Any = _UNSET,
    current_user_id: Any = _UNSET,
    include_archived: bool = False,
    architect_view: bool = False,
) -> list[dict[str, Any]]:
    """Lista progetti visibili.

    Aggiunge per ciascuna riga:
      - owner_email / owner_first_name / owner_last_name (JOIN su users)
      - n_files (count da project_files)
      - size_bytes (sum size_bytes da project_files)
    """
    tenant_id = _resolve_tenant(tenant_id)
    user_id = _resolve_user(current_user_id)

    clauses: list[str] = []
    args: list[Any] = []

    if tenant_id is not None:
        clauses.append("p.tenant_id = %s")
        args.append(tenant_id)
    if not include_archived:
        clauses.append("p.is_archived = FALSE")
    if not architect_view and user_id is not None:
        clause, vargs = _visibility_clause_for_user(user_id)
        clauses.append(clause)
        args.extend(vargs)

    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    sql = (
        "SELECT p.*, "
        "       u.email      AS owner_email, "
        "       u.first_name AS owner_first_name, "
        "       u.last_name  AS owner_last_name, "
        "       (SELECT COUNT(*) FROM project_files pf "
        "        WHERE pf.project_id = p.id) AS n_files, "
        "       (SELECT COALESCE(SUM(pf.size_bytes), 0) FROM project_files pf "
        "        WHERE pf.project_id = p.id) AS size_bytes "
        "FROM projects p "
        "LEFT JOIN users u ON u.id = p.owner_user_id "
        f"{where} "
        "ORDER BY p.updated_at DESC, p.id DESC"
    )
    with connect() as con:
        rows = con.execute(sql, tuple(args)).fetchall()
    return [dict(r) for r in rows]


def get_project(
    project_id: int,
    *,
    tenant_id: Any = _UNSET,
    current_user_id: Any = _UNSET,
    architect_view: bool = False,
) -> dict[str, Any] | None:
    tenant_id = _resolve_tenant(tenant_id)
    user_id = _resolve_user(current_user_id)

    clauses = ["p.id = %s"]
    args: list[Any] = [project_id]
    if tenant_id is not None:
        clauses.append("p.tenant_id = %s")
        args.append(tenant_id)
    if not architect_view and user_id is not None:
        clause, vargs = _visibility_clause_for_user(user_id)
        clauses.append(clause)
        args.extend(vargs)

    sql = (
        "SELECT p.*, "
        "       u.email      AS owner_email, "
        "       u.first_name AS owner_first_name, "
        "       u.last_name  AS owner_last_name "
        "FROM projects p "
        "LEFT JOIN users u ON u.id = p.owner_user_id "
        f"WHERE {' AND '.join(clauses)}"
    )
    with connect() as con:
        row = con.execute(sql, tuple(args)).fetchone()
    return dict(row) if row else None


def get_project_by_folder_uuid(folder_uuid: str) -> dict[str, Any] | None:
    """Lookup diretto via UUID, senza filtro tenant. Usato dal discovery locale
    per associare cartelle fisiche al loro record. Il chiamante e' responsabile
    di applicare il filtro di visibilita' se necessario."""
    with connect() as con:
        row = con.execute(
            "SELECT p.*, u.email AS owner_email "
            "FROM projects p LEFT JOIN users u ON u.id = p.owner_user_id "
            "WHERE p.folder_uuid = %s",
            (folder_uuid,),
        ).fetchone()
    return dict(row) if row else None


def create_project(
    *,
    title: str,
    description: str | None = None,
    visibility: str = "tenant",
    folder_uuid: str | None = None,
    tenant_id: Any = _UNSET,
    owner_user_id: Any = _UNSET,
) -> int:
    """INSERT progetto. Ritorna l'id del nuovo record.
    Se `folder_uuid` e' None, ne genera uno nuovo.
    """
    if visibility not in ("tenant", "user"):
        raise ValueError(f"visibility non valida: {visibility}")
    tenant_id = _resolve_tenant(tenant_id)
    owner = _resolve_user(owner_user_id)
    if tenant_id is None:
        raise ValueError("create_project richiede un tenant_id (super_admin non puo' creare).")
    if owner is None:
        raise ValueError("create_project richiede un owner_user_id.")
    fuuid = folder_uuid or str(uuid_lib.uuid4())
    with connect() as con:
        row = con.execute(
            "INSERT INTO projects "
            "  (tenant_id, owner_user_id, folder_uuid, title, description, visibility) "
            "VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
            (
                tenant_id,
                owner,
                fuuid,
                title.strip(),
                (description or "").strip() or None,
                visibility,
            ),
        ).fetchone()
        con.commit()
        return int(row["id"])


def update_project(
    project_id: int,
    *,
    title: str | None = None,
    description: str | None = None,
    visibility: str | None = None,
    owner_user_id: int | None = None,
    tenant_id: Any = _UNSET,
) -> None:
    tenant_id = _resolve_tenant(tenant_id)
    fields: list[str] = []
    args: list[Any] = []
    if title is not None:
        fields.append("title = %s")
        args.append(title.strip())
    if description is not None:
        fields.append("description = %s")
        args.append((description or "").strip() or None)
    if visibility is not None:
        if visibility not in ("tenant", "user"):
            raise ValueError(f"visibility non valida: {visibility}")
        fields.append("visibility = %s")
        args.append(visibility)
    if owner_user_id is not None:
        fields.append("owner_user_id = %s")
        args.append(int(owner_user_id))
    if not fields:
        return
    fields.append("updated_at = NOW()")
    where_args: list[Any] = [project_id]
    where = "id = %s"
    if tenant_id is not None:
        where += " AND tenant_id = %s"
        where_args.append(tenant_id)
    sql = f"UPDATE projects SET {', '.join(fields)} WHERE {where}"
    with connect() as con:
        con.execute(sql, tuple(args) + tuple(where_args))
        con.commit()


def set_project_archived(project_id: int, archived: bool, *, tenant_id: Any = _UNSET) -> None:
    """Soft delete: marca is_archived. Cartella fisica e .argos/ non vengono toccati."""
    tenant_id = _resolve_tenant(tenant_id)
    where_args: list[Any] = [archived, project_id]
    where = "id = %s"
    if tenant_id is not None:
        where += " AND tenant_id = %s"
        where_args.append(tenant_id)
    with connect() as con:
        con.execute(
            f"UPDATE projects SET is_archived = %s, updated_at = NOW() WHERE {where}",
            tuple(where_args),
        )
        con.commit()


def delete_project(project_id: int, *, tenant_id: Any = _UNSET) -> bool:
    """Hard delete del progetto.

    CASCADE pulisce in automatico `project_files`, `project_users`,
    `project_chat_messages`. La cartella fisica sul disco NON viene toccata
    da questa funzione: se vuoi cancellarla, fallo a livello route con
    `fascicoli.fs.delete_project_folder` PRIMA di chiamare delete_project.

    Ritorna True se ha cancellato qualcosa, False se progetto non trovato
    o fuori dal tenant.
    """
    tenant_id = _resolve_tenant(tenant_id)
    where = "id = %s"
    args: list[Any] = [project_id]
    if tenant_id is not None:
        where += " AND tenant_id = %s"
        args.append(tenant_id)
    with connect() as con:
        cur = con.execute(f"DELETE FROM projects WHERE {where}", tuple(args))
        con.commit()
        rc = cur.rowcount if cur.rowcount is not None else 0
    return rc > 0


# ---------------------------------------------------------------------------
# Project members (ACL per User-Use)
# ---------------------------------------------------------------------------

def list_project_members(project_id: int) -> list[dict[str, Any]]:
    """Membri espliciti (non include l'owner)."""
    with connect() as con:
        rows = con.execute(
            "SELECT pu.user_id, pu.role, pu.added_at, "
            "       u.email, u.first_name, u.last_name "
            "FROM project_users pu "
            "JOIN users u ON u.id = pu.user_id "
            "WHERE pu.project_id = %s "
            "ORDER BY pu.added_at",
            (project_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def add_project_member(project_id: int, user_id: int, role: str = "viewer") -> None:
    if role not in ("viewer", "editor"):
        raise ValueError(f"role non valido: {role}")
    with connect() as con:
        con.execute(
            "INSERT INTO project_users (project_id, user_id, role) "
            "VALUES (%s, %s, %s) "
            "ON CONFLICT (project_id, user_id) DO UPDATE SET role = EXCLUDED.role",
            (project_id, user_id, role),
        )
        con.commit()


def remove_project_member(project_id: int, user_id: int) -> None:
    with connect() as con:
        con.execute(
            "DELETE FROM project_users WHERE project_id = %s AND user_id = %s",
            (project_id, user_id),
        )
        con.commit()


# ---------------------------------------------------------------------------
# Project files (registro metadati — mai contenuto)
# ---------------------------------------------------------------------------

def list_project_files(project_id: int) -> list[dict[str, Any]]:
    with connect() as con:
        rows = con.execute(
            "SELECT * FROM project_files WHERE project_id = %s "
            "ORDER BY relative_path",
            (project_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def upsert_project_file(
    *,
    project_id: int,
    relative_path: str,
    name: str,
    size_bytes: int,
    content_hash: str | None,
    mime_type: str | None,
    mtime: datetime | None,
    added_by_user_id: Any = _UNSET,
) -> None:
    """INSERT-or-UPDATE su (project_id, relative_path)."""
    added_by = _resolve_user(added_by_user_id)
    with connect() as con:
        con.execute(
            "INSERT INTO project_files "
            "  (project_id, relative_path, name, size_bytes, content_hash, "
            "   mime_type, mtime, added_by_user_id) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (project_id, relative_path) DO UPDATE SET "
            "  name = EXCLUDED.name, "
            "  size_bytes = EXCLUDED.size_bytes, "
            "  content_hash = EXCLUDED.content_hash, "
            "  mime_type = EXCLUDED.mime_type, "
            "  mtime = EXCLUDED.mtime",
            (
                project_id, relative_path, name, size_bytes, content_hash,
                mime_type, mtime, added_by,
            ),
        )
        con.commit()


def delete_project_file(project_id: int, relative_path: str) -> None:
    with connect() as con:
        con.execute(
            "DELETE FROM project_files WHERE project_id = %s AND relative_path = %s",
            (project_id, relative_path),
        )
        con.commit()


def delete_project_files_not_in(project_id: int, kept_paths: Iterable[str]) -> int:
    """Elimina dal registro tutti i file del progetto NON nella lista `kept_paths`.
    Ritorna il numero di righe eliminate. Usato dalla reconciliation."""
    kept = list(set(kept_paths))
    with connect() as con:
        if not kept:
            cur = con.execute(
                "DELETE FROM project_files WHERE project_id = %s",
                (project_id,),
            )
        else:
            placeholders = ",".join(["%s"] * len(kept))
            cur = con.execute(
                f"DELETE FROM project_files "
                f"WHERE project_id = %s AND relative_path NOT IN ({placeholders})",
                (project_id, *kept),
            )
        con.commit()
        return cur.rowcount if cur.rowcount is not None else 0


def get_project_size_bytes(project_id: int) -> int:
    with connect() as con:
        row = con.execute(
            "SELECT COALESCE(SUM(size_bytes), 0) AS total "
            "FROM project_files WHERE project_id = %s",
            (project_id,),
        ).fetchone()
    return int(row["total"]) if row else 0


def mark_file_indexed(project_id: int, relative_path: str) -> None:
    """Aggiorna last_indexed_at = NOW() per il file dato.
    Usato quando il pipeline RAG completa l'embedding del file."""
    with connect() as con:
        con.execute(
            "UPDATE project_files SET last_indexed_at = NOW() "
            "WHERE project_id = %s AND relative_path = %s",
            (project_id, relative_path),
        )
        con.commit()


# ---------------------------------------------------------------------------
# Chat history (RAG Q&A su fascicolo) — v2
# ---------------------------------------------------------------------------

def list_chat_messages(project_id: int, limit: int = 200) -> list[dict[str, Any]]:
    """Cronologia chat per il progetto, oldest first. Parsa `citations` (JSON)."""
    import json as _json
    with connect() as con:
        rows = con.execute(
            "SELECT id, role, content, citations, created_at, user_id "
            "FROM project_chat_messages "
            "WHERE project_id = %s "
            "ORDER BY id "
            "LIMIT %s",
            (project_id, limit),
        ).fetchall()
    out: list[dict[str, Any]] = []
    for r in rows:
        d = dict(r)
        raw = d.get("citations")
        if raw:
            try:
                d["citations"] = _json.loads(raw)
            except (ValueError, TypeError):
                d["citations"] = []
        else:
            d["citations"] = []
        out.append(d)
    return out


def add_chat_message(
    project_id: int,
    *,
    role: str,
    content: str,
    user_id: int | None = None,
    citations: list[dict] | None = None,
) -> int:
    """Inserisce un messaggio nella cronologia chat del progetto."""
    import json as _json
    if role not in ("user", "assistant", "system"):
        raise ValueError(f"role non valido: {role}")
    cit_json = _json.dumps(citations) if citations else None
    with connect() as con:
        row = con.execute(
            "INSERT INTO project_chat_messages "
            "  (project_id, user_id, role, content, citations) "
            "VALUES (%s, %s, %s, %s, %s) RETURNING id",
            (project_id, user_id, role, content, cit_json),
        ).fetchone()
        con.commit()
        return int(row["id"])


def clear_chat_messages(project_id: int) -> None:
    """Pulisce la cronologia chat del progetto (l'indice embeddings resta)."""
    with connect() as con:
        con.execute(
            "DELETE FROM project_chat_messages WHERE project_id = %s",
            (project_id,),
        )
        con.commit()
