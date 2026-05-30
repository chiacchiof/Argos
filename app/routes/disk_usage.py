"""Spazio occupato (architect): panoramica + pulizia delle folder dei run
generati da task e workflow su disco (RESULTS_DIR), piu' un pannello sulle
sessioni browser persistenti.

Sicurezza (vedi memoria "No shortcuts: sicurezza + performance"):
  - Router gated con `require_architect_or_admin` → operator bloccati.
  - Ogni endpoint che tocca una cartella task fa PRIMA `db.get_task(task_id)`:
    None cross-tenant → 404, quindi niente accesso a folder di altri tenant.
  - Le cancellazioni passano dagli helper `storage.*` che fanno resolve()+
    _is_inside (anti path-traversal) e accettano solo figli diretti.
  - Guard "job attivo": non si cancella un run di un task con job
    queued/running/paused (il runner ci sta ancora scrivendo).
  - Orfani e sessioni: SOLO super_admin (di un orfano non si puo' provare
    l'ownership tenant; le sessioni sono globali).
"""
from __future__ import annotations

import os
from urllib.parse import quote_plus

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from .. import db, storage
from ..auth import require_architect_or_admin
from ..config import RESULTS_DIR
from ..templates import templates


router = APIRouter(dependencies=[Depends(require_architect_or_admin)])

_ACTIVE_STATUSES = ("queued", "running", "paused")
_SORT_COLS = ("size", "name", "kind", "runs", "mtime")
_EXT_MAP = {"md": {".md"}, "json": {".json"}}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _current_user(request: Request):
    return getattr(request.state, "current_user", None)


def _is_super_admin(request: Request) -> bool:
    u = _current_user(request)
    return bool(u and getattr(u, "is_super_admin", False))


def _task_has_active_job(task_id: int) -> bool:
    """True se il task ha un job attivo. Query diretta per status invece di
    db.list_jobs (che ha LIMIT 100 e potrebbe mancare un job attivo vecchio se
    il task ha >100 job). Fail-closed: su errore DB ritorna True → blocca la
    cancellazione (meglio negare che cancellare sotto un runner vivo)."""
    try:
        with db.connect() as con:
            row = con.execute(
                "SELECT 1 FROM jobs WHERE task_id = %s "
                "AND status IN ('queued','running','paused') LIMIT 1",
                (task_id,),
            ).fetchone()
        return row is not None
    except Exception:
        return True


def _any_job_running() -> bool:
    """True se esiste QUALSIASI job attivo (globale). Usato come guard prima di
    cancellare una sessione browser, che un runner vivo potrebbe tenere aperta.
    Fail-closed: su errore DB ritorna True (assume un job in corso)."""
    try:
        with db.connect() as con:
            row = con.execute(
                "SELECT 1 FROM jobs WHERE status IN ('queued','running','paused') LIMIT 1"
            ).fetchone()
        return row is not None
    except Exception:
        return True


def _reconcile_result_paths(task_ids) -> None:
    """Dopo una cancellazione su disco, azzera `jobs.result_path` per i job dei
    task il cui file/dir puntato non esiste piu' → niente link di download rotti.
    Best-effort (LIMIT 100 di list_jobs copre il caso comune: run recenti)."""
    for tid in {int(t) for t in task_ids}:
        try:
            for j in db.list_jobs(tid):
                rp = j.get("result_path")
                if rp and not os.path.exists(rp):
                    db.update_job(int(j["id"]), result_path=None)
        except Exception:
            continue


def _task_rows(tasks: list[dict]) -> list[dict]:
    rows: list[dict] = []
    for t in tasks:
        tid = int(t["id"])
        s = storage.task_disk_summary(tid)
        if s["run_count"] == 0:
            continue
        rows.append({
            "kind": "task", "id": tid,
            "name": t.get("name") or f"task #{tid}",
            "subtitle": t.get("agent_mode") or "—",
            "size": s["size"], "runs": s["run_count"], "mtime": s["last_mtime"],
        })
    return rows


def _workflow_units(workflow_id: int) -> list[dict]:
    """Run unit (deduped) toccate dai job di questo workflow.
    Ogni elemento: {task_id, name, type, size, files_count, mtime}."""
    runs = db.list_workflow_runs(workflow_id, limit=1000)
    seen: set[tuple[int, str]] = set()
    units: list[dict] = []
    for r in runs:
        for j in db.list_jobs_for_workflow_run(int(r["id"])):
            tid = int(j["task_id"])
            run_name = storage.run_name_of(tid, j.get("result_path"))
            if not run_name:
                continue
            key = (tid, run_name)
            if key in seen:
                continue
            seen.add(key)
            info = storage.run_unit_info(tid, run_name)
            if not info:
                continue
            units.append({**info, "task_id": tid})
    units.sort(key=lambda u: u["mtime"], reverse=True)
    return units


def _workflow_rows(workflows: list[dict]) -> list[dict]:
    rows: list[dict] = []
    for w in workflows:
        wid = int(w["id"])
        units = _workflow_units(wid)
        if not units:
            continue
        rows.append({
            "kind": "workflow", "id": wid,
            "name": w.get("name") or f"workflow #{wid}",
            "subtitle": "workflow",
            "size": sum(u["size"] for u in units),
            "runs": len(units),
            "mtime": max((u["mtime"] for u in units), default=0.0),
        })
    return rows


def _orphan_rows() -> list[dict]:
    """SOLO super_admin: cartelle in RESULTS_DIR senza task corrispondente."""
    rows: list[dict] = []
    if not RESULTS_DIR.is_dir():
        return rows
    for d in RESULTS_DIR.iterdir():
        if not d.is_dir():
            continue
        try:
            tid = int(d.name)
        except ValueError:
            continue  # _watchdog.log, ecc.
        if db.get_task(tid) is not None:
            continue  # esiste ancora (di qualche tenant) → non orfano
        s = storage.task_disk_summary(tid)
        rows.append({
            "kind": "orphan", "id": tid,
            "name": f"task #{tid} (cancellato)",
            "subtitle": "orfano",
            "size": s["size"], "runs": s["run_count"], "mtime": s["last_mtime"],
        })
    return rows


def _task_runs_partial(request: Request, task: dict, info: str = "", error: str = ""):
    tid = int(task["id"])
    runs = storage.list_task_runs(tid)
    return templates.TemplateResponse(request, "partials/disk_task_runs.html", {
        "task": task, "runs": runs,
        "total": sum(r["size"] for r in runs),
        "active": _task_has_active_job(tid),
        "info": info, "error": error,
    })


def _run_files_partial(request: Request, task: dict, run_name: str,
                       info: str = "", error: str = ""):
    tid = int(task["id"])
    files = storage.list_run_files(tid, run_name) or []
    return templates.TemplateResponse(request, "partials/disk_run_files.html", {
        "task": task, "run_name": run_name, "files": files,
        "total": sum(f["size"] for f in files),
        "active": _task_has_active_job(tid),
        "info": info, "error": error,
    })


def _workflow_runs_partial(request: Request, wf: dict, info: str = "", error: str = ""):
    units = _workflow_units(int(wf["id"]))
    return templates.TemplateResponse(request, "partials/disk_workflow_runs.html", {
        "wf": wf, "units": units,
        "total": sum(u["size"] for u in units),
        "info": info, "error": error,
    })


# ---------------------------------------------------------------------------
# Pagina principale
# ---------------------------------------------------------------------------

@router.get("/spazio-occupato", response_class=HTMLResponse)
async def disk_usage_page(
    request: Request,
    q: str | None = None,
    sort: str | None = None,
    sort_dir: str | None = None,
):
    is_sa = _is_super_admin(request)
    rows = _task_rows(db.list_tasks()) + _workflow_rows(db.list_workflows())
    if is_sa:
        rows += _orphan_rows()

    search = (q or "").strip().lower()
    if search:
        rows = [
            r for r in rows
            if search in r["name"].lower() or search in (r["subtitle"] or "").lower()
        ]

    sort_v = (sort or "size").strip().lower()
    if sort_v not in _SORT_COLS:
        sort_v = "size"
    sort_dir_v = (sort_dir or "desc").strip().lower()
    if sort_dir_v not in ("asc", "desc"):
        sort_dir_v = "desc"
    keymap = {
        "size": lambda r: r["size"],
        "runs": lambda r: r["runs"],
        "mtime": lambda r: r["mtime"],
        "name": lambda r: r["name"].lower(),
        "kind": lambda r: r["kind"],
    }
    rows.sort(key=keymap[sort_v], reverse=(sort_dir_v == "desc"))

    # Totale "results": solo task+orfani (le righe workflow ri-vedono gli stessi
    # byte dei task → escluse per non gonfiare il totale).
    base_rows = [r for r in rows if r["kind"] in ("task", "orphan")]
    total_results = sum(r["size"] for r in base_rows)
    n_runs = sum(r["runs"] for r in base_rows)

    sessions = storage.session_breakdown() if is_sa else []
    sessions_total = sum(s["size"] for s in sessions)

    qs_base = f"q={quote_plus(search)}" if search else ""

    return templates.TemplateResponse(request, "spazio_occupato.html", {
        "rows": rows, "q": search, "sort": sort_v, "sort_dir": sort_dir_v,
        "qs_base": qs_base,
        "total_results": total_results, "n_runs": n_runs,
        "sessions": sessions, "sessions_total": sessions_total,
        "is_super_admin": is_sa,
        "msg": request.query_params.get("_msg") or "",
    })


# ---------------------------------------------------------------------------
# Modale: run di un task
# ---------------------------------------------------------------------------

@router.get("/spazio-occupato/task/{task_id}/runs", response_class=HTMLResponse)
async def disk_task_runs(request: Request, task_id: int):
    task = db.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="task non trovato")
    return _task_runs_partial(request, task)


@router.get(
    "/spazio-occupato/task/{task_id}/run/{run_name}/files",
    response_class=HTMLResponse,
)
async def disk_run_files(request: Request, task_id: int, run_name: str):
    task = db.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="task non trovato")
    if storage.list_run_files(task_id, run_name) is None:
        raise HTTPException(status_code=404, detail="run non trovata")
    return _run_files_partial(request, task, run_name)


@router.post("/spazio-occupato/task/{task_id}/runs/delete", response_class=HTMLResponse)
async def disk_task_delete_runs(request: Request, task_id: int):
    task = db.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="task non trovato")
    if _task_has_active_job(task_id):
        return _task_runs_partial(
            request, task,
            error="Ci sono job attivi su questo task: fermali prima di cancellare.",
        )
    form = await request.form()
    mode = (form.get("mode") or "selected").strip()
    if mode == "all":
        names = [r["name"] for r in storage.list_task_runs(task_id)]
    else:
        names = [v for v in form.getlist("run_names") if v]
    n = sum(1 for name in names if storage.delete_run_unit(task_id, name))
    _reconcile_result_paths([task_id])
    return _task_runs_partial(request, task, info=f"Cancellati {n} run.")


@router.post(
    "/spazio-occupato/task/{task_id}/runs/delete-ext",
    response_class=HTMLResponse,
)
async def disk_task_delete_ext(request: Request, task_id: int):
    task = db.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="task non trovato")
    form = await request.form()
    kind = (form.get("ext") or "").strip().lower()
    exts = _EXT_MAP.get(kind)
    if not exts:
        raise HTTPException(status_code=400, detail="estensione non valida")
    if _task_has_active_job(task_id):
        return _task_runs_partial(
            request, task, error="Job attivi: fermali prima di cancellare.",
        )
    names = [v for v in form.getlist("run_names") if v] or None
    res = storage.delete_task_files_by_ext(task_id, exts, names)
    _reconcile_result_paths([task_id])
    scope = "nei run selezionati" if names else "in tutti i run"
    return _task_runs_partial(
        request, task,
        info=f"Cancellati {res['deleted']} file {kind.upper()} {scope}.",
    )


@router.post(
    "/spazio-occupato/task/{task_id}/run/{run_name}/file/delete",
    response_class=HTMLResponse,
)
async def disk_run_file_delete(request: Request, task_id: int, run_name: str):
    task = db.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="task non trovato")
    form = await request.form()
    file_path = (form.get("file_path") or "").strip()
    if _task_has_active_job(task_id):
        return _run_files_partial(
            request, task, run_name, error="Job attivi: fermali prima.",
        )
    ok = storage.delete_run_file(task_id, run_name, file_path)
    _reconcile_result_paths([task_id])
    return _run_files_partial(
        request, task, run_name,
        info="File cancellato." if ok else "Impossibile cancellare il file.",
    )


# ---------------------------------------------------------------------------
# Modale: run di un workflow
# ---------------------------------------------------------------------------

@router.get(
    "/spazio-occupato/workflow/{workflow_id}/runs",
    response_class=HTMLResponse,
)
async def disk_workflow_runs(request: Request, workflow_id: int):
    wf = db.get_workflow(workflow_id)
    if not wf:
        raise HTTPException(status_code=404, detail="workflow non trovato")
    return _workflow_runs_partial(request, wf)


@router.post(
    "/spazio-occupato/workflow/{workflow_id}/runs/delete",
    response_class=HTMLResponse,
)
async def disk_workflow_delete_runs(request: Request, workflow_id: int):
    wf = db.get_workflow(workflow_id)
    if not wf:
        raise HTTPException(status_code=404, detail="workflow non trovato")
    units = _workflow_units(workflow_id)
    allowed = {(u["task_id"], u["name"]) for u in units}
    involved = {u["task_id"] for u in units}
    blocked = sorted(tid for tid in involved if _task_has_active_job(tid))
    if blocked:
        return _workflow_runs_partial(
            request, wf,
            error=f"Job attivi sui task {blocked}: fermali prima di cancellare.",
        )
    form = await request.form()
    mode = (form.get("mode") or "selected").strip()
    if mode == "all":
        pairs = list(allowed)
    else:
        pairs = []
        for v in form.getlist("units"):
            tid_s, _, rn = v.partition(":")
            if tid_s.isdigit() and rn and (int(tid_s), rn) in allowed:
                pairs.append((int(tid_s), rn))
    n = 0
    for tid, rn in pairs:
        # difesa: l'ownership tenant del task e' gia' garantita perche' le pair
        # provengono da `allowed` (job del workflow del tenant corrente), ma
        # ri-verifichiamo comunque.
        if db.get_task(tid) is None:
            continue
        if storage.delete_run_unit(tid, rn):
            n += 1
    _reconcile_result_paths(involved)
    return _workflow_runs_partial(request, wf, info=f"Cancellati {n} run.")


@router.post(
    "/spazio-occupato/workflow/{workflow_id}/runs/delete-ext",
    response_class=HTMLResponse,
)
async def disk_workflow_delete_ext(request: Request, workflow_id: int):
    wf = db.get_workflow(workflow_id)
    if not wf:
        raise HTTPException(status_code=404, detail="workflow non trovato")
    form = await request.form()
    kind = (form.get("ext") or "").strip().lower()
    exts = _EXT_MAP.get(kind)
    if not exts:
        raise HTTPException(status_code=400, detail="estensione non valida")
    units = _workflow_units(workflow_id)
    involved = {u["task_id"] for u in units}
    blocked = sorted(tid for tid in involved if _task_has_active_job(tid))
    if blocked:
        return _workflow_runs_partial(
            request, wf, error=f"Job attivi sui task {blocked}: fermali prima.",
        )
    by_task: dict[int, list[str]] = {}
    for u in units:
        by_task.setdefault(u["task_id"], []).append(u["name"])
    total = 0
    for tid, names in by_task.items():
        if db.get_task(tid) is None:
            continue
        total += storage.delete_task_files_by_ext(tid, exts, names)["deleted"]
    _reconcile_result_paths(by_task.keys())
    return _workflow_runs_partial(
        request, wf, info=f"Cancellati {total} file {kind.upper()}.",
    )


# ---------------------------------------------------------------------------
# Orfani (super_admin)
# ---------------------------------------------------------------------------

@router.post("/spazio-occupato/orphans/delete")
async def disk_delete_orphan(request: Request, task_id: int = Form(...)):
    if not _is_super_admin(request):
        raise HTTPException(status_code=403, detail="solo super-admin")
    if db.get_task(task_id) is not None:
        raise HTTPException(status_code=400, detail="il task esiste ancora: non e' un orfano")
    ok = storage.delete_task_folder(task_id)
    msg = (
        f"Cancellata+cartella+orfana+task+%23{task_id}"
        if ok else f"Cancellazione+fallita+per+task+%23{task_id}"
    )
    return RedirectResponse(url=f"/spazio-occupato?_msg={msg}", status_code=303)


# ---------------------------------------------------------------------------
# Sessioni browser (super_admin, delete protetto)
# ---------------------------------------------------------------------------

@router.post("/spazio-occupato/sessions/delete")
async def disk_delete_session(
    request: Request,
    root_key: str = Form(...),
    name: str = Form(...),
    confirm: str = Form(""),
):
    if not _is_super_admin(request):
        raise HTTPException(status_code=403, detail="solo super-admin")
    if confirm.strip().upper() != "ELIMINA":
        raise HTTPException(status_code=400, detail="conferma 'ELIMINA' richiesta")
    if _any_job_running():
        return RedirectResponse(
            url="/spazio-occupato?_msg=Ci+sono+job+in+esecuzione:+ferma+i+job+prima+di+cancellare+una+sessione",
            status_code=303,
        )
    ok = storage.delete_session_entry(root_key, name)
    msg = "Sessione+cancellata" if ok else "Cancellazione+sessione+fallita"
    return RedirectResponse(url=f"/spazio-occupato?_msg={msg}", status_code=303)
