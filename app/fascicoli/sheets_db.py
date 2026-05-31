"""CRUD + storage realtime per i Fogli collaborativi.

Tabelle (vedi `app/db.py` SCHEMA_SQL):
  - project_sheets           : metadati foglio (tenant-scoped, project_id NULLABLE)
  - project_sheet_cells      : stato corrente celle (upsert per patch)
  - project_sheet_revisions  : log append-only delle patch (recupero reconnect/audit)

Convenzioni identiche a `app/fascicoli/db.py`:
  - `tenant_id: Any = _UNSET` legge dal ContextVar settato dal middleware HTTP.
    NB: il WebSocket handler e il listener Redis girano FUORI dal middleware HTTP,
    quindi DEVONO passare `tenant_id` esplicito (o settare il ContextVar a mano).
  - %s placeholders psycopg; `with connect() as con: ... con.commit()`.

Sorgente di verita' = Postgres. Redis (se attivo) e' solo bus realtime: la
sequenza delle revisioni vive in project_sheet_revisions, non in Redis.
"""
from __future__ import annotations

import json
from typing import Any

from psycopg.types.json import Json

from ..db import _UNSET, _resolve_tenant, _resolve_user, connect


# ---------------------------------------------------------------------------
# Limiti di validazione (MVP) — vedi piano §Validazione Payload
# ---------------------------------------------------------------------------
MAX_ROWS = 1000           # righe massime per foglio
MAX_COLS = 100            # colonne massime per foglio
MAX_CELLS_PER_PATCH = 500  # celle massime in una singola patch
MAX_VALUE_LEN = 20_000     # 20 KB per valore cella
MAX_STYLE_LEN = 4_000      # JSON serializzato dello stile cella
DEFAULT_ROWS = 100
DEFAULT_COLS = 26


class SheetValidationError(ValueError):
    """Payload patch non valido (indici fuori range, troppe celle, valore troppo
    grande, ...). Il chiamante traduce in HTTP 400 / WS error 'bad_request'."""


class SheetForbidden(PermissionError):
    """Il foglio non esiste nel tenant indicato (tenant mismatch)."""


# ---------------------------------------------------------------------------
# Visibilita'
# ---------------------------------------------------------------------------

def _sheet_visibility_clause(user_id: int) -> tuple[str, list[Any]]:
    """Sub-clause WHERE per i fogli visibili a `user_id` (non-architect).

    Un foglio e' visibile se:
      - standalone (project_id NULL) e visibility='tenant' OPPURE creato dall'utente
      - agganciato a un progetto VISIBILE all'utente (stessa regola dei fascicoli)
    Referenzia l'alias `s` per project_sheets."""
    clause = (
        "( (s.project_id IS NULL AND (s.visibility = 'tenant' OR s.created_by_user_id = %s)) "
        "  OR (s.project_id IS NOT NULL AND EXISTS ( "
        "        SELECT 1 FROM projects p WHERE p.id = s.project_id "
        "          AND (p.visibility = 'tenant' OR p.owner_user_id = %s "
        "               OR EXISTS (SELECT 1 FROM project_users pu "
        "                          WHERE pu.project_id = p.id AND pu.user_id = %s)))) "
        "  OR EXISTS (SELECT 1 FROM project_sheet_users su "
        "             WHERE su.sheet_id = s.id AND su.user_id = %s) )"
    )
    return clause, [user_id, user_id, user_id, user_id]


# ---------------------------------------------------------------------------
# CRUD foglio
# ---------------------------------------------------------------------------

def create_sheet(
    *,
    title: str,
    project_id: int | None = None,
    visibility: str = "tenant",
    tenant_role: str = "editor",
    n_rows: int = DEFAULT_ROWS,
    n_cols: int = DEFAULT_COLS,
    tenant_id: Any = _UNSET,
    created_by_user_id: Any = _UNSET,
) -> int:
    """INSERT foglio. Ritorna l'id. Richiede un tenant (super_admin non crea)."""
    if visibility not in ("tenant", "user"):
        raise ValueError(f"visibility non valida: {visibility}")
    if tenant_role not in ("viewer", "editor"):
        raise ValueError(f"tenant_role non valido: {tenant_role}")
    title = (title or "").strip() or "Nuovo foglio"
    n_rows = max(1, min(int(n_rows), MAX_ROWS))
    n_cols = max(1, min(int(n_cols), MAX_COLS))
    tid = _resolve_tenant(tenant_id)
    uid = _resolve_user(created_by_user_id)
    if tid is None:
        raise ValueError("create_sheet richiede un tenant_id (super_admin non puo' creare).")
    with connect() as con:
        # Se project_id e' indicato, verifica che il progetto sia dello stesso
        # tenant (no aggancio cross-tenant).
        if project_id is not None:
            prow = con.execute(
                "SELECT 1 FROM projects WHERE id = %s AND tenant_id = %s",
                (project_id, tid),
            ).fetchone()
            if prow is None:
                raise SheetForbidden("project_id non appartiene al tenant.")
        row = con.execute(
            "INSERT INTO project_sheets "
            "  (tenant_id, project_id, title, visibility, tenant_role, created_by_user_id, n_rows, n_cols) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
            (tid, project_id, title, visibility, tenant_role, uid, n_rows, n_cols),
        ).fetchone()
        con.commit()
        return int(row["id"])


def list_sheets(
    *,
    tenant_id: Any = _UNSET,
    current_user_id: Any = _UNSET,
    project_id: int | None = None,
    only_project: bool = False,
    include_archived: bool = False,
    architect_view: bool = False,
) -> list[dict[str, Any]]:
    """Lista fogli visibili, piu' recenti prima.

    - `project_id` + `only_project=True` -> solo i fogli di quel fascicolo.
    - `project_id=None` + `only_project=False` -> tutti i fogli visibili del tenant
      (standalone + agganciati).
    Aggiunge `project_title` (JOIN) e `creator_email`.
    """
    tid = _resolve_tenant(tenant_id)
    uid = _resolve_user(current_user_id)

    clauses: list[str] = []
    args: list[Any] = []
    if tid is not None:
        clauses.append("s.tenant_id = %s")
        args.append(tid)
    if not include_archived:
        clauses.append("s.is_archived = FALSE")
    if only_project:
        clauses.append("s.project_id = %s")
        args.append(project_id)
    if not architect_view and uid is not None:
        clause, vargs = _sheet_visibility_clause(uid)
        clauses.append(clause)
        args.extend(vargs)

    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    sql = (
        "SELECT s.*, p.title AS project_title, u.email AS creator_email "
        "FROM project_sheets s "
        "LEFT JOIN projects p ON p.id = s.project_id "
        "LEFT JOIN users u ON u.id = s.created_by_user_id "
        f"{where} "
        "ORDER BY s.updated_at DESC, s.id DESC"
    )
    with connect() as con:
        rows = con.execute(sql, tuple(args)).fetchall()
    return [dict(r) for r in rows]


def get_sheet(
    sheet_id: int,
    *,
    tenant_id: Any = _UNSET,
    current_user_id: Any = _UNSET,
    architect_view: bool = False,
) -> dict[str, Any] | None:
    """Carica un foglio (tenant-scoped + filtro visibilita' salvo architect_view).
    Ritorna None se non esiste / non accessibile."""
    tid = _resolve_tenant(tenant_id)
    uid = _resolve_user(current_user_id)
    clauses = ["s.id = %s"]
    args: list[Any] = [sheet_id]
    if tid is not None:
        clauses.append("s.tenant_id = %s")
        args.append(tid)
    if not architect_view and uid is not None:
        clause, vargs = _sheet_visibility_clause(uid)
        clauses.append(clause)
        args.extend(vargs)
    sql = (
        "SELECT s.*, p.title AS project_title, u.email AS creator_email "
        "FROM project_sheets s "
        "LEFT JOIN projects p ON p.id = s.project_id "
        "LEFT JOIN users u ON u.id = s.created_by_user_id "
        f"WHERE {' AND '.join(clauses)}"
    )
    with connect() as con:
        row = con.execute(sql, tuple(args)).fetchone()
    return dict(row) if row else None


def rename_sheet(sheet_id: int, title: str, *, tenant_id: Any = _UNSET) -> None:
    tid = _resolve_tenant(tenant_id)
    title = (title or "").strip()
    if not title:
        raise ValueError("Titolo vuoto.")
    where = "id = %s"
    args: list[Any] = [title, sheet_id]
    if tid is not None:
        where += " AND tenant_id = %s"
        args.append(tid)
    with connect() as con:
        con.execute(
            f"UPDATE project_sheets SET title = %s, updated_at = NOW() WHERE {where}",
            tuple(args),
        )
        con.commit()


def set_sheet_access(sheet_id: int, visibility: str, tenant_role: str = "editor", *, tenant_id: Any = _UNSET) -> None:
    """Imposta visibilita' + ruolo di default del tenant (quando visibility='tenant':
    'editor' = tutti modificano, 'viewer' = tutti in sola lettura)."""
    if visibility not in ("tenant", "user"):
        raise ValueError(f"visibility non valida: {visibility}")
    if tenant_role not in ("viewer", "editor"):
        raise ValueError(f"tenant_role non valido: {tenant_role}")
    tid = _resolve_tenant(tenant_id)
    where = "id = %s"
    args: list[Any] = [visibility, tenant_role, sheet_id]
    if tid is not None:
        where += " AND tenant_id = %s"
        args.append(tid)
    with connect() as con:
        con.execute(
            f"UPDATE project_sheets SET visibility = %s, tenant_role = %s, updated_at = NOW() WHERE {where}",
            tuple(args),
        )
        con.commit()


def set_sheet_visibility(sheet_id: int, visibility: str, *, tenant_id: Any = _UNSET) -> None:
    """Compat: imposta solo la visibilita' (lascia tenant_role invariato)."""
    if visibility not in ("tenant", "user"):
        raise ValueError(f"visibility non valida: {visibility}")
    tid = _resolve_tenant(tenant_id)
    where = "id = %s"
    args: list[Any] = [visibility, sheet_id]
    if tid is not None:
        where += " AND tenant_id = %s"
        args.append(tid)
    with connect() as con:
        con.execute(
            f"UPDATE project_sheets SET visibility = %s, updated_at = NOW() WHERE {where}",
            tuple(args),
        )
        con.commit()


def set_sheet_archived(sheet_id: int, archived: bool, *, tenant_id: Any = _UNSET) -> None:
    tid = _resolve_tenant(tenant_id)
    where = "id = %s"
    args: list[Any] = [archived, sheet_id]
    if tid is not None:
        where += " AND tenant_id = %s"
        args.append(tid)
    with connect() as con:
        con.execute(
            f"UPDATE project_sheets SET is_archived = %s, updated_at = NOW() WHERE {where}",
            tuple(args),
        )
        con.commit()


def delete_sheet(sheet_id: int, *, tenant_id: Any = _UNSET) -> bool:
    """Hard delete. CASCADE pulisce cells + revisions."""
    tid = _resolve_tenant(tenant_id)
    where = "id = %s"
    args: list[Any] = [sheet_id]
    if tid is not None:
        where += " AND tenant_id = %s"
        args.append(tid)
    with connect() as con:
        cur = con.execute(f"DELETE FROM project_sheets WHERE {where}", tuple(args))
        con.commit()
        return (cur.rowcount or 0) > 0


# ---------------------------------------------------------------------------
# Condivisione: membri del foglio (ACL per-utente)
# ---------------------------------------------------------------------------

def _sheet_in_tenant(con, sheet_id: int, tenant_id: int | None) -> bool:
    if tenant_id is None:
        row = con.execute("SELECT 1 FROM project_sheets WHERE id = %s", (sheet_id,)).fetchone()
    else:
        row = con.execute("SELECT 1 FROM project_sheets WHERE id = %s AND tenant_id = %s",
                          (sheet_id, tenant_id)).fetchone()
    return row is not None


def list_sheet_members(sheet_id: int, *, tenant_id: Any = _UNSET) -> list[dict[str, Any]]:
    """Membri espliciti del foglio (utenti con cui e' condiviso). Tenant-scoped."""
    tid = _resolve_tenant(tenant_id)
    args: list[Any] = [sheet_id]
    guard = ""
    if tid is not None:
        guard = " AND EXISTS (SELECT 1 FROM project_sheets s WHERE s.id = su.sheet_id AND s.tenant_id = %s)"
        args.append(tid)
    with connect() as con:
        rows = con.execute(
            "SELECT su.user_id, su.role, su.added_at, u.email, u.first_name, u.last_name "
            "FROM project_sheet_users su JOIN users u ON u.id = su.user_id "
            f"WHERE su.sheet_id = %s{guard} ORDER BY su.added_at",
            tuple(args),
        ).fetchall()
    return [dict(r) for r in rows]


def add_sheet_member(sheet_id: int, user_id: int, role: str = "viewer", *, tenant_id: Any = _UNSET) -> None:
    """Condivide il foglio con un utente del tenant (upsert ruolo). Verifica che
    sheet e utente appartengano allo stesso tenant (no condivisione cross-tenant)."""
    if role not in ("viewer", "editor"):
        raise ValueError(f"role non valido: {role}")
    tid = _resolve_tenant(tenant_id)
    with connect() as con:
        if not _sheet_in_tenant(con, sheet_id, tid):
            raise SheetForbidden("Foglio non nel tenant.")
        # l'utente target deve essere dello stesso tenant del foglio
        srow = con.execute("SELECT tenant_id FROM project_sheets WHERE id = %s", (sheet_id,)).fetchone()
        sheet_tenant = srow["tenant_id"] if srow else None
        urow = con.execute("SELECT 1 FROM users WHERE id = %s AND tenant_id = %s",
                           (user_id, sheet_tenant)).fetchone()
        if urow is None:
            raise SheetForbidden("Utente non appartiene al tenant del foglio.")
        con.execute(
            "INSERT INTO project_sheet_users (sheet_id, user_id, role) VALUES (%s, %s, %s) "
            "ON CONFLICT (sheet_id, user_id) DO UPDATE SET role = EXCLUDED.role",
            (sheet_id, user_id, role),
        )
        con.commit()


def remove_sheet_member(sheet_id: int, user_id: int, *, tenant_id: Any = _UNSET) -> None:
    tid = _resolve_tenant(tenant_id)
    with connect() as con:
        if not _sheet_in_tenant(con, sheet_id, tid):
            return
        con.execute("DELETE FROM project_sheet_users WHERE sheet_id = %s AND user_id = %s",
                    (sheet_id, user_id))
        con.commit()


def sheet_member_role(sheet_id: int, user_id: int) -> str | None:
    """Ruolo dell'utente sul foglio ('viewer'/'editor') o None se non membro."""
    with connect() as con:
        row = con.execute(
            "SELECT role FROM project_sheet_users WHERE sheet_id = %s AND user_id = %s",
            (sheet_id, user_id),
        ).fetchone()
    return row["role"] if row else None


# ---------------------------------------------------------------------------
# Celle: snapshot + revisioni
# ---------------------------------------------------------------------------

def _verify_sheet_tenant(con, sheet_id: int, tenant_id: int | None) -> dict | None:
    """Ritorna la riga (id, n_rows, n_cols, revision) del foglio se appartiene al
    tenant (o tenant None = super_admin), bloccandola con FOR UPDATE. None se non
    trovato/tenant mismatch."""
    if tenant_id is None:
        sql = "SELECT id, n_rows, n_cols, revision FROM project_sheets WHERE id = %s FOR UPDATE"
        params: tuple = (sheet_id,)
    else:
        sql = ("SELECT id, n_rows, n_cols, revision FROM project_sheets "
               "WHERE id = %s AND tenant_id = %s FOR UPDATE")
        params = (sheet_id, tenant_id)
    row = con.execute(sql, params).fetchone()
    return dict(row) if row else None


def get_cells(sheet_id: int, *, tenant_id: Any = _UNSET) -> list[dict[str, Any]]:
    """Snapshot di tutte le celle non vuote del foglio. Tenant-scoped difensivo."""
    tid = _resolve_tenant(tenant_id)
    args: list[Any] = [sheet_id]
    tenant_guard = ""
    if tid is not None:
        tenant_guard = (
            " AND EXISTS (SELECT 1 FROM project_sheets s "
            "             WHERE s.id = c.sheet_id AND s.tenant_id = %s)"
        )
        args.append(tid)
    with connect() as con:
        rows = con.execute(
            "SELECT c.row_idx, c.col_idx, c.value, c.formula, c.style_json, c.revision "
            f"FROM project_sheet_cells c WHERE c.sheet_id = %s{tenant_guard} "
            "ORDER BY c.row_idx, c.col_idx",
            tuple(args),
        ).fetchall()
    return [_cell_to_wire(dict(r)) for r in rows]


def rows_as_dicts(
    sheet_id: int,
    *,
    tenant_id: Any = _UNSET,
    header_row: int = 0,
) -> list[dict[str, str]]:
    """Ricostruisce un foglio (griglia sparsa di celle) come lista di righe-dict.

    La `header_row` (default 0) fornisce i NOMI delle colonne: la cella
    (header_row, col) e' il nome della colonna `col`. Ogni riga successiva
    diventa `{nome_colonna: valore}`. Le colonne senza header e le righe
    completamente vuote sono saltate. Tenant-scoped (delega a get_sheet/get_cells).

    Usato dal runner portal_fill per mappare una riga del foglio ai campi di un
    form. Ritorna [] se il foglio non esiste / non e' accessibile dal tenant.
    """
    sheet = get_sheet(sheet_id, tenant_id=tenant_id, architect_view=True)
    if not sheet:
        return []
    cells = get_cells(sheet_id, tenant_id=tenant_id)
    if not cells:
        return []
    # griglia sparsa: (row, col) -> testo (value, fallback formula)
    grid: dict[tuple[int, int], str] = {}
    max_row = 0
    for c in cells:
        r, col = int(c["row"]), int(c["col"])
        txt = c.get("value")
        if txt is None or txt == "":
            txt = c.get("formula") or ""
        grid[(r, col)] = str(txt)
        if r > max_row:
            max_row = r
    # header: col_idx -> nome colonna (solo colonne con nome non vuoto)
    headers: dict[int, str] = {}
    n_cols = int(sheet.get("n_cols") or 0)
    for col in range(n_cols):
        name = (grid.get((header_row, col)) or "").strip()
        if name:
            headers[col] = name
    if not headers:
        return []
    out: list[dict[str, str]] = []
    for r in range(header_row + 1, max_row + 1):
        row_dict = {name: grid.get((r, col), "") for col, name in headers.items()}
        if any(v for v in row_dict.values()):
            out.append(row_dict)
    return out


def sheet_column_names(
    sheet_id: int,
    *,
    tenant_id: Any = _UNSET,
    header_row: int = 0,
) -> list[str]:
    """Nomi colonna di un foglio = celle non vuote della riga header (default 0),
    in ordine di colonna. Tenant-scoped. [] se foglio assente/senza intestazione.

    Usato dal pannello di mapping campo->colonna della UI portal_fill."""
    sheet = get_sheet(sheet_id, tenant_id=tenant_id, architect_view=True)
    if not sheet:
        return []
    cells = get_cells(sheet_id, tenant_id=tenant_id)
    by_col: dict[int, str] = {}
    for c in cells:
        if int(c["row"]) != header_row:
            continue
        name = (c.get("value") or c.get("formula") or "").strip()
        if name:
            by_col[int(c["col"])] = name
    return [by_col[k] for k in sorted(by_col)]


def _cell_to_wire(r: dict[str, Any]) -> dict[str, Any]:
    """Normalizza una riga cella nel formato wire del protocollo WS."""
    out: dict[str, Any] = {
        "row": r["row_idx"],
        "col": r["col_idx"],
        "value": r.get("value"),
        "formula": r.get("formula"),
    }
    style = r.get("style_json")
    if style:
        out["style"] = style
    return out


def get_head_revision(sheet_id: int, *, tenant_id: Any = _UNSET) -> int:
    """Revisione corrente del foglio (0 se mai modificato)."""
    tid = _resolve_tenant(tenant_id)
    args: list[Any] = [sheet_id]
    where = "id = %s"
    if tid is not None:
        where += " AND tenant_id = %s"
        args.append(tid)
    with connect() as con:
        row = con.execute(
            f"SELECT revision FROM project_sheets WHERE {where}", tuple(args)
        ).fetchone()
    return int(row["revision"]) if row else 0


def _validate_patch_cells(cells: Any, n_rows: int, n_cols: int) -> list[dict[str, Any]]:
    """Valida e normalizza la lista celle di una patch. Solleva SheetValidationError."""
    if not isinstance(cells, list):
        raise SheetValidationError("`cells` deve essere una lista.")
    if len(cells) == 0:
        raise SheetValidationError("Patch vuota.")
    if len(cells) > MAX_CELLS_PER_PATCH:
        raise SheetValidationError(
            f"Troppe celle nella patch ({len(cells)} > {MAX_CELLS_PER_PATCH})."
        )
    norm: list[dict[str, Any]] = []
    seen: set[tuple[int, int]] = set()
    for c in cells:
        if not isinstance(c, dict):
            raise SheetValidationError("Cella non valida (atteso oggetto).")
        try:
            row = int(c["row"])
            col = int(c["col"])
        except (KeyError, TypeError, ValueError):
            raise SheetValidationError("Cella senza indici row/col interi.")
        if row < 0 or col < 0:
            raise SheetValidationError("Indici di cella negativi non ammessi.")
        if row >= n_rows or col >= n_cols:
            raise SheetValidationError(
                f"Cella ({row},{col}) fuori dalla griglia {n_rows}x{n_cols}."
            )
        if (row, col) in seen:
            raise SheetValidationError(f"Cella duplicata nella patch: ({row},{col}).")
        seen.add((row, col))

        value = c.get("value")
        if value is not None:
            if not isinstance(value, str):
                value = str(value)
            if len(value) > MAX_VALUE_LEN:
                raise SheetValidationError(
                    f"Valore cella ({row},{col}) troppo grande (> {MAX_VALUE_LEN} char)."
                )
        # MVP: la formula e' trattata come testo (vedi piano). La conserviamo
        # come stringa ma NON la valutiamo lato server.
        formula = c.get("formula")
        if formula is not None:
            if not isinstance(formula, str):
                formula = str(formula)
            if len(formula) > MAX_VALUE_LEN:
                raise SheetValidationError(f"Formula cella ({row},{col}) troppo grande.")
        style = c.get("style")
        if style is not None:
            if not isinstance(style, dict):
                raise SheetValidationError("`style` deve essere un oggetto.")
            # Bound dimensione/nesting + verifica serializzabilita' JSON prima di
            # passare a psycopg (evita errori DB criptici e DoS da deep-nesting).
            try:
                if len(json.dumps(style)) > MAX_STYLE_LEN:
                    raise SheetValidationError(f"`style` cella ({row},{col}) troppo grande.")
            except (TypeError, ValueError):
                raise SheetValidationError(f"`style` cella ({row},{col}) non serializzabile.")
        norm.append({"row": row, "col": col, "value": value, "formula": formula, "style": style})
    return norm


def apply_cell_patch(
    sheet_id: int,
    cells: Any,
    *,
    tenant_id: Any = _UNSET,
    actor_user_id: Any = _UNSET,
) -> dict[str, Any]:
    """Applica una patch di celle in UNA transazione (vedi piano §Flusso Modifica Cella).

    1. SELECT ... FOR UPDATE sul foglio -> serializza patch concorrenti, legge la
       revisione corrente e le dimensioni griglia.
    2. Valida le celle contro le dimensioni.
    3. Calcola new_revision = revision + 1.
    4. Upsert celle (revision = new_revision).
    5. INSERT in project_sheet_revisions (patch append-only).
    6. UPDATE project_sheets.revision/updated_at.
    7. COMMIT.

    Ritorna {"revision": int, "cells": [...wire...]} (la patch normalizzata, da
    pubblicare su Redis / broadcastare dopo il commit). Solleva SheetForbidden se
    il foglio non e' nel tenant, SheetValidationError se la patch e' invalida.
    """
    tid = _resolve_tenant(tenant_id)
    uid = _resolve_user(actor_user_id)
    with connect() as con:
        sheet = _verify_sheet_tenant(con, sheet_id, tid)
        if sheet is None:
            raise SheetForbidden("Foglio non trovato nel tenant.")
        norm = _validate_patch_cells(cells, sheet["n_rows"], sheet["n_cols"])
        new_rev = int(sheet["revision"]) + 1

        for c in norm:
            con.execute(
                "INSERT INTO project_sheet_cells "
                "  (sheet_id, row_idx, col_idx, value, formula, style_json, "
                "   updated_by_user_id, updated_at, revision) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, NOW(), %s) "
                "ON CONFLICT (sheet_id, row_idx, col_idx) DO UPDATE SET "
                "  value = EXCLUDED.value, "
                "  formula = EXCLUDED.formula, "
                "  style_json = EXCLUDED.style_json, "
                "  updated_by_user_id = EXCLUDED.updated_by_user_id, "
                "  updated_at = NOW(), "
                "  revision = EXCLUDED.revision",
                (
                    sheet_id, c["row"], c["col"], c["value"], c["formula"],
                    Json(c["style"]) if c["style"] is not None else None,
                    uid, new_rev,
                ),
            )
        patch_payload = {"cells": [
            {"row": c["row"], "col": c["col"], "value": c["value"],
             "formula": c["formula"], **({"style": c["style"]} if c["style"] else {})}
            for c in norm
        ]}
        con.execute(
            "INSERT INTO project_sheet_revisions (sheet_id, actor_user_id, revision, patch_json) "
            "VALUES (%s, %s, %s, %s)",
            (sheet_id, uid, new_rev, Json(patch_payload)),
        )
        con.execute(
            "UPDATE project_sheets SET revision = %s, updated_at = NOW() WHERE id = %s",
            (new_rev, sheet_id),
        )
        con.commit()
    return {"revision": new_rev, "cells": patch_payload["cells"]}


def get_revisions_since(
    sheet_id: int, after_revision: int, *, tenant_id: Any = _UNSET, limit: int = 500
) -> list[dict[str, Any]]:
    """Revisioni con revision > after_revision, ordinate. Per il recupero al
    reconnect (vedi piano §Reconnect). Tenant-scoped difensivo."""
    tid = _resolve_tenant(tenant_id)
    args: list[Any] = [sheet_id, after_revision]
    tenant_guard = ""
    if tid is not None:
        tenant_guard = (
            " AND EXISTS (SELECT 1 FROM project_sheets s "
            "             WHERE s.id = r.sheet_id AND s.tenant_id = %s)"
        )
        args.append(tid)
    args.append(int(limit))
    with connect() as con:
        rows = con.execute(
            "SELECT r.revision, r.actor_user_id, r.patch_json "
            f"FROM project_sheet_revisions r "
            f"WHERE r.sheet_id = %s AND r.revision > %s{tenant_guard} "
            "ORDER BY r.revision ASC LIMIT %s",
            tuple(args),
        ).fetchall()
    return [
        {"revision": r["revision"], "actor_user_id": r["actor_user_id"], "patch": r["patch_json"]}
        for r in rows
    ]
