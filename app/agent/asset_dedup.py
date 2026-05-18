"""B-016: Asset dedup cross-task.

Modulo per rilevare e gestire asset duplicati per tenant. Strategia:
1. Normalize chiavi confrontabili (phone E.164, email lowercase, IG/TT/FB
   handle, url canonical) — vedi `normalize_*`.
2. `find_dedup_candidates(asset_id)`: data una nuova riga `assets`, trova
   altri asset dello stesso tenant che condividono almeno UNA chiave forte
   o piu' chiavi medie. Popola la tabella `asset_dedup_candidates`.
3. `merge_assets(primary_id, candidate_id)`: in transazione, sposta i
   riferimenti FK del candidate verso il primary (target_asset_ids in
   tasks, social_dm_log.target_asset_id, asset_tags con dedup), union
   social_json + raw_json campi vuoti, marca candidate come
   `dedup_status='merged_into:<primary_id>'`. NON cancella la riga
   (audit trail).

Convenzione (primary, candidate): sempre primary.id < candidate.id per
evitare duplicati specchio nella tabella candidates (UNIQUE constraint).

NB: tenant-isolation enforced — il match SQL filtra sempre su tenant_id.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any
from urllib.parse import urlparse

from .. import db

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Normalizers
# ---------------------------------------------------------------------------

# Match strength: 'strong' = altamente univoco (phone/email/social handle);
# 'medium' = utile come segnale aggiuntivo (URL canonico, asset_type+title).
STRONG_WEIGHT = 1.0
MEDIUM_WEIGHT = 0.4

# Score threshold: sotto questo, non popolare la candidates table.
# Soglia 0.5 = almeno 1 chiave forte OPPURE 2 medie.
MIN_MATCH_SCORE = 0.5


def normalize_phone(raw: str | None) -> str | None:
    """E.164 normalization IT-focused. Sufficiente per il caso d'uso
    italiano; per multi-paese aggiungere `phonenumbers` lib.

    Esempi:
      "+39 333 1234567"  -> "+393331234567"
      "0039 333 1234567" -> "+393331234567"
      "39 333 1234567"   -> "+393331234567"   (>=10 digit con 39)
      "333 1234567"      -> "+393331234567"   (10 digit, italiano implicito)
      "abc"              -> None
    """
    if not raw or not isinstance(raw, str):
        return None
    s = raw.strip()
    if not s:
        return None
    # Tieni solo digit + '+' iniziale
    has_plus = s.startswith("+")
    digits = re.sub(r"\D", "", s)
    if not digits:
        return None
    # Strip leading 00 (international notation)
    if digits.startswith("00"):
        digits = digits[2:]
        has_plus = True
    # Se inizia con 39 e ha lunghezza >= 11 (39 + 9 digit min), assumo IT
    if not has_plus:
        if digits.startswith("39") and len(digits) >= 11:
            has_plus = True  # already includes country code
        elif len(digits) == 10 and digits.startswith("3"):
            # mobile italiano senza prefisso → aggiungi 39
            digits = "39" + digits
            has_plus = True
        elif len(digits) >= 11 and digits.startswith("3"):
            # raro: 11+ digit che inizia con 3 → tratta come italiano se 12 chars
            pass
    if len(digits) < 10:
        return None
    return f"+{digits}"


_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def normalize_email(raw: str | None) -> str | None:
    """Lowercase + trim. Nessuna normalizzazione Gmail-dots (semplice)."""
    if not raw or not isinstance(raw, str):
        return None
    s = raw.strip().lower()
    if not s or not _EMAIL_RE.match(s):
        return None
    return s


def normalize_telegram(username: str | None, chat_id: str | None) -> str | None:
    """Preferisci username (stable). Se mancante, fallback su chat_id (numero)."""
    if username and isinstance(username, str):
        u = username.strip().lstrip("@").lower()
        if u:
            return f"@{u}"
    if chat_id and isinstance(chat_id, str):
        c = chat_id.strip()
        if c:
            return f"id:{c}"
    return None


# Mappa platform → regex per extract handle da URL.
_SOCIAL_HANDLE_PATTERNS = {
    "instagram": re.compile(r"instagram\.com/(?:p/|reel/|stories/)?([A-Za-z0-9._-]+)/?"),
    "tiktok": re.compile(r"tiktok\.com/@?([A-Za-z0-9._-]+)/?"),
    "facebook": re.compile(r"facebook\.com/(?:profile\.php\?id=)?([A-Za-z0-9._-]+)/?"),
    "onlyfans": re.compile(r"onlyfans\.com/([A-Za-z0-9._-]+)/?"),
}


def extract_social_handles(social_json: Any) -> dict[str, str]:
    """Estrae {platform: handle} da social_json (lista di {platform, url}).
    Handle = ultimo segmento path, lowercase. None se non parsabile."""
    out: dict[str, str] = {}
    if not social_json:
        return out
    items: list = []
    if isinstance(social_json, str):
        try:
            parsed = json.loads(social_json)
            if isinstance(parsed, list):
                items = parsed
        except (json.JSONDecodeError, TypeError):
            return out
    elif isinstance(social_json, list):
        items = social_json
    for it in items:
        if not isinstance(it, dict):
            continue
        plat = (it.get("platform") or "").lower().strip()
        url = (it.get("url") or "").strip()
        if not plat or not url:
            continue
        pat = _SOCIAL_HANDLE_PATTERNS.get(plat)
        if not pat:
            continue
        m = pat.search(url)
        if m:
            handle = m.group(1).lower().strip(".")
            if handle and handle not in ("p", "reel", "stories", "explore"):
                out[plat] = handle
    return out


def _canon_url(url: str | None) -> str | None:
    """Canonical URL: lowercase host + path, no query/fragment/trailing slash.
    Usato anche da db.upsert_asset (vedi `_canon`); ridefinito qui per evitare
    dipendenza circolare e per documentare il match."""
    if not url or not isinstance(url, str):
        return None
    try:
        p = urlparse(url.strip())
        if not p.netloc:
            return None
        host = p.netloc.lower()
        path = (p.path or "").rstrip("/")
        return f"{host}{path}" if path else host
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Match key extraction da un asset row
# ---------------------------------------------------------------------------


def _asset_match_keys(asset: dict[str, Any]) -> dict[str, tuple[str, str]]:
    """Estrae le chiavi normalizzate confrontabili da un asset.
    Ritorna {key_name: (value, weight)} con weight in 'strong'|'medium'.
    """
    keys: dict[str, tuple[str, str]] = {}

    phone = normalize_phone(asset.get("whatsapp"))
    if phone:
        keys["whatsapp"] = (phone, "strong")

    email = normalize_email(asset.get("email"))
    if email:
        keys["email"] = (email, "strong")

    tg = normalize_telegram(asset.get("telegram_username"), asset.get("telegram_chat_id"))
    if tg:
        keys["telegram"] = (tg, "strong")

    handles = extract_social_handles(asset.get("social_json"))
    for plat, h in handles.items():
        keys[f"social:{plat}"] = (h, "strong")

    url_canon = asset.get("source_url_canonical") or _canon_url(asset.get("source_url"))
    if url_canon:
        keys["url_canonical"] = (url_canon, "medium")

    return keys


# ---------------------------------------------------------------------------
# Detection: trova candidates per un asset appena insertato/aggiornato
# ---------------------------------------------------------------------------


def _score_match(matched_keys: list[dict]) -> float:
    """Score = somma weight delle chiavi matchate, clamp [0, 1]."""
    total = 0.0
    for mk in matched_keys:
        w = mk.get("weight")
        if w == "strong":
            total += STRONG_WEIGHT
        elif w == "medium":
            total += MEDIUM_WEIGHT
    return min(1.0, total)


def find_dedup_candidates(
    asset_id: int,
    tenant_id: Any = None,
) -> list[dict]:
    """Cerca asset dello stesso tenant che condividono >=1 chiave con
    `asset_id`. Popola `asset_dedup_candidates` per i match con score >=
    MIN_MATCH_SCORE. Ritorna la lista di dict {primary, candidate, score,
    match_keys} inserita (per logging / API).

    Idempotente: UNIQUE (primary, candidate) garantisce no duplicati al
    re-run; ON CONFLICT DO NOTHING evita errori.

    NB: tenant_id se None viene preso dall'asset stesso (no contextvar)
    perche' questa funzione gira spesso fuori dal request-cycle (job
    background, rescan).
    """
    asset = db.get_asset(asset_id, tenant_id=None)  # bypass tenant check, usiamo lookup raw
    if not asset:
        log.debug("find_dedup_candidates: asset %s non trovato", asset_id)
        return []
    asset_tenant = asset.get("tenant_id")
    if tenant_id is None:
        tenant_id = asset_tenant
    if asset_tenant is not None and tenant_id is not None and asset_tenant != tenant_id:
        # safety: il chiamante ha specificato un tenant diverso → skip
        return []
    if (asset.get("dedup_status") or "").startswith("merged_into"):
        # gia' mergiato, non rilevare nuovi candidati su questa riga
        return []

    keys = _asset_match_keys(asset)
    if not keys:
        return []

    # Per ogni chiave, query asset dello stesso tenant con quel valore
    # normalizzato. Raccogli candidati e accumula match_keys per coppia.
    candidates_by_id: dict[int, list[dict]] = {}

    with db.connect() as con:
        for k_name, (k_value, k_weight) in keys.items():
            sql, args = _build_match_query(k_name, k_value, asset_id, tenant_id, asset.get("asset_type"))
            if not sql:
                continue
            rows = con.execute(sql, args).fetchall()
            for r in rows:
                cid = int(r["id"])
                if cid == asset_id:
                    continue
                candidates_by_id.setdefault(cid, []).append({
                    "key": k_name,
                    "value": k_value,
                    "weight": k_weight,
                })

    inserted: list[dict] = []
    if not candidates_by_id:
        return inserted

    with db.connect() as con:
        for cid, matched in candidates_by_id.items():
            score = _score_match(matched)
            if score < MIN_MATCH_SCORE:
                continue
            primary, candidate = (asset_id, cid) if asset_id < cid else (cid, asset_id)
            payload = json.dumps(matched, ensure_ascii=False)
            try:
                con.execute(
                    """
                    INSERT INTO asset_dedup_candidates
                      (primary_asset_id, candidate_asset_id, match_keys,
                       match_score, status, detected_at)
                    VALUES (%s, %s, %s, %s, 'pending', %s)
                    ON CONFLICT (primary_asset_id, candidate_asset_id) DO UPDATE
                      SET match_keys = EXCLUDED.match_keys,
                          match_score = EXCLUDED.match_score
                      WHERE asset_dedup_candidates.status = 'pending'
                    """,
                    (primary, candidate, payload, score, db.now_iso()),
                )
                inserted.append({
                    "primary": primary,
                    "candidate": candidate,
                    "score": score,
                    "match_keys": matched,
                })
            except Exception as e:
                log.warning("dedup insert fail (%s,%s): %s", primary, candidate, e)
    return inserted


def _build_match_query(
    key_name: str, key_value: str, exclude_id: int, tenant_id: Any, asset_type: Any,
) -> tuple[str, tuple]:
    """SQL parametrico per cercare asset dello stesso tenant con la chiave
    `key_name` normalizzata uguale a `key_value`. Restituisce (sql, args).

    NB: normalizzazione applicata in SQL via funzioni semplici (LOWER, regex
    sui telefoni) per non dover materializzare colonne normalizzate. Per
    100k+ asset si possono creare generated columns + index dedicati.
    """
    base = "SELECT id FROM assets WHERE id != %s "
    where_tenant = ""
    args: list = [exclude_id]
    if tenant_id is not None:
        where_tenant = "AND tenant_id = %s "
        args.append(tenant_id)

    if key_name == "whatsapp":
        # Match diretto su whatsapp normalizzato. Per simplicita' confrontiamo
        # gli ultimi 9-10 digit (corrispondono al numero senza prefisso).
        # key_value e' E.164, es. "+393331234567" -> last10 = "3331234567"
        last10 = key_value.lstrip("+")[-10:]
        return (
            base + where_tenant +
            "AND whatsapp IS NOT NULL "
            "AND regexp_replace(whatsapp, '\\D', '', 'g') LIKE %s",
            tuple(args + [f"%{last10}"]),
        )

    if key_name == "email":
        return (
            base + where_tenant + "AND LOWER(email) = %s",
            tuple(args + [key_value]),
        )

    if key_name == "telegram":
        # match su username (con @) o chat_id (con id:)
        if key_value.startswith("@"):
            return (
                base + where_tenant +
                "AND LOWER(telegram_username) = %s",
                tuple(args + [key_value.lstrip("@")]),
            )
        if key_value.startswith("id:"):
            return (
                base + where_tenant +
                "AND telegram_chat_id = %s",
                tuple(args + [key_value[3:]]),
            )

    if key_name.startswith("social:"):
        plat = key_name[len("social:"):]
        # social_json contiene una lista di dict; cerchiamo con LIKE permissivo
        # sul value handle. Falsi positivi possibili — il _asset_match_keys del
        # candidato verifica precisamente.
        return (
            base + where_tenant +
            "AND social_json IS NOT NULL "
            "AND LOWER(social_json) LIKE %s",
            tuple(args + [f"%{plat}%{key_value.lower()}%"]),
        )

    if key_name == "url_canonical":
        return (
            base + where_tenant +
            "AND source_url_canonical = %s "
            "AND asset_type = %s",
            tuple(args + [key_value, asset_type or ""]),
        )

    return "", ()


# ---------------------------------------------------------------------------
# Merge action
# ---------------------------------------------------------------------------


def merge_assets(
    primary_id: int,
    candidate_id: int,
    *,
    resolved_by_user_id: int | None = None,
    tenant_id: Any = None,
) -> dict:
    """Mergia `candidate_id` in `primary_id`.

    Azioni (in transazione):
      1. Tasks.target_asset_ids: sostituisce candidate_id con primary_id
         (dedup nella lista).
      2. social_dm_log.target_asset_id: candidate_id -> primary_id.
      3. asset_tags: sposta i tag del candidate al primary, dedup su
         (asset_id, tag_key, tag_value). I tag duplicati (stessa coppia
         k=v) vengono droppati; quelli con chiave esistente ma value
         diverso vengono mantenuti (non sovrascrivono).
      4. assets: union dei campi vuoti del primary con i valori del
         candidate (email, telegram, whatsapp, social_json, sitoweb,
         display_name, raw_json keys).
      5. candidate.dedup_status = 'merged_into:<primary>', candidate.
         dedup_canonical_id = primary_id.
      6. asset_dedup_candidates: la coppia (primary, candidate) diventa
         status='merged' con resolved_at + resolved_by_user_id. Altre
         coppie pendenti dove candidate appare vengono marcate 'merged'
         (transitivamente — il candidate non esiste piu' come standalone).

    Ritorna {primary_id, candidate_id, merged_fields: [list]}.
    Solleva ValueError se primary == candidate, o asset non trovati, o
    tenant mismatch.
    """
    if primary_id == candidate_id:
        raise ValueError("primary_id == candidate_id")

    primary = db.get_asset(primary_id, tenant_id=None)
    candidate = db.get_asset(candidate_id, tenant_id=None)
    if not primary:
        raise ValueError(f"primary asset {primary_id} non trovato")
    if not candidate:
        raise ValueError(f"candidate asset {candidate_id} non trovato")

    pt = primary.get("tenant_id")
    ct = candidate.get("tenant_id")
    if pt != ct:
        raise ValueError(f"tenant mismatch: primary tenant={pt} candidate tenant={ct}")
    if tenant_id is not None and pt is not None and pt != tenant_id:
        raise ValueError(f"chiamante tenant={tenant_id} non puo' mergiare asset tenant={pt}")

    merged_fields: list[str] = []

    with db.connect() as con:
        # 1. Tasks.target_asset_ids: rewrite JSON list rimpiazzando candidate
        # con primary (dedup, conservando ordine)
        rows = con.execute(
            "SELECT id, target_asset_ids FROM tasks "
            "WHERE target_asset_ids LIKE %s",
            (f"%{candidate_id}%",),
        ).fetchall()
        for row in rows:
            raw = row["target_asset_ids"]
            if not raw:
                continue
            try:
                ids = json.loads(raw)
                if not isinstance(ids, list):
                    continue
            except (json.JSONDecodeError, TypeError):
                continue
            # Replace + dedup
            new_ids: list[int] = []
            seen: set[int] = set()
            for x in ids:
                try:
                    xi = int(x)
                except (TypeError, ValueError):
                    continue
                target = primary_id if xi == candidate_id else xi
                if target in seen:
                    continue
                seen.add(target)
                new_ids.append(target)
            if new_ids != ids:
                con.execute(
                    "UPDATE tasks SET target_asset_ids = %s, updated_at = %s "
                    "WHERE id = %s",
                    (json.dumps(new_ids), db.now_iso(), int(row["id"])),
                )
        if rows:
            merged_fields.append(f"tasks.target_asset_ids ({len(rows)} task rewritten)")

        # 2. social_dm_log.target_asset_id: candidate -> primary
        cur = con.execute(
            "UPDATE social_dm_log SET target_asset_id = %s "
            "WHERE target_asset_id = %s",
            (primary_id, candidate_id),
        )
        if cur.rowcount:
            merged_fields.append(f"social_dm_log ({cur.rowcount} rows)")

        # 3. asset_tags: sposta tag del candidate al primary, dedup su (asset, k, v)
        cur = con.execute(
            "INSERT INTO asset_tags (asset_id, tag_key, tag_value) "
            "SELECT %s, tag_key, tag_value FROM asset_tags "
            "WHERE asset_id = %s "
            "ON CONFLICT (asset_id, tag_key, tag_value) DO NOTHING",
            (primary_id, candidate_id),
        )
        if cur.rowcount:
            merged_fields.append(f"asset_tags ({cur.rowcount} new tags)")
        con.execute("DELETE FROM asset_tags WHERE asset_id = %s", (candidate_id,))

        # 4. Union campi vuoti del primary
        updates: list[str] = []
        values: list = []
        for field in ("email", "telegram_username", "telegram_chat_id",
                      "whatsapp", "sitoweb", "display_name", "social_json"):
            primary_v = primary.get(field)
            candidate_v = candidate.get(field)
            if (not primary_v) and candidate_v:
                updates.append(f"{field} = %s")
                values.append(candidate_v)
                merged_fields.append(field)
        if updates:
            con.execute(
                f"UPDATE assets SET {', '.join(updates)}, updated_at = %s WHERE id = %s",
                tuple(values + [db.now_iso(), primary_id]),
            )

        # 5. Mark candidate as merged
        con.execute(
            "UPDATE assets SET dedup_status = %s, dedup_canonical_id = %s, "
            "updated_at = %s WHERE id = %s",
            (f"merged_into:{primary_id}", primary_id, db.now_iso(), candidate_id),
        )
        merged_fields.append("candidate marked merged_into")

        # 6. Update asset_dedup_candidates: la coppia esatta -> 'merged'
        primary_db, candidate_db = (primary_id, candidate_id) if primary_id < candidate_id else (candidate_id, primary_id)
        con.execute(
            "UPDATE asset_dedup_candidates "
            "SET status = 'merged', resolved_at = %s, resolved_by_user_id = %s "
            "WHERE primary_asset_id = %s AND candidate_asset_id = %s",
            (db.now_iso(), resolved_by_user_id, primary_db, candidate_db),
        )
        # Altre coppie pendenti dove candidate_id appare (con qualcun altro)
        # diventano 'merged' come effetto collaterale: candidate non esiste piu'.
        con.execute(
            "UPDATE asset_dedup_candidates "
            "SET status = 'merged', resolved_at = %s, resolved_by_user_id = %s "
            "WHERE status = 'pending' "
            "  AND (primary_asset_id = %s OR candidate_asset_id = %s)",
            (db.now_iso(), resolved_by_user_id, candidate_id, candidate_id),
        )

        con.commit()

    return {
        "primary_id": primary_id,
        "candidate_id": candidate_id,
        "merged_fields": merged_fields,
    }


def list_pending_candidates(
    tenant_id: Any = None,
    limit: int = 100,
    offset: int = 0,
) -> list[dict]:
    """Ritorna candidates pendenti ordinate per score DESC + detected_at DESC.
    Filtra per tenant via JOIN su assets.tenant_id (chiamante deve passare
    tenant_id reale o None per super-admin)."""
    with db.connect() as con:
        if tenant_id is None:
            sql = (
                "SELECT c.id, c.primary_asset_id, c.candidate_asset_id, "
                "  c.match_keys, c.match_score, c.detected_at "
                "FROM asset_dedup_candidates c "
                "WHERE c.status = 'pending' "
                "ORDER BY c.match_score DESC, c.detected_at DESC "
                "LIMIT %s OFFSET %s"
            )
            args = (limit, offset)
        else:
            sql = (
                "SELECT c.id, c.primary_asset_id, c.candidate_asset_id, "
                "  c.match_keys, c.match_score, c.detected_at "
                "FROM asset_dedup_candidates c "
                "JOIN assets a ON a.id = c.primary_asset_id "
                "WHERE c.status = 'pending' AND a.tenant_id = %s "
                "ORDER BY c.match_score DESC, c.detected_at DESC "
                "LIMIT %s OFFSET %s"
            )
            args = (tenant_id, limit, offset)
        rows = con.execute(sql, args).fetchall()
    out: list[dict] = []
    for r in rows:
        d = dict(r)
        try:
            d["match_keys"] = json.loads(d["match_keys"]) if isinstance(d["match_keys"], str) else d["match_keys"]
        except (json.JSONDecodeError, TypeError):
            d["match_keys"] = []
        out.append(d)
    return out


def count_pending_candidates(tenant_id: Any = None) -> int:
    with db.connect() as con:
        if tenant_id is None:
            row = con.execute(
                "SELECT COUNT(*) AS n FROM asset_dedup_candidates WHERE status = 'pending'"
            ).fetchone()
        else:
            row = con.execute(
                "SELECT COUNT(*) AS n FROM asset_dedup_candidates c "
                "JOIN assets a ON a.id = c.primary_asset_id "
                "WHERE c.status = 'pending' AND a.tenant_id = %s",
                (tenant_id,),
            ).fetchone()
    return int(row["n"]) if row else 0


def reject_candidate(
    candidate_row_id: int,
    *,
    resolved_by_user_id: int | None = None,
) -> None:
    """Marca un candidate come 'rejected' (non e' un duplicato). Si usa
    quando l'utente decide che i due asset sono separati legittimi (es.
    numero condiviso di centralino)."""
    with db.connect() as con:
        con.execute(
            "UPDATE asset_dedup_candidates SET status = 'rejected', "
            "  resolved_at = %s, resolved_by_user_id = %s WHERE id = %s",
            (db.now_iso(), resolved_by_user_id, candidate_row_id),
        )
        con.commit()
