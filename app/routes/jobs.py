from __future__ import annotations

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse

from .. import db, jobs
from ..dashboard import compute_dashboard
from ..templates import templates

router = APIRouter()


@router.post("/tasks/{task_id}/run", response_class=HTMLResponse)
async def run_task(request: Request, task_id: int):
    task = db.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="task non trovato")
    job_id = jobs.start_job(task_id)
    data = compute_dashboard(job_id)
    return templates.TemplateResponse(
        request, "partials/job_dashboard.html", {"d": data}
    )


@router.get("/jobs/{job_id}/status", response_class=HTMLResponse)
async def job_status(request: Request, job_id: int):
    job = db.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="job non trovato")
    task = db.get_task(job["task_id"])
    return templates.TemplateResponse(
        request, "partials/job_status.html", {"job": job, "task": task}
    )


@router.get("/jobs/{job_id}/dashboard", response_class=HTMLResponse)
async def job_dashboard(request: Request, job_id: int):
    data = compute_dashboard(job_id)
    if not data:
        raise HTTPException(status_code=404, detail="job non trovato")
    return templates.TemplateResponse(
        request, "partials/job_dashboard.html", {"d": data}
    )


@router.post("/jobs/{job_id}/control", response_class=HTMLResponse)
async def job_control(request: Request, job_id: int, signal: str = Form("")):
    """Manda un segnale al runner: pause | resume | stop."""
    job = db.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="job non trovato")
    sig = signal.strip().lower()
    if sig not in {"pause", "resume", "stop"}:
        raise HTTPException(status_code=400, detail="signal non valido")

    # 'resume' = clear control_signal (il runner uscirà dal wait loop)
    if sig == "resume":
        db.set_control_signal(job_id, None)
        if (job.get("status") or "") == "paused":
            db.update_job(job_id, status="running")
            db.append_job_log(job_id, "Richiesta RESUME dall'utente.")
    elif sig == "stop":
        # Hard stop: cancella il task asyncio (interrompe browser-use ovunque sia)
        # E imposta anche control_signal per coprire il caso "tra una seed e l'altra".
        db.set_control_signal(job_id, "stop")
        cancelled = jobs.hard_stop_job(job_id)
        if cancelled:
            db.append_job_log(
                job_id,
                "Richiesta STOP dall'utente — task cancellato (hard stop).",
            )
        else:
            # Runner non più in memoria: chiudi subito il job
            db.append_job_log(
                job_id,
                "Richiesta STOP dall'utente — runner non più attivo, chiudo direttamente.",
            )
            cur = db.get_job(job_id)
            if cur and (cur.get("status") or "") in ("queued", "running", "paused"):
                db.update_job(job_id, status="cancelled", finished_at=db.now_iso())
                db.set_control_signal(job_id, None)
    elif sig == "pause":
        db.set_control_signal(job_id, "pause")
        db.append_job_log(
            job_id, "Richiesta PAUSE dall'utente — verrà applicata al prossimo seed."
        )

    data = compute_dashboard(job_id)
    return templates.TemplateResponse(
        request, "partials/job_dashboard.html", {"d": data}
    )
