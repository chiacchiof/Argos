"""Asset: vista generalizzata di tutto il materiale estratto dai runner.

Le righe di `profiles.jsonl` di un task vengono ingestate in tabella `assets`
con tag derivati dichiarativamente (vedi app/agent/asset_tags.py). Qui esponi
una lista filtrabile per asset_type + tag.
"""
from __future__ import annotations

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

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


@router.get("/assets/{asset_id}", response_class=HTMLResponse)
async def asset_detail(request: Request, asset_id: int):
    asset = db.get_asset(asset_id)
    if not asset:
        raise HTTPException(status_code=404, detail="asset non trovato")
    return templates.TemplateResponse(
        request,
        "asset_detail.html",
        {"asset": asset},
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
