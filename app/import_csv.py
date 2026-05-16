"""Core logic per import CSV → tabelle `assets` + `asset_tags` + `contacts`.

Architettura:
  - `parse_csv_safe(path)` legge il file con fallback encoding e cap sicurezza.
  - `parse_mapping(form)` traduce i dropdown del wizard step2 in `ColumnMapping`.
  - `plan_row(headers, row, mapping)` calcola cosa fare per UNA riga (dry-run).
  - `execute_import(...)` orchestra il loop: upsert_asset + upsert_contact +
    asset_tags, in dry-run o produzione.

Niente Request/UploadFile qui — solo path/file/list. Testabile in isolamento.
"""
from __future__ import annotations

import csv
import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from . import db

log = logging.getLogger(__name__)

# Cap sicurezza per parsing
MAX_ROWS = 50_000
MAX_COLS = 200
MAX_CELL_LEN = 5 * 1024 * 1024  # 5 MB per cella
ALLOWED_ENCODINGS = ("utf-8", "utf-8-sig", "latin-1")

# Regex sanitarie
_ASSET_TYPE_RE = re.compile(r"^[a-z][a-z0-9_-]{0,49}$")
_TAG_KEY_RE = re.compile(r"^[a-z][a-z0-9_-]{0,49}$")


# === Target field catalog ===
# Ogni voce è (kind, target_field, label_umana). Usato dal template step2
# per popolare i dropdown.
TARGET_FIELDS: list[tuple[str, str, str]] = [
    # Asset core
    ("asset", "title", "Asset · nome (title)"),
    ("asset", "source_url", "Asset · URL canonico (dedup key)"),
    ("asset", "notes", "Asset · note libere"),
    # Contact
    ("contact", "display_name", "Contatto · nome visualizzato"),
    ("contact", "email", "Contatto · email"),
    ("contact", "telegram_username", "Contatto · @telegram"),
    ("contact", "whatsapp", "Contatto · WhatsApp"),
    ("contact", "sitoweb", "Contatto · sito web"),
    # Social (raggruppati in social_json)
    ("social", "instagram.handle", "Social · Instagram handle"),
    ("social", "instagram.url", "Social · Instagram URL"),
    ("social", "facebook.url", "Social · Facebook URL"),
    ("social", "tiktok.url", "Social · TikTok URL"),
    # Tag (declarative — il tag_key è scelto dall'utente nella UI)
    ("tag", "", "Tag asset (chiave personalizzata)"),
    # Ignore
    ("ignore", "", "(ignora questa colonna)"),
]


@dataclass(frozen=True)
class ColumnMapping:
    column_index: int
    column_name: str
    target_kind: Literal["asset", "contact", "social", "tag", "ignore"]
    target_field: str  # "title", "instagram.handle", ... | "" per tag/ignore
    tag_key: str | None = None  # solo quando target_kind == "tag"


@dataclass
class ImportPlanRow:
    row_index: int
    action: Literal["insert", "update", "skip"]
    asset_data: dict[str, Any] = field(default_factory=dict)
    asset_tags: dict[str, list[str]] = field(default_factory=dict)
    contact_data: dict[str, Any] | None = None
    reason: str | None = None


@dataclass
class ImportStats:
    total: int = 0
    inserted: int = 0
    updated: int = 0
    skipped: int = 0
    errors: int = 0
    skip_reasons: dict[str, int] = field(default_factory=dict)
    sample_rows: list[ImportPlanRow] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "total": self.total,
            "inserted": self.inserted,
            "updated": self.updated,
            "skipped": self.skipped,
            "errors": self.errors,
            "skip_reasons": dict(self.skip_reasons),
            "sample_rows": [
                {
                    "row_index": r.row_index,
                    "action": r.action,
                    "asset_data": r.asset_data,
                    "asset_tags": r.asset_tags,
                    "contact_data": r.contact_data,
                    "reason": r.reason,
                }
                for r in self.sample_rows
            ],
        }


# === CSV parsing ===

def parse_csv_safe(
    path: Path,
    *,
    max_rows: int | None = None,
) -> tuple[list[str], list[list[str]], list[str]]:
    """Apre un CSV con encoding fallback. Ritorna (headers, rows, warnings).

    Salta righe con `len != len(headers)` (con warning). Trunca a `max_rows` o
    a MAX_ROWS. Si ferma se trova celle oltre MAX_CELL_LEN.
    """
    warnings: list[str] = []
    text = None
    used_enc = None
    raw_bytes = path.read_bytes()
    for enc in ALLOWED_ENCODINGS:
        try:
            text = raw_bytes.decode(enc)
            used_enc = enc
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        raise ValueError("Impossibile decodificare il CSV (encoding non riconosciuto)")
    if used_enc != "utf-8":
        warnings.append(f"encoding usato: {used_enc} (Excel/legacy)")

    reader = csv.reader(text.splitlines())
    try:
        headers = next(reader)
    except StopIteration:
        return [], [], ["CSV vuoto"]

    headers = [h.strip() for h in headers]
    if len(headers) > MAX_COLS:
        raise ValueError(f"Troppe colonne ({len(headers)} > {MAX_COLS})")
    if any(len(h) > 200 for h in headers):
        warnings.append("Alcuni header sono molto lunghi (>200 char)")

    cap = min(max_rows or MAX_ROWS, MAX_ROWS)
    rows: list[list[str]] = []
    for i, row in enumerate(reader, start=2):  # riga 2 = primo dato
        if len(rows) >= cap:
            warnings.append(f"Limite righe ({cap}) raggiunto, troncato")
            break
        if len(row) != len(headers):
            warnings.append(f"riga {i}: {len(row)} colonne (atteso {len(headers)}) → skip")
            continue
        # Verifica celle non troppo grandi
        oversized = any(len(c) > MAX_CELL_LEN for c in row)
        if oversized:
            warnings.append(f"riga {i}: cella oversize, skip")
            continue
        rows.append([c.strip() for c in row])
    return headers, rows, warnings


# === Mapping parsing ===

def parse_mapping(form_dict: dict[str, str], headers: list[str]) -> list[ColumnMapping]:
    """Da un dict di form fields tipo `map__0=asset.title`, `map__3=tag`,
    `tag_key__3=municipality` → list[ColumnMapping].

    Form convention:
      - `map__<i>` = "kind.field" oppure "kind" (per tag/ignore)
      - `tag_key__<i>` = chiave del tag (lower, snake-case) — solo se kind=tag
    """
    out: list[ColumnMapping] = []
    for i, col_name in enumerate(headers):
        raw = (form_dict.get(f"map__{i}") or "").strip()
        if not raw or raw == "ignore":
            out.append(ColumnMapping(i, col_name, "ignore", ""))
            continue

        if "." in raw:
            kind, field_part = raw.split(".", 1)
        else:
            kind, field_part = raw, ""

        if kind not in ("asset", "contact", "social", "tag", "ignore"):
            out.append(ColumnMapping(i, col_name, "ignore", ""))
            continue

        tag_key = None
        if kind == "tag":
            tag_key_raw = (form_dict.get(f"tag_key__{i}") or "").strip().lower()
            tag_key_raw = re.sub(r"\s+", "_", tag_key_raw)
            if not _TAG_KEY_RE.match(tag_key_raw):
                # Tag senza chiave valida → ignora
                out.append(ColumnMapping(i, col_name, "ignore", ""))
                continue
            tag_key = tag_key_raw

        out.append(ColumnMapping(i, col_name, kind, field_part, tag_key=tag_key))  # type: ignore[arg-type]
    return out


def suggest_mapping(headers: list[str]) -> dict[int, tuple[str, str]]:
    """Euristica: per ogni header propone un (kind.field) sensato.

    Pattern:
      - "name"/"nome"/"business_name"/"title" → asset.title
      - "url"/"link" + "instagram" → social.instagram.url
      - "handle"/"@" + "instagram" → social.instagram.handle
      - "email" → contact.email
      - "phone"/"whatsapp"/"telefono" → contact.whatsapp
      - "sitoweb"/"website" → contact.sitoweb
      - "municipality"/"comune"/"city"/"citta" → tag(municipality)
      - "address"/"indirizzo" → tag(address)
      - "category"/"categoria"/"tipo" → tag(category)
      - Altrimenti → tag(<header_normalizzato>)
    """
    out: dict[int, tuple[str, str]] = {}
    for i, h in enumerate(headers):
        low = h.lower().strip()
        if any(k in low for k in ("business_name", "nome_palestra", "nome_centro", "title")):
            out[i] = ("asset.title", "")
        elif "instagram" in low and ("url" in low or "link" in low):
            out[i] = ("social.instagram.url", "")
            # NB: per il dedup useremo source_url. Lo step di mapping può
            # essere cambiato dall'utente in asset.source_url se vuole.
        elif "instagram" in low and ("handle" in low or "@" in low or "user" in low):
            out[i] = ("social.instagram.handle", "")
        elif "facebook" in low and ("url" in low or "link" in low):
            out[i] = ("social.facebook.url", "")
        elif "tiktok" in low and ("url" in low or "link" in low):
            out[i] = ("social.tiktok.url", "")
        elif "email" in low or "mail" in low:
            out[i] = ("contact.email", "")
        elif "whatsapp" in low or "telefono" in low or "phone" in low:
            out[i] = ("contact.whatsapp", "")
        elif "sitoweb" in low or "website" in low or low == "site":
            out[i] = ("contact.sitoweb", "")
        elif "municipality" in low or "comune" in low or "city" in low or "citta" in low:
            out[i] = ("tag", "municipality")
        elif "address" in low or "indirizzo" in low or "via" in low:
            out[i] = ("tag", "address")
        elif "category" in low or "categoria" in low or "tipo" in low:
            out[i] = ("tag", "category")
        elif low == "id":
            out[i] = ("ignore", "")
        elif "source_url" in low or low.endswith("_url") or low == "url" or low == "link":
            out[i] = ("asset.source_url", "")
        else:
            tag_key = re.sub(r"[^a-z0-9_-]", "_", low)[:50] or "extra"
            out[i] = ("tag", tag_key)
    return out


# === Planning ===

def _build_social_json(socials_partial: dict[str, dict[str, str]]) -> list[dict[str, str]]:
    """Da {platform: {handle: '...', url: '...'}} → [{platform, handle, url}, ...]."""
    result: list[dict[str, str]] = []
    for platform, fields in socials_partial.items():
        if not any(v for v in fields.values()):
            continue
        entry: dict[str, str] = {"platform": platform}
        for k, v in fields.items():
            if v:
                entry[k] = v
        result.append(entry)
    return result


def plan_row(
    headers: list[str],
    row: list[str],
    mapping: list[ColumnMapping],
    asset_type: str,
    import_name: str,
) -> ImportPlanRow:
    """Pianifica cosa fare per una riga. Non tocca il DB."""
    asset_data: dict[str, Any] = {"asset_type": asset_type}
    asset_tags: dict[str, list[str]] = {}
    socials: dict[str, dict[str, str]] = {}
    contact_data: dict[str, Any] = {}
    raw_row: dict[str, str] = {}

    for m in mapping:
        if m.column_index >= len(row):
            continue
        value = row[m.column_index].strip()
        if value:
            raw_row[m.column_name or f"col_{m.column_index}"] = value
        if not value:
            continue
        if m.target_kind == "asset":
            # asset.<field>
            asset_data[m.target_field] = value
        elif m.target_kind == "contact":
            contact_data[m.target_field] = value
        elif m.target_kind == "social":
            # social.<platform>.<field>
            parts = m.target_field.split(".", 1)
            if len(parts) == 2:
                platform, sub = parts
                socials.setdefault(platform, {})[sub] = value
        elif m.target_kind == "tag":
            if m.tag_key:
                asset_tags.setdefault(m.tag_key, []).append(value)
        # ignore: skip

    # raw_json (sempre presente per audit)
    asset_data["raw_json"] = json.dumps(raw_row, ensure_ascii=False)
    if import_name:
        prev_notes = asset_data.get("notes") or ""
        prefix = f"[import:{import_name}] "
        if not prev_notes.startswith(prefix):
            asset_data["notes"] = prefix + prev_notes if prev_notes else prefix.strip()

    # Validazione minima: title + source_url devono esistere
    title = (asset_data.get("title") or "").strip()
    source_url = (asset_data.get("source_url") or "").strip()
    if not title:
        return ImportPlanRow(row_index=0, action="skip", reason="missing_title")
    if not source_url:
        # Provo a usare Instagram URL come fallback se mappato
        ig_url = socials.get("instagram", {}).get("url", "")
        if ig_url:
            asset_data["source_url"] = ig_url
            source_url = ig_url
        else:
            return ImportPlanRow(row_index=0, action="skip", reason="missing_source_url")

    # Costruisci contact_data finale (se almeno un campo contact o social valorizzato)
    if socials:
        contact_data["social"] = _build_social_json(socials)
    if contact_data:
        contact_data.setdefault("display_name", title)
        contact_data.setdefault("source_url", source_url)
        contact_data.setdefault("status", "new")
    else:
        contact_data_out: dict[str, Any] | None = None  # type: ignore[assignment]
    contact_data_out = contact_data if contact_data else None

    # action: insert/update verrà determinato in execute_import a contatto col DB.
    return ImportPlanRow(
        row_index=0,
        action="insert",  # placeholder, sostituito in execute_import
        asset_data=asset_data,
        asset_tags=asset_tags,
        contact_data=contact_data_out,
    )


# === Execution ===

def execute_import(
    csv_path: Path,
    mapping: list[ColumnMapping],
    *,
    asset_type: str,
    import_name: str,
    description: str = "",
    dry_run: bool = False,
    sample_size: int = 20,
) -> ImportStats:
    """Loop sulle righe, plan_row + (se non dry-run) upsert_asset + upsert_contact.

    Ritorna ImportStats con sample dei primi `sample_size` plan rows.
    """
    stats = ImportStats()

    if not _ASSET_TYPE_RE.match(asset_type):
        raise ValueError(
            f"asset_type non valido '{asset_type}' — usa lowercase, a-z 0-9 _ -, max 50"
        )

    headers, rows, _ = parse_csv_safe(csv_path)
    stats.total = len(rows)

    from .agent.url_canonical import canonical_url

    for idx, row in enumerate(rows, start=2):  # riga 2 = primo dato
        try:
            plan = plan_row(headers, row, mapping, asset_type=asset_type, import_name=import_name)
            plan.row_index = idx

            if plan.action == "skip":
                stats.skipped += 1
                stats.skip_reasons[plan.reason or "unknown"] = (
                    stats.skip_reasons.get(plan.reason or "unknown", 0) + 1
                )
                if len(stats.sample_rows) < sample_size:
                    stats.sample_rows.append(plan)
                continue

            if dry_run:
                # Determina insert vs update consultando il canonical del DB
                src_url = plan.asset_data.get("source_url")
                canon = canonical_url(src_url) if src_url else None
                with db.connect() as con:
                    row_exist = None
                    if canon:
                        row_exist = con.execute(
                            "SELECT id FROM assets WHERE source_url_canonical = %s "
                            "AND asset_type = %s LIMIT 1",
                            (canon, asset_type),
                        ).fetchone()
                    if not row_exist and src_url:
                        row_exist = con.execute(
                            "SELECT id FROM assets WHERE source_url = %s "
                            "AND asset_type = %s LIMIT 1",
                            (src_url, asset_type),
                        ).fetchone()
                plan.action = "update" if row_exist else "insert"
                if plan.action == "insert":
                    stats.inserted += 1
                else:
                    stats.updated += 1
            else:
                # Esegui upsert vero
                asset_id = db.upsert_asset(plan.asset_data, tags=plan.asset_tags)
                plan.action = "insert"  # upsert_asset non distingue, lo classifichiamo dopo
                if plan.contact_data:
                    plan.contact_data["asset_id"] = asset_id
                    db.upsert_contact(plan.contact_data)
                stats.inserted += 1  # approssimato; chi vuole dettaglio usa dry-run

            if len(stats.sample_rows) < sample_size:
                stats.sample_rows.append(plan)

        except Exception as e:
            log.exception("import row %d failed", idx)
            stats.errors += 1
            stats.skip_reasons[f"error_{type(e).__name__}"] = (
                stats.skip_reasons.get(f"error_{type(e).__name__}", 0) + 1
            )

    return stats
