"""Helper centralizzato per export CSV di asset (usato da /qualified e /assets).

Definisce:
  - `AVAILABLE_FIELDS`: catalogo dei campi disponibili, raggruppati per
    categoria (core, contact, social, origin, outreach, qualifier, dedup),
    con label human-readable e funzione di estrazione dal row asset.
  - `render_assets_csv(assets, fields, include_tags_as_columns)`: ritorna un
    iteratore di stringhe CSV (header + righe), pronto per StreamingResponse.

CSV format: RFC 4180 con BOM UTF-8 (Excel compat italiano). Quoting QUOTE_MINIMAL.

Per i tag: due modalita' alternative:
  - `tags_mode='columns'`: ogni `tag_key` distinto presente nel dataset
    diventa una colonna; valore = ";"-joined dei tag_value (multi-valore).
  - `tags_mode='flat'`: una sola colonna `tags` con "k=v;k=v;..." (compact).
  - `tags_mode='none'`: niente colonna tag.
"""
from __future__ import annotations

import csv
import io
import json
from collections.abc import Iterable
from typing import Any, Callable

from . import db


def _social_url(row: dict, platform: str) -> str:
    """Estrae l'URL della prima entry di una platform dal social_json."""
    raw = row.get("social_json")
    if not raw:
        return ""
    try:
        items = json.loads(raw) if isinstance(raw, str) else raw
    except (json.JSONDecodeError, TypeError):
        return ""
    if not isinstance(items, list):
        return ""
    for it in items:
        if not isinstance(it, dict):
            continue
        if (it.get("platform") or "").strip().lower() == platform:
            return (it.get("url") or "").strip()
    return ""


def _qualifier_summary(row: dict, asset_tags: dict[int, list[tuple[str, str]]]) -> tuple[str, str]:
    """Da asset_tags estrae:
      - qualifier_slugs: csv di slug attivi (qualifier_<slug>=qualified)
      - qualifier_scores: csv di score (qualifier_score_<slug>=N)
    """
    tags = asset_tags.get(row["id"], [])
    slugs: list[str] = []
    scores: list[str] = []
    for k, v in tags:
        if k.startswith("qualifier_") and not k.startswith("qualifier_score_"):
            slug = k[len("qualifier_"):]
            if (v or "").lower() == "qualified":
                slugs.append(slug)
        elif k.startswith("qualifier_score_"):
            slug = k[len("qualifier_score_"):]
            scores.append(f"{slug}={v}")
    return (",".join(slugs), ",".join(scores))


# Catalogo campi: {category: [(field_key, label, extractor_fn)]}.
# Extractor riceve (row, asset_tags_dict) e ritorna str.
def _v(row: dict, key: str) -> str:
    v = row.get(key)
    return "" if v is None else str(v)


FIELD_CATEGORIES: dict[str, list[tuple[str, str, Callable[[dict, dict], str]]]] = {
    "core": [
        ("id", "ID", lambda r, _t: _v(r, "id")),
        ("asset_type", "Tipo asset", lambda r, _t: _v(r, "asset_type")),
        ("title", "Titolo", lambda r, _t: _v(r, "title")),
        ("status", "Status", lambda r, _t: _v(r, "status")),
        ("qualifier_score", "Score qualifier", lambda r, _t: _v(r, "qualifier_score")),
        ("notes", "Note", lambda r, _t: _v(r, "notes")),
        ("created_at", "Creato il", lambda r, _t: _v(r, "created_at")),
        ("updated_at", "Aggiornato il", lambda r, _t: _v(r, "updated_at")),
        ("source_url", "URL sorgente", lambda r, _t: _v(r, "source_url")),
        ("source_url_canonical", "URL canonico", lambda r, _t: _v(r, "source_url_canonical")),
    ],
    "contact": [
        ("display_name", "Display name", lambda r, _t: _v(r, "display_name")),
        ("email", "Email", lambda r, _t: _v(r, "email")),
        ("whatsapp", "WhatsApp", lambda r, _t: _v(r, "whatsapp")),
        ("whatsapp_consent", "WA consent", lambda r, _t: _v(r, "whatsapp_consent")),
        ("telegram_username", "TG username", lambda r, _t: _v(r, "telegram_username")),
        ("telegram_chat_id", "TG chat_id", lambda r, _t: _v(r, "telegram_chat_id")),
        ("sitoweb", "Sito web", lambda r, _t: _v(r, "sitoweb")),
    ],
    "social": [
        ("social_instagram", "Instagram URL", lambda r, _t: _social_url(r, "instagram")),
        ("social_tiktok", "TikTok URL", lambda r, _t: _social_url(r, "tiktok")),
        ("social_facebook", "Facebook URL", lambda r, _t: _social_url(r, "facebook")),
        ("social_onlyfans", "OnlyFans URL", lambda r, _t: _social_url(r, "onlyfans")),
        ("social_json_raw", "social_json (raw)", lambda r, _t: _v(r, "social_json")),
    ],
    "origin": [
        ("source_task_id", "Task sorgente", lambda r, _t: _v(r, "source_task_id")),
        ("source_job_id", "Job sorgente", lambda r, _t: _v(r, "source_job_id")),
        ("source_domain", "Dominio sorgente", lambda r, _t: _v(r, "source_domain")),
    ],
    "outreach": [
        ("outreach_status", "Outreach status", lambda r, _t: _v(r, "outreach_status")),
        ("whatsapp_last_inbound_at", "WA ultimo inbound", lambda r, _t: _v(r, "whatsapp_last_inbound_at")),
    ],
    "qualifier": [
        ("qualifier_slugs", "Qualifier (slugs)", lambda r, t: _qualifier_summary(r, t)[0]),
        ("qualifier_scores", "Qualifier (scores)", lambda r, t: _qualifier_summary(r, t)[1]),
    ],
    "dedup": [
        ("dedup_status", "Dedup status", lambda r, _t: _v(r, "dedup_status")),
        ("dedup_canonical_id", "Dedup canonical_id", lambda r, _t: _v(r, "dedup_canonical_id")),
    ],
}

# Default selection: campi piu' utili per outreach/analisi.
DEFAULT_FIELDS = {
    "id", "asset_type", "title", "status",
    "email", "whatsapp", "telegram_username",
    "social_instagram", "social_tiktok", "social_facebook",
    "source_task_id",
}


def all_field_keys() -> list[str]:
    return [f[0] for cat in FIELD_CATEGORIES.values() for f in cat]


def _load_tags_for_assets(asset_ids: list[int]) -> dict[int, list[tuple[str, str]]]:
    """Fetcha tag (key, value) per ogni asset in un singolo round-trip."""
    if not asset_ids:
        return {}
    out: dict[int, list[tuple[str, str]]] = {aid: [] for aid in asset_ids}
    with db.connect() as con:
        placeholders = ",".join(["%s"] * len(asset_ids))
        rows = con.execute(
            f"SELECT asset_id, tag_key, tag_value FROM asset_tags "
            f"WHERE asset_id IN ({placeholders})",
            tuple(asset_ids),
        ).fetchall()
    for r in rows:
        out.setdefault(int(r["asset_id"]), []).append((r["tag_key"], r["tag_value"]))
    return out


def render_assets_csv(
    assets: list[dict],
    fields: list[str],
    *,
    tags_mode: str = "none",  # 'none' | 'flat' | 'columns'
) -> Iterable[bytes]:
    """Generator che yelda chunk CSV (bytes UTF-8 con BOM).

    `fields`: lista di field_key (vedi FIELD_CATEGORIES). I non riconosciuti
    sono skippati silenziosamente.

    `tags_mode`:
      - 'none': niente colonna tag
      - 'flat': una colonna 'tags' con "k=v;k=v;..."
      - 'columns': una colonna per ogni tag_key distinto presente
    """
    # Build flat catalog di estrattori validi
    catalog: dict[str, tuple[str, Callable]] = {}
    for cat_fields in FIELD_CATEGORIES.values():
        for key, label, fn in cat_fields:
            catalog[key] = (label, fn)
    selected = [f for f in fields if f in catalog]
    if not selected:
        selected = list(DEFAULT_FIELDS)
        selected = [f for f in selected if f in catalog]

    asset_ids = [a["id"] for a in assets if a.get("id") is not None]
    asset_tags = _load_tags_for_assets(asset_ids)

    # Tag columns: se 'columns', raccogli tutte le chiavi distinte (ordinate).
    tag_keys_sorted: list[str] = []
    if tags_mode == "columns":
        seen = set()
        for tags in asset_tags.values():
            for k, _v in tags:
                if k not in seen:
                    seen.add(k)
                    tag_keys_sorted.append(k)
        tag_keys_sorted.sort()

    # Header row
    header = [catalog[f][0] for f in selected]
    if tags_mode == "flat":
        header.append("Tags")
    elif tags_mode == "columns":
        header.extend([f"tag:{k}" for k in tag_keys_sorted])

    # Buffer + writer
    def _row_to_csv(row_values: list[str]) -> bytes:
        buf = io.StringIO()
        w = csv.writer(buf, quoting=csv.QUOTE_MINIMAL, lineterminator="\n")
        w.writerow(row_values)
        return buf.getvalue().encode("utf-8")

    # BOM UTF-8 per Excel italiano
    yield b"\xef\xbb\xbf"
    yield _row_to_csv(header)

    for a in assets:
        row_values: list[str] = []
        for f in selected:
            _label, fn = catalog[f]
            try:
                row_values.append(fn(a, asset_tags))
            except Exception:
                row_values.append("")
        if tags_mode == "flat":
            tags = asset_tags.get(a["id"], [])
            row_values.append(";".join(f"{k}={v}" for k, v in tags))
        elif tags_mode == "columns":
            by_key: dict[str, list[str]] = {}
            for k, v in asset_tags.get(a["id"], []):
                by_key.setdefault(k, []).append(v)
            for k in tag_keys_sorted:
                row_values.append(";".join(by_key.get(k, [])))
        yield _row_to_csv(row_values)
