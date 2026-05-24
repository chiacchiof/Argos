"""Asset: vista generalizzata di tutto il materiale estratto dai runner.

Le righe di `profiles.jsonl` di un task vengono ingestate in tabella `assets`
con tag derivati dichiarativamente (vedi app/agent/asset_tags.py). Qui esponi
una lista filtrabile per asset_type + tag.
"""
from __future__ import annotations

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse

from .. import db
from .. import export_csv
from ..agent import asset_dedup
from ..templates import templates
from . import _tenant_filter as _tf


router = APIRouter()


def _parse_tag_filters(raw: str | None) -> list[tuple[str, str]]:
    """Parsa il legacy querystring `tags=key:value,key2:value2` in coppie.

    Mantenuto per backward-compat con bookmark / link salvati / il filtro
    dropdown del tipo. Il nuovo formato canonico per /assets e /qualified e'
    `tag_key__N` / `tag_value__N` (slot numerati) parsato da
    `_parse_extra_tag_filters_from_mapping`.
    """
    if not raw:
        return []
    out: list[tuple[str, str]] = []
    for piece in raw.split(","):
        piece = piece.strip()
        if not piece or ":" not in piece:
            continue
        k, _, v = piece.partition(":")
        k = k.strip().lower()
        v = v.strip()
        if k and v:
            out.append((k, v))
    return out


def _serialize_tag_filters(pairs: list[tuple[str, str]]) -> str:
    return ",".join(f"{k}:{v}" for k, v in pairs)


def _resolve_unified_tag_filters(
    request: Request,
    legacy_tags_param: str | None,
) -> list[tuple[str, str]]:
    """Risolve i tag filter unificando i due formati storici di /assets e /qualified.

    Priorita':
      1) Slot numerati `tag_key__N` / `tag_value__N` (formato canonico nuovo,
         allineato con /qualified).
      2) Fallback al legacy `tags=k:v,k:v` (string serializzata) — preserva
         i bookmark e i link salvati che usavano il vecchio formato.

    Cosi' /assets parla la stessa "grammatica" di /qualified senza rompere URL.
    """
    new_format = _parse_extra_tag_filters_from_mapping(request.query_params)
    if new_format:
        return new_format
    return _parse_tag_filters(legacy_tags_param)


_ASSETS_PAGE_SIZE = 100


@router.get("/assets", response_class=HTMLResponse)
async def assets_list(
    request: Request,
    asset_type: str | None = None,
    status: str | None = None,
    tags: str | None = None,           # legacy: tags=k:v,k:v (parsato per backcompat)
    source_task_id: int | None = None,
    q: str | None = None,
    tag_mode: str = "and",
    tag_expr: str = "",
    has_contacts: str = "",
    has_social: str = "",
    page: int = 1,
    per_page: int = _ASSETS_PAGE_SIZE,
):
    asset_type = (asset_type or "").strip() or None
    status = (status or "").strip() or None
    q_clean = (q or "").strip() or None
    # Tag filters: nuovo formato (tag_key__N) preferito, legacy 'tags=' fallback.
    tag_filters = _resolve_unified_tag_filters(request, tags)
    tag_mode_v = (tag_mode or "and").strip().lower()
    if tag_mode_v not in ("and", "or", "custom"):
        tag_mode_v = "and"
    tag_expr_v = (tag_expr or "").strip()
    tag_expr_error: str | None = None
    if tag_mode_v == "custom" and not tag_expr_v:
        tag_mode_v = "and"
    if tag_mode_v == "custom" and tag_filters and tag_expr_v:
        from ..agent.tag_expr import parse_tag_expr as _parse_te
        try:
            _parse_te(tag_expr_v, len(tag_filters))
        except ValueError as e:
            tag_expr_error = str(e)
            tag_mode_v = "and"
            tag_expr_v = ""
    has_contacts_v = bool(has_contacts and has_contacts.strip())
    has_social_v = bool(has_social and has_social.strip())
    # Sanitize paginazione
    per_page = max(10, min(int(per_page or _ASSETS_PAGE_SIZE), 500))
    page = max(1, int(page or 1))
    offset = (page - 1) * per_page

    tenant_arg = _tf.tenant_query_arg(request)
    total = db.count_assets(
        asset_type=asset_type,
        status=status,
        source_task_id=source_task_id,
        tag_filters=tag_filters or None,
        search=q_clean,
        tag_mode=tag_mode_v,
        tag_expr=tag_expr_v or None,
        has_contacts=has_contacts_v,
        has_social=has_social_v,
        tenant_id=tenant_arg,
    )
    total_pages = max(1, (total + per_page - 1) // per_page)
    # Clamp page se l'utente passa un numero oltre la fine
    if page > total_pages:
        page = total_pages
        offset = (page - 1) * per_page

    assets = db.list_assets(
        asset_type=asset_type,
        status=status,
        source_task_id=source_task_id,
        tag_filters=tag_filters or None,
        search=q_clean,
        tag_mode=tag_mode_v,
        tag_expr=tag_expr_v or None,
        has_contacts=has_contacts_v,
        has_social=has_social_v,
        limit=per_page,
        offset=offset,
        tenant_id=tenant_arg,
    )
    types_in_use = db.list_asset_types_in_use()
    # Tag keys per il widget condiviso _tag_filter_widget.html (dropdown F1-F6).
    # Esclude qualifier_* (vivono in /qualified). Il vecchio facets panel è
    # stato rimosso: la discovery è già fornita dal count `(N)` nelle dropdown.
    available_tag_keys = db.list_distinct_tag_keys_for_assets(
        exclude_qualifier_tags=True,
        asset_type=asset_type,
    )

    # Querystring base per i link di paginazione (senza page=).
    # Usiamo il NUOVO formato tag_key__N / tag_value__N per essere coerenti con
    # /qualified — i link salvati con `tags=` legacy continuano a funzionare in input.
    from urllib.parse import quote
    qs_parts: list[str] = []
    if asset_type: qs_parts.append(f"asset_type={quote(asset_type)}")
    if status: qs_parts.append(f"status={quote(status)}")
    if source_task_id is not None: qs_parts.append(f"source_task_id={source_task_id}")
    for i, (k, v) in enumerate(tag_filters):
        qs_parts.append(f"tag_key__{i}={quote(k)}")
        qs_parts.append(f"tag_value__{i}={quote(v)}")
    if tag_filters and tag_mode_v != "and":
        qs_parts.append(f"tag_mode={tag_mode_v}")
    if tag_mode_v == "custom" and tag_expr_v:
        qs_parts.append(f"tag_expr={quote(tag_expr_v)}")
    if has_contacts_v: qs_parts.append("has_contacts=1")
    if has_social_v: qs_parts.append("has_social=1")
    if q_clean: qs_parts.append(f"q={quote(q_clean)}")
    if per_page != _ASSETS_PAGE_SIZE: qs_parts.append(f"per_page={per_page}")
    qs_base = "&".join(qs_parts)

    return templates.TemplateResponse(
        request,
        "assets_list.html",
        {
            "assets": assets,
            "filter_type": asset_type or "",
            "filter_status": status or "",
            "filter_tags": tag_filters,
            "filter_tags_str": _serialize_tag_filters(tag_filters),
            "filter_task": source_task_id,
            "filter_q": q_clean or "",
            "tag_mode": tag_mode_v,
            "tag_expr": tag_expr_v,
            "tag_expr_error": tag_expr_error,
            "has_contacts": has_contacts_v,
            "has_social": has_social_v,
            "types_in_use": types_in_use,
            "available_tag_keys": available_tag_keys,
            # paginazione
            "page": page,
            "per_page": per_page,
            "total": total,
            "total_pages": total_pages,
            "offset": offset,
            "qs_base": qs_base,
            "export_field_groups": _export_field_groups_for_template(),
            "default_export_fields": list(export_csv.DEFAULT_FIELDS),
            **_tf.picker_context(request),
        },
    )


# ===========================================================================
# /qualified — vista asset-centric filtrabile per qualifier (Fase 1)
# ===========================================================================

_QUALIFIED_PAGE_SIZE = 100


_MAX_EXTRA_TAG_SLOTS = 6


def _parse_extra_tag_filters_from_mapping(mp) -> list[tuple[str, str]]:
    """Helper interno: estrae `tag_key__N`/`tag_value__N` (N=0..5) da un mapping
    qualsiasi (Starlette QueryParams o FormData). Pattern usato in /inbox/contacts
    e /qualified."""
    out: list[tuple[str, str]] = []
    for i in range(_MAX_EXTRA_TAG_SLOTS):
        k = (mp.get(f"tag_key__{i}") or "").strip().lower()
        v = (mp.get(f"tag_value__{i}") or "").strip()
        if k and v:
            out.append((k, v))
    return out


def _parse_extra_tag_filters(request: Request) -> list[tuple[str, str]]:
    """Parsa tag filters dal querystring (GET). Per form data (POST) usa
    `_parse_extra_tag_filters_from_mapping(form_data)` da `routes/tasks.py`."""
    return _parse_extra_tag_filters_from_mapping(request.query_params)


def _parse_optional_int(value: str | None) -> int | None:
    """Parsa querystring → int o None. Tollera stringa vuota (i form HTML
    inviano `?param=` anche quando il campo è lasciato vuoto, che FastAPI
    non sa castare automaticamente a `int | None`)."""
    if value is None:
        return None
    s = value.strip()
    if not s:
        return None
    try:
        return int(s)
    except ValueError:
        return None


@router.get("/qualified", response_class=HTMLResponse)
async def qualified_assets_list(
    request: Request,
    qualifiers: str = "",  # comma-separated slug list
    status: str = "qualified",  # qualified | rejected | both
    score_min: str = "",
    asset_type: str = "",
    source_task_id: str = "",
    q: str = "",
    page: str = "1",
    per_page: str = "",
    tag_mode: str = "and",
    tag_expr: str = "",
    return_to_task: str = "",
    has_contacts: str = "",
    has_social: str = "",
):
    """Tab Qualified: asset-centric con multi-select qualifier + filtri.

    Query string:
        qualifiers=slug1,slug2       (intersezione AND)
        status=qualified|rejected|both
        score_min=N                  (applicato a TUTTI i qualifier selezionati)
        asset_type, source_task_id, q (search title/raw_json)
        tag_key__0 / tag_value__0    (fino a 5 slot extra-tag, AND)
        page, per_page               (paginazione)

    Tutti i numerici accettano stringa vuota (form HTML invia `?param=`).
    """
    qualifier_slugs = [s.strip() for s in (qualifiers or "").split(",") if s.strip()]
    if status not in ("qualified", "rejected", "both"):
        status = "qualified"
    asset_type_v: str | None = (asset_type or "").strip() or None
    source_task_id_v = _parse_optional_int(source_task_id)
    score_min_v = _parse_optional_int(score_min)
    search = (q or "").strip() or None
    extra_tag_filters = _parse_extra_tag_filters(request)
    has_contacts_v = bool(has_contacts and has_contacts.strip())
    has_social_v = bool(has_social and has_social.strip())
    tag_mode_v = (tag_mode or "and").strip().lower()
    if tag_mode_v not in ("and", "or", "custom"):
        tag_mode_v = "and"
    tag_expr_v = (tag_expr or "").strip()
    # Se mode=custom ma expr e' vuota, l'utente ha selezionato 'custom' nel radio
    # ma non ha (ancora) scritto l'espressione. Fallback ad AND per evitare 500
    # da build_where_clause (che richiede expr per mode=custom).
    if tag_mode_v == "custom" and not tag_expr_v:
        tag_mode_v = "and"
    # Validazione precoce: se mode=custom + expr presente, parse_tag_expr deve
    # passare. In caso di errore, fallback ad AND + propaga error al template.
    tag_expr_error: str | None = None
    if tag_mode_v == "custom" and extra_tag_filters and tag_expr_v:
        from ..agent.tag_expr import parse_tag_expr as _parse_te
        try:
            _parse_te(tag_expr_v, len(extra_tag_filters))
        except ValueError as e:
            tag_expr_error = str(e)
            tag_mode_v = "and"
            tag_expr_v = ""
    per_page_v = _parse_optional_int(per_page) or _QUALIFIED_PAGE_SIZE
    per_page_v = max(10, min(per_page_v, 500))
    page_v = _parse_optional_int(page) or 1
    page_v = max(1, page_v)
    offset = (page_v - 1) * per_page_v

    tenant_arg = _tf.tenant_query_arg(request)
    total = db.count_qualified_assets(
        qualifier_slugs=qualifier_slugs,
        status_filter=status,
        score_min=score_min_v,
        asset_type=asset_type_v,
        source_task_id=source_task_id_v,
        search=search,
        extra_tag_filters=extra_tag_filters or None,
        tag_mode=tag_mode_v,
        tag_expr=tag_expr_v or None,
        has_contacts=has_contacts_v,
        has_social=has_social_v,
        tenant_id=tenant_arg,
    )
    total_pages = max(1, (total + per_page_v - 1) // per_page_v)
    if page_v > total_pages:
        page_v = total_pages
        offset = (page_v - 1) * per_page_v

    assets = db.list_qualified_assets(
        qualifier_slugs=qualifier_slugs,
        status_filter=status,
        score_min=score_min_v,
        asset_type=asset_type_v,
        source_task_id=source_task_id_v,
        search=search,
        extra_tag_filters=extra_tag_filters or None,
        limit=per_page_v,
        offset=offset,
        tag_mode=tag_mode_v,
        tag_expr=tag_expr_v or None,
        has_contacts=has_contacts_v,
        has_social=has_social_v,
        tenant_id=tenant_arg,
    )
    # Parse social_json di ogni asset una volta sola (lo usa il template per
    # renderizzare l'icona platform + handle / URL nella colonna Contatti/Social).
    import json as _json_parse
    for _a in assets:
        _socials_list: list[dict] = []
        _raw = _a.get("social_json") or ""
        if _raw:
            try:
                _parsed = _json_parse.loads(_raw) if isinstance(_raw, str) else _raw
                if isinstance(_parsed, list):
                    _socials_list = [x for x in _parsed if isinstance(x, dict)]
                elif isinstance(_parsed, dict):
                    _socials_list = [_parsed]
            except (ValueError, TypeError):
                _socials_list = []
        _a["_socials"] = _socials_list

    qualifier_menu = db.list_distinct_qualifier_slugs()
    types_in_use = db.list_asset_types_in_use()
    available_source_tasks = db.list_distinct_asset_source_tasks(only_qualified=True)
    # Tag keys ristretti al tipo selezionato (se attivo) — coerente con UX:
    # filtro tipo "ig_profile" mostra solo i tag presenti su asset ig_profile.
    available_tag_keys = db.list_distinct_tag_keys_for_assets(
        exclude_qualifier_tags=True,
        asset_type=asset_type_v,
    )

    qs_parts: list[str] = []
    if qualifiers: qs_parts.append(f"qualifiers={qualifiers}")
    if status != "qualified": qs_parts.append(f"status={status}")
    if score_min_v is not None: qs_parts.append(f"score_min={score_min_v}")
    if asset_type_v: qs_parts.append(f"asset_type={asset_type_v}")
    if source_task_id_v is not None: qs_parts.append(f"source_task_id={source_task_id_v}")
    if search: qs_parts.append(f"q={search}")
    for i, (k, v) in enumerate(extra_tag_filters):
        qs_parts.append(f"tag_key__{i}={k}")
        qs_parts.append(f"tag_value__{i}={v}")
    if tag_mode_v != "and": qs_parts.append(f"tag_mode={tag_mode_v}")
    if tag_mode_v == "custom" and tag_expr_v:
        from urllib.parse import quote as _q
        qs_parts.append(f"tag_expr={_q(tag_expr_v, safe='')}")
    if has_contacts_v: qs_parts.append("has_contacts=1")
    if has_social_v: qs_parts.append("has_social=1")
    if per_page_v != _QUALIFIED_PAGE_SIZE: qs_parts.append(f"per_page={per_page_v}")
    qs_base = "&".join(qs_parts)

    return templates.TemplateResponse(
        request,
        "qualified_list.html",
        {
            "assets": assets,
            "qualifier_menu": qualifier_menu,
            "selected_qualifiers": qualifier_slugs,
            "filter_status": status,
            "filter_score_min": score_min_v,
            "filter_type": asset_type_v or "",
            "filter_task": source_task_id_v,
            "filter_search": search or "",
            "extra_tag_filters": extra_tag_filters,
            "types_in_use": types_in_use,
            "available_tag_keys": available_tag_keys,
            "available_source_tasks": available_source_tasks,
            "tag_mode": tag_mode_v,
            "tag_expr": tag_expr_v,
            "tag_expr_error": tag_expr_error,
            "return_to_task": _parse_optional_int(return_to_task),
            "has_contacts": has_contacts_v,
            "has_social": has_social_v,
            "page": page_v,
            "per_page": per_page_v,
            "total": total,
            "total_pages": total_pages,
            "offset": offset,
            "qs_base": qs_base,
            "export_field_groups": _export_field_groups_for_template(),
            "default_export_fields": list(export_csv.DEFAULT_FIELDS),
            **_tf.picker_context(request),
        },
    )


# Label human-readable per le categorie del modal export CSV (vedi
# app/export_csv.py FIELD_CATEGORIES).
_EXPORT_CATEGORY_LABELS = {
    "core": "📋 Core",
    "contact": "📞 Contatti",
    "social": "📷 Social",
    "origin": "🏷️ Origine",
    "outreach": "📤 Outreach",
    "qualifier": "✅ Qualifier",
    "dedup": "🔀 Dedup",
}


def _export_field_groups_for_template() -> list[tuple[str, str, list[tuple[str, str]]]]:
    """Trasforma FIELD_CATEGORIES (in export_csv) in formato consumabile dal
    template del modal: [(cat_key, cat_label, [(field_key, field_label), ...])]."""
    out: list[tuple[str, str, list[tuple[str, str]]]] = []
    for cat_key, fields in export_csv.FIELD_CATEGORIES.items():
        label = _EXPORT_CATEGORY_LABELS.get(cat_key, cat_key)
        items = [(f[0], f[1]) for f in fields]
        out.append((cat_key, label, items))
    return out


_QUALIFIER_SLUG_RE = __import__("re").compile(r"^[a-z0-9_]+$")


@router.post("/qualified/add", response_class=HTMLResponse)
async def qualified_add(
    asset_id: str = Form(""),
    qualifier_slug: str = Form("manual"),
    decision: str = Form("qualified"),
    score: str = Form(""),
):
    """Marca manualmente un asset esistente come qualified/rejected per un
    qualifier (slug arbitrario, default 'manual').

    Aggiunge i tag `qualifier_<slug>` e (se score) `qualifier_score_<slug>`
    su asset_tags; aggiorna `assets.status` (no-downgrade da 'qualified').
    L'asset DEVE esistere — non si crea da zero. Per creare nuovi asset
    usa /assets/new o un task scraper.
    """
    aid_v = _parse_optional_int(asset_id)
    if aid_v is None or aid_v <= 0:
        raise HTTPException(status_code=400, detail="asset_id mancante o invalido")
    slug = (qualifier_slug or "manual").strip().lower()
    if not slug:
        slug = "manual"
    if not _QUALIFIER_SLUG_RE.match(slug):
        raise HTTPException(
            status_code=400,
            detail=f"qualifier_slug invalido: '{slug}' — usa solo a-z, 0-9, underscore",
        )
    if decision not in ("qualified", "rejected"):
        raise HTTPException(status_code=400, detail="decision deve essere 'qualified' o 'rejected'")
    score_v = _parse_optional_int(score)
    if score_v is not None and not (0 <= score_v <= 10):
        raise HTTPException(status_code=400, detail="score deve essere tra 0 e 10")

    asset = db.get_asset(aid_v)
    if not asset:
        raise HTTPException(status_code=404, detail=f"asset #{aid_v} non trovato (o non accessibile al tenant)")

    # Set tag puntuali — set_asset_tag e' singleton (rimuove eventuali precedenti
    # per la stessa key, garantisce idempotenza).
    try:
        db.set_asset_tag(aid_v, f"qualifier_{slug}", decision)
        if score_v is not None:
            db.set_asset_tag(aid_v, f"qualifier_score_{slug}", str(score_v))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"set_asset_tag fallito: {e}")

    # Aggiorna status dell'asset (no-downgrade qualified).
    try:
        db.update_asset_qualifier(
            aid_v,
            score=score_v or 0,
            status=decision,
            notes=f"qualifier manuale '{slug}'" if slug == "manual" else None,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"update_asset_qualifier fallito: {e}")

    flash = f"Asset+%23{aid_v}+marcato+{decision}+per+qualifier+'{slug}'"
    return RedirectResponse(
        url=f"/qualified?qualifiers={slug}&status={decision}&flash={flash}",
        status_code=303,
    )


# ===========================================================================
# CSV export — /qualified e /assets
# ===========================================================================

_MAX_EXPORT = 50000  # cap per non saturare memoria/timeout su DB enormi


def _audience_from_qualified_form(form) -> list[dict]:
    """Estrae la lista asset (dicts) per export CSV su /qualified.
    Riusa la stessa logica di `_extract_audience_from_form` (routes/tasks.py):
      - asset_ids[] presenti + no flag => selezione esplicita (fetch per id)
      - altrimenti => applica i filtri + cap _MAX_EXPORT
    """
    select_all_filtered = (form.get("select_all_filtered") or "").strip() == "1"
    explicit_raw: list[str] = (
        form.getlist("asset_ids") if hasattr(form, "getlist") else []
    )
    if explicit_raw and not select_all_filtered:
        seen: set[int] = set()
        ids: list[int] = []
        for v in explicit_raw:
            try:
                i = int(v)
            except (TypeError, ValueError):
                continue
            if i not in seen:
                seen.add(i)
                ids.append(i)
        out: list[dict] = []
        for aid in ids:
            a = db.get_asset(aid)
            if a:
                out.append(a)
        return out

    # Filtri da form
    qualifier_slugs = [
        s.strip() for s in (form.get("qualifiers") or "").split(",") if s.strip()
    ]
    status = (form.get("status") or "qualified").strip()
    if status not in ("qualified", "rejected", "both"):
        status = "qualified"
    score_min_v = _parse_optional_int(form.get("score_min") or "")
    asset_type_v = (form.get("asset_type") or "").strip() or None
    source_task_id_v = _parse_optional_int(form.get("source_task_id") or "")
    search = (form.get("q") or "").strip() or None
    extra_tag_filters = _parse_extra_tag_filters_from_mapping(form)
    tag_mode_v = (form.get("tag_mode") or "and").strip().lower()
    if tag_mode_v not in ("and", "or", "custom"):
        tag_mode_v = "and"
    tag_expr_v = (form.get("tag_expr") or "").strip() or None
    has_contacts_v = bool((form.get("has_contacts") or "").strip())
    has_social_v = bool((form.get("has_social") or "").strip())

    return db.list_qualified_assets(
        qualifier_slugs=qualifier_slugs,
        status_filter=status,
        score_min=score_min_v,
        asset_type=asset_type_v,
        source_task_id=source_task_id_v,
        search=search,
        extra_tag_filters=extra_tag_filters or None,
        limit=_MAX_EXPORT,
        offset=0,
        tag_mode=tag_mode_v,
        tag_expr=tag_expr_v,
        has_contacts=has_contacts_v,
        has_social=has_social_v,
    )


def _csv_filename(prefix: str) -> str:
    ts = db.now_iso()[:19].replace(":", "").replace("-", "")
    return f"{prefix}_{ts}.csv"


def _parse_export_fields(form) -> tuple[list[str], str]:
    """Da form `fields` (multivalue checkbox) + `tags_mode` ritorna
    (fields_list, tags_mode in {none, flat, columns})."""
    fields: list[str] = (
        form.getlist("fields") if hasattr(form, "getlist") else []
    )
    fields = [f for f in fields if isinstance(f, str) and f.strip()]
    tags_mode = (form.get("tags_mode") or "none").strip().lower()
    if tags_mode not in ("none", "flat", "columns"):
        tags_mode = "none"
    return fields, tags_mode


@router.post("/qualified/export.csv")
async def qualified_export_csv(request: Request):
    """Export CSV dell'audience /qualified. POST per supportare asset_ids[]
    grandi + filtri + selezione campi. Vedi app/export_csv.py per i field key."""
    form = await request.form()
    assets = _audience_from_qualified_form(form)
    if not assets:
        raise HTTPException(
            status_code=400,
            detail="Audience vuota: seleziona almeno un asset o raffina i filtri.",
        )
    fields, tags_mode = _parse_export_fields(form)
    fname = _csv_filename("qualified_export")
    gen = export_csv.render_assets_csv(assets, fields, tags_mode=tags_mode)
    return StreamingResponse(
        gen,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


@router.post("/assets/export.csv")
async def assets_export_csv(request: Request):
    """Export CSV degli asset filtrati. Riusa i filtri di /assets:
      asset_type, status, tags (csv "k:v,..."), q (search), source_task_id."""
    form = await request.form()
    # Filtri /assets standard
    asset_type = (form.get("asset_type") or "").strip() or None
    status = (form.get("status") or "").strip() or None
    tags_csv = (form.get("tags") or "").strip()
    tag_filters = _parse_tag_filters(tags_csv) if tags_csv else []
    source_task_id_v = _parse_optional_int(form.get("source_task_id") or "")
    # Flag "Tutti i N filtrati" vince su asset_ids esplicito.
    select_all_filtered = (form.get("select_all_filtered") or "").strip() == "1"
    # Selezione esplicita da bulk checkbox (se utente ha selezionato righe).
    explicit_raw = form.getlist("asset_ids") if hasattr(form, "getlist") else []
    if explicit_raw and not select_all_filtered:
        seen: set[int] = set()
        ids: list[int] = []
        for v in explicit_raw:
            try:
                i = int(v)
            except (TypeError, ValueError):
                continue
            if i not in seen:
                seen.add(i)
                ids.append(i)
        assets: list[dict] = []
        for aid in ids:
            a = db.get_asset(aid)
            if a:
                assets.append(a)
    else:
        # Re-fetch dai filtri (no paginazione: ritorna fino a _MAX_EXPORT).
        assets = db.list_assets(
            asset_type=asset_type,
            status=status,
            tag_filters=tag_filters or None,
            source_task_id=source_task_id_v,
            limit=_MAX_EXPORT,
            offset=0,
        )
    if not assets:
        raise HTTPException(
            status_code=400,
            detail="Audience vuota: nessun asset matching.",
        )
    fields, tags_mode = _parse_export_fields(form)
    fname = _csv_filename("assets_export")
    gen = export_csv.render_assets_csv(assets, fields, tags_mode=tags_mode)
    return StreamingResponse(
        gen,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


@router.get("/assets/preview_inline", response_class=HTMLResponse)
async def asset_preview_inline(asset_id: str = ""):
    """HTMX endpoint: ritorna un <div> con preview minima di un asset dato l'ID.
    Usato dal form "Aggiungi qualified" per dare feedback all'utente che sta
    digitando l'asset_id corretto."""
    aid = _parse_optional_int(asset_id)
    if aid is None or aid <= 0:
        return HTMLResponse('<span class="muted small">— inserisci un ID per vedere l\'anteprima —</span>')
    asset = db.get_asset(aid)
    if not asset:
        return HTMLResponse(f'<span class="error-inline">⚠️ Asset #{aid} non trovato (o non accessibile).</span>')
    # Escape minimo
    def esc(s: str | None) -> str:
        if not s:
            return ""
        return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
    title = esc(asset.get("title") or "(senza titolo)")
    atype = esc(asset.get("asset_type") or "?")
    status = esc(asset.get("status") or "?")
    domain = esc(asset.get("source_domain") or "—")
    return HTMLResponse(
        f'<div class="asset-preview-ok">'
        f'<strong>#{aid}</strong> <code>{atype}</code> — '
        f'<a href="/assets/{aid}" target="_blank">{title}</a> '
        f'<span class="muted small">({domain} · stato: {status})</span>'
        f'</div>'
    )


# === Search HTMX + preview JSON (per import in form contact) ===
# Definiti PRIMA di /assets/{asset_id} per evitare conflitto di routing.

@router.get("/assets/tag_values", response_class=HTMLResponse)
async def asset_tag_values_htmx(
    request: Request, key: str = "", scope: str = "contacts", asset_type: str = "",
    q: str = "", format: str = "select",
):
    """HTMX endpoint: ritorna <option> per il dropdown tag_value dato un
    tag_key. Usato sia dal form task (filtro outreach multi-tag) sia dai
    filtri tag in /inbox/contacts e /qualified.

    scope=contacts (default, backward-compat): count = N contatti per value
    scope=assets:                              count = N asset per value
    asset_type (solo con scope=assets): restringe ai value presenti su quel tipo.
    q (opzionale, solo scope=assets): substring LIKE %q% per typeahead (datalist).
    format='select' (default): include un'option placeholder. format='datalist':
        nessun placeholder (datalist non li vuole come hint utili).
    """
    key = (key or "").strip().lower()
    fmt = (format or "select").strip().lower()
    if not key:
        if fmt == "datalist":
            return HTMLResponse("")
        return HTMLResponse('<option value="">— prima scegli una key —</option>')
    use_assets = (scope or "").strip().lower() == "assets"
    at = (asset_type or "").strip() or None
    q_clean = (q or "").strip() or None
    # Cap aumentato a 200 per typeahead: se l'utente digita "fit" su un tag
    # come 'interests_inferred' con 1000+ valori, 100 LIKE-match potrebbero
    # essere troppo restrittivi. Con server-side q-filter è ancora gestibile.
    try:
        if use_assets:
            values = db.list_distinct_tag_values_for_assets(
                key, limit=200, asset_type=at, q=q_clean,
            )
        else:
            # Scope=contacts non ha q-filter (legacy, raramente usato con typeahead).
            values = db.list_distinct_tag_values_for_contacts(key, limit=100)
    except Exception:
        values = []
    parts: list[str] = []
    if fmt != "datalist":
        parts.append('<option value="">— seleziona —</option>')
    for v in values:
        # Escape HTML semplice
        val = (v.get("value") or "").replace('"', "&quot;").replace("<", "&lt;")
        n = v.get("count", 0)
        # Per datalist mettiamo il count nel `label` invece che dentro al text:
        # alcuni browser nascondono il text di <option> nella datalist e mostrano
        # solo il value, quindi il count finirebbe invisibile.
        if fmt == "datalist":
            parts.append(f'<option value="{val}" label="{val} ({n})">')
        else:
            parts.append(f'<option value="{val}">{val} ({n})</option>')
    return HTMLResponse("".join(parts))


@router.get("/assets/search", response_class=HTMLResponse)
async def asset_search_htmx(
    request: Request,
    q: str = "",
    asset_type: str = "",
    limit: int = 10,
    callback: str = "",
):
    """Search HTMX: ritorna lista di asset matching `q` su title / asset_type /
    tag value. Usata dal modal 'Aggiungi contatto' (callback=importFromAsset,
    default) e dal modal /qualified add (callback=selectAssetForQualifier).
    """
    q = (q or "").strip()
    asset_type = (asset_type or "").strip().lower()
    limit = max(1, min(int(limit or 10), 30))
    if len(q) < 2 and not asset_type:
        return HTMLResponse("")
    pat = f"%{q.lower()}%"
    sql = (
        "SELECT DISTINCT a.id, a.asset_type, a.title, a.source_url, a.status "
        "FROM assets a "
        "LEFT JOIN asset_tags t ON t.asset_id = a.id "
        "WHERE 1=1 "
    )
    args: list = []
    if q:
        sql += "AND (LOWER(a.title) LIKE %s OR LOWER(a.asset_type) LIKE %s OR LOWER(t.tag_value) LIKE %s) "
        args += [pat, pat, pat]
    if asset_type:
        sql += "AND a.asset_type = %s "
        args.append(asset_type)
    sql += "ORDER BY a.id DESC LIMIT %s"
    args.append(limit)
    with db.connect() as con:
        rows = con.execute(sql, args).fetchall()
    items = [dict(r) for r in rows]
    cb = (callback or "").strip()
    # Whitelist callback name per evitare XSS via querystring (il template
    # interpola il nome dentro un onclick="").
    if cb not in ("importFromAsset", "selectAssetForQualifier", "audienceAddAsset"):
        cb = "importFromAsset"
    return templates.TemplateResponse(
        request,
        "_asset_search_results.html",
        {"items": items, "q": q, "callback": cb},
    )


@router.get("/assets/{asset_id}/preview_json")
async def asset_preview_json(asset_id: int) -> JSONResponse:
    """Ritorna i campi key di un asset, pre-formattati per popolare il form
    'Aggiungi contatto'. Estrae:
    - display_name = asset.title
    - source_url / source_domain
    - email/telegram/whatsapp/sitoweb da raw_json (se presenti)
    - instagram/tiktok/facebook URL da raw_json['social'] o tag
    """
    asset = db.get_asset(asset_id)
    if not asset:
        raise HTTPException(status_code=404, detail="asset non trovato")
    import json as _json
    raw: dict = {}
    try:
        rj = asset.get("raw_json") or ""
        parsed = _json.loads(rj) if isinstance(rj, str) else rj
        if isinstance(parsed, dict):
            raw = parsed
    except Exception:
        pass

    # URL social estratti dal raw_json (formato: list[{platform, url}] o campi
    # diretti tipo 'instagram_url')
    social_urls: dict[str, str] = {}
    socials = raw.get("social") or raw.get("socials") or []
    if isinstance(socials, list):
        for s in socials:
            if not isinstance(s, dict):
                continue
            plat = (s.get("platform") or "").lower()
            if plat in ("instagram", "tiktok", "facebook"):
                social_urls[plat] = s.get("url") or social_urls.get(plat) or ""
    for plat in ("instagram", "tiktok", "facebook"):
        if not social_urls.get(plat):
            v = raw.get(f"{plat}_url") or raw.get(f"{plat}")
            if v:
                social_urls[plat] = str(v)
    # source_url stesso può essere il link social principale
    src_url = asset.get("source_url") or ""
    src_low = src_url.lower()
    for plat in ("instagram", "tiktok", "facebook"):
        if plat + ".com" in src_low and not social_urls.get(plat):
            social_urls[plat] = src_url

    return JSONResponse({
        "asset_id": asset["id"],
        "asset_type": asset.get("asset_type"),
        "display_name": asset.get("title") or "",
        "source_url": asset.get("source_url") or "",
        "source_domain": asset.get("source_domain") or "",
        "email": raw.get("email") or "",
        "telegram_username": raw.get("telegram") or raw.get("telegram_username") or "",
        "whatsapp": raw.get("whatsapp") or "",
        "sitoweb": raw.get("sitoweb") or raw.get("website") or "",
        "instagram_url": social_urls.get("instagram", ""),
        "tiktok_url": social_urls.get("tiktok", ""),
        "facebook_url": social_urls.get("facebook", ""),
        "notes": (asset.get("notes") or "")[:300],
    })


# === B-016: Asset dedup UI =================================================
# Lista candidates pendenti, side-by-side comparison, merge / reject action.
# Path /assets/duplicates definito PRIMA di /assets/{asset_id} per evitare
# match come asset_id int.

@router.get("/assets/duplicates", response_class=HTMLResponse)
async def assets_duplicates_list(request: Request, limit: int = 50, offset: int = 0):
    """Pagina lista candidates dedup pendenti per il tenant corrente."""
    limit = max(1, min(int(limit or 50), 200))
    offset = max(0, int(offset or 0))
    tenant_id = db.current_tenant_id()
    rows = asset_dedup.list_pending_candidates(
        tenant_id=tenant_id, limit=limit, offset=offset,
    )
    total = asset_dedup.count_pending_candidates(tenant_id=tenant_id)

    # Hydrate: per ogni coppia, fetch i 2 asset con i campi rilevanti.
    clusters: list[dict] = []
    for r in rows:
        pa = db.get_asset(r["primary_asset_id"], tenant_id=None)
        ca = db.get_asset(r["candidate_asset_id"], tenant_id=None)
        if not pa or not ca:
            continue
        # Tag count per asset (aiuta a decidere quale e' "piu' ricco")
        with db.connect() as con:
            pa_tags = con.execute(
                "SELECT COUNT(*) AS n FROM asset_tags WHERE asset_id = %s",
                (pa["id"],),
            ).fetchone()
            ca_tags = con.execute(
                "SELECT COUNT(*) AS n FROM asset_tags WHERE asset_id = %s",
                (ca["id"],),
            ).fetchone()
        clusters.append({
            "id": r["id"],
            "score": r["match_score"],
            "match_keys": r["match_keys"],
            "detected_at": r["detected_at"],
            "primary": {**pa, "tag_count": int(pa_tags["n"]) if pa_tags else 0},
            "candidate": {**ca, "tag_count": int(ca_tags["n"]) if ca_tags else 0},
        })

    return templates.TemplateResponse(
        request,
        "assets_duplicates.html",
        {
            "clusters": clusters,
            "total": total,
            "limit": limit,
            "offset": offset,
        },
    )


@router.post("/assets/duplicates/{cand_id}/merge")
async def assets_duplicates_merge(cand_id: int, primary_id: int = Form(...)):
    """Esegue il merge. `primary_id` (dal form) decide quale dei due asset
    e' il primary (l'utente puo' invertire). L'altro diventa il candidate."""
    tenant_id = db.current_tenant_id()
    user_id = db.current_user_id()
    # Recupera la coppia
    with db.connect() as con:
        row = con.execute(
            "SELECT primary_asset_id, candidate_asset_id FROM asset_dedup_candidates "
            "WHERE id = %s AND status = 'pending'",
            (cand_id,),
        ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="candidate non trovato o gia' risolto")
    a, b = int(row["primary_asset_id"]), int(row["candidate_asset_id"])
    if primary_id not in (a, b):
        raise HTTPException(status_code=400, detail="primary_id deve essere uno dei due asset del cluster")
    candidate_id = b if primary_id == a else a
    try:
        asset_dedup.merge_assets(
            primary_id, candidate_id,
            resolved_by_user_id=user_id, tenant_id=tenant_id,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return RedirectResponse(
        url=f"/assets/duplicates?flash=Merge+OK+{candidate_id}+%E2%86%92+{primary_id}",
        status_code=303,
    )


@router.post("/assets/duplicates/{cand_id}/reject")
async def assets_duplicates_reject(cand_id: int):
    """Marca il cluster come 'rejected' (non e' un duplicato)."""
    user_id = db.current_user_id()
    asset_dedup.reject_candidate(cand_id, resolved_by_user_id=user_id)
    return RedirectResponse(
        url="/assets/duplicates?flash=Cluster+marcato+come+non-duplicato",
        status_code=303,
    )


# === ADD MANUALE === (definito PRIMA di /assets/{asset_id} per evitare che
# "new" venga matchato come asset_id int → 422.)

@router.get("/assets/new", response_class=HTMLResponse)
async def asset_new_form(request: Request):
    return templates.TemplateResponse(
        request,
        "asset_new.html",
        {
            "existing_types": db.list_asset_types_in_use(),
        },
    )


@router.post("/assets/new")
async def asset_new_submit(
    request: Request,
    asset_type: str = Form(""),
    title: str = Form(""),
    source_url: str = Form(""),
    notes: str = Form(""),
    status: str = Form("new"),
    raw_json: str = Form(""),
):
    asset_type = asset_type.strip().lower()
    title = title.strip()
    source_url = source_url.strip()
    status = status.strip() or "new"

    import re as _re
    if not _re.match(r"^[a-z][a-z0-9_-]{0,49}$", asset_type):
        raise HTTPException(status_code=400, detail="asset_type non valido (lowercase a-z 0-9 _-, max 50)")
    if not title:
        raise HTTPException(status_code=400, detail="title obbligatorio")
    if status not in {"new", "qualified", "rejected", "archived"}:
        raise HTTPException(status_code=400, detail="status non valido")
    import json as _json
    raw_data: dict = {}
    if raw_json.strip():
        # Tollera input umani da editor/browser che inseriscono caratteri
        # invisibili: smart-quotes (U+201C/201D/2018/2019 da Word/Notion),
        # non-breaking space (U+00A0), whitespace edge. Rompono json.loads
        # con errori criptici tipo "Expecting ',' delimiter" su stringhe
        # apparentemente valide.
        normalized = (
            raw_json.strip()
            .replace("“", '"').replace("”", '"')
            .replace("‘", "'").replace("’", "'")
            .replace(" ", " ")
        )
        try:
            raw_data = _json.loads(normalized)
            if not isinstance(raw_data, dict):
                raise ValueError("raw_json deve essere un oggetto JSON (dict)")
        except _json.JSONDecodeError as e:
            # Aggiungi contesto: ~15 char prima/dopo l'errore
            pos = e.pos
            ctx_start = max(0, pos - 15)
            ctx_end = min(len(normalized), pos + 15)
            ctx = normalized[ctx_start:ctx_end]
            ptr = " " * (pos - ctx_start) + "^"
            raise HTTPException(
                status_code=400,
                detail=(
                    f"raw_json non valido alla posizione {pos}: {e.msg}. "
                    f"Contesto: ...{ctx}... (carattere sotto: {ptr}). "
                    f"Suggerimento: assicurati di usare virgolette dritte (\") e niente "
                    f"caratteri speciali da copia-incolla."
                ),
            )
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"raw_json non valido: {e}")

    form = await request.form()
    asset_tags: dict[str, list[str]] = {}
    for k, v in (form.multi_items() if hasattr(form, "multi_items") else form.items()):
        if not isinstance(k, str) or not k.startswith("tag_key__"):
            continue
        idx = k[len("tag_key__"):]
        key = (v or "").strip().lower()
        val = (form.get(f"tag_value__{idx}") or "").strip()
        if not key or not val:
            continue
        if not _re.match(r"^[a-z][a-z0-9_-]{0,49}$", key):
            continue
        asset_tags.setdefault(key, []).append(val[:200])

    # === Campi rapidi (UI key=value, alternativa friendly al raw_json) ===
    # Merge nel raw_data: per ogni quick_key__N + quick_value__N nel form,
    # aggiungiamo la coppia al raw_data DICT (sovrascrive eventuali chiavi
    # gia' presenti nel raw_json esplicito — i campi rapidi vincono perche'
    # l'utente li ha digitati direttamente). La logica di promozione sotto
    # gestira' la mappatura colonna/tag.
    for k, v in (form.multi_items() if hasattr(form, "multi_items") else form.items()):
        if not isinstance(k, str) or not k.startswith("quick_key__"):
            continue
        idx = k[len("quick_key__"):]
        qk = (v or "").strip().lower()
        qv = (form.get(f"quick_value__{idx}") or "").strip()
        if not qk or not qv:
            continue
        if not _re.match(r"^[a-z][a-z0-9_-]{0,49}$", qk):
            continue
        # Inseriamo in raw_data: la promozione sotto si occupera' del routing
        # (colonna dedicata se chiave nota, tag se chiave libera).
        raw_data[qk] = qv[:500]

    # Per agevolare il pattern "asset = contatto/persona manuale", se raw_json
    # contiene chiavi note (whatsapp, email, telegram, role, organization, ...)
    # promuovile a colonne dedicate dell'asset cosi' che siano subito utilizzabili
    # dai runner outreach senza dover passare per raw_json.
    promoted_fields: dict = {}
    if isinstance(raw_data, dict):
        _COL_MAP = {
            "whatsapp": "whatsapp",
            "email": "email",
            "telegram": "telegram_username",
            "telegram_username": "telegram_username",
            "telegram_chat_id": "telegram_chat_id",
            "display_name": "display_name",
            "sitoweb": "sitoweb",
            "website": "sitoweb",
            "social": "social_json",
            "social_json": "social_json",
        }
        for k_in, col in _COL_MAP.items():
            v_in = raw_data.get(k_in)
            if v_in is None:
                continue
            if col == "social_json" and not isinstance(v_in, str):
                import json as _j
                v_in = _j.dumps(v_in, ensure_ascii=False)
            promoted_fields[col] = v_in
        # Tag custom dal raw_json per chiavi non promosse a colonna (es. role,
        # organization): vanno in asset_tags se non gia' settate altrove.
        for k_in, v_in in raw_data.items():
            if k_in in _COL_MAP:
                continue
            if not isinstance(v_in, (str, int, float)):
                continue
            tk = str(k_in).strip().lower()
            if not _re.match(r"^[a-z][a-z0-9_-]{0,49}$", tk):
                continue
            tv = str(v_in).strip()[:200]
            if not tv:
                continue
            asset_tags.setdefault(tk, []).append(tv)

    asset_id = db.upsert_asset(
        {
            "asset_type": asset_type,
            "title": title,
            "source_url": source_url or None,
            "notes": notes.strip() or None,
            "raw_json": raw_data,
            **promoted_fields,
        },
        tags=asset_tags,
    )
    if status != "new":
        db.update_asset_status(asset_id, status)
    return RedirectResponse(
        url=f"/assets/{asset_id}?flash=Asset+%23{asset_id}+creato",
        status_code=303,
    )


@router.get("/assets/{asset_id}", response_class=HTMLResponse)
async def asset_detail(request: Request, asset_id: int):
    asset = db.get_asset(asset_id)
    if not asset:
        raise HTTPException(status_code=404, detail="asset non trovato")

    # Parsing del raw_json per rendering ricco (narrative, interests, liked_pages, ecc.)
    import json as _json
    raw_data: dict = {}
    raw_pretty: str = asset.get("raw_json") or ""
    try:
        parsed = _json.loads(raw_pretty) if isinstance(raw_pretty, str) else raw_pretty
        if isinstance(parsed, dict):
            raw_data = parsed
            raw_pretty = _json.dumps(parsed, ensure_ascii=False, indent=2)
    except Exception:
        pass

    # Contact linkati a questo asset (1 → N)
    linked_contacts = []
    try:
        with db.connect() as con:
            rows = con.execute(
                "SELECT id, display_name, email, telegram_username, whatsapp, "
                "sitoweb, social_json, status, qualifier_score "
                "FROM contacts WHERE asset_id = %s ORDER BY id",
                (asset_id,),
            ).fetchall()
            linked_contacts = [dict(r) for r in rows]
    except Exception:
        pass

    return templates.TemplateResponse(
        request,
        "asset_detail.html",
        {
            "asset": asset,
            "raw_data": raw_data,
            "raw_pretty": raw_pretty,
            "linked_contacts": linked_contacts,
        },
    )


@router.post("/assets/{asset_id}/status")
async def asset_set_status(
    asset_id: int,
    status: str = Form(""),
    notes: str = Form(""),
):
    if status.strip() not in {"new", "qualified", "rejected", "archived"}:
        raise HTTPException(status_code=400, detail="status non valido")
    if not db.get_asset(asset_id):
        raise HTTPException(status_code=404, detail="asset non trovato")
    db.update_asset_status(asset_id, status.strip(), notes=(notes.strip() or None))
    return RedirectResponse(url=f"/assets/{asset_id}?flash=Stato+aggiornato", status_code=303)


# === EDIT manuale ===

@router.get("/assets/{asset_id}/edit", response_class=HTMLResponse)
async def asset_edit_form(request: Request, asset_id: int):
    asset = db.get_asset(asset_id)
    if not asset:
        raise HTTPException(status_code=404, detail="asset non trovato")
    import json as _json
    raw_pretty = asset.get("raw_json") or ""
    try:
        parsed = _json.loads(raw_pretty) if isinstance(raw_pretty, str) else raw_pretty
        if isinstance(parsed, dict):
            raw_pretty = _json.dumps(parsed, ensure_ascii=False, indent=2)
    except Exception:
        pass
    # Conta contact linkati per warning + checkbox propaga
    n_linked = 0
    try:
        with db.connect() as con:
            row = con.execute(
                "SELECT COUNT(*) FROM contacts WHERE asset_id = %s", (asset_id,)
            ).fetchone()
            n_linked = int(row["count"] if isinstance(row, dict) else row[0])
    except Exception:
        pass
    return templates.TemplateResponse(
        request,
        "asset_edit.html",
        {
            "asset": asset,
            "raw_pretty": raw_pretty,
            "existing_types": db.list_asset_types_in_use(),
            "n_linked_contacts": n_linked,
        },
    )


@router.post("/assets/{asset_id}/edit")
async def asset_edit_submit(
    asset_id: int,
    asset_type: str = Form(""),
    title: str = Form(""),
    source_url: str = Form(""),
    notes: str = Form(""),
    status: str = Form("new"),
    raw_json: str = Form(""),
    propagate_to_contacts: str = Form(""),
):
    asset = db.get_asset(asset_id)
    if not asset:
        raise HTTPException(status_code=404, detail="asset non trovato")
    asset_type = asset_type.strip().lower()
    title = title.strip()
    source_url = source_url.strip()
    status = status.strip() or "new"

    import re as _re
    if not _re.match(r"^[a-z][a-z0-9_-]{0,49}$", asset_type):
        raise HTTPException(status_code=400, detail="asset_type non valido")
    if not title:
        raise HTTPException(status_code=400, detail="title obbligatorio")
    if status not in {"new", "qualified", "rejected", "archived"}:
        raise HTTPException(status_code=400, detail="status non valido")
    import json as _json
    raw_value = None
    if raw_json.strip():
        try:
            raw_value = _json.loads(raw_json)
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"raw_json non valido: {e}")

    update_fields: dict = {
        "asset_type": asset_type,
        "title": title,
        "source_url": source_url or None,
        "notes": notes.strip() or None,
        "status": status,
    }
    if raw_value is not None:
        update_fields["raw_json"] = raw_value
    db.update_asset(asset_id, **update_fields)

    # Propagazione opzionale ai contact linkati (display_name + source_url).
    # Se la checkbox è valorizzata ("1"/"on"/"true"), aggiorna i contact
    # con asset_id = questo asset.
    propagate = (propagate_to_contacts or "").strip().lower() in ("1", "on", "true", "yes")
    flash = "Asset+aggiornato"
    if propagate:
        n_updated = 0
        try:
            with db.connect() as con:
                cur = con.execute(
                    "UPDATE contacts SET display_name = %s, source_url = %s, updated_at = %s "
                    "WHERE asset_id = %s",
                    (title, source_url or None, db.now_iso(), asset_id),
                )
                n_updated = cur.rowcount
        except Exception:
            pass
        flash = f"Asset+aggiornato+(propagato+a+{n_updated}+contact)"
    return RedirectResponse(
        url=f"/assets/{asset_id}?flash={flash}",
        status_code=303,
    )


# === TAG add/remove ===

@router.post("/assets/{asset_id}/tags/add")
async def asset_tag_add(
    asset_id: int,
    tag_key: str = Form(""),
    tag_value: str = Form(""),
):
    if not db.get_asset(asset_id):
        raise HTTPException(status_code=404, detail="asset non trovato")
    db.add_asset_tag(asset_id, tag_key, tag_value)
    return RedirectResponse(url=f"/assets/{asset_id}/edit?flash=Tag+aggiunto", status_code=303)


@router.post("/assets/{asset_id}/tags/remove")
async def asset_tag_remove(
    asset_id: int,
    tag_key: str = Form(""),
    tag_value: str = Form(""),
):
    if not db.get_asset(asset_id):
        raise HTTPException(status_code=404, detail="asset non trovato")
    db.remove_asset_tag(asset_id, tag_key, tag_value)
    return RedirectResponse(url=f"/assets/{asset_id}/edit?flash=Tag+rimosso", status_code=303)


@router.post("/assets/{asset_id}/delete")
async def asset_delete(asset_id: int):
    if not db.get_asset(asset_id):
        raise HTTPException(status_code=404, detail="asset non trovato")
    db.delete_asset(asset_id)
    return RedirectResponse(url="/assets?flash=Asset+%23{}+cancellato".format(asset_id), status_code=303)


@router.post("/assets/delete-bulk")
async def assets_delete_bulk(
    request: Request,
    redirect_to: str = Form("/assets"),
):
    """Cancella in massa gli asset selezionati. Riceve `asset_ids` come lista di
    checkbox dal form della lista. Tag associati cascade-eliminati per FK.

    Se `select_all_filtered=1` e' presente, ignora asset_ids[] e re-estrae
    tutti gli asset matching i filtri della pagina (asset_type, status, tags,
    source_task_id) — fino a _MAX_EXPORT. Cosi' l'utente puo' cancellare
    bulk anche oltre la pagina corrente.
    """
    form = await request.form()
    select_all_filtered = (form.get("select_all_filtered") or "").strip() == "1"
    if select_all_filtered:
        asset_type = (form.get("asset_type") or "").strip() or None
        status = (form.get("status") or "").strip() or None
        tags_csv = (form.get("tags") or "").strip()
        tag_filters = _parse_tag_filters(tags_csv) if tags_csv else []
        source_task_id_v = _parse_optional_int(form.get("source_task_id") or "")
        rows = db.list_assets(
            asset_type=asset_type,
            status=status,
            tag_filters=tag_filters or None,
            source_task_id=source_task_id_v,
            limit=_MAX_EXPORT,
            offset=0,
        )
        ids = [int(r["id"]) for r in rows if r.get("id") is not None]
    else:
        raw_ids = form.getlist("asset_ids") if hasattr(form, "getlist") else form.get("asset_ids")
        if not isinstance(raw_ids, list):
            raw_ids = [raw_ids] if raw_ids else []
        ids = []
        for v in raw_ids:
            try:
                ids.append(int(v))
            except (TypeError, ValueError):
                continue
    n = db.delete_assets_bulk(ids) if ids else 0
    target = redirect_to if redirect_to.startswith("/") else "/assets"
    sep = "&" if "?" in target else "?"
    return RedirectResponse(
        url=f"{target}{sep}flash={n}+asset+cancellati",
        status_code=303,
    )
