"""Asset: vista generalizzata di tutto il materiale estratto dai runner.

Le righe di `profiles.jsonl` di un task vengono ingestate in tabella `assets`
con tag derivati dichiarativamente (vedi app/agent/asset_tags.py). Qui esponi
una lista filtrabile per asset_type + tag.
"""
from __future__ import annotations

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from .. import db
from ..templates import templates


router = APIRouter()


def _parse_tag_filters(raw: str | None) -> list[tuple[str, str]]:
    """Parsa il querystring `tags=key:value,key2:value2` in coppie."""
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


_ASSETS_PAGE_SIZE = 100


@router.get("/assets", response_class=HTMLResponse)
async def assets_list(
    request: Request,
    asset_type: str | None = None,
    status: str | None = None,
    tags: str | None = None,
    source_task_id: int | None = None,
    page: int = 1,
    per_page: int = _ASSETS_PAGE_SIZE,
):
    asset_type = (asset_type or "").strip() or None
    status = (status or "").strip() or None
    tag_filters = _parse_tag_filters(tags)
    # Sanitize paginazione
    per_page = max(10, min(int(per_page or _ASSETS_PAGE_SIZE), 500))
    page = max(1, int(page or 1))
    offset = (page - 1) * per_page

    total = db.count_assets(
        asset_type=asset_type,
        status=status,
        source_task_id=source_task_id,
        tag_filters=tag_filters or None,
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
        limit=per_page,
        offset=offset,
    )
    types_in_use = db.list_asset_types_in_use()
    tag_keys = db.list_asset_tag_keys(asset_type=asset_type)
    facet_values: dict[str, list[dict]] = {}
    for k in tag_keys:
        facet_values[k] = db.list_asset_tag_values(k, asset_type=asset_type, limit=30)

    # Querystring base per i link di paginazione (senza page=)
    qs_parts: list[str] = []
    if asset_type: qs_parts.append(f"asset_type={asset_type}")
    if status: qs_parts.append(f"status={status}")
    if source_task_id is not None: qs_parts.append(f"source_task_id={source_task_id}")
    if tag_filters: qs_parts.append(f"tags={_serialize_tag_filters(tag_filters)}")
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
            "types_in_use": types_in_use,
            "facet_values": facet_values,
            "tag_keys": tag_keys,
            # paginazione
            "page": page,
            "per_page": per_page,
            "total": total,
            "total_pages": total_pages,
            "offset": offset,
            "qs_base": qs_base,
        },
    )


# ===========================================================================
# /qualified — vista asset-centric filtrabile per qualifier (Fase 1)
# ===========================================================================

_QUALIFIED_PAGE_SIZE = 100


def _parse_extra_tag_filters(request: Request) -> list[tuple[str, str]]:
    """Parsa form fields `tag_key__N` / `tag_value__N` (con N=0..4) dal querystring.
    Pattern compatibile con quello già usato in /inbox/contacts."""
    out: list[tuple[str, str]] = []
    for i in range(5):
        k = (request.query_params.get(f"tag_key__{i}") or "").strip().lower()
        v = (request.query_params.get(f"tag_value__{i}") or "").strip()
        if k and v:
            out.append((k, v))
    return out


@router.get("/qualified", response_class=HTMLResponse)
async def qualified_assets_list(
    request: Request,
    qualifiers: str = "",  # comma-separated slug list
    status: str = "qualified",  # qualified | rejected | both
    score_min: int | None = None,
    asset_type: str | None = None,
    source_task_id: int | None = None,
    q: str = "",
    page: int = 1,
    per_page: int = _QUALIFIED_PAGE_SIZE,
):
    """Tab Qualified: asset-centric con multi-select qualifier + filtri.

    Query string:
        qualifiers=slug1,slug2       (intersezione AND)
        status=qualified|rejected|both
        score_min=N                  (applicato a TUTTI i qualifier selezionati)
        asset_type, source_task_id, q (search title/raw_json)
        tag_key__0 / tag_value__0    (fino a 5 slot extra-tag, AND)
        page, per_page               (paginazione)
    """
    qualifier_slugs = [s.strip() for s in (qualifiers or "").split(",") if s.strip()]
    if status not in ("qualified", "rejected", "both"):
        status = "qualified"
    asset_type = (asset_type or "").strip() or None
    search = (q or "").strip() or None
    extra_tag_filters = _parse_extra_tag_filters(request)
    per_page = max(10, min(int(per_page or _QUALIFIED_PAGE_SIZE), 500))
    page = max(1, int(page or 1))
    offset = (page - 1) * per_page

    total = db.count_qualified_assets(
        qualifier_slugs=qualifier_slugs,
        status_filter=status,
        score_min=score_min,
        asset_type=asset_type,
        source_task_id=source_task_id,
        search=search,
        extra_tag_filters=extra_tag_filters or None,
    )
    total_pages = max(1, (total + per_page - 1) // per_page)
    if page > total_pages:
        page = total_pages
        offset = (page - 1) * per_page

    assets = db.list_qualified_assets(
        qualifier_slugs=qualifier_slugs,
        status_filter=status,
        score_min=score_min,
        asset_type=asset_type,
        source_task_id=source_task_id,
        search=search,
        extra_tag_filters=extra_tag_filters or None,
        limit=per_page,
        offset=offset,
    )

    # Menu qualifier (sempre da tutto il tenant, non filtrati per selezione corrente)
    qualifier_menu = db.list_distinct_qualifier_slugs()
    types_in_use = db.list_asset_types_in_use()

    # Querystring base per i link di paginazione (esclude page=)
    qs_parts: list[str] = []
    if qualifiers: qs_parts.append(f"qualifiers={qualifiers}")
    if status != "qualified": qs_parts.append(f"status={status}")
    if score_min is not None: qs_parts.append(f"score_min={score_min}")
    if asset_type: qs_parts.append(f"asset_type={asset_type}")
    if source_task_id is not None: qs_parts.append(f"source_task_id={source_task_id}")
    if search: qs_parts.append(f"q={search}")
    for i, (k, v) in enumerate(extra_tag_filters):
        qs_parts.append(f"tag_key__{i}={k}")
        qs_parts.append(f"tag_value__{i}={v}")
    if per_page != _QUALIFIED_PAGE_SIZE: qs_parts.append(f"per_page={per_page}")
    qs_base = "&".join(qs_parts)

    return templates.TemplateResponse(
        request,
        "qualified_list.html",
        {
            "assets": assets,
            "qualifier_menu": qualifier_menu,
            "selected_qualifiers": qualifier_slugs,
            "filter_status": status,
            "filter_score_min": score_min,
            "filter_type": asset_type or "",
            "filter_task": source_task_id,
            "filter_search": search or "",
            "extra_tag_filters": extra_tag_filters,
            "types_in_use": types_in_use,
            # paginazione
            "page": page,
            "per_page": per_page,
            "total": total,
            "total_pages": total_pages,
            "offset": offset,
            "qs_base": qs_base,
        },
    )


# === Search HTMX + preview JSON (per import in form contact) ===
# Definiti PRIMA di /assets/{asset_id} per evitare conflitto di routing.

@router.get("/assets/tag_values", response_class=HTMLResponse)
async def asset_tag_values_htmx(request: Request, key: str = ""):
    """HTMX endpoint: ritorna <option> per il dropdown tag_value dato un
    tag_key. Usato dal form task per popolare dinamicamente i value disponibili
    quando l'utente sceglie una key (filtro outreach multi-tag).
    """
    key = (key or "").strip().lower()
    if not key:
        return HTMLResponse('<option value="">— prima scegli una key —</option>')
    try:
        values = db.list_distinct_tag_values_for_contacts(key, limit=100)
    except Exception:
        values = []
    parts = ['<option value="">— seleziona —</option>']
    for v in values:
        # Escape HTML semplice
        val = (v.get("value") or "").replace('"', "&quot;").replace("<", "&lt;")
        n = v.get("count", 0)
        parts.append(f'<option value="{val}">{val} ({n})</option>')
    return HTMLResponse("".join(parts))


@router.get("/assets/search", response_class=HTMLResponse)
async def asset_search_htmx(
    request: Request,
    q: str = "",
    asset_type: str = "",
    limit: int = 10,
):
    """Search HTMX: ritorna lista di asset matching `q` su title / asset_type /
    tag value. Usata dal modal 'Aggiungi contatto' per importare info da un
    asset esistente.
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
    return templates.TemplateResponse(
        request,
        "_asset_search_results.html",
        {"items": items, "q": q},
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
        try:
            raw_data = _json.loads(raw_json)
            if not isinstance(raw_data, dict):
                raise ValueError("raw_json deve essere un oggetto JSON")
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

    asset_id = db.upsert_asset(
        {
            "asset_type": asset_type,
            "title": title,
            "source_url": source_url or None,
            "notes": notes.strip() or None,
            "raw_json": raw_data,
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
    """
    form = await request.form()
    raw_ids = form.getlist("asset_ids") if hasattr(form, "getlist") else form.get("asset_ids")
    if not isinstance(raw_ids, list):
        raw_ids = [raw_ids] if raw_ids else []
    ids: list[int] = []
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
