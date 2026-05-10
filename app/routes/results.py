from __future__ import annotations

import json
import mimetypes

from pathlib import Path

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse

from .. import db, storage
from ..templates import templates

router = APIRouter()

JSONL_SUFFIXES = (".jsonl", ".ndjson")


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
