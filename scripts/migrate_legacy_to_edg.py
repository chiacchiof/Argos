"""Migrazione one-shot: SQLite legacy `data/agentscraper.db` → Postgres dev
sotto tenant EDG, created_by_user_id=Ferdinando.

Usage:
    python scripts/migrate_legacy_to_edg.py

Prerequisiti:
- DATABASE_URL settata in `.env` (puntando a Postgres locale o Neon)
- Container Postgres avviato
- Lo schema Postgres viene creato/aggiornato dallo script (chiama init_db)
- L'utente edgAdmin (super-admin) viene creato via env BOOTSTRAP_SUPER_ADMIN_*
- Tenant EDG + utente Ferdinando vengono creati se non esistono

Output:
- Copia 20 tabelle business da SQLite a Postgres con tenant_id=EDG e
  created_by_user_id=Ferdinando dove pertinente.
- Conserva gli ID originali (per non rompere le FK fra tabelle).
- Reset delle sequenze BIGSERIAL al MAX(id)+1.
- Counts source vs target.
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

# Triggera load_dotenv + apply_override
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app import config as _cfg  # noqa
from app import db, db_cloud
from app.auth import hash_password


SQLITE_PATH = Path("data/agentscraper.db")


def _ensure_bootstrap() -> tuple[int, int]:
    """Crea schema + tenant EDG + utente Ferdinando se non esistono.
    Ritorna (tenant_id_EDG, user_id_Ferdinando)."""
    db_cloud.init_db()
    db.init_db()

    edg = db_cloud.get_tenant_by_slug("edg")
    if edg:
        tenant_id = int(edg["id"])
        print(f"[bootstrap] Tenant EDG già esistente: id={tenant_id}")
    else:
        tenant_id = db_cloud.create_tenant("EDG", "edg")
        print(f"[bootstrap] Tenant EDG creato: id={tenant_id}")

    fer = db_cloud.get_user_by_email("ferdinando.chiacchio@etnadg.com")
    if fer:
        user_id = int(fer["id"])
        print(f"[bootstrap] Ferdinando già esistente: id={user_id}")
    else:
        user_id = db_cloud.create_user(
            tenant_id=tenant_id,
            email="ferdinando.chiacchio@etnadg.com",
            password_hash=hash_password("T@ncredi2018!"),
            role="tenant_user",
        )
        print(f"[bootstrap] Ferdinando creato: id={user_id} tenant_id={tenant_id}")

    return tenant_id, user_id


# Tabelle in ordine topologico (FK risolti).
# Per ognuna: (nome, tenant_id_col, created_by_user_id_col)
# - tenant_id_col=True → aggiungi tenant_id=EDG (None se globale)
# - created_by_user_id_col=True → aggiungi created_by_user_id=Ferdinando
TABLES_ORDERED: list[tuple[str, bool, bool]] = [
    # (table, has_tenant_id, has_created_by_user_id)
    ("tasks", True, True),
    ("workflows", True, True),
    ("workflow_runs", True, False),
    ("workflow_edges", True, False),
    ("jobs", True, True),
    ("assets", True, True),
    ("asset_tags", True, False),
    ("social_accounts", True, True),
    ("whatsapp_api_config", True, True),
    ("contacts", True, True),
    ("threads", True, False),
    ("messages", True, False),
    ("orchestrator_messages", True, False),
    ("site_patterns", False, False),  # globale
    ("site_playbooks", False, False),  # globale
    ("social_dm_log", True, False),
    ("recon_runs", True, False),
    ("recon_checkpoints", True, False),
    ("recon_visited", True, False),
    ("channel_config", True, False),
]


def _migrate_table(
    sqlite_con: sqlite3.Connection,
    table: str,
    tenant_id: int,
    user_id: int,
    add_tenant: bool,
    add_user: bool,
) -> tuple[int, int]:
    """Copia rows da SQLite a Postgres. Ritorna (src_count, inserted_count)."""
    try:
        src_rows = sqlite_con.execute(f"SELECT * FROM {table}").fetchall()
    except sqlite3.OperationalError:
        print(f"  [skip] {table}: non esiste in SQLite source")
        return 0, 0
    src_count = len(src_rows)
    if not src_count:
        print(f"  [skip] {table}: source vuoto")
        return 0, 0

    src_cols = [c[0] for c in sqlite_con.execute(f"SELECT * FROM {table} LIMIT 1").description]

    # Aggiunge colonne extra se serve
    dst_cols = list(src_cols)
    if add_tenant and "tenant_id" not in dst_cols:
        dst_cols.append("tenant_id")
    if add_user and "created_by_user_id" not in dst_cols:
        dst_cols.append("created_by_user_id")

    placeholders = ", ".join(["%s"] * len(dst_cols))
    sql = f'INSERT INTO {table} ({", ".join(dst_cols)}) VALUES ({placeholders})'

    inserted = 0
    with db.connect() as pg:
        for r in src_rows:
            values: list[Any] = []
            for c in src_cols:
                v = r[c]
                # Conversione tipi: SQLite INTEGER per bool → resta INTEGER (ok),
                # SQLite BLOB → bytes (psycopg gestisce per BYTEA).
                values.append(v)
            if add_tenant:
                values.append(tenant_id)
            if add_user:
                values.append(user_id)
            try:
                pg.execute(sql, values)
                inserted += 1
            except Exception as exc:
                print(f"  [error] {table} id={r['id'] if 'id' in r.keys() else '?'}: {exc}")
                pg.rollback()
                # Continua con i prossimi
                continue
        pg.commit()
    return src_count, inserted


def _reset_sequences(tables: list[str]) -> None:
    """Allinea le sequenze BIGSERIAL.id al MAX(id)+1 per ogni tabella.

    Skip per asset_tags (no `id`, PK composta). Una connessione separata
    per ogni tabella per evitare l'effetto cascade del transaction-aborted
    su psycopg."""
    NO_ID_TABLES = {"asset_tags"}
    for t in tables:
        if t in NO_ID_TABLES:
            continue
        try:
            with db.connect() as pg:
                pg.execute(
                    f"SELECT setval(pg_get_serial_sequence('{t}', 'id'), "
                    f"COALESCE((SELECT MAX(id) FROM {t}), 1))"
                )
                pg.commit()
        except Exception as exc:
            print(f"  [warn] reset seq {t}: {exc}")


def main() -> int:
    if not SQLITE_PATH.exists():
        print(f"❌ SQLite source non trovato: {SQLITE_PATH}")
        return 1

    print("=" * 60)
    print("MIGRAZIONE SQLite legacy → Postgres (tenant EDG)")
    print("=" * 60)
    print(f"Source: {SQLITE_PATH}")
    print(f"Target: $DATABASE_URL")
    print()

    tenant_id, user_id = _ensure_bootstrap()
    print()

    sqlite_con = sqlite3.connect(str(SQLITE_PATH))
    sqlite_con.row_factory = sqlite3.Row

    report: list[tuple[str, int, int]] = []
    try:
        for table, has_tenant, has_user in TABLES_ORDERED:
            print(f"[migrate] {table}")
            src, ins = _migrate_table(
                sqlite_con,
                table=table,
                tenant_id=tenant_id,
                user_id=user_id,
                add_tenant=has_tenant,
                add_user=has_user,
            )
            report.append((table, src, ins))
            print(f"  → src={src}  inserted={ins}")
    finally:
        sqlite_con.close()

    print()
    print("[seq-reset] Allineo le sequenze BIGSERIAL...")
    _reset_sequences([t for t, _, _ in TABLES_ORDERED])

    print()
    print("=" * 60)
    print("RIEPILOGO MIGRAZIONE")
    print("=" * 60)
    print(f"{'Tabella':<25}{'Source':>10}{'Inserted':>12}")
    print("-" * 47)
    for t, s, i in report:
        print(f"{t:<25}{s:>10}{i:>12}")
    print("-" * 47)
    print(f"{'TOTALE':<25}{sum(s for _, s, _ in report):>10}{sum(i for _, _, i in report):>12}")
    print()
    print("✅ Migrazione completata.")
    print(f"   Tenant EDG id={tenant_id}, Ferdinando id={user_id}")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
