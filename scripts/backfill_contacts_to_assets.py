"""Backfill one-shot: contacts → assets (Fase 2B).

Prepara `assets` come single-source-of-truth per il refactor asset-centric.
Per ogni contact:

  1. Se `contact.asset_id IS NOT NULL` → copia i canali (email/telegram/
     whatsapp/social/sitoweb/display_name) sull'asset linkato, SENZA
     sovrascrivere valori già presenti (COALESCE-like).

  2. Se `contact.asset_id IS NULL` (orfano):
     2a. Match per `contact.source_url` su `assets.source_url` (esatto):
         se trovato, link `contact.asset_id` a quell'asset e backfill come (1).
     2b. Altrimenti crea "shadow asset" con `asset_type='contact_legacy'`,
         popola tutti i canali + tag `qualifier_legacy=qualified` se
         `contact.qualifier_score` valorizzato (preserva storia).
         Link `contact.asset_id` al nuovo asset.

`outreach_status` derivato da `contact.status`:
  new/qualified/rejected → pending  (nessun outreach ancora avviato)
  contacted              → contacted
  replied                → replied
  optedout               → optedout

Idempotente: rieseguibile, salta i contacts già processati. Le scritture
usano COALESCE per non sovrascrivere valori asset esistenti.

Usage:
    python scripts/backfill_contacts_to_assets.py [--dry-run] [--limit N]

Esempio:
    python scripts/backfill_contacts_to_assets.py --dry-run --limit 100
    python scripts/backfill_contacts_to_assets.py    # full run
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

# Carica .env (per AGENTSCRAPER_SECRET, ecc.) PRIMA di app.config
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
try:
    from dotenv import load_dotenv
    load_dotenv(PROJECT_ROOT / ".env", override=False)
except ImportError:
    pass

from app import db  # noqa: E402


# Mapping status contact → outreach_status asset
_STATUS_MAP = {
    "new": "pending",
    "qualified": "pending",
    "rejected": "pending",
    "contacted": "contacted",
    "replied": "replied",
    "optedout": "optedout",
}


def _outreach_status_of(contact_status: str | None) -> str:
    if not contact_status:
        return "pending"
    return _STATUS_MAP.get(contact_status.lower(), "pending")


def _backfill_linked(con, c: dict, dry_run: bool) -> tuple[bool, str]:
    """Caso (1): contact ha asset_id. Copia campi su asset esistente.
    Ritorna (changed, reason)."""
    aid = int(c["asset_id"])
    # Quali campi del contact sono valorizzati e potrebbero arricchire l'asset?
    fields_to_copy = {
        "display_name": c.get("display_name"),
        "email": c.get("email"),
        "telegram_username": c.get("telegram_username"),
        "telegram_chat_id": c.get("telegram_chat_id"),
        "whatsapp": c.get("whatsapp"),
        "whatsapp_consent": c.get("whatsapp_consent"),
        "whatsapp_last_inbound_at": c.get("whatsapp_last_inbound_at"),
        "social_json": c.get("social_json"),
        "sitoweb": c.get("sitoweb"),
    }
    # Outreach status: lo sovrascriviamo se non è 'pending' (cioè se il contact
    # ha già uno status più avanzato di pending), altrimenti lasciamo l'esistente.
    new_status = _outreach_status_of(c.get("status"))

    # Niente da copiare?
    if not any(v for v in fields_to_copy.values()) and new_status == "pending":
        return (False, "no fields")

    set_parts: list[str] = []
    params: list[Any] = []
    for col, val in fields_to_copy.items():
        if val:  # skip NULL/empty
            set_parts.append(f"{col} = COALESCE({col}, %s)")
            params.append(val)
    if new_status != "pending":
        # Per outreach_status, sovrascriviamo solo se è ancora il default 'pending'.
        set_parts.append(
            "outreach_status = CASE WHEN outreach_status = 'pending' OR outreach_status IS NULL "
            "THEN %s ELSE outreach_status END"
        )
        params.append(new_status)
    if not set_parts:
        return (False, "all NULL")

    sql = f"UPDATE assets SET {', '.join(set_parts)}, updated_at = %s WHERE id = %s"
    params.extend([db.now_iso(), aid])
    if not dry_run:
        con.execute(sql, params)
    return (True, "linked_backfill")


def _try_match_by_source_url(con, source_url: str | None, tenant_id: int | None) -> int | None:
    """Cerca un asset con stesso source_url nello stesso tenant. Ritorna asset.id o None."""
    if not source_url:
        return None
    sql = "SELECT id FROM assets WHERE source_url = %s"
    params: list[Any] = [source_url]
    if tenant_id is not None:
        sql += " AND tenant_id = %s"
        params.append(tenant_id)
    sql += " LIMIT 1"
    row = con.execute(sql, params).fetchone()
    return int(row["id"]) if row else None


def _create_shadow_asset(con, c: dict, dry_run: bool) -> int:
    """Crea un asset 'shadow' dal contact orfano. Ritorna asset.id (0 se dry-run)."""
    if dry_run:
        return 0
    ts = db.now_iso()
    raw = c.get("raw_json") or "{}"
    if not isinstance(raw, str):
        raw = json.dumps(raw, ensure_ascii=False)
    new_status = _outreach_status_of(c.get("status"))

    cur = con.execute(
        """
        INSERT INTO assets (
            asset_type, source_task_id, source_job_id, source_url, source_domain,
            title, raw_json, status, qualifier_score, notes,
            display_name, email, telegram_username, telegram_chat_id,
            whatsapp, whatsapp_consent, whatsapp_last_inbound_at,
            social_json, sitoweb, outreach_status,
            tenant_id, created_by_user_id,
            created_at, updated_at
        ) VALUES (
            'contact_legacy', %s, %s, %s, %s,
            %s, %s, %s, %s, %s,
            %s, %s, %s, %s,
            %s, %s, %s,
            %s, %s, %s,
            %s, %s,
            %s, %s
        ) RETURNING id
        """,
        (
            c.get("source_task_id"), c.get("source_job_id"), c.get("source_url"),
            c.get("source_domain"),
            c.get("display_name") or "Contact (shadow)", raw,
            "qualified" if c.get("status") == "qualified" else "new",
            c.get("qualifier_score"), c.get("notes"),
            c.get("display_name"), c.get("email"),
            c.get("telegram_username"), c.get("telegram_chat_id"),
            c.get("whatsapp"), c.get("whatsapp_consent") or "cold",
            c.get("whatsapp_last_inbound_at"),
            c.get("social_json"), c.get("sitoweb"), new_status,
            c.get("tenant_id"), c.get("created_by_user_id"),
            ts, ts,
        ),
    )
    new_id = int(cur.fetchone()["id"])
    # Se il contact era qualified, copia un tag minimale per preservare la storia
    if c.get("qualifier_score") is not None or c.get("status") == "qualified":
        con.execute(
            "INSERT INTO asset_tags (asset_id, tag_key, tag_value) "
            "VALUES (%s, 'qualifier_legacy', 'qualified') ON CONFLICT DO NOTHING",
            (new_id,),
        )
        if c.get("qualifier_score") is not None:
            con.execute(
                "INSERT INTO asset_tags (asset_id, tag_key, tag_value) "
                "VALUES (%s, 'qualifier_score_legacy', %s) ON CONFLICT DO NOTHING",
                (new_id, str(int(c["qualifier_score"]))),
            )
    return new_id


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true", help="Non scrive nulla, mostra solo cosa farebbe.")
    parser.add_argument("--limit", type=int, default=None, help="Processa solo i primi N contacts (test).")
    args = parser.parse_args()

    print("=" * 60)
    print("Backfill contacts -> assets")
    print(f"  Dry-run: {args.dry_run}")
    print(f"  Limit:   {args.limit or '(no limit)'}")
    print("=" * 60)
    print()

    stats = {
        "total": 0,
        "linked_already": 0,
        "linked_backfill": 0,
        "linked_no_change": 0,
        "orphan_matched_by_url": 0,
        "orphan_shadow_created": 0,
        "errors": 0,
    }

    with db.connect() as con:
        sql = "SELECT * FROM contacts ORDER BY id"
        if args.limit:
            sql += f" LIMIT {int(args.limit)}"
        rows = list(con.execute(sql).fetchall())
        stats["total"] = len(rows)
        print(f"Contacts da processare: {len(rows)}\n")

        for i, c in enumerate(rows):
            try:
                if c.get("asset_id"):
                    # Caso 1: già linkato
                    stats["linked_already"] += 1
                    changed, reason = _backfill_linked(con, c, args.dry_run)
                    if changed:
                        stats["linked_backfill"] += 1
                    else:
                        stats["linked_no_change"] += 1
                else:
                    # Caso 2: orfano
                    matched = _try_match_by_source_url(con, c.get("source_url"), c.get("tenant_id"))
                    if matched:
                        # Link contact a quell'asset + backfill
                        if not args.dry_run:
                            con.execute(
                                "UPDATE contacts SET asset_id = %s, updated_at = %s WHERE id = %s",
                                (matched, db.now_iso(), c["id"]),
                            )
                        c["asset_id"] = matched
                        _backfill_linked(con, c, args.dry_run)
                        stats["orphan_matched_by_url"] += 1
                    else:
                        # Crea shadow asset
                        new_aid = _create_shadow_asset(con, c, args.dry_run)
                        if not args.dry_run and new_aid:
                            con.execute(
                                "UPDATE contacts SET asset_id = %s, updated_at = %s WHERE id = %s",
                                (new_aid, db.now_iso(), c["id"]),
                            )
                        stats["orphan_shadow_created"] += 1
                if (i + 1) % 500 == 0:
                    print(f"  ...{i + 1}/{len(rows)} processati")
            except Exception as exc:
                stats["errors"] += 1
                print(f"  [ERROR] contact id={c.get('id')}: {exc}")

        if not args.dry_run:
            con.commit()

    print()
    print("=" * 60)
    print("RIEPILOGO BACKFILL")
    print("=" * 60)
    for k, v in stats.items():
        print(f"  {k:<28} {v:>6}")
    print()
    if args.dry_run:
        print("[DRY-RUN] Nessuna modifica scritta sul DB.")
    else:
        print("[OK] Backfill completato.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
