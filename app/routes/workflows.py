"""Route per i Workflow (entità di prima classe).

Un workflow contiene un DAG di task con edges tra loro. Si può creare/editare
e eseguire come blocco unico (▶ Esegui workflow → lancia i task root).
"""
from __future__ import annotations

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import ValidationError

from .. import db, jobs
from ..models import WorkflowIn
from ..templates import templates

router = APIRouter()


@router.get("/workflows", response_class=HTMLResponse)
async def workflows_list(request: Request):
    workflows = db.list_workflows()
    # arricchisci con count di task / edges per workflow
    enriched = []
    for w in workflows:
        edges = db.list_edges(workflow_id=w["id"])
        task_ids = set()
        for e in edges:
            task_ids.add(e["from_task_id"])
            task_ids.add(e["to_task_id"])
        enriched.append({**w, "n_edges": len(edges), "n_tasks": len(task_ids)})
    return templates.TemplateResponse(
        request, "workflows_list.html", {"workflows": enriched}
    )


@router.get("/workflows/new", response_class=HTMLResponse)
async def new_workflow_form(request: Request):
    return templates.TemplateResponse(
        request, "workflow_form.html",
        {"workflow": None, "errors": None},
    )


@router.post("/workflows")
async def create_workflow(
    request: Request,
    name: str = Form(""),
    description: str = Form(""),
):
    try:
        validated = WorkflowIn(name=name, description=(description or "").strip() or None)
    except ValidationError as e:
        return templates.TemplateResponse(
            request, "workflow_form.html",
            {"workflow": {"name": name, "description": description}, "errors": e.errors()},
            status_code=400,
        )
    wid = db.create_workflow(validated.name, validated.description)
    return RedirectResponse(url=f"/workflows/{wid}", status_code=303)


@router.get("/workflows/{workflow_id}/edit", response_class=HTMLResponse)
async def edit_workflow_form(request: Request, workflow_id: int):
    w = db.get_workflow(workflow_id)
    if not w:
        raise HTTPException(status_code=404, detail="workflow non trovato")
    return templates.TemplateResponse(
        request, "workflow_form.html", {"workflow": w, "errors": None}
    )


@router.post("/workflows/{workflow_id}")
async def update_workflow(
    workflow_id: int,
    request: Request,
    name: str = Form(""),
    description: str = Form(""),
):
    if not db.get_workflow(workflow_id):
        raise HTTPException(status_code=404, detail="workflow non trovato")
    try:
        validated = WorkflowIn(name=name, description=(description or "").strip() or None)
    except ValidationError as e:
        return templates.TemplateResponse(
            request, "workflow_form.html",
            {"workflow": {"id": workflow_id, "name": name, "description": description},
             "errors": e.errors()},
            status_code=400,
        )
    db.update_workflow(workflow_id, validated.name, validated.description)
    return RedirectResponse(url=f"/workflows/{workflow_id}", status_code=303)


@router.post("/workflows/{workflow_id}/delete")
async def delete_workflow(workflow_id: int):
    db.delete_workflow(workflow_id)
    return RedirectResponse(url="/workflows", status_code=303)


@router.get("/workflows/{workflow_id}", response_class=HTMLResponse)
async def workflow_detail(request: Request, workflow_id: int):
    w = db.get_workflow(workflow_id)
    if not w:
        raise HTTPException(status_code=404, detail="workflow non trovato")
    edges = db.list_edges(workflow_id=workflow_id)
    all_tasks = db.list_tasks()
    tmap = {t["id"]: t for t in all_tasks}
    edges_enriched = []
    for e in edges:
        edges_enriched.append({
            **e,
            "from_name": (tmap.get(e["from_task_id"]) or {}).get("name", f"#{e['from_task_id']}"),
            "to_name": (tmap.get(e["to_task_id"]) or {}).get("name", f"#{e['to_task_id']}"),
        })
    roots = db.find_workflow_roots(workflow_id)
    live = _workflow_live_view(workflow_id, edges)
    return templates.TemplateResponse(
        request,
        "workflow_detail.html",
        {
            "workflow": w,
            "edges": edges_enriched,
            "tasks": all_tasks,
            "roots": roots,
            **live,
        },
    )


@router.get("/workflows/{workflow_id}/runs", response_class=HTMLResponse)
async def workflow_runs_partial(request: Request, workflow_id: int):
    w = db.get_workflow(workflow_id)
    if not w:
        raise HTTPException(status_code=404, detail="workflow non trovato")
    edges = db.list_edges(workflow_id=workflow_id)
    live = _workflow_live_view(workflow_id, edges)
    return templates.TemplateResponse(
        request,
        "partials/workflow_runs_wrapper.html",
        {"workflow": w, **live},
    )


def _workflow_live_view(workflow_id: int, edges: list[dict]) -> dict:
    """Calcola runs + stato per task del run piu' recente attivo."""
    runs = db.list_workflow_runs(workflow_id, limit=20)
    has_active = any(r.get("status") in ("queued", "running") for r in runs)
    active_run = next((r for r in runs if r.get("status") in ("queued", "running")), None)
    active_tasks: list[dict] = []
    if active_run:
        task_ids: set[int] = set()
        for e in edges:
            if e.get("from_task_id"):
                task_ids.add(int(e["from_task_id"]))
            if e.get("to_task_id"):
                task_ids.add(int(e["to_task_id"]))
        for r in db.find_workflow_roots(workflow_id):
            task_ids.add(int(r))
        run_jobs = db.list_jobs_for_workflow_run(int(active_run["id"]))
        latest_per_task: dict[int, dict] = {}
        for j in run_jobs:
            tid = j.get("task_id")
            if tid is None:
                continue
            tid = int(tid)
            prev = latest_per_task.get(tid)
            if prev is None or int(j.get("id") or 0) > int(prev.get("id") or 0):
                latest_per_task[tid] = j
        for tid in sorted(task_ids):
            t = db.get_task(tid)
            j = latest_per_task.get(tid)
            active_tasks.append(
                {
                    "task_id": tid,
                    "task_name": (t or {}).get("name") or "(eliminato)",
                    "agent_mode": (t or {}).get("agent_mode") or "?",
                    "job_id": (j or {}).get("id"),
                    "status": (j or {}).get("status"),
                    "started_at": (j or {}).get("started_at"),
                    "finished_at": (j or {}).get("finished_at"),
                }
            )
    return {
        "runs": runs,
        "has_active": has_active,
        "active_run": active_run,
        "active_tasks": active_tasks,
    }


@router.post("/workflows/{workflow_id}/edges")
async def create_edge(
    workflow_id: int,
    from_task_id: str = Form(""),
    to_task_id: str = Form(""),
    pass_artifact: str = Form(""),
    enabled: str = Form(""),
):
    if not db.get_workflow(workflow_id):
        raise HTTPException(status_code=404, detail="workflow non trovato")

    # Validazione esplicita: entrambi i campi devono essere int validi
    def _quote(msg: str) -> str:
        return msg.replace(" ", "+").replace("'", "%27")

    if not from_task_id.strip() or not to_task_id.strip():
        return RedirectResponse(
            url=f"/workflows/{workflow_id}?error={_quote('Seleziona sia il task upstream che quello downstream')}",
            status_code=303,
        )
    try:
        from_id = int(from_task_id)
        to_id = int(to_task_id)
    except ValueError:
        return RedirectResponse(
            url=f"/workflows/{workflow_id}?error={_quote('ID task non valido')}",
            status_code=303,
        )

    # Verifica esistenza tasks (per dare messaggio chiaro invece del FK error generico)
    if not db.get_task(from_id):
        return RedirectResponse(
            url=f"/workflows/{workflow_id}?error={_quote(f'Task upstream #{from_id} non esiste')}",
            status_code=303,
        )
    if not db.get_task(to_id):
        return RedirectResponse(
            url=f"/workflows/{workflow_id}?error={_quote(f'Task downstream #{to_id} non esiste')}",
            status_code=303,
        )

    artifact = pass_artifact.strip() or None
    try:
        db.create_edge(
            from_task_id=from_id,
            to_task_id=to_id,
            workflow_id=workflow_id,
            pass_artifact=artifact,
            enabled=bool(enabled),
        )
    except ValueError as e:
        return RedirectResponse(
            url=f"/workflows/{workflow_id}?error={_quote(str(e))}",
            status_code=303,
        )
    except Exception as e:
        return RedirectResponse(
            url=f"/workflows/{workflow_id}?error={_quote(f'{type(e).__name__}: {str(e)[:120]}')}",
            status_code=303,
        )
    return RedirectResponse(
        url=f"/workflows/{workflow_id}?flash=Edge+creato",
        status_code=303,
    )


@router.post("/workflow_edges/{edge_id}/toggle")
async def toggle_edge(edge_id: int, redirect_to: str = Form("/workflows")):
    edges = db.list_all_edges()
    cur = next((e for e in edges if e["id"] == edge_id), None)
    if not cur:
        raise HTTPException(status_code=404, detail="edge non trovato")
    db.toggle_edge(edge_id, not bool(cur.get("enabled")))
    return RedirectResponse(url=redirect_to, status_code=303)


@router.post("/workflow_edges/{edge_id}/delete")
async def delete_edge(edge_id: int, redirect_to: str = Form("/workflows")):
    db.delete_edge(edge_id)
    return RedirectResponse(url=redirect_to, status_code=303)


@router.post("/workflows/{workflow_id}/run")
async def run_workflow(workflow_id: int):
    if not db.get_workflow(workflow_id):
        raise HTTPException(status_code=404, detail="workflow non trovato")
    try:
        result = jobs.start_workflow(workflow_id)
    except ValueError as e:
        return RedirectResponse(
            url=f"/workflows/{workflow_id}?error={str(e).replace(' ', '+')}",
            status_code=303,
        )
    return RedirectResponse(
        url=f"/workflows/{workflow_id}?flash=Workflow+avviato%3A+run+%23{result['workflow_run_id']}",
        status_code=303,
    )
