from __future__ import annotations

import csv
import json
import mimetypes
import os
import tempfile
import zipfile

from io import StringIO
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from starlette.background import BackgroundTask

from .. import db, storage
from ..templates import templates

router = APIRouter()

JSONL_SUFFIXES = (".jsonl", ".ndjson")
MARKDOWN_SUFFIXES = (".md", ".markdown")
TEXT_SUFFIXES = (".txt", ".log")
JSON_SUFFIXES = (".json",)
CSV_SUFFIXES = (".csv",)

CSV_MAX_ROWS = 200
CSV_MAX_CELL = 500


def _guess_media_type(name: str) -> str:
    if name.endswith(JSONL_SUFFIXES):
        return "application/x-ndjson; charset=utf-8"
    if name.endswith(".md"):
        return "text/markdown; charset=utf-8"
    mt, _ = mimetypes.guess_type(name)
    if mt:
        return mt + ("; charset=utf-8" if mt.startswith("text/") else "")
    return "application/octet-stream"


def _is_jsonl(path: Path) -> bool:
    return path.name.lower().endswith(JSONL_SUFFIXES)


def _read_jsonl_page(path: Path, *, offset: int, limit: int) -> dict:
    rows: list[dict] = []
    total = 0
    valid = 0
    invalid = 0
    with path.open("r", encoding="utf-8", errors="replace") as f:
        for idx, raw_line in enumerate(f, start=1):
            total += 1
            raw = raw_line.rstrip("\n")
            parsed = None
            error = ""
            pretty = raw
            ok = False
            if raw.strip():
                try:
                    parsed = json.loads(raw)
                    pretty = json.dumps(parsed, ensure_ascii=False, indent=2)
                    ok = True
                    valid += 1
                except json.JSONDecodeError as e:
                    error = f"JSON non valido: {e.msg} colonna {e.colno}"
                    invalid += 1
            else:
                error = "riga vuota"
                invalid += 1

            if offset < idx <= offset + limit:
                rows.append(
                    {
                        "line_no": idx,
                        "ok": ok,
                        "pretty": pretty,
                        "raw": raw,
                        "error": error,
                    }
                )
    return {
        "rows": rows,
        "total": total,
        "valid": valid,
        "invalid": invalid,
        "offset": offset,
        "limit": limit,
        "has_prev": offset > 0,
        "has_next": offset + limit < total,
        "prev_offset": max(0, offset - limit),
        "next_offset": offset + limit,
        "end_index": min(offset + limit, total),
    }


def _jsonl_template(
    request: Request,
    *,
    task: dict,
    path: Path,
    title: str,
    download_url: str,
    viewer_url: str,
    offset: int,
    limit: int,
) -> HTMLResponse:
    if not _is_jsonl(path):
        raise HTTPException(status_code=400, detail="viewer disponibile solo per .jsonl/.ndjson")
    page = _read_jsonl_page(path, offset=offset, limit=limit)
    return templates.TemplateResponse(
        request,
        "jsonl_viewer.html",
        {
            "task": task,
            "title": title,
            "path": path,
            "download_url": download_url,
            "viewer_url": viewer_url,
            "page": page,
        },
    )


@router.get("/tasks/{task_id}/results", response_class=HTMLResponse)
async def task_results(request: Request, task_id: int):
    task = db.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="task non trovato")
    items = storage.list_results(task_id)
    return templates.TemplateResponse(
        request, "results_list.html", {"task": task, "items": items}
    )


@router.get("/tasks/{task_id}/results/{name}")
async def download_or_show(request: Request, task_id: int, name: str):
    """File flat → download; nome è una run dir → mostra contenuti."""
    task = db.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="task non trovato")

    flat = storage.read_result_file(task_id, name)
    if flat is not None:
        return FileResponse(flat, filename=name, media_type=_guess_media_type(name))

    files = storage.list_run_files(task_id, name)
    if files is None:
        raise HTTPException(status_code=404, detail="risorsa non trovata")

    return templates.TemplateResponse(
        request,
        "results_run.html",
        {"task": task, "run_name": name, "files": files},
    )


@router.get("/tasks/{task_id}/results/{name}/view", response_class=HTMLResponse)
async def view_flat_jsonl(
    request: Request,
    task_id: int,
    name: str,
    offset: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=500),
):
    task = db.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="task non trovato")
    path = storage.read_result_file(task_id, name)
    if not path:
        raise HTTPException(status_code=404, detail="file non trovato")
    return _jsonl_template(
        request,
        task=task,
        path=path,
        title=name,
        download_url=f"/tasks/{task_id}/results/{name}",
        viewer_url=f"/tasks/{task_id}/results/{name}/view",
        offset=offset,
        limit=limit,
    )


@router.get("/tasks/{task_id}/results-view/{run_name}/{file_path:path}", response_class=HTMLResponse)
async def view_run_jsonl(
    request: Request,
    task_id: int,
    run_name: str,
    file_path: str,
    offset: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=500),
):
    task = db.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="task non trovato")
    path = storage.read_run_file(task_id, run_name, file_path)
    if not path:
        raise HTTPException(status_code=404, detail="file non trovato")
    return _jsonl_template(
        request,
        task=task,
        path=path,
        title=f"{run_name}/{file_path}",
        download_url=f"/tasks/{task_id}/results/{run_name}/{file_path}",
        viewer_url=f"/tasks/{task_id}/results-view/{run_name}/{file_path}",
        offset=offset,
        limit=limit,
    )


@router.get("/tasks/{task_id}/results/{run_name}/{file_path:path}")
async def download_run_file(task_id: int, run_name: str, file_path: str):
    path = storage.read_run_file(task_id, run_name, file_path)
    if not path:
        raise HTTPException(status_code=404, detail="file non trovato")
    return FileResponse(path, filename=path.name, media_type=_guess_media_type(path.name))


# ============================================================================
# File viewer unificato + ZIP download + scorciatoia "ultimi report"
# ============================================================================

def _classify(name: str) -> str:
    """Dispatch per file_viewer.html. Suffix → kind ∈ {markdown,json,csv,text,
    jsonl,binary}. jsonl/ndjson hanno il loro viewer paginato dedicato.
    """
    n = name.lower()
    if n.endswith(JSONL_SUFFIXES):
        return "jsonl"
    if n.endswith(MARKDOWN_SUFFIXES):
        return "markdown"
    if n.endswith(JSON_SUFFIXES):
        return "json"
    if n.endswith(CSV_SUFFIXES):
        return "csv"
    if n.endswith(TEXT_SUFFIXES):
        return "text"
    return "binary"


def _render_csv_preview(raw: str) -> dict:
    """Parsa il CSV e ritorna un dict con headers + rows troncati. Limit hard:
    CSV_MAX_ROWS e celle troncate a CSV_MAX_CELL per evitare DoS visivo.
    """
    reader = csv.reader(StringIO(raw))
    rows: list[list[str]] = []
    truncated = False
    total = 0
    for row in reader:
        total += 1
        if len(rows) >= CSV_MAX_ROWS:
            truncated = True
            continue
        rows.append(
            [
                (c if len(c) <= CSV_MAX_CELL else c[:CSV_MAX_CELL] + "…")
                for c in row
            ]
        )
    headers = rows[0] if rows else []
    body = rows[1:] if len(rows) > 1 else []
    return {
        "headers": headers,
        "rows": body,
        "total_rows": total,
        "shown_rows": min(total, CSV_MAX_ROWS),
        "truncated": truncated,
    }


def _build_view_context(
    *,
    task: dict,
    path: Path,
    raw: str,
    title: str,
    download_url: str,
    parent_url: str,
    parent_label: str,
) -> dict:
    kind = _classify(path.name)
    ctx: dict = {
        "task": task,
        "title": title,
        "download_url": download_url,
        "parent_url": parent_url,
        "parent_label": parent_label,
        "kind": kind,
        "size": len(raw.encode("utf-8", errors="replace")),
        "name": path.name,
    }
    if kind == "markdown":
        ctx["raw"] = raw
    elif kind == "json":
        try:
            parsed = json.loads(raw) if raw.strip() else None
            ctx["pretty"] = (
                json.dumps(parsed, ensure_ascii=False, indent=2) if parsed is not None else raw
            )
            ctx["json_ok"] = parsed is not None
        except json.JSONDecodeError as e:
            ctx["pretty"] = raw
            ctx["json_ok"] = False
            ctx["json_error"] = f"{e.msg} (linea {e.lineno}, col {e.colno})"
    elif kind == "csv":
        ctx["csv"] = _render_csv_preview(raw)
    else:
        ctx["raw"] = raw
    return ctx


def _serve_view(
    request: Request,
    task: dict,
    parts: list[str],
    title: str,
    download_url: str,
    parent_url: str,
    parent_label: str,
) -> HTMLResponse:
    result = storage.read_text_safe(task["id"], parts)
    if result is None:
        # File esiste ma troppo grande, o non esiste, o suffisso non viewabile:
        # ricontrolla l'esistenza fisica per dare il messaggio giusto.
        if len(parts) == 1:
            phys = storage.read_result_file(task["id"], parts[0])
        else:
            phys = storage.read_run_file(task["id"], parts[0], "/".join(parts[1:]))
        if phys is None:
            raise HTTPException(status_code=404, detail="file non trovato")
        # File esistente ma >5 MB: redirige al download.
        return RedirectResponse(url=download_url, status_code=303)
    path, raw = result
    ctx = _build_view_context(
        task=task,
        path=path,
        raw=raw,
        title=title,
        download_url=download_url,
        parent_url=parent_url,
        parent_label=parent_label,
    )
    return templates.TemplateResponse(request, "file_viewer.html", ctx)


@router.get("/tasks/{task_id}/file-view/{name}", response_class=HTMLResponse)
async def view_flat_file(
    request: Request,
    task_id: int,
    name: str,
    offset: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=500),
):
    """Reader file flat al top-level del task. JSONL → viewer paginato dedicato."""
    task = db.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="task non trovato")
    kind = _classify(name)
    if kind == "jsonl":
        return await view_flat_jsonl(request, task_id, name, offset, limit)
    if kind == "binary":
        # Suffix non riconosciuto: redirige al download (nessun reader sicuro).
        return RedirectResponse(url=f"/tasks/{task_id}/results/{name}", status_code=303)
    return _serve_view(
        request,
        task=task,
        parts=[name],
        title=name,
        download_url=f"/tasks/{task_id}/results/{name}",
        parent_url=f"/tasks/{task_id}/results",
        parent_label="Risultati",
    )


@router.get(
    "/tasks/{task_id}/file-view/{run_name}/{file_path:path}",
    response_class=HTMLResponse,
)
async def view_run_file(
    request: Request,
    task_id: int,
    run_name: str,
    file_path: str,
    offset: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=500),
):
    """Reader file dentro una cartella di run (anche annidato)."""
    task = db.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="task non trovato")
    full_name = Path(file_path).name
    kind = _classify(full_name)
    if kind == "jsonl":
        return await view_run_jsonl(
            request, task_id, run_name, file_path, offset, limit
        )
    if kind == "binary":
        return RedirectResponse(
            url=f"/tasks/{task_id}/results/{run_name}/{file_path}", status_code=303
        )
    parts = [run_name] + file_path.split("/")
    return _serve_view(
        request,
        task=task,
        parts=parts,
        title=f"{run_name}/{file_path}",
        download_url=f"/tasks/{task_id}/results/{run_name}/{file_path}",
        parent_url=f"/tasks/{task_id}/results/{run_name}",
        parent_label=run_name,
    )


def _build_zip(task_id: int, sub: str | None) -> str:
    """Crea uno ZIP temporaneo su disco e ne ritorna il path. Cleanup affidato
    al BackgroundTask della FileResponse del caller.
    """
    tmp = tempfile.NamedTemporaryFile(suffix=".zip", delete=False)
    tmp_path = tmp.name
    tmp.close()
    try:
        with zipfile.ZipFile(tmp_path, "w", zipfile.ZIP_DEFLATED, allowZip64=True) as zf:
            for path, arcname in storage.iter_files_for_zip(task_id, sub):
                # Prefisso arcname con task_{id} / run quando esiste, per evitare
                # ZIP "flat" senza root folder.
                if sub:
                    full_arc = f"{sub}/{arcname}"
                else:
                    full_arc = f"task_{task_id}/{arcname}"
                try:
                    zf.write(path, full_arc)
                except OSError:
                    continue
    except Exception:
        # In caso di errore durante la creazione, rimuovo il file orfano.
        try:
            os.unlink(tmp_path)
        finally:
            raise
    return tmp_path


@router.get("/tasks/{task_id}/results.zip")
async def zip_all_results(task_id: int):
    """Scarica ZIP di tutto `data/results/{task_id}/`."""
    task = db.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="task non trovato")
    size = storage.total_size_safe(task_id, sub=None)
    if size is None:
        raise HTTPException(
            status_code=413,
            detail="Risultati troppo grandi per ZIP (cap 500 MB / 10.000 file). Scarica le run singolarmente.",
        )
    if size == 0:
        raise HTTPException(status_code=404, detail="nessun file da archiviare")
    tmp_path = _build_zip(task_id, sub=None)
    return FileResponse(
        tmp_path,
        filename=f"task_{task_id}_results.zip",
        media_type="application/zip",
        background=BackgroundTask(_safe_unlink, tmp_path),
    )


@router.get("/tasks/{task_id}/results/{run_name}.zip")
async def zip_run(task_id: int, run_name: str):
    """Scarica ZIP di una singola run."""
    task = db.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="task non trovato")
    size = storage.total_size_safe(task_id, sub=run_name)
    if size is None:
        raise HTTPException(
            status_code=413,
            detail="Run troppo grande per ZIP (cap 500 MB / 10.000 file).",
        )
    if size == 0:
        raise HTTPException(status_code=404, detail="run vuota o inesistente")
    tmp_path = _build_zip(task_id, sub=run_name)
    return FileResponse(
        tmp_path,
        filename=f"task_{task_id}_{run_name}.zip",
        media_type="application/zip",
        background=BackgroundTask(_safe_unlink, tmp_path),
    )


def _safe_unlink(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


@router.get("/tasks/{task_id}/recent-reports", response_class=HTMLResponse)
async def recent_reports_partial(request: Request, task_id: int):
    """Partial HTMX per la mini-sezione 'Ultimi 3 report' su task_detail.html."""
    task = db.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="task non trovato")
    items = storage.list_recent_reports(task_id, limit=3)
    return templates.TemplateResponse(
        request,
        "partials/recent_reports.html",
        {"task": task, "items": items},
    )
