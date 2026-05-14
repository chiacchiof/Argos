"""Wizard import CSV → assets + asset_tags + contacts (3 step).

Step 1  GET  /import           → form metadata + upload
Step 2  POST /import/upload    → parse CSV, render mapping con preview
Step 3  POST /import/preview   → dry-run, mostra stats + sample
        POST /import/run       → import vero, redirect a /assets

Pattern di sicurezza copiato da `tasks.upload_artifact`: file salvato sotto
`UPLOADS_DIR/imports/<ts>/<safe_name>.csv`, validazione path traversal su tutti
i POST successivi.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse

from .. import db
from ..config import UPLOADS_DIR
from ..import_csv import (
    TARGET_FIELDS,
    ColumnMapping,
    execute_import,
    parse_csv_safe,
    parse_mapping,
    suggest_mapping,
)
from ..templates import templates


router = APIRouter()

IMPORTS_DIR = UPLOADS_DIR / "imports"
MAX_UPLOAD_BYTES = 10 * 1024 * 1024  # 10 MB

_ASSET_TYPE_RE = re.compile(r"^[a-z][a-z0-9_-]{0,49}$")


def _safe_imports_path(upload_path: str) -> Path:
    """Valida che `upload_path` sia dentro IMPORTS_DIR (anti path traversal).
    Solleva HTTPException 400 se no."""
    if not upload_path:
        raise HTTPException(status_code=400, detail="upload_path mancante")
    p = Path(upload_path).resolve()
    base = IMPORTS_DIR.resolve()
    try:
        p.relative_to(base)
    except ValueError:
        raise HTTPException(status_code=400, detail="upload_path non valido")
    if not p.exists() or not p.is_file():
        raise HTTPException(status_code=400, detail=f"file non trovato: {p.name}")
    return p


def _existing_types_with_schema() -> list[dict[str, Any]]:
    """Lista degli asset_type esistenti con count e tag_keys associati.

    Ritorna [{asset_type, count, tag_keys: [...]}, ...] ordinato per count desc.
    Usato dallo step 1 per mostrare i "type già definiti" e i loro attributi
    noti (cosi' l'utente sa cosa il sistema gia' conosce).
    """
    try:
        types = db.list_asset_types_in_use()
        out: list[dict[str, Any]] = []
        for t in types:
            atype = t.get("asset_type")
            if not atype:
                continue
            tag_keys = db.list_asset_tag_keys(asset_type=atype)
            out.append({
                "asset_type": atype,
                "count": t.get("count", 0),
                "tag_keys": tag_keys,
            })
        return out
    except Exception:
        return []


# === Step 1 ===

@router.get("/import", response_class=HTMLResponse)
async def import_step1(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "import_csv_step1.html",
        {
            "existing_types": _existing_types_with_schema(),
        },
    )


# === Step 2: upload + mapping ===

@router.post("/import/upload", response_class=HTMLResponse)
async def import_upload(
    request: Request,
    import_name: str = Form(...),
    asset_type: str = Form(...),
    description: str = Form(""),
    file: UploadFile = File(...),
) -> HTMLResponse:
    # Validazioni base
    import_name = import_name.strip()[:200]
    asset_type = asset_type.strip().lower()
    description = description.strip()

    if not import_name:
        raise HTTPException(status_code=400, detail="Nome import richiesto")
    if not _ASSET_TYPE_RE.match(asset_type):
        raise HTTPException(
            status_code=400,
            detail=f"asset_type non valido: '{asset_type}'. Usa lowercase a-z 0-9 _ -, max 50.",
        )
    fname = (file.filename or "").strip()
    if not fname:
        raise HTTPException(status_code=400, detail="nome file mancante")
    if not fname.lower().endswith(".csv"):
        raise HTTPException(status_code=400, detail="solo file .csv supportati")

    safe_name = re.sub(r"[^A-Za-z0-9._-]", "_", fname)[:120] or "upload.csv"
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    dest_dir = IMPORTS_DIR / ts
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest_path = dest_dir / safe_name

    # Stream chunked write
    total = 0
    try:
        with dest_path.open("wb") as out:
            while True:
                chunk = await file.read(64 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_UPLOAD_BYTES:
                    out.close()
                    dest_path.unlink(missing_ok=True)
                    raise HTTPException(
                        status_code=413,
                        detail=f"file troppo grande (>{MAX_UPLOAD_BYTES // (1024*1024)} MB)",
                    )
                out.write(chunk)
    except HTTPException:
        raise
    except Exception as e:
        dest_path.unlink(missing_ok=True)
        raise HTTPException(status_code=500, detail=f"upload fallito: {e}")

    # Parse + suggest
    try:
        headers, rows, warnings = parse_csv_safe(dest_path, max_rows=500)
    except Exception as e:
        dest_path.unlink(missing_ok=True)
        raise HTTPException(status_code=400, detail=f"parsing CSV fallito: {e}")
    if not headers:
        dest_path.unlink(missing_ok=True)
        raise HTTPException(status_code=400, detail="CSV vuoto o senza header")

    suggested = suggest_mapping(headers)
    preview = rows[:5]

    # Lookup type esistente per dare feedback "nuovo" vs "esistente" e
    # popolare il datalist dei tag_keys con quelli già conosciuti per questo
    # type. Per type nuovi entrambi sono vuoti.
    try:
        existing_types = db.list_asset_types_in_use()
        type_count = next(
            (t["count"] for t in existing_types if t.get("asset_type") == asset_type),
            0,
        )
        known_tag_keys = db.list_asset_tag_keys(asset_type=asset_type)
    except Exception:
        type_count = 0
        known_tag_keys = []
    type_is_new = type_count == 0

    return templates.TemplateResponse(
        request,
        "import_csv_step2.html",
        {
            "import_name": import_name,
            "asset_type": asset_type,
            "type_is_new": type_is_new,
            "type_existing_count": type_count,
            "known_tag_keys": known_tag_keys,
            "description": description,
            "upload_path": str(dest_path),
            "filename": safe_name,
            "headers": headers,
            "preview_rows": preview,
            "preview_count": len(rows),
            "warnings": warnings,
            "suggested": suggested,
            "target_fields": TARGET_FIELDS,
        },
    )


# === Step 3: dry-run preview ===

@router.post("/import/preview", response_class=HTMLResponse)
async def import_preview(request: Request) -> HTMLResponse:
    form = await request.form()
    form_dict: dict[str, str] = {k: str(v) for k, v in form.items()}

    import_name = (form_dict.get("import_name") or "").strip()[:200]
    asset_type = (form_dict.get("asset_type") or "").strip().lower()
    description = (form_dict.get("description") or "").strip()
    upload_path_str = form_dict.get("upload_path") or ""

    upload_path = _safe_imports_path(upload_path_str)

    headers, _, _ = parse_csv_safe(upload_path, max_rows=1)
    mapping = parse_mapping(form_dict, headers)

    # Pre-validate: deve esistere ALMENO una mappatura asset.title e una source_url
    has_title = any(m.target_kind == "asset" and m.target_field == "title" for m in mapping)
    has_url = any(
        (m.target_kind == "asset" and m.target_field == "source_url") or
        (m.target_kind == "social" and m.target_field.endswith(".url"))
        for m in mapping
    )
    if not has_title:
        raise HTTPException(
            status_code=400,
            detail="Mappatura mancante: nessuna colonna è mappata su 'asset.title'.",
        )
    if not has_url:
        raise HTTPException(
            status_code=400,
            detail="Mappatura mancante: serve una colonna su 'asset.source_url' o "
                   "su 'social.*.url' (fallback dedup).",
        )

    # Dry-run
    stats = execute_import(
        upload_path,
        mapping,
        asset_type=asset_type,
        import_name=import_name,
        description=description,
        dry_run=True,
        sample_size=20,
    )

    # Serializza mapping_json per il form di conferma (str → str, non oggetti)
    mapping_serialized = json.dumps(
        [
            {
                "column_index": m.column_index,
                "column_name": m.column_name,
                "target_kind": m.target_kind,
                "target_field": m.target_field,
                "tag_key": m.tag_key,
            }
            for m in mapping
        ],
        ensure_ascii=False,
    )

    return templates.TemplateResponse(
        request,
        "import_csv_step3.html",
        {
            "import_name": import_name,
            "asset_type": asset_type,
            "description": description,
            "upload_path": str(upload_path),
            "mapping_json": mapping_serialized,
            "stats": stats.to_dict(),
        },
    )


# === Step 4: import vero ===

@router.post("/import/run")
async def import_run(
    request: Request,
    import_name: str = Form(...),
    asset_type: str = Form(...),
    description: str = Form(""),
    upload_path: str = Form(...),
    mapping_json: str = Form(...),
) -> RedirectResponse:
    import_name = import_name.strip()[:200]
    asset_type = asset_type.strip().lower()
    if not _ASSET_TYPE_RE.match(asset_type):
        raise HTTPException(status_code=400, detail="asset_type non valido")

    csv_path = _safe_imports_path(upload_path)

    try:
        mapping_data: list[dict[str, Any]] = json.loads(mapping_json)
    except Exception:
        raise HTTPException(status_code=400, detail="mapping_json non valido")

    mapping = [
        ColumnMapping(
            column_index=int(m["column_index"]),
            column_name=str(m.get("column_name") or ""),
            target_kind=m["target_kind"],
            target_field=str(m.get("target_field") or ""),
            tag_key=m.get("tag_key"),
        )
        for m in mapping_data
    ]

    stats = execute_import(
        csv_path,
        mapping,
        asset_type=asset_type,
        import_name=import_name,
        description=description,
        dry_run=False,
    )

    # Cleanup file dopo import (sicurezza + spazio disco)
    try:
        csv_path.unlink(missing_ok=True)
    except Exception:
        pass

    n = stats.inserted + stats.updated
    return RedirectResponse(
        url=f"/assets?asset_type={asset_type}&imported={n}",
        status_code=303,
    )
