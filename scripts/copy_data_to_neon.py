"""Copia idempotente dei dati local -> Neon (produzione).

USAGE:
    python scripts/copy_data_to_neon.py --dry-run     # mostra cosa farebbe
    python scripts/copy_data_to_neon.py --apply       # esegue veramente (con conferma)
    python scripts/copy_data_to_neon.py --apply --yes # conferma automatica

LOGICA:
    Per ogni tabella business (in ordine di dipendenza FK):
      1. SELECT * FROM local.{table}
      2. INSERT INTO neon.{table} (...) VALUES (...) ON CONFLICT (id) DO NOTHING

    Quindi:
      - Righe gia' presenti su Neon (per PK id) vengono SKIPPATE (nessun overwrite).
      - Righe nuove (presenti su local ma non su Neon) vengono COPIATE.
      - Sequence dei PK vengono risincronizzate alla fine (SETVAL al max+1).

    Idempotente: rilanciato non duplica niente. Sicuro contro interruzioni.

NOTE SUI CONFLITTI ID:
    Se local.tenants ha id=1 'EDG' e neon.tenants ha id=1 'DeepTeco' (diverso
    nome), DopO il copy Neon mantiene 'DeepTeco' (ON CONFLICT DO NOTHING) ma
    TUTTE le righe downstream di local che referenziano tenant_id=1 verranno
    copiate e linkate al record 'DeepTeco' su Neon — risultato: dati di 'EDG'
    appaiono come se appartenessero a 'DeepTeco'.

    Lo script PRINTA un warning prominente quando rileva questo caso.
    L'utente decide: o droppa i conflicting records su Neon prima, o procede
    cosciente del fatto.

DSN:
    - Local: letta da .env::DATABASE_URL (no /dbconfig override)
    - Neon : risolta in ordine -> /dbconfig override -> NEON_DATABASE_URL env ->
             c:/tmp/neon_url.txt (stesso fallback di scripts/db.py)
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

try:
    from dotenv import load_dotenv
    load_dotenv(PROJECT_ROOT / ".env", override=False)
except ImportError:
    pass


# Riusa i resolver DSN di scripts/db.py per evitare drift.
from scripts.db import _mask, _resolve_local_dsn, _resolve_neon_dsn  # noqa: E402


# ---------------------------------------------------------------------------
# Tabelle in ordine di dipendenza FK (parent prima del child).
# Se aggiungi una nuova tabella business, aggiungila qui nel posto giusto.
# ---------------------------------------------------------------------------
TABLES_IN_ORDER: list[str] = [
    # Tier 0: base (no FK fra di loro)
    "tenants",
    # Tier 1: FK a tenants
    "users",
    # Tier 2: FK a tenants/users
    "llm_api_keys",
    "channel_config",
    "scraping_policies",
    "orchestrator_messages",
    "email_accounts",
    "telegram_bots",
    "whatsapp_api_config",
    "social_accounts",
    # Tier 3: FK a tenants/users + opz llm_api_keys
    "tasks",
    "workflows",
    # Tier 4: FK a workflows/tasks
    "workflow_edges",
    "workflow_runs",
    # Tier 5: FK a tasks/workflow_runs
    "jobs",
    # Tier 6: FK a jobs (+ tenants/users)
    "assets",
    "recon_runs",
    "site_patterns",
    "site_playbooks",
    "site_intelligence",
    # Tier 7: FK ad assets / recon_runs
    "asset_tags",
    "asset_dedup_candidates",
    "contacts",
    "recon_visited",
    "recon_checkpoints",
    # Tier 8: FK a contacts
    "threads",
    "social_dm_log",
    # Tier 9: FK a threads
    "messages",
    # Tier 10: Fascicoli & Fogli (FK a tenants/users; child a projects/project_sheets)
    "projects",                      # FK -> tenants, users
    "project_users",                 # FK -> projects, users
    "project_files",                 # FK -> projects, users
    "project_chat_conversations",    # FK -> projects, users
    "project_chat_messages",         # FK -> project_chat_conversations, projects, users
    "project_sheets",                # FK -> projects, tenants, users
    "project_sheet_users",           # FK -> project_sheets, users
    "project_sheet_cells",           # FK -> project_sheets, users
    "project_sheet_revisions",       # FK -> project_sheets, users
]


def _get_columns(conn: Any, table: str) -> list[str]:
    """Lista delle colonne di una tabella in ordine. Usato per costruire INSERT
    dinamici senza dipendere da hardcode di schema."""
    rows = conn.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema='public' AND table_name=%s "
        "ORDER BY ordinal_position",
        (table,),
    ).fetchall()
    return [r[0] for r in rows]


def _table_exists(conn: Any, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM information_schema.tables "
        "WHERE table_schema='public' AND table_name=%s",
        (table,),
    ).fetchone()
    return row is not None


def _row_count(conn: Any, table: str) -> int:
    if not _table_exists(conn, table):
        return -1
    row = conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()
    return int(row[0])


def _has_id_column(cols: list[str]) -> bool:
    return "id" in cols


def _detect_id_conflicts(
    local_conn: Any, neon_conn: Any, table: str, cols: list[str],
) -> list[dict]:
    """Identifica righe con stesso PK ma contenuto diverso. Ritorna lista di
    dict {id, local_row, neon_row, differing_cols}. Lentino (SELECT su entrambi),
    eseguito solo in --dry-run o se l'utente lo chiede esplicitamente.
    """
    if not _has_id_column(cols):
        return []
    # Carica righe da entrambi
    local_rows = {
        r[0]: dict(zip(cols, r))
        for r in local_conn.execute(f'SELECT {",".join(f"{chr(34)}{c}{chr(34)}" for c in cols)} FROM "{table}"').fetchall()
    }
    neon_rows = {
        r[0]: dict(zip(cols, r))
        for r in neon_conn.execute(f'SELECT {",".join(f"{chr(34)}{c}{chr(34)}" for c in cols)} FROM "{table}"').fetchall()
    }
    conflicts: list[dict] = []
    common_ids = set(local_rows.keys()) & set(neon_rows.keys())
    for rid in common_ids:
        differing = [
            c for c in cols
            if c != "id" and local_rows[rid].get(c) != neon_rows[rid].get(c)
        ]
        if differing:
            conflicts.append({
                "id": rid,
                "local_row": local_rows[rid],
                "neon_row": neon_rows[rid],
                "differing_cols": differing,
            })
    return conflicts


def _copy_table(
    local_conn: Any, neon_conn: Any, table: str,
    *, dry_run: bool, verbose: bool = True,
) -> dict[str, int]:
    """Copia una tabella local -> neon con ON CONFLICT DO NOTHING.

    Ritorna {n_source, n_inserted, n_skipped}.
    Su dry-run: simula INSERT contando le righe gia' presenti per PK.
    """
    if not _table_exists(local_conn, table):
        return {"n_source": 0, "n_inserted": 0, "n_skipped": 0, "missing": True}
    if not _table_exists(neon_conn, table):
        if verbose:
            print(f"  [SKIP] {table}: tabella NON ESISTE su Neon (esegui prima `python scripts/db.py promote`).")
        return {"n_source": 0, "n_inserted": 0, "n_skipped": 0, "missing_target": True}

    cols_local = _get_columns(local_conn, table)
    cols_neon = _get_columns(neon_conn, table)
    cols = [c for c in cols_local if c in cols_neon]  # intersezione: colonne presenti in entrambi
    if not cols:
        if verbose:
            print(f"  [SKIP] {table}: nessuna colonna comune fra local e Neon.")
        return {"n_source": 0, "n_inserted": 0, "n_skipped": 0, "no_common_cols": True}

    n_source = _row_count(local_conn, table)
    if n_source == 0:
        return {"n_source": 0, "n_inserted": 0, "n_skipped": 0}

    # Carica righe da local
    cols_quoted = ", ".join(f'"{c}"' for c in cols)
    local_rows = local_conn.execute(f'SELECT {cols_quoted} FROM "{table}"').fetchall()

    if dry_run:
        # Conta quanti id (o tuple PK) sono gia' su Neon
        if _has_id_column(cols):
            neon_ids = {
                r[0] for r in neon_conn.execute(f'SELECT id FROM "{table}"').fetchall()
            }
            local_ids = {r[cols.index("id")] for r in local_rows}
            n_already = len(local_ids & neon_ids)
            n_new = len(local_ids - neon_ids)
            return {"n_source": n_source, "n_inserted": n_new, "n_skipped": n_already}
        else:
            # Senza PK 'id' non possiamo predire i conflitti senza fare insert reale
            return {"n_source": n_source, "n_inserted": -1, "n_skipped": -1, "no_id_col": True}

    # Apply mode: INSERT ON CONFLICT (isolato in savepoint per non bloccare le
    # tabelle successive in caso di FK violation o errore).
    placeholders = ", ".join(["%s"] * len(cols))
    on_conflict = "ON CONFLICT (id) DO NOTHING" if _has_id_column(cols) else ""
    sql = f'INSERT INTO "{table}" ({cols_quoted}) VALUES ({placeholders}) {on_conflict}'
    sp = f"_copy_{table}"
    try:
        # Ogni tabella ha la sua transazione: PostgreSQL non supporta savepoint
        # in modalita' autocommit, ma commit dopo ogni tabella isola gli errori.
        cur = neon_conn.cursor()
        cur.executemany(sql, local_rows)
        inserted = cur.rowcount if cur.rowcount is not None else 0
        skipped = n_source - inserted
        neon_conn.commit()
    except Exception as exc:
        # Rollback per non lasciare la conn in stato "aborted"
        try:
            neon_conn.rollback()
        except Exception:
            pass
        return {
            "n_source": n_source, "n_inserted": 0, "n_skipped": 0,
            "error": f"{type(exc).__name__}: {str(exc)[:200]}",
        }

    # Risincronizza la sequence dell'id (se presente) per evitare collisioni future
    if _has_id_column(cols):
        try:
            neon_conn.execute(
                f"SELECT setval(pg_get_serial_sequence(%s, 'id'), "
                f"COALESCE((SELECT MAX(id) FROM \"{table}\"), 1))",
                (table,),
            )
            neon_conn.commit()
        except Exception:
            try:
                neon_conn.rollback()
            except Exception:
                pass

    return {"n_source": n_source, "n_inserted": inserted, "n_skipped": skipped}


def _format_row_diff(row: dict, cols: list[str], max_len: int = 50) -> str:
    parts = []
    for c in cols:
        v = row.get(c)
        s = repr(v)
        if len(s) > max_len:
            s = s[:max_len - 3] + "..."
        parts.append(f"{c}={s}")
    return ", ".join(parts)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    g = parser.add_mutually_exclusive_group(required=True)
    g.add_argument("--dry-run", action="store_true", help="Simula senza scrivere su Neon.")
    g.add_argument("--apply", action="store_true", help="Esegue il copy vero.")
    parser.add_argument("--yes", "-y", action="store_true", help="Salta la conferma interattiva.")
    parser.add_argument(
        "--check-conflicts", action="store_true",
        help="Verifica conflitti di PK fra local e Neon (lento, per tabelle piccole).",
    )
    args = parser.parse_args()

    import psycopg

    local_dsn = _resolve_local_dsn()
    neon_dsn = _resolve_neon_dsn()

    if local_dsn == neon_dsn:
        print("[ERROR] local e Neon risolvono alla stessa DSN — copia annullata.")
        print(f"        {_mask(local_dsn)}")
        return 1

    print()
    print("=" * 72)
    print("  COPY DATA LOCAL -> NEON")
    print("=" * 72)
    print(f"  Source (local): {_mask(local_dsn)}")
    print(f"  Target (Neon):  {_mask(neon_dsn)}")
    print(f"  Modalita':      {'DRY-RUN (simulazione)' if args.dry_run else 'APPLY (scrittura reale)'}")
    print()

    # Connect
    with psycopg.connect(local_dsn) as local_conn, psycopg.connect(neon_dsn) as neon_conn:
        # Pre-check: tabelle previste devono esistere su Neon
        missing_neon = [t for t in TABLES_IN_ORDER if not _table_exists(neon_conn, t)]
        if missing_neon:
            print(f"[WARN] Su Neon mancano {len(missing_neon)} tabelle: {', '.join(missing_neon)}")
            print(f"       Esegui prima `python scripts/db.py promote` per allineare lo schema.")
            print()

        # Conflict check (opzionale)
        if args.check_conflicts:
            print("[INFO] Controllo conflitti di PK su tabelle critiche (tenants, users)...")
            for t in ("tenants", "users"):
                if not (_table_exists(local_conn, t) and _table_exists(neon_conn, t)):
                    continue
                cols = _get_columns(local_conn, t)
                conflicts = _detect_id_conflicts(local_conn, neon_conn, t, cols)
                if conflicts:
                    print(f"  [WARN] {t}: {len(conflicts)} conflitti PK (stessa id, contenuto diverso).")
                    for c in conflicts[:5]:
                        print(f"    id={c['id']}:")
                        print(f"      LOCAL: {_format_row_diff(c['local_row'], c['differing_cols'])}")
                        print(f"      NEON : {_format_row_diff(c['neon_row'], c['differing_cols'])}")
                    if len(conflicts) > 5:
                        print(f"    ... ({len(conflicts) - 5} altri)")
                    print(f"    Con ON CONFLICT DO NOTHING le righe di NEON vengono PRESERVATE.")
                    print(f"    Le righe downstream di local che FK referenziano l'id verranno linkate")
                    print(f"    al record di Neon (potenziale data leak fra tenant).")
            print()

        # Conferma per --apply
        if args.apply and not args.yes:
            ans = input("Procedere con il COPY REALE? [y/N] ").strip().lower()
            if ans not in ("y", "yes", "s", "si"):
                print("Annullato.")
                return 1

        # Esegui copy per ogni tabella
        results: list[tuple[str, dict]] = []
        for t in TABLES_IN_ORDER:
            r = _copy_table(local_conn, neon_conn, t, dry_run=args.dry_run)
            results.append((t, r))

        # Report finale
        print()
        print("=" * 72)
        print(f"  REPORT ({'DRY-RUN' if args.dry_run else 'APPLIED'})")
        print("=" * 72)
        print(f"  {'TABLE':<30} {'SOURCE':>8} {'INSERTED':>10} {'SKIPPED':>10}")
        print("  " + "-" * 60)
        tot_source = tot_inserted = tot_skipped = 0
        errors: list[tuple[str, str]] = []
        for t, r in results:
            if r.get("missing"):
                print(f"  {t:<30}   (non esiste su local — skip)")
                continue
            if r.get("missing_target"):
                print(f"  {t:<30}   (non esiste su Neon — skip)")
                continue
            if r.get("error"):
                print(f"  {t:<30}   [ERROR] {r['error'][:60]}")
                errors.append((t, r["error"]))
                continue
            ns = r["n_source"]
            ni = r["n_inserted"]
            nk = r["n_skipped"]
            print(f"  {t:<30} {ns:>8} {ni:>10} {nk:>10}")
            tot_source += ns
            tot_inserted += max(0, ni)
            tot_skipped += max(0, nk)
        if errors:
            print()
            print(f"  [WARN] {len(errors)} tabelle hanno errori:")
            for t, err in errors:
                print(f"    {t}: {err}")
        print("  " + "-" * 60)
        print(f"  {'TOTAL':<30} {tot_source:>8} {tot_inserted:>10} {tot_skipped:>10}")
        print()
        if args.dry_run:
            print("  Dry-run completato. Per eseguire veramente:")
            print("    python scripts/copy_data_to_neon.py --apply")
        else:
            print("  Apply completato.")
        print()

    return 0


if __name__ == "__main__":
    sys.exit(main())
