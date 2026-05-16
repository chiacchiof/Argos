"""Migrazione one-shot SQLite locale -> Postgres cloud (multi-tenant).

⚠️  SCAFFOLDING — Fase 5 del piano in SETUP_CLOUD_DB_TENANT.md.
La logica di conversione tipi e ingestione è ancora da implementare.
Prerequisito: Fase 2 (refactor app/db.py SQLite→PG) e Fase 3 (tenant_id su
tutte le tabelle business) devono essere COMPLETE.

Uso atteso (quando completo):

    python scripts/migrate_to_cloud.py \\
        --target "postgresql://user:pwd@host/db?sslmode=require" \\
        --bootstrap-email edgAdmin \\
        --bootstrap-password 'Entra123!' \\
        --tenant-name "Default" \\
        --tenant-slug default

Flag:
    --target          DATABASE_URL del Postgres di destinazione (default: env DATABASE_URL).
    --sqlite-path     Path del SQLite sorgente (default: data/agentscraper.db).
    --bootstrap-email Email/username del super-admin da creare in cloud.
    --bootstrap-password
                      Password iniziale del super-admin (verrà hash-ata con bcrypt).
    --tenant-name     Nome del tenant Default su cui caricare tutti i dati legacy.
    --tenant-slug     Slug del tenant Default (default: "default").
    --dry-run         Stampa il piano di migrazione senza scrivere.
    --resume          Salta tabelle che hanno già righe (idempotenza).
    --force           Permetti migrazione anche se Postgres ha già dati.

Output finale: counts source vs target per ogni tabella.
"""
from __future__ import annotations

import argparse
import logging
import os
import sqlite3
import sys
from pathlib import Path
from typing import Any


log = logging.getLogger("migrate_to_cloud")


# Ordine topologico delle tabelle (rispetta i FK). Riempi/correggi quando le
# tabelle business avranno tenant_id (Fase 3).
TABLE_ORDER = [
    # Globali (no tenant_id):
    "site_patterns",
    "site_playbooks",
    # Per-tenant (richiedono tenant_id aggiunto):
    "tasks",
    "workflows",
    "workflow_runs",
    "workflow_edges",
    "jobs",
    "assets",
    "asset_tags",
    "contacts",
    "threads",
    "messages",
    "orchestrator_messages",
    "social_accounts",
    "social_dm_log",
    "recon_runs",
    "recon_checkpoints",
    "recon_visited",
    "whatsapp_api_config",
    "channel_config",
]


# Colonne booleane che in SQLite sono INTEGER 0/1 e in PG diventano BOOLEAN.
# Aggiungere durante la Fase 2 quando lo schema PG viene definito.
BOOLEAN_COLUMNS: dict[str, list[str]] = {
    # "tasks": ["disabled", "crawler_enabled"],
    # "messages": ["llm_generated"],
    # ...
}


# Colonne JSON che in SQLite sono TEXT e in PG diventano JSONB.
JSON_COLUMNS: dict[str, list[str]] = {
    # "assets": ["raw_json"],
    # "contacts": ["raw_json", "social_json"],
    # ...
}


# Colonne BLOB Fernet che in SQLite sono BLOB e in PG diventano BYTEA.
BLOB_COLUMNS: dict[str, list[str]] = {
    # "social_accounts": ["encrypted_password"],
    # "whatsapp_api_config": ["encrypted_access_token"],
}


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--target", default=os.environ.get("DATABASE_URL"))
    p.add_argument("--sqlite-path", default="data/agentscraper.db", type=Path)
    p.add_argument("--bootstrap-email", required=False, default=os.environ.get("BOOTSTRAP_SUPER_ADMIN_EMAIL"))
    p.add_argument("--bootstrap-password", required=False, default=os.environ.get("BOOTSTRAP_SUPER_ADMIN_PASSWORD"))
    p.add_argument("--tenant-name", default="Default")
    p.add_argument("--tenant-slug", default="default")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--resume", action="store_true")
    p.add_argument("--force", action="store_true")
    p.add_argument("--verbose", "-v", action="store_true")
    return p.parse_args()


def setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


# ---------------------------------------------------------------------------
# Step 1: validation
# ---------------------------------------------------------------------------

def open_source(sqlite_path: Path) -> sqlite3.Connection:
    if not sqlite_path.exists():
        raise SystemExit(f"❌ SQLite source non trovato: {sqlite_path}")
    con = sqlite3.connect(str(sqlite_path))
    con.row_factory = sqlite3.Row
    return con


def open_target(database_url: str):
    """Ritorna una connessione psycopg3 al Postgres target."""
    try:
        import psycopg
    except ImportError:
        raise SystemExit("❌ psycopg non installato. pip install 'psycopg[binary]'") from None
    return psycopg.connect(database_url)


def check_target_empty(pg_conn, force: bool) -> None:
    """Verifica che il target non abbia già dati (a meno di --force)."""
    with pg_conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM information_schema.tables WHERE table_schema = 'public'")
        tcount = cur.fetchone()[0]
    if tcount > 0 and not force:
        cur = pg_conn.cursor()
        try:
            cur.execute("SELECT COUNT(*) FROM tenants")
            existing = cur.fetchone()[0]
        except Exception:
            existing = 0
        if existing > 0:
            raise SystemExit(
                f"❌ Postgres target ha già {existing} tenant. Usa --force per sovrascrivere."
            )


# ---------------------------------------------------------------------------
# Step 2: bootstrap (super-admin + tenant Default)
# ---------------------------------------------------------------------------

def init_schema(pg_conn) -> None:
    """Crea schema multi-tenant + tabelle business in Postgres.

    TODO Fase 2: chiamare `app.db.init_db()` riadattato per PG invece dell'init
    SQLite. Per ora si appoggia a `app.db_cloud.init_db()` che crea solo
    tenants/users — non basta.
    """
    log.info("Init schema Postgres (TODO: includere tabelle business — Fase 2 da completare).")
    # Implementare quando Fase 2 sarà pronta:
    # from app.db_pg import SCHEMA_SQL as BUSINESS_SCHEMA
    # with pg_conn.cursor() as cur:
    #     cur.execute(BUSINESS_SCHEMA)
    # pg_conn.commit()


def bootstrap_super_admin(pg_conn, email: str, password: str) -> int:
    """Crea il super-admin in users se non esiste. Ritorna user_id."""
    from app.auth import hash_password

    email_norm = email.strip().lower()
    with pg_conn.cursor() as cur:
        cur.execute("SELECT id FROM users WHERE email = %s", (email_norm,))
        row = cur.fetchone()
        if row:
            log.info("Super-admin %s già esistente (id=%s).", email_norm, row[0])
            return int(row[0])
        cur.execute(
            "INSERT INTO users (tenant_id, email, password_hash, role) "
            "VALUES (NULL, %s, %s, 'super_admin') RETURNING id",
            (email_norm, hash_password(password)),
        )
        new_id = int(cur.fetchone()[0])
    pg_conn.commit()
    log.info("Super-admin creato: %s (id=%s).", email_norm, new_id)
    return new_id


def bootstrap_default_tenant(pg_conn, name: str, slug: str) -> int:
    with pg_conn.cursor() as cur:
        cur.execute("SELECT id FROM tenants WHERE slug = %s", (slug,))
        row = cur.fetchone()
        if row:
            log.info("Tenant '%s' già esistente (id=%s).", slug, row[0])
            return int(row[0])
        cur.execute(
            "INSERT INTO tenants (name, slug) VALUES (%s, %s) RETURNING id",
            (name, slug),
        )
        new_id = int(cur.fetchone()[0])
    pg_conn.commit()
    log.info("Tenant Default creato: %s (id=%s).", slug, new_id)
    return new_id


# ---------------------------------------------------------------------------
# Step 3: copia tabelle SQLite → Postgres
# ---------------------------------------------------------------------------

def _convert_row(table: str, row: sqlite3.Row) -> dict[str, Any]:
    """Converte una row SQLite per l'insert in PG.

    - Boolean: INTEGER 0/1 -> TRUE/FALSE
    - JSON: TEXT serializzato -> dict (per psycopg.types.json.Jsonb)
    - BLOB: bytes -> bytes (passa attraverso, psycopg gestisce BYTEA)
    - Timestamp: stringa ISO -> datetime aware (cfr. Fase 2)

    TODO: implementare conversione effettiva durante Fase 5 — richiede
    schema PG completo (Fase 2).
    """
    import json
    from psycopg.types.json import Jsonb

    out: dict[str, Any] = {}
    bool_cols = set(BOOLEAN_COLUMNS.get(table, ()))
    json_cols = set(JSON_COLUMNS.get(table, ()))
    blob_cols = set(BLOB_COLUMNS.get(table, ()))

    for col in row.keys():
        val = row[col]
        if val is None:
            out[col] = None
        elif col in bool_cols:
            out[col] = bool(val)
        elif col in json_cols:
            try:
                parsed = json.loads(val) if isinstance(val, str) else val
            except (json.JSONDecodeError, TypeError):
                parsed = None
            out[col] = Jsonb(parsed) if parsed is not None else None
        elif col in blob_cols:
            out[col] = bytes(val) if val is not None else None
        else:
            out[col] = val
    return out


def migrate_table(
    sqlite_con: sqlite3.Connection,
    pg_conn,
    table: str,
    *,
    tenant_id: int | None,
    super_admin_id: int,
    resume: bool,
    dry_run: bool,
) -> tuple[int, int]:
    """Migra una tabella SQLite -> Postgres. Ritorna (rows_source, rows_inserted)."""
    cur = sqlite_con.execute(f"SELECT * FROM {table}")
    src_rows = cur.fetchall()

    if dry_run:
        log.info("[dry-run] %s: %s righe da migrare", table, len(src_rows))
        return len(src_rows), 0

    if resume:
        with pg_conn.cursor() as pcur:
            pcur.execute(f"SELECT COUNT(*) FROM {table}")
            existing = pcur.fetchone()[0]
        if existing > 0:
            log.info("%s: già %s righe in PG, skip (--resume).", table, existing)
            return len(src_rows), 0

    inserted = 0
    with pg_conn.cursor() as pcur:
        for r in src_rows:
            data = _convert_row(table, r)
            # Aggiungi tenant_id / created_by_user_id se la tabella li ha
            # (TODO Fase 5: lista colonne da iniettare per ogni tabella).
            if tenant_id is not None and table not in ("site_patterns", "site_playbooks"):
                data["tenant_id"] = tenant_id
                data.setdefault("created_by_user_id", super_admin_id)

            cols = list(data.keys())
            placeholders = ", ".join(["%s"] * len(cols))
            col_list = ", ".join(cols)
            try:
                pcur.execute(
                    f"INSERT INTO {table} ({col_list}) VALUES ({placeholders})",
                    [data[c] for c in cols],
                )
                inserted += 1
            except Exception as exc:
                log.error("Errore insert %s id=%s: %s", table, r["id"] if "id" in r.keys() else "?", exc)
                raise
    pg_conn.commit()
    log.info("%s: %s/%s righe migrate.", table, inserted, len(src_rows))
    return len(src_rows), inserted


def reset_sequences(pg_conn) -> None:
    """Allinea le sequenze BIGSERIAL al max(id) di ogni tabella."""
    with pg_conn.cursor() as cur:
        for table in TABLE_ORDER + ["tenants", "users"]:
            try:
                cur.execute(
                    f"SELECT setval(pg_get_serial_sequence(%s, 'id'), "
                    f"COALESCE((SELECT MAX(id) FROM {table}), 1))",
                    (table,),
                )
            except Exception as exc:
                log.warning("Reset sequenza %s skipped: %s", table, exc)
    pg_conn.commit()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    args = parse_args()
    setup_logging(args.verbose)

    if not args.target:
        log.error("--target (o env DATABASE_URL) obbligatorio.")
        return 2
    if not args.bootstrap_email or not args.bootstrap_password:
        log.error("--bootstrap-email e --bootstrap-password obbligatori per il primo run.")
        return 2

    log.info("⚠️  SCAFFOLDING: lo script è incompleto, vedi Fase 5 in SETUP_CLOUD_DB_TENANT.md")
    log.info("Sorgente: %s", args.sqlite_path)
    log.info("Target:   %s", args.target.split("@")[-1] if "@" in args.target else args.target)
    log.info("Dry-run:  %s", args.dry_run)

    sqlite_con = open_source(args.sqlite_path)
    pg_conn = open_target(args.target)

    try:
        if not args.dry_run:
            check_target_empty(pg_conn, args.force)
            init_schema(pg_conn)
            super_admin_id = bootstrap_super_admin(pg_conn, args.bootstrap_email, args.bootstrap_password)
            tenant_id = bootstrap_default_tenant(pg_conn, args.tenant_name, args.tenant_slug)
        else:
            super_admin_id = 0
            tenant_id = 0

        report: list[tuple[str, int, int]] = []
        for table in TABLE_ORDER:
            try:
                src_count, inserted = migrate_table(
                    sqlite_con,
                    pg_conn,
                    table,
                    tenant_id=tenant_id,
                    super_admin_id=super_admin_id,
                    resume=args.resume,
                    dry_run=args.dry_run,
                )
                report.append((table, src_count, inserted))
            except sqlite3.OperationalError as exc:
                log.warning("Skip tabella %s (non esiste in SQLite source): %s", table, exc)

        if not args.dry_run:
            reset_sequences(pg_conn)

        log.info("=" * 60)
        log.info("RIEPILOGO MIGRAZIONE")
        log.info("=" * 60)
        for table, src, ins in report:
            log.info("%-25s src=%6d  ins=%6d", table, src, ins)
        log.info("=" * 60)
        return 0
    finally:
        sqlite_con.close()
        pg_conn.close()


if __name__ == "__main__":
    sys.exit(main())
