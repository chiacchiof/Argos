"""Route per i Workflow (entità di prima classe).

Un workflow contiene un DAG di task con edges tra loro. Si può creare/editare
e eseguire come blocco unico (▶ Esegui workflow → lancia i task root).
"""
from __future__ import annotations

from urllib.parse import quote_plus

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import ValidationError

from .. import db, jobs, storage
from ..models import WorkflowIn
from ..templates import templates
from . import _tenant_filter as _tf

router = APIRouter()


def _workflow_detail_url(workflow_id: int, *, flash: str | None = None, error: str | None = None) -> str:
    if error:
        return f"/workflows/{workflow_id}?error={quote_plus(error)}"
    if flash:
        return f"/workflows/{workflow_id}?flash={quote_plus(flash)}"
    return f"/workflows/{workflow_id}"


@router.get("/workflows", response_class=HTMLResponse)
async def workflows_list(
    request: Request,
    author: str = "",
    q: str = "",
    _partial: str = "",
):
    """Lista workflow. `author`:
       - `mine` (default per tenant_user): solo workflow creati dall'utente
       - `tenant` (default per super_admin): tutti i workflow visibili
    `q`: filtra per match testuale su name + description (case-insensitive).
    `_partial=1`: ritorna solo il wrapper tabella (usato dal polling HTMX).
    """
    current_user = getattr(request.state, "current_user", None)
    is_super_admin = bool(current_user and current_user.is_super_admin)
    current_uid = db.current_user_id()
    default_author = "tenant" if is_super_admin else "mine"
    author_norm = (author or default_author).strip().lower()
    if author_norm not in ("mine", "tenant", "all"):
        author_norm = default_author
    filter_uid = current_uid if (author_norm == "mine" and current_uid is not None) else None
    tenant_arg = _tf.tenant_query_arg(request)
    workflows = db.list_workflows(tenant_id=tenant_arg, created_by_user_id=filter_uid)
    total_tenant = len(db.list_workflows(tenant_id=tenant_arg)) if author_norm == "mine" else len(workflows)
    q_norm = (q or "").strip().lower()
    if q_norm:
        workflows = [
            w for w in workflows
            if q_norm in (str(w.get("name") or "") + " " + str(w.get("description") or "")).lower()
        ]
    # arricchisci con count di task / edges per workflow.
    # Batch: 1 query per TUTTI gli edge dei workflow filtrati (vs 1 query per
    # workflow, lento su DB remoto).
    workflow_ids = [w["id"] for w in workflows]
    edges_by_wf = db.list_edges_by_workflow_ids(workflow_ids)
    # Batch query: workflow_run attivi (queued/running) + N job attivi per ognuno.
    active_by_wf = db.list_active_workflow_runs_for_workflows(workflow_ids)
    enriched = []
    for w in workflows:
        edges = edges_by_wf.get(int(w["id"]), [])
        task_ids: set[int] = set()
        for e in edges:
            task_ids.add(e["from_task_id"])
            task_ids.add(e["to_task_id"])
        enriched.append({**w, "n_edges": len(edges), "n_tasks": len(task_ids)})
    has_active = bool(active_by_wf)
    ctx = {
        "workflows": enriched,
        "active_by_wf": active_by_wf,
        "has_active": has_active,
        "author_filter": author_norm,
        "total_tenant": total_tenant,
        "current_user_authenticated": current_uid is not None,
        "filter_q": q_norm,
        **_tf.picker_context(request),
    }
    if _partial:
        return templates.TemplateResponse(
            request, "partials/workflows_list_table.html", ctx,
        )
    return templates.TemplateResponse(request, "workflows_list.html", ctx)


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


@router.post("/workflows/{workflow_id}/toggle-disabled")
async def toggle_workflow_disabled(workflow_id: int, redirect_to: str = Form("/workflows")):
    """Toggle del flag `disabled` di un workflow."""
    w = db.get_workflow(workflow_id)
    if w is None:
        return RedirectResponse(url=redirect_to or "/workflows", status_code=303)
    new_val = not bool(w.get("disabled"))
    db.set_workflow_disabled(workflow_id, new_val)
    return RedirectResponse(url=redirect_to or "/workflows", status_code=303)


@router.get("/workflows/{workflow_id}", response_class=HTMLResponse)
async def workflow_detail(request: Request, workflow_id: int):
    w = db.get_workflow(workflow_id)
    if not w:
        raise HTTPException(status_code=404, detail="workflow non trovato")
    edges = db.list_edges(workflow_id=workflow_id)
    all_tasks = db.list_tasks()
    tmap = {int(t["id"]): t for t in all_tasks}
    edges_enriched = []
    for e in edges:
        from_id = int(e["from_task_id"])
        to_id = int(e["to_task_id"])
        edges_enriched.append({
            **e,
            "from_name": (tmap.get(from_id) or {}).get("name", f"#{from_id}"),
            "to_name": (tmap.get(to_id) or {}).get("name", f"#{to_id}"),
        })
    roots = db.find_workflow_roots(workflow_id)
    workflow_graph = _workflow_graph_view(edges_enriched, tmap)
    live = _workflow_live_view(workflow_id, edges)
    return templates.TemplateResponse(
        request,
        "workflow_detail.html",
        {
            "workflow": w,
            "edges": edges_enriched,
            "tasks": all_tasks,
            "roots": roots,
            "workflow_graph": workflow_graph,
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


@router.get(
    "/workflows/{workflow_id}/runs/{run_id}/jobs",
    response_class=HTMLResponse,
)
async def workflow_run_jobs_partial(
    request: Request, workflow_id: int, run_id: int,
):
    """Restituisce un partial con la lista dei job appartenenti a un singolo
    workflow_run: task name, status, link al result_path, link al log.
    Usato in HTMX expand dalla riga "Esecuzioni recenti" del detail.
    Tenant-safe via `get_workflow` (404 cross-tenant) + `list_jobs_for_workflow_run`.
    """
    w = db.get_workflow(workflow_id)
    if not w:
        raise HTTPException(status_code=404, detail="workflow non trovato")
    jobs_in_run = db.list_jobs_for_workflow_run(run_id)
    # Recupero il nome del task per ogni job in un colpo solo (vs N query).
    task_ids = sorted({int(j["task_id"]) for j in jobs_in_run if j.get("task_id")})
    tasks_by_id = db.get_tasks_by_ids(task_ids) if task_ids else {}
    enriched: list[dict] = []
    for j in jobs_in_run:
        tid = int(j["task_id"]) if j.get("task_id") else None
        t = tasks_by_id.get(tid) if tid is not None else None
        rp = j.get("result_path") or ""
        rel_path = None
        if rp and tid is not None:
            norm = rp.replace("\\", "/")
            marker = f"/results/{tid}/"
            if marker in norm:
                rel_path = norm.split(marker)[-1]
        enriched.append({
            "job_id": int(j["id"]),
            "task_id": tid,
            "task_name": (t or {}).get("name") or "(task eliminato)",
            "status": j.get("status"),
            "started_at": j.get("started_at"),
            "finished_at": j.get("finished_at"),
            "result_rel_path": rel_path,
            "error": j.get("error"),
        })
    return templates.TemplateResponse(
        request,
        "partials/workflow_run_jobs.html",
        {"workflow": w, "run_id": run_id, "jobs": enriched},
    )


@router.post("/workflows/{workflow_id}/runs/delete")
async def delete_workflow_runs_action(request: Request, workflow_id: int):
    """Hard delete batch dei workflow_runs selezionati + cascade sui job
    associati (record DB + cartelle FS).

    Workflow:
      1. Valida che il workflow esista (tenant-safe via get_workflow).
      2. Legge `run_ids` dal form bulk.
      3. `db.delete_workflow_runs` espande con i jobs, rifiuta se trova run o
         job attivi (queued/running/paused).
      4. Per ogni job cancellato, pulisce il FS via storage.delete_job_artifact.
      5. Redirect 303 al workflow detail con flash.
    """
    w = db.get_workflow(workflow_id)
    if not w:
        raise HTTPException(status_code=404, detail="workflow non trovato")
    form = await request.form()
    raw_ids = form.getlist("run_ids")
    run_ids = [int(x) for x in raw_ids if str(x).strip().lstrip("-").isdigit()]
    if not run_ids:
        return RedirectResponse(
            url=f"/workflows/{workflow_id}?flash=Nessun+run+selezionato",
            status_code=303,
        )

    result = db.delete_workflow_runs(run_ids)
    if result["active_blockers"]:
        blockers = ",".join(
            f"%23{b['id']}({b['status']},{b['kind']})"
            for b in result["active_blockers"]
        )
        return RedirectResponse(
            url=(
                f"/workflows/{workflow_id}?flash="
                f"Ferma+prima+i+run/job+attivi:+{blockers}"
            ),
            status_code=303,
        )

    n_runs = len(result["deleted_runs"])
    n_jobs = len(result["deleted_jobs"])
    n_fs = 0
    for j in result["deleted_jobs"]:
        rp = j.get("result_path")
        if not rp:
            continue
        try:
            if storage.delete_job_artifact(int(j["task_id"]), rp):
                n_fs += 1
        except Exception:
            continue

    return RedirectResponse(
        url=(
            f"/workflows/{workflow_id}?flash="
            f"Cancellati+{n_runs}+run+%28{n_jobs}+job%2C+{n_fs}+cartelle+FS%29"
        ),
        status_code=303,
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
        tasks_by_id = db.get_tasks_by_ids(sorted(task_ids))
        for tid in sorted(task_ids):
            t = tasks_by_id.get(tid)
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


def _workflow_graph_view(edges: list[dict], tasks_by_id: dict[int, dict]) -> dict:
    """Build a compact left-to-right DAG layout for the workflow detail page."""
    node_ids: set[int] = set()
    edge_pairs: list[tuple[int, int]] = []
    outgoing_counts: dict[int, int] = {}
    incoming_counts: dict[int, int] = {}
    default_artifacts: dict[int, str] = {}
    for e in edges:
        try:
            from_id = int(e["from_task_id"])
            to_id = int(e["to_task_id"])
        except (TypeError, ValueError, KeyError):
            continue
        node_ids.add(from_id)
        node_ids.add(to_id)
        outgoing_counts[from_id] = outgoing_counts.get(from_id, 0) + 1
        incoming_counts[to_id] = incoming_counts.get(to_id, 0) + 1
        if e.get("pass_artifact") and not default_artifacts.get(from_id):
            default_artifacts[from_id] = str(e["pass_artifact"])
        if from_id != to_id:
            edge_pairs.append((from_id, to_id))

    if not node_ids:
        return {
            "nodes": [],
            "edges": [],
            "width": 0,
            "height": 0,
        }

    ranks = _workflow_graph_ranks(node_ids, edge_pairs)
    if ranks is None:
        enabled_pairs = [
            (int(e["from_task_id"]), int(e["to_task_id"]))
            for e in edges
            if e.get("enabled") and e.get("from_task_id") != e.get("to_task_id")
        ]
        ranks = _workflow_graph_ranks(node_ids, enabled_pairs)
    if ranks is None:
        ranks = {node_id: i for i, node_id in enumerate(sorted(node_ids))}

    rank_values = sorted(set(ranks.values()))
    compact_rank = {rank: i for i, rank in enumerate(rank_values)}
    columns: dict[int, list[int]] = {i: [] for i in range(len(rank_values))}
    for node_id in sorted(
        node_ids,
        key=lambda tid: (
            compact_rank.get(ranks.get(tid, 0), 0),
            str((tasks_by_id.get(tid) or {}).get("name") or "").lower(),
            tid,
        ),
    ):
        columns.setdefault(compact_rank.get(ranks.get(node_id, 0), 0), []).append(node_id)

    node_w = 176
    node_h = 84
    gap_x = 88
    gap_y = 24
    pad = 18
    n_cols = max(len(columns), 1)
    max_rows = max((len(v) for v in columns.values()), default=1)
    inner_h = max_rows * node_h + max(max_rows - 1, 0) * gap_y
    width = (n_cols * node_w) + max(n_cols - 1, 0) * gap_x + pad * 2
    height = inner_h + pad * 2

    positioned: dict[int, dict] = {}
    nodes: list[dict] = []
    for col_idx in sorted(columns):
        ids = columns[col_idx]
        col_h = len(ids) * node_h + max(len(ids) - 1, 0) * gap_y
        y0 = pad + max((inner_h - col_h) / 2, 0)
        x = pad + col_idx * (node_w + gap_x)
        for row_idx, node_id in enumerate(ids):
            task = tasks_by_id.get(node_id) or {}
            top = int(round(y0 + row_idx * (node_h + gap_y)))
            left = int(round(x))
            meta = _workflow_node_meta(task)
            node = {
                "id": node_id,
                "name": task.get("name") or f"Task #{node_id}",
                "mode": task.get("agent_mode") or "?",
                "left": left,
                "top": top,
                "width": node_w,
                "height": node_h,
                "cx": left + node_w / 2,
                "cy": top + node_h / 2,
                "rank": col_idx,
                "outgoing_count": outgoing_counts.get(node_id, 0),
                "incoming_count": incoming_counts.get(node_id, 0),
                "default_artifact": default_artifacts.get(node_id, ""),
                **meta,
            }
            positioned[node_id] = node
            nodes.append(node)

    drawn_edges: list[dict] = []
    for e in edges:
        try:
            from_id = int(e["from_task_id"])
            to_id = int(e["to_task_id"])
        except (TypeError, ValueError, KeyError):
            continue
        from_node = positioned.get(from_id)
        to_node = positioned.get(to_id)
        if not from_node or not to_node:
            continue

        same_rank = from_node["rank"] == to_node["rank"]
        if same_rank:
            source_above = from_node["top"] <= to_node["top"]
            x1 = from_node["left"] + node_w / 2
            y1 = from_node["top"] + (node_h if source_above else 0)
            x2 = to_node["left"] + node_w / 2
            y2 = to_node["top"] + (0 if source_above else node_h)
            mid_y = (y1 + y2) / 2
            path = f"M {x1:.1f} {y1:.1f} C {x1:.1f} {mid_y:.1f}, {x2:.1f} {mid_y:.1f}, {x2:.1f} {y2:.1f}"
        else:
            forward = from_node["left"] < to_node["left"]
            if forward:
                x1 = from_node["left"] + node_w
                x2 = to_node["left"]
            else:
                x1 = from_node["left"]
                x2 = to_node["left"] + node_w
            y1 = from_node["top"] + node_h / 2
            y2 = to_node["top"] + node_h / 2
            dx = max(abs(x2 - x1) / 2, 42)
            c1x = x1 + dx if forward else x1 - dx
            c2x = x2 - dx if forward else x2 + dx
            path = f"M {x1:.1f} {y1:.1f} C {c1x:.1f} {y1:.1f}, {c2x:.1f} {y2:.1f}, {x2:.1f} {y2:.1f}"

        drawn_edges.append({
            "id": e.get("id"),
            "path": path,
            "enabled": bool(e.get("enabled")),
            "artifact": e.get("pass_artifact") or "",
            "from_name": e.get("from_name") or f"#{from_id}",
            "to_name": e.get("to_name") or f"#{to_id}",
        })

    return {
        "nodes": nodes,
        "edges": drawn_edges,
        "width": int(width),
        "height": int(height),
    }


def _workflow_graph_ranks(
    node_ids: set[int],
    edge_pairs: list[tuple[int, int]],
) -> dict[int, int] | None:
    adjacency: dict[int, list[int]] = {node_id: [] for node_id in node_ids}
    indegree: dict[int, int] = {node_id: 0 for node_id in node_ids}
    for from_id, to_id in edge_pairs:
        if from_id not in node_ids or to_id not in node_ids:
            continue
        adjacency[from_id].append(to_id)
        indegree[to_id] += 1

    ranks = {node_id: 0 for node_id in node_ids}
    ready = sorted(node_id for node_id, degree in indegree.items() if degree == 0)
    visited: list[int] = []
    while ready:
        node_id = ready.pop(0)
        visited.append(node_id)
        for downstream_id in sorted(adjacency[node_id]):
            ranks[downstream_id] = max(ranks[downstream_id], ranks[node_id] + 1)
            indegree[downstream_id] -= 1
            if indegree[downstream_id] == 0:
                ready.append(downstream_id)
                ready.sort()

    if len(visited) != len(node_ids):
        return None
    return ranks


def _workflow_node_meta(task: dict) -> dict:
    mode = task.get("agent_mode") or "react"
    scraper_icons = {
        "auto_extract": "i-cpu",
        "bulk_extract": "i-assets",
        "browser_use": "i-eye",
        "site_explorer": "i-search",
        "recon_social": "i-target",
    }
    if mode in scraper_icons:
        return {"kind": "scraper", "icon": scraper_icons[mode]}
    if mode == "qualifier":
        return {"kind": "qualifier", "icon": "i-qualified"}
    if mode == "outreach":
        return {"kind": "outreach", "icon": "i-mail"}
    if mode == "outreach_social":
        return {"kind": "outreach", "icon": "i-send"}
    if mode == "outreach_whatsapp":
        return {"kind": "outreach", "icon": "i-message"}
    if mode == "responder":
        return {"kind": "responder", "icon": "i-inbox"}
    return {"kind": "agent", "icon": "i-cpu"}


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
    # Lookup tenant-filtered: ritorna None se edge appartiene ad altro tenant.
    cur = db.get_edge(edge_id)
    if not cur:
        raise HTTPException(status_code=404, detail="edge non trovato")
    db.toggle_edge(edge_id, not bool(cur.get("enabled")))
    return RedirectResponse(url=redirect_to, status_code=303)


@router.post("/workflow_edges/{edge_id}/delete")
async def delete_edge(edge_id: int, redirect_to: str = Form("/workflows")):
    # Lookup tenant-filtered: 404 se edge appartiene ad altro tenant.
    if not db.get_edge(edge_id):
        raise HTTPException(status_code=404, detail="edge non trovato")
    db.delete_edge(edge_id)
    return RedirectResponse(url=redirect_to, status_code=303)


@router.post("/workflow_edges/{edge_id}/artifact")
async def update_edge_artifact(
    edge_id: int,
    pass_artifact: str = Form(""),
    redirect_to: str = Form("/workflows"),
):
    # Lookup tenant-filtered: 404 se edge appartiene ad altro tenant.
    if not db.get_edge(edge_id):
        raise HTTPException(status_code=404, detail="edge non trovato")
    db.update_edge_artifact(edge_id, pass_artifact)
    return RedirectResponse(url=redirect_to, status_code=303)


@router.post("/workflows/{workflow_id}/nodes/{task_id}/replace")
async def replace_workflow_node_task(
    workflow_id: int,
    task_id: int,
    new_task_id: str = Form(""),
):
    if not db.get_workflow(workflow_id):
        raise HTTPException(status_code=404, detail="workflow non trovato")
    if not new_task_id.strip():
        return RedirectResponse(
            url=_workflow_detail_url(workflow_id, error="Seleziona un task sostitutivo"),
            status_code=303,
        )
    try:
        new_id = int(new_task_id)
    except ValueError:
        return RedirectResponse(
            url=_workflow_detail_url(workflow_id, error="ID task sostitutivo non valido"),
            status_code=303,
        )
    if new_id == task_id:
        return RedirectResponse(
            url=_workflow_detail_url(workflow_id, flash="Nodo invariato"),
            status_code=303,
        )
    if not db.get_task(new_id):
        return RedirectResponse(
            url=_workflow_detail_url(workflow_id, error=f"Task #{new_id} non esiste"),
            status_code=303,
        )
    try:
        changed = db.replace_workflow_node_task(workflow_id, task_id, new_id)
    except ValueError as e:
        return RedirectResponse(
            url=_workflow_detail_url(workflow_id, error=str(e)),
            status_code=303,
        )
    if changed <= 0:
        return RedirectResponse(
            url=_workflow_detail_url(workflow_id, error=f"Nodo task #{task_id} non trovato nel workflow"),
            status_code=303,
        )
    return RedirectResponse(
        url=_workflow_detail_url(workflow_id, flash=f"Nodo sostituito: #{task_id} -> #{new_id}"),
        status_code=303,
    )


@router.post("/workflows/{workflow_id}/nodes/{task_id}/delete")
async def delete_workflow_node(workflow_id: int, task_id: int):
    if not db.get_workflow(workflow_id):
        raise HTTPException(status_code=404, detail="workflow non trovato")
    deleted_edges = db.delete_workflow_node(workflow_id, task_id)
    if deleted_edges <= 0:
        return RedirectResponse(
            url=_workflow_detail_url(workflow_id, error=f"Nodo task #{task_id} non trovato nel workflow"),
            status_code=303,
        )
    return RedirectResponse(
        url=_workflow_detail_url(
            workflow_id,
            flash=f"Nodo #{task_id} rimosso dal workflow ({deleted_edges} edge eliminati)",
        ),
        status_code=303,
    )


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
