from __future__ import annotations

import mimetypes

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse

from .. import db, storage
from ..templates import templates

router = APIRouter()


def _guess_media_type(name: str) -> str:
    if name.endswith((".jsonl", ".ndjson")):
        return "application/x-ndjson; charset=utf-8"
    if name.endswith(".md"):
        return "text/markdown; charset=utf-8"
    mt, _ = mimetypes.guess_type(name)
    if mt:
        return mt + ("; charset=utf-8" if mt.startswith("text/") else "")
    return "application/octet-stream"


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


@router.get("/tasks/{task_id}/results/{run_name}/{file_path:path}")
async def download_run_file(task_id: int, run_name: str, file_path: str):
    path = storage.read_run_file(task_id, run_name, file_path)
    if not path:
        raise HTTPException(status_code=404, detail="file non trovato")
    return FileResponse(path, filename=path.name, media_type=_guess_media_type(path.name))
