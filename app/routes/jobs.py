from __future__ import annotations

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

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
    """Status + log di un job.

    Doppia modalita' di rendering:
      - HTMX swap (header `HX-Request: true`): ritorna il partial spoglio,
        usato per polling e swap inline nel dashboard.
      - Full-page (link aperto in nuova tab dalla cronologia): ritorna la
        pagina completa che estende `base.html` (header, nav, CSS).

    Calcola anche il `display_id` `#parent.N` se il job e' un sub-job
    (ha `triggered_by_job_id` valorizzato), replicando la stessa logica di
    `_build_jobs_tree` (ordering by id ASC). Cosi' l'utente che apre il log
    di un sub vede sia il DB id (#219) sia il display id contestualizzato
    (#218.2) e ha un link "torna al parent" — niente piu' mismatch UX
    (incident 2026-05-23: l'utente non riusciva a collegare la pagina
    Job #219 al sub #218.2 della cronologia).
    """
    job = db.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="job non trovato")
    task = db.get_task(job["task_id"])

    display_id: str | None = None
    parent_id: int | None = None
    raw_parent = job.get("triggered_by_job_id")
    if raw_parent:
        try:
            parent_id = int(raw_parent)
        except (TypeError, ValueError):
            parent_id = None
        if parent_id:
            try:
                siblings = db.list_subjobs(parent_id)
            except Exception:
                siblings = []
            siblings_sorted = sorted(siblings, key=lambda s: int(s["id"]))
            for idx, s in enumerate(siblings_sorted, start=1):
                if int(s["id"]) == int(job["id"]):
                    display_id = f"#{parent_id}.{idx}"
                    break

    is_htmx = request.headers.get("HX-Request") == "true"
    template_name = (
        "partials/job_status.html" if is_htmx else "job_status_page.html"
    )
    return templates.TemplateResponse(
        request, template_name,
        {
            "job": job, "task": task,
            "display_id": display_id, "parent_id": parent_id,
        },
    )


@router.post("/jobs/{job_id}/regenerate-summary", response_class=HTMLResponse)
async def regenerate_master_summary_endpoint(request: Request, job_id: int):
    """Forza la rigenerazione del file `master_summary.md` per un parent job.

    Utile quando il prompt del summary e' stato aggiornato (es. nuovi pattern
    P9, regole anti-allucinazione) e il file salvato sul filesystem contiene
    diagnosi obsolete. Sovrascrive il file esistente.

    Ritorna un partial HTML con il messaggio risultato (usabile da HTMX) o
    redirect 303 al task detail se chiamato da un form normale.
    """
    job = db.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="job non trovato")
    from ..agent.master_summary import generate_master_summary
    new_path = await generate_master_summary(job_id)
    task_id = int(job["task_id"])
    if new_path:
        msg = f"Master+summary+rigenerato+per+job+%23{job_id}"
    else:
        msg = (
            f"Impossibile+rigenerare+summary+per+job+%23{job_id}+"
            "(parent+senza+sub-job%2C+run_dir+mancante%2C+o+errore+LLM)"
        )
    return RedirectResponse(
        url=f"/tasks/{task_id}?flash={msg}", status_code=303,
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
async def job_control(
    request: Request,
    job_id: int,
    signal: str = Form(""),
    complete_downstream: str = Form(""),
):
    """Manda un segnale al runner: pause | resume | stop | stop_complete.

    - `signal=stop`: stop immediato (hard kill). Status -> cancelled.
      Downstream non parte (comportamento storico).
    - `signal=stop_complete`: stop graceful + flag che dice "tratta come done"
      a fine. Il runner finalizza (save queue, ingest in DB), poi al cleanup
      il workflow downstream parte. Fix N3.
    - `signal=pause`: solo per agent_mode che lo supportano (vedi
      MODES_SUPPORTING_PAUSE in runner_control).
    - `signal=resume`: clear pause.
    """
    job = db.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="job non trovato")
    sig = signal.strip().lower()
    # Legacy compat: alcuni client passano `complete_downstream=1` con signal=stop
    if sig == "stop" and complete_downstream.strip() in ("1", "true", "on", "yes"):
        sig = "stop_complete"
    if sig not in {"pause", "resume", "stop", "stop_complete"}:
        raise HTTPException(status_code=400, detail="signal non valido")

    if sig == "resume":
        db.set_control_signal(job_id, None)
        if (job.get("status") or "") == "paused":
            db.update_job(job_id, status="running")
            db.append_job_log(job_id, "Richiesta RESUME dall'utente.")
    elif sig == "stop":
        db.set_control_signal(job_id, "stop")
        cancelled = jobs.hard_stop_job(job_id)
        if cancelled:
            db.append_job_log(
                job_id,
                "Richiesta STOP dall'utente — task cancellato (hard stop). Downstream NON partira'.",
            )
        else:
            db.append_job_log(
                job_id,
                "Richiesta STOP dall'utente — runner non più attivo, chiudo direttamente.",
            )
            cur = db.get_job(job_id)
            if cur and (cur.get("status") or "") in ("queued", "running", "paused"):
                db.update_job(job_id, status="cancelled", finished_at=db.now_iso())
                db.set_control_signal(job_id, None)
    elif sig == "stop_complete":
        # Stop graceful + downstream trigger. Fix N3 (incidente 2026-05-12).
        # NO hard_stop_job: lascia che il runner finisca naturalmente il loop
        # corrente (1-2 step LLM) e poi si fermi via control_signal=stop.
        # Cosi' l'ingest_to_assets gira nel finally.
        db.set_control_signal(job_id, "stop")
        jobs.mark_complete_downstream_on_cancel(job_id)
        db.append_job_log(
            job_id,
            "Richiesta STOP+COMPLETE dall'utente — fermo graceful, "
            "ingest in DB dei profili gia' estratti, lancio downstream (qualifier/outreach).",
        )
    elif sig == "pause":
        # Verifica che l'agent_mode supporti la pausa, altrimenti notifica.
        from ..agent.runner_control import MODES_SUPPORTING_PAUSE
        task = db.get_task(job["task_id"])
        mode = (task or {}).get("agent_mode", "")
        if mode not in MODES_SUPPORTING_PAUSE:
            db.append_job_log(
                job_id,
                f"⚠️ PAUSE richiesto ma agent_mode='{mode}' non lo supporta. "
                f"Modi supportati: {sorted(MODES_SUPPORTING_PAUSE)}. Ignoro.",
            )
            raise HTTPException(
                status_code=400,
                detail=(
                    f"La modalita' '{mode}' non supporta la pausa. "
                    f"Modalita' supportate: {', '.join(sorted(MODES_SUPPORTING_PAUSE))}. "
                    "Usa Stop se vuoi interrompere."
                ),
            )
        db.set_control_signal(job_id, "pause")
        db.append_job_log(
            job_id, "Richiesta PAUSE dall'utente — verrà applicata al prossimo seed."
        )

    data = compute_dashboard(job_id)
    return templates.TemplateResponse(
        request, "partials/job_dashboard.html", {"d": data}
    )
