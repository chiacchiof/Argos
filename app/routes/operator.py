"""Routes UI Operator: dashboard semplificata per utenti non tecnici.

Ruolo target: `tenant_user` (operator). Vede solo agenti pubblicati dagli
architect, li lancia con parametri minimi, chatta con l'orchestrator in drawer.

Endpoint:
- GET  /home                          dashboard principale
- GET  /home?_partial=active          frammento "agenti attivi" (polling HTMX)
- GET  /agents/{kind}/{id}/launch     modal launch form
- POST /agents/{kind}/{id}/run        avvia agente (crea job)
- GET  /messages                      inbox semplificata (riusa list_threads)

Gating: tutti router-level `require_operator` → 403 per super_admin/architect
(che usano la UI completa).
"""
from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse

from .. import db, jobs
from ..auth import require_operator
from ..templates import templates


log = logging.getLogger(__name__)
router = APIRouter(dependencies=[Depends(require_operator)])


# ===========================================================================
# Dashboard /home
# ===========================================================================

# Mapping categoria -> label parlante (italiano). L'architect digita la chiave
# (es. "lead-discovery"), qui la rendiamo "Ricerca contatti".
CATEGORY_LABELS: dict[str, str] = {
    "lead-discovery": "Ricerca contatti",
    "discovery": "Ricerca contatti",
    "outreach": "Contatto e messaggi",
    "enrichment": "Arricchimento dati",
    "analysis": "Analisi e qualifica",
    "qualifier": "Analisi e qualifica",
    "responder": "Risposte automatiche",
    "altri": "Altri",
}

CATEGORY_ORDER = (
    "lead-discovery",
    "discovery",
    "outreach",
    "enrichment",
    "analysis",
    "qualifier",
    "responder",
    "altri",
)


def _category_label(cat: str) -> str:
    return CATEGORY_LABELS.get((cat or "altri").lower(), (cat or "altri").title())


def _group_agents_by_category(agents: list[dict]) -> list[dict]:
    """Raggruppa per categoria mantenendo ordine CATEGORY_ORDER + alfabetico
    per categorie sconosciute."""
    by_cat: dict[str, list[dict]] = {}
    for a in agents:
        by_cat.setdefault(a.get("category") or "altri", []).append(a)
    ordered_keys: list[str] = []
    for k in CATEGORY_ORDER:
        if k in by_cat:
            ordered_keys.append(k)
    for k in sorted(by_cat.keys()):
        if k not in ordered_keys:
            ordered_keys.append(k)
    return [
        {"key": k, "label": _category_label(k), "agents": by_cat[k]}
        for k in ordered_keys
    ]


def _active_agents(agents: list[dict]) -> list[dict]:
    """Filtra solo gli agenti con job attivi (running/queued)."""
    return [a for a in agents if int(a.get("active_jobs") or 0) > 0]


def _build_agenda_data(tenant_id: int | None = None) -> list[dict]:
    """Lista degli agenti pubblicati con `cron` valorizzato + prossima esecuzione
    calcolata. Ordinati per data prossima esecuzione ascendente."""
    out: list[dict] = []
    try:
        from .. import db as _db
        sql = (
            "SELECT id, name, agent_display_name, agent_icon, agent_category, "
            "  cron, agent_mode "
            "FROM tasks WHERE is_published_agent = TRUE "
            "  AND cron IS NOT NULL AND cron != '' "
        )
        args: list = []
        if tenant_id is not None:
            sql += "AND tenant_id = %s "
            args.append(tenant_id)
        sql += "ORDER BY id"
        with _db.connect() as con:
            rows = con.execute(sql, tuple(args)).fetchall()
        # Calcolo prossima esecuzione via croniter (best-effort)
        try:
            from croniter import croniter
            from datetime import datetime, timezone
            now = datetime.now(timezone.utc)
            for r in rows:
                cron_expr = (r.get("cron") or "").strip()
                next_dt = None
                try:
                    next_dt = croniter(cron_expr, now).get_next(datetime)
                except Exception:
                    next_dt = None
                out.append({
                    "task_id": r["id"],
                    "display_name": r.get("agent_display_name") or r.get("name"),
                    "icon": r.get("agent_icon") or "op-i-clock",
                    "category": r.get("agent_category") or "",
                    "cron": cron_expr,
                    "agent_mode": r.get("agent_mode"),
                    "next_run": next_dt.isoformat() if next_dt else None,
                    "next_run_human": _format_next_run(next_dt, now) if next_dt else None,
                })
        except ImportError:
            # croniter non disponibile — popoliamo senza next_run
            for r in rows:
                out.append({
                    "task_id": r["id"],
                    "display_name": r.get("agent_display_name") or r.get("name"),
                    "icon": r.get("agent_icon") or "op-i-clock",
                    "category": r.get("agent_category") or "",
                    "cron": (r.get("cron") or "").strip(),
                    "agent_mode": r.get("agent_mode"),
                    "next_run": None,
                    "next_run_human": None,
                })
    except Exception:
        log.exception("operator agenda build failed")
    # Ordina per next_run (None alla fine)
    out.sort(key=lambda a: (a["next_run"] is None, a["next_run"] or ""))
    return out


def _format_next_run(dt, now) -> str:
    """Formato umano-friendly: 'oggi alle 14:00', 'domani alle 9:00', '3 giorni'."""
    delta = dt - now
    days = delta.days
    seconds = delta.total_seconds()
    if seconds < 0:
        return "in passato (verifica)"
    if seconds < 60:
        return "imminente"
    if seconds < 3600:
        return f"fra {int(seconds // 60)} minuti"
    if days == 0:
        return f"oggi alle {dt.strftime('%H:%M')}"
    if days == 1:
        return f"domani alle {dt.strftime('%H:%M')}"
    if days < 7:
        weekdays = ["lun", "mar", "mer", "gio", "ven", "sab", "dom"]
        return f"{weekdays[dt.weekday()]} alle {dt.strftime('%H:%M')}"
    return f"fra {days} giorni"


def _build_history_data(tenant_id: int | None = None) -> dict:
    """Statistiche storiche: ultimi 7 giorni di esecuzioni.
    Ritorna {days: [{date, done, error, total}], total, success_rate}."""
    from datetime import datetime, timezone, timedelta
    days_out: list[dict] = []
    total = 0
    successes = 0
    try:
        from .. import db as _db
        now = datetime.now(timezone.utc)
        sql = (
            "SELECT date_trunc('day', finished_at::timestamptz) AS day, status, COUNT(*) AS n "
            "FROM jobs "
            "WHERE finished_at IS NOT NULL "
            "  AND finished_at::timestamptz >= NOW() - INTERVAL '7 days' "
        )
        args: list = []
        if tenant_id is not None:
            sql += "AND tenant_id = %s "
            args.append(tenant_id)
        sql += "GROUP BY day, status"
        with _db.connect() as con:
            rows = con.execute(sql, tuple(args)).fetchall()
        # Aggreghiamo per giorno
        by_day: dict[str, dict[str, int]] = {}
        for r in rows:
            day_str = r["day"].strftime("%Y-%m-%d") if r["day"] else "unknown"
            d = by_day.setdefault(day_str, {"done": 0, "error": 0, "cancelled": 0, "total": 0})
            st = r["status"]
            n = int(r["n"])
            if st in ("done", "error", "cancelled"):
                d[st] = n
            d["total"] += n
        # Genera ultimi 7 giorni anche se vuoti
        for i in range(6, -1, -1):
            day = (now - timedelta(days=i)).strftime("%Y-%m-%d")
            d = by_day.get(day, {"done": 0, "error": 0, "cancelled": 0, "total": 0})
            days_out.append({
                "date": day,
                "day_short": (now - timedelta(days=i)).strftime("%a")[:3],
                "done": d.get("done", 0),
                "error": d.get("error", 0),
                "total": d.get("total", 0),
            })
            total += d.get("total", 0)
            successes += d.get("done", 0)
    except Exception:
        log.exception("operator history build failed")
    success_rate = int(round(100 * successes / total)) if total > 0 else 0
    return {
        "days": days_out,
        "total": total,
        "successes": successes,
        "success_rate": success_rate,
        "max_total": max((d["total"] for d in days_out), default=1),
    }


def _build_live_panel_data(tenant_id: int | None = None) -> tuple[list[dict], list[dict]]:
    """Costruisce (live_jobs, recent_finished) per il pannello laterale.

    Strategia: pesca i job recenti nel tenant (anche queued/running/paused)
    + ultime esecuzioni terminate (done/error/cancelled).
    Arricchisce con metadata dell'agente pubblicato (icona, display_name)
    quando il task/workflow associato e' pubblicato.
    """
    import time as _time
    from datetime import datetime, timezone

    live_jobs: list[dict] = []
    recent_finished: list[dict] = []
    try:
        from .. import db as _db
        with _db.connect() as con:
            # Job attivi (queued/running/paused), ultimi 5 per evitare clutter
            sql_active = (
                "SELECT j.id AS job_id, j.task_id, j.status, j.started_at, j.log, "
                "  t.agent_display_name, t.agent_icon, t.name, t.is_published_agent "
                "FROM jobs j JOIN tasks t ON t.id = j.task_id "
                "WHERE j.status IN ('queued','running','paused') "
                "  AND t.is_published_agent = TRUE "
            )
            args: list = []
            if tenant_id is not None:
                sql_active += "AND j.tenant_id = %s "
                args.append(tenant_id)
            sql_active += "ORDER BY j.id DESC LIMIT 5"
            for r in con.execute(sql_active, tuple(args)).fetchall():
                started = r.get("started_at")
                elapsed = None
                if started:
                    try:
                        dt = datetime.fromisoformat(str(started).replace("Z", "+00:00"))
                        if dt.tzinfo is None:
                            dt = dt.replace(tzinfo=timezone.utc)
                        elapsed = max(0, int((datetime.now(timezone.utc) - dt).total_seconds()))
                    except Exception:
                        elapsed = None
                # Tenta di estrarre uno snippet "stat" dal log (es. "12 contatti trovati")
                stat_text = ""
                job_log_lines = (r.get("log") or "").strip().splitlines()
                if job_log_lines:
                    last_line = job_log_lines[-1].strip()
                    if len(last_line) < 100:
                        stat_text = last_line
                live_jobs.append({
                    "job_id": r["job_id"],
                    "task_id": r["task_id"],
                    "kind": "task",
                    "agent_id": r["task_id"],
                    "display_name": r.get("agent_display_name") or r.get("name") or f"Task #{r['task_id']}",
                    "icon": r.get("agent_icon") or "op-i-cog",
                    "status": r["status"],
                    "started_at": started,
                    "elapsed_sec": elapsed,
                    "progress_pct": None,        # MVP: nessuna progress structured
                    "progress_label": "",
                    "stat_text": stat_text,
                })
            # Job recenti terminati (ultimi 3 done/error/cancelled negli ultimi 24h)
            sql_recent = (
                "SELECT j.id AS job_id, j.task_id, j.status, j.started_at, j.finished_at, "
                "  t.agent_display_name, t.agent_icon, t.name "
                "FROM jobs j JOIN tasks t ON t.id = j.task_id "
                "WHERE j.status IN ('done','error','cancelled') "
                "  AND t.is_published_agent = TRUE "
                "  AND j.finished_at IS NOT NULL "
            )
            args2: list = []
            if tenant_id is not None:
                sql_recent += "AND j.tenant_id = %s "
                args2.append(tenant_id)
            sql_recent += "ORDER BY j.finished_at DESC LIMIT 3"
            for r in con.execute(sql_recent, tuple(args2)).fetchall():
                duration = None
                if r.get("started_at") and r.get("finished_at"):
                    try:
                        s = datetime.fromisoformat(str(r["started_at"]).replace("Z", "+00:00"))
                        f = datetime.fromisoformat(str(r["finished_at"]).replace("Z", "+00:00"))
                        duration = max(0, int((f - s).total_seconds()))
                    except Exception:
                        pass
                recent_finished.append({
                    "job_id": r["job_id"],
                    "task_id": r["task_id"],
                    "kind": "task",
                    "agent_id": r["task_id"],
                    "display_name": r.get("agent_display_name") or r.get("name") or f"Task #{r['task_id']}",
                    "icon": r.get("agent_icon") or "op-i-cog",
                    "status": r["status"],
                    "finished_at": r.get("finished_at"),
                    "duration_sec": duration,
                })
    except Exception:
        log.exception("operator live_panel data build failed")
    return live_jobs, recent_finished


@router.get("/home", response_class=HTMLResponse)
async def operator_home(
    request: Request,
    _partial: str = "",
):
    """Dashboard operator: hero + agenti per categoria + pannello live.

    Con `?_partial=live` ritorna solo il pannello live (polling HTMX 3s).
    """
    # Build dati pannello live (sempre, anche per render iniziale).
    tenant_id = db.current_tenant_id()
    live_jobs, recent_finished = _build_live_panel_data(tenant_id=tenant_id)
    agenda = _build_agenda_data(tenant_id=tenant_id)
    history = _build_history_data(tenant_id=tenant_id)

    if _partial == "live":
        # Solo il pannello live (polling 3s — solo Tab 1 va aggiornato live).
        return templates.TemplateResponse(
            request,
            "operator/partials/live_panel.html",
            {
                "live_jobs": live_jobs,
                "recent_finished": recent_finished,
                "agenda": agenda,
                "history": history,
            },
        )

    agents = db.list_published_agents()
    grouped = _group_agents_by_category(agents)
    try:
        chat_history = db.list_orchestrator_messages(limit=30)
    except Exception:
        chat_history = []
    return templates.TemplateResponse(
        request,
        "operator/home.html",
        {
            "agent_groups": grouped,
            "live_jobs": live_jobs,
            "recent_finished": recent_finished,
            "agenda": agenda,
            "history": history,
            "n_agents": len(agents),
            "chat_history": chat_history,
        },
    )


# ===========================================================================
# Lancio agente
# ===========================================================================

@router.get("/agents/task/schedule", response_class=HTMLResponse)
async def operator_schedule_picker_modal(request: Request):
    """Modal per pianificare un agente: dropdown task pubblicati + preset cron."""
    agents = db.list_published_agents()
    task_agents = [a for a in agents if a.get("kind") == "task"]
    return templates.TemplateResponse(
        request,
        "operator/partials/schedule_modal.html",
        {
            "agents": task_agents,
            "agent": None,    # nuovo schedule, no preselect
            "current_cron": "",
        },
    )


@router.get("/agents/task/{task_id}/schedule", response_class=HTMLResponse)
async def operator_schedule_edit_modal(request: Request, task_id: int):
    """Modal per modificare schedule esistente di un agente specifico."""
    agent = db.get_published_agent("task", task_id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agente non trovato")
    t = db.get_task(task_id)
    current_cron = (t.get("cron") if t else None) or ""
    agents = db.list_published_agents()
    task_agents = [a for a in agents if a.get("kind") == "task"]
    return templates.TemplateResponse(
        request,
        "operator/partials/schedule_modal.html",
        {
            "agents": task_agents,
            "agent": agent,
            "current_cron": current_cron,
        },
    )


@router.post("/agents/task/{task_id}/schedule")
async def operator_schedule_save(
    request: Request,
    task_id: int,
    cron: str = Form(""),
):
    """Salva (o rimuove) la cron expression sul task."""
    from urllib.parse import quote
    agent = db.get_published_agent("task", task_id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agente non trovato")
    cron_clean = (cron or "").strip()
    # Validazione minimale via croniter se disponibile
    if cron_clean:
        try:
            from croniter import croniter
            croniter(cron_clean)
        except Exception as exc:
            return RedirectResponse(
                url=f"/home?_err={quote(f'Cron expression non valida: {exc}')}",
                status_code=303,
            )
    try:
        tenant_id = db.current_tenant_id()
        with db.connect() as con:
            sql = "UPDATE tasks SET cron = %s, updated_at = %s WHERE id = %s"
            args: list = [cron_clean or None, db.now_iso(), task_id]
            if tenant_id is not None:
                sql += " AND tenant_id = %s"
                args.append(tenant_id)
            con.execute(sql, tuple(args))
            con.commit()
        # Re-fresh dello scheduler APScheduler (best-effort)
        try:
            from .. import jobs as _jobs
            _jobs._refresh_schedules()
        except Exception:
            pass
        msg = "Pianificazione aggiornata" if cron_clean else "Pianificazione rimossa"
    except Exception as exc:
        log.exception("operator_schedule_save failed")
        return RedirectResponse(
            url=f"/home?_err={quote(f'Errore salvataggio: {type(exc).__name__}')}",
            status_code=303,
        )
    return RedirectResponse(url=f"/home?_msg={quote(msg)}", status_code=303)


@router.get("/agents/{kind}/{agent_id}/details", response_class=HTMLResponse)
async def operator_agent_details_modal(
    request: Request, kind: str, agent_id: int,
):
    """Modal read-only con i dettagli dell'agente (per la click-card).
    Mostra: nome, descrizione, kind, categoria, agent_mode (task), parametri
    richiesti, ultime esecuzioni con stato/durata.
    """
    agent = db.get_published_agent(kind, agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agente non trovato")
    # Ultime esecuzioni
    recent_jobs: list[dict] = []
    if kind == "task":
        try:
            recent = db.list_recent_jobs_for_tasks([agent_id], last_n=5)
            recent_jobs = recent.get(int(agent_id), [])
        except Exception:
            recent_jobs = []
    elif kind == "workflow":
        try:
            # Per i workflow prendiamo gli ultimi workflow_runs (proxy "esecuzioni").
            from .. import db as _db
            with _db.connect() as con:
                rows = con.execute(
                    "SELECT id, status, started_at, finished_at "
                    "FROM workflow_runs WHERE workflow_id = %s "
                    "ORDER BY id DESC LIMIT 5",
                    (agent_id,),
                ).fetchall()
            recent_jobs = [dict(r) for r in rows]
        except Exception:
            recent_jobs = []

    # Schema input per visualizzazione
    schema_raw = agent.get("input_schema") or []
    fields: list[dict] = []
    if isinstance(schema_raw, list):
        for f in schema_raw:
            if not isinstance(f, dict):
                continue
            fields.append({
                "name": f.get("name") or "",
                "label": f.get("label") or f.get("name") or "",
                "type": f.get("type") or "text",
                "required": bool(f.get("required", False)),
            })

    # Per i task, andiamo a leggere agent_mode + model dal record reale
    extra_meta: dict[str, str] = {}
    if kind == "task":
        try:
            t = db.get_task(agent_id)
            if t:
                extra_meta["agent_mode"] = t.get("agent_mode") or ""
                extra_meta["model"] = t.get("model") or ""
                extra_meta["llm_provider"] = t.get("llm_provider") or ""
        except Exception:
            pass

    return templates.TemplateResponse(
        request,
        "operator/partials/details_modal.html",
        {
            "agent": agent,
            "fields": fields,
            "recent_jobs": recent_jobs,
            "extra_meta": extra_meta,
        },
    )


@router.get("/agents/{kind}/{agent_id}/launch", response_class=HTMLResponse)
async def operator_agent_launch_modal(
    request: Request, kind: str, agent_id: int,
):
    """Renderizza il modal di lancio (form dinamico dallo schema input).

    Se l'agente non richiede parametri (schema vuoto), il modal mostra solo
    una conferma "Avviare {nome}?" e basta.
    """
    agent = db.get_published_agent(kind, agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agente non trovato")
    # input_schema arriva come list di dict (gia' deserializzato da JSONB)
    schema_raw = agent.get("input_schema") or []
    fields: list[dict] = []
    if isinstance(schema_raw, list):
        for f in schema_raw:
            if not isinstance(f, dict):
                continue
            name = (f.get("name") or "").strip()
            if not name:
                continue
            fields.append({
                "name": name,
                "label": f.get("label") or name,
                "type": f.get("type") or "text",
                "required": bool(f.get("required", False)),
                "placeholder": f.get("placeholder") or "",
                "options": f.get("options") or [],  # per select
                "default": f.get("default") or "",
            })
    return templates.TemplateResponse(
        request,
        "operator/partials/launch_modal.html",
        {"agent": agent, "fields": fields},
    )


@router.post("/agents/{kind}/{agent_id}/run")
async def operator_agent_run(
    request: Request, kind: str, agent_id: int,
):
    """Avvia un agente. I parametri runtime vengono raccolti dal form e
    iniettati come `agent_input` (JSON) nel task — il runner lo legge se
    interessato (oggi: best-effort, scriviamo in `notes` per audit + in un
    futuro campo dedicato `last_agent_input`).

    Per i task: chiama `jobs.start_job`.
    Per i workflow: chiama `jobs.start_workflow`.
    """
    agent = db.get_published_agent(kind, agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agente non trovato")
    form = await request.form()
    # Raccogli i parametri dal form: tutti i campi con prefix `input.`
    # vengono interpretati come parametri dell'agent_input_schema.
    user_input: dict[str, Any] = {}
    for k, v in form.items():
        if k.startswith("input."):
            user_input[k[len("input."):]] = v.strip() if isinstance(v, str) else v
    # Valida required
    schema_raw = agent.get("input_schema") or []
    if isinstance(schema_raw, list):
        for f in schema_raw:
            if not isinstance(f, dict):
                continue
            if f.get("required") and not user_input.get(f.get("name") or ""):
                raise HTTPException(
                    status_code=400,
                    detail=f"Campo obbligatorio mancante: {f.get('label') or f.get('name')}",
                )

    # Log audit dell'operator-launch (utile per ricostruire chi ha lanciato cosa
    # con quali parametri — l'integrazione runtime dei parametri nel runner e' v2).
    audit_blob = json.dumps(user_input, ensure_ascii=False) if user_input else "{}"
    log.info(
        "operator-launch user=%s kind=%s agent_id=%s name=%r input=%s",
        getattr(getattr(request.state, "current_user", None), "email", "?"),
        kind, agent_id, agent.get("display_name"), audit_blob,
    )

    from urllib.parse import quote
    try:
        if kind == "task":
            job_id = jobs.start_job(agent_id)
            log.info("operator-launch task=%s -> job=%s", agent_id, job_id)
        elif kind == "workflow":
            result = jobs.start_workflow(agent_id)
            log.info(
                "operator-launch workflow=%s -> run=%s jobs=%s",
                agent_id, result.get("workflow_run_id"), result.get("started_jobs"),
            )
        else:
            return RedirectResponse(
                url=f"/home?_err={quote(f'Tipo agente non valido: {kind}')}",
                status_code=303,
            )
    except ValueError as e:
        log.warning("operator-launch ValueError: %s", e)
        return RedirectResponse(
            url=f"/home?_err={quote(f'Impossibile avviare: {e}')}",
            status_code=303,
        )
    except Exception as e:
        log.exception("operator-launch failed")
        err_msg = f"{type(e).__name__}: {str(e)[:300]}"
        return RedirectResponse(
            url=f"/home?_err={quote(f'Errore avvio agente: {err_msg}')}",
            status_code=303,
        )

    msg = f"Agente '{agent['display_name']}' avviato."
    return RedirectResponse(url=f"/home?_msg={quote(msg)}", status_code=303)


# ===========================================================================
# Job status (per toast + modal) — endpoints lightweight read-only
# ===========================================================================

@router.get("/jobs/{job_id}/operator-status")
async def operator_job_status(request: Request, job_id: int):
    """Stato finale di un job (per il toast notification). Ritorna JSON con:
    {status, display_name, message, finished_at}.
    Filtra per tenant del current_user (operator non vede job di altri tenant).
    """
    from fastapi.responses import JSONResponse
    try:
        job = db.get_job(job_id)
    except Exception:
        job = None
    if not job:
        return JSONResponse({"error": "not_found"}, status_code=404)
    # Pesca display_name dal task associato
    display_name: str = ""
    try:
        if job.get("task_id"):
            t = db.get_task(int(job["task_id"]))
            if t:
                display_name = t.get("agent_display_name") or t.get("name") or f"Task #{t['id']}"
    except Exception:
        pass
    # Costruisci un messaggio sintetico in base allo status finale
    status = job.get("status") or "unknown"
    if status == "done":
        msg = "Esecuzione completata."
    elif status == "error":
        err = (job.get("error") or "").strip()
        msg = err[:140] if err else "Si e' verificato un errore."
    elif status == "cancelled":
        msg = "Esecuzione annullata."
    else:
        msg = ""
    return JSONResponse({
        "job_id": job_id,
        "status": status,
        "display_name": display_name,
        "message": msg,
        "finished_at": job.get("finished_at"),
    })


@router.get("/jobs/{job_id}/operator-file", response_class=HTMLResponse)
async def operator_job_file_view(
    request: Request,
    job_id: int,
    file: str,
):
    """Renderizza un file generato da un job DENTRO un modal operator.
    Riusa le funzioni `_classify`, `_build_view_context` di results.py per
    decidere come visualizzare (markdown / json / csv / raw).

    URL del file relativo alla cartella task: es. `report.md` oppure
    `20260524T160000Z/report.md` per run sub-folders.
    """
    from .. import storage
    from .results import _classify, _build_view_context

    try:
        job = db.get_job(job_id)
    except Exception:
        job = None
    if not job:
        raise HTTPException(status_code=404, detail="Job non trovato")
    task_id = int(job.get("task_id") or 0)
    if not task_id:
        raise HTTPException(status_code=404, detail="Task non trovato")
    task = db.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task non trovato")

    # Path safety: file rilativi al task. Splita su / per gestire run/file.
    parts = [p for p in file.split("/") if p and p != ".." and ".." not in p]
    if not parts:
        raise HTTPException(status_code=400, detail="path non valido")

    result = storage.read_text_safe(task_id, parts)
    if result is None:
        raise HTTPException(status_code=404, detail="File non trovato o troppo grande")
    path, raw = result
    ctx = _build_view_context(
        task=task, path=path, raw=raw, title=path.name,
        download_url=f"/tasks/{task_id}/results/{'/'.join(parts)}",
        parent_url="", parent_label="",
    )
    # Aggiungo varianti operator
    ctx["job_id"] = job_id
    return templates.TemplateResponse(
        request,
        "operator/partials/file_viewer_modal.html",
        ctx,
    )


@router.get("/jobs/{job_id}/operator-details", response_class=HTMLResponse)
async def operator_job_details_modal(request: Request, job_id: int):
    """Modal con dettagli di un singolo job (per operator).
    Mostra: stato + durata + log recenti + lista file generati (se done).
    """
    try:
        job = db.get_job(job_id)
    except Exception:
        job = None
    if not job:
        raise HTTPException(status_code=404, detail="Job non trovato")
    task = None
    try:
        if job.get("task_id"):
            task = db.get_task(int(job["task_id"]))
    except Exception:
        pass

    # Lista file generati: prende i file della cartella del task ordinati per data desc.
    # `storage.list_results(task_id)` ritorna [{type, name, size, ...}], gia' ordinato
    # per name desc (file/dir mixed). Filtriamo `type='file'` e prendiamo i primi N.
    files: list[dict] = []
    try:
        from .. import storage
        entries = storage.list_results(int(job["task_id"])) or []
        # Filtriamo solo file (escludiamo cartelle di run browser-use), prime 10
        files = [e for e in entries if e.get("type") == "file"][:10]
    except Exception:
        files = []

    # Tail degli ultimi log (max 20 righe)
    log_text = (job.get("log") or "").strip()
    log_tail: list[str] = []
    if log_text:
        log_tail = log_text.splitlines()[-20:]

    # Durata
    duration_sec: int | None = None
    try:
        if job.get("started_at") and job.get("finished_at"):
            from datetime import datetime, timezone
            s = datetime.fromisoformat(str(job["started_at"]).replace("Z", "+00:00"))
            f = datetime.fromisoformat(str(job["finished_at"]).replace("Z", "+00:00"))
            duration_sec = max(0, int((f - s).total_seconds()))
    except Exception:
        pass

    return templates.TemplateResponse(
        request,
        "operator/partials/job_details_modal.html",
        {
            "job": job,
            "task": task,
            "files": files,
            "log_tail": log_tail,
            "duration_sec": duration_sec,
        },
    )


# ===========================================================================
# Lead operator: vista read-only di asset (e qualified) prodotti dagli agenti
# ===========================================================================

_MAX_LEAD_TAG_SLOTS = 6


def _parse_lead_tag_filters(qp) -> list[tuple[str, str]]:
    """Estrae tag_key__N / tag_value__N (N=0..5) dalla querystring.
    Stesso pattern usato in /qualified architect."""
    out: list[tuple[str, str]] = []
    for i in range(_MAX_LEAD_TAG_SLOTS):
        k = (qp.get(f"tag_key__{i}") or "").strip().lower()
        v = (qp.get(f"tag_value__{i}") or "").strip()
        if k and v:
            out.append((k, v))
    return out


def _parse_optional_int(value):
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    try:
        return int(s)
    except (TypeError, ValueError):
        return None


@router.get("/leads", response_class=HTMLResponse)
async def operator_leads(
    request: Request,
    q: str | None = None,
    asset_type: str | None = None,
    status: str | None = None,
    source_task_id: str | None = None,
    has_contacts: str = "",
    has_social: str = "",
    tag_mode: str = "and",
    page: str = "1",
):
    """Lista lead (asset) generati dagli agenti del tenant. Read-only
    + opt-out per riga. Stessi filtri di /assets architect."""
    per_page = 60
    page_v = _parse_optional_int(page) or 1
    page_v = max(1, page_v)
    offset = (page_v - 1) * per_page
    search = (q or "").strip() or None
    type_filter = (asset_type or "").strip() or None
    status_filter = (status or "").strip() or None
    source_task_id_v = _parse_optional_int(source_task_id)
    has_contacts_v = bool(has_contacts and has_contacts.strip())
    has_social_v = bool(has_social and has_social.strip())
    tag_mode_v = (tag_mode or "and").strip().lower()
    if tag_mode_v not in ("and", "or"):
        tag_mode_v = "and"
    tag_filters = _parse_lead_tag_filters(request.query_params)

    try:
        total = db.count_assets(
            asset_type=type_filter, status=status_filter, source_task_id=source_task_id_v,
            tag_filters=tag_filters or None, search=search,
            tag_mode=tag_mode_v, has_contacts=has_contacts_v, has_social=has_social_v,
        )
        rows = db.list_assets(
            asset_type=type_filter, status=status_filter, source_task_id=source_task_id_v,
            tag_filters=tag_filters or None, search=search,
            tag_mode=tag_mode_v, has_contacts=has_contacts_v, has_social=has_social_v,
            limit=per_page, offset=offset,
        )
        types_in_use = db.list_asset_types_in_use()
        available_tag_keys = db.list_distinct_tag_keys_for_assets(
            exclude_qualifier_tags=True, asset_type=type_filter,
        )
    except Exception:
        log.exception("operator_leads failed")
        total = 0
        rows = []
        types_in_use = []
        available_tag_keys = []
    total_pages = max(1, (total + per_page - 1) // per_page)
    return templates.TemplateResponse(
        request,
        "operator/leads.html",
        {
            "leads": rows,
            "total": total,
            "filter_q": search or "",
            "filter_type": type_filter or "",
            "filter_status": status_filter or "",
            "filter_source_task_id": source_task_id_v,
            "has_contacts": has_contacts_v,
            "has_social": has_social_v,
            "tag_filters": tag_filters,
            "tag_mode": tag_mode_v,
            "types_in_use": types_in_use,
            "available_tag_keys": available_tag_keys,
            "page": page_v,
            "per_page": per_page,
            "total_pages": total_pages,
            "is_qualified_view": False,
        },
    )


@router.get("/leads/qualified", response_class=HTMLResponse)
async def operator_leads_qualified(
    request: Request,
    q: str | None = None,
    qualifiers: str = "",
    score_min: str = "",
    asset_type: str | None = None,
    source_task_id: str | None = None,
    status: str = "qualified",
    has_contacts: str = "",
    has_social: str = "",
    tag_mode: str = "and",
    page: str = "1",
):
    """Lista asset qualificati con i filtri completi della vista architect /qualified."""
    per_page = 60
    page_v = _parse_optional_int(page) or 1
    page_v = max(1, page_v)
    offset = (page_v - 1) * per_page
    search = (q or "").strip() or None
    qualifier_slugs = [s.strip() for s in (qualifiers or "").split(",") if s.strip()]
    if status not in ("qualified", "rejected", "both"):
        status = "qualified"
    score_min_v = _parse_optional_int(score_min)
    type_filter = (asset_type or "").strip() or None
    source_task_id_v = _parse_optional_int(source_task_id)
    has_contacts_v = bool(has_contacts and has_contacts.strip())
    has_social_v = bool(has_social and has_social.strip())
    tag_mode_v = (tag_mode or "and").strip().lower()
    if tag_mode_v not in ("and", "or"):
        tag_mode_v = "and"
    tag_filters = _parse_lead_tag_filters(request.query_params)

    try:
        total = db.count_qualified_assets(
            qualifier_slugs=qualifier_slugs, status_filter=status,
            score_min=score_min_v, asset_type=type_filter,
            source_task_id=source_task_id_v, search=search,
            extra_tag_filters=tag_filters or None, tag_mode=tag_mode_v,
            has_contacts=has_contacts_v, has_social=has_social_v,
        )
        rows = db.list_qualified_assets(
            qualifier_slugs=qualifier_slugs, status_filter=status,
            score_min=score_min_v, asset_type=type_filter,
            source_task_id=source_task_id_v, search=search,
            extra_tag_filters=tag_filters or None, tag_mode=tag_mode_v,
            has_contacts=has_contacts_v, has_social=has_social_v,
            limit=per_page, offset=offset,
        )
        qualifier_menu = db.list_distinct_qualifier_slugs()
        types_in_use = db.list_asset_types_in_use()
        available_tag_keys = db.list_distinct_tag_keys_for_assets(
            exclude_qualifier_tags=True, asset_type=type_filter,
        )
    except Exception:
        log.exception("operator_leads_qualified failed")
        total = 0
        rows = []
        qualifier_menu = []
        types_in_use = []
        available_tag_keys = []
    total_pages = max(1, (total + per_page - 1) // per_page)
    return templates.TemplateResponse(
        request,
        "operator/leads.html",
        {
            "leads": rows,
            "total": total,
            "filter_q": search or "",
            "filter_type": type_filter or "",
            "filter_status": status,
            "filter_source_task_id": source_task_id_v,
            "filter_score_min": score_min_v,
            "selected_qualifiers": qualifier_slugs,
            "has_contacts": has_contacts_v,
            "has_social": has_social_v,
            "tag_filters": tag_filters,
            "tag_mode": tag_mode_v,
            "qualifier_menu": qualifier_menu,
            "types_in_use": types_in_use,
            "available_tag_keys": available_tag_keys,
            "page": page_v,
            "per_page": per_page,
            "total_pages": total_pages,
            "is_qualified_view": True,
        },
    )


@router.post("/leads/{asset_id}/optout")
async def operator_lead_optout(request: Request, asset_id: int):
    """Marca un asset come 'optedout' su tutti i canali outreach.
    Operator-friendly: un click per dire "non contattare piu' questo lead".
    """
    try:
        # Riusiamo l'helper db.set_asset_outreach_status se esiste, altrimenti
        # UPDATE diretto sicuro tenant-scoped.
        tenant_id = db.current_tenant_id()
        with db.connect() as con:
            sql = (
                "UPDATE assets SET outreach_status = 'optedout', "
                "  whatsapp_consent = COALESCE(whatsapp_consent, 'optedout'), "
                "  updated_at = %s WHERE id = %s"
            )
            args: list = [db.now_iso(), int(asset_id)]
            if tenant_id is not None:
                sql += " AND tenant_id = %s"
                args.append(tenant_id)
            cur = con.execute(sql, tuple(args))
            con.commit()
            n = cur.rowcount or 0
    except Exception as exc:
        log.exception("operator_lead_optout failed for asset_id=%s", asset_id)
        return RedirectResponse(
            url=f"/leads?_err=Errore+opt-out:+{type(exc).__name__}",
            status_code=303,
        )
    # Redirect back to the referrer (preserve filters) or /leads default
    referer = request.headers.get("referer") or "/leads"
    sep = "&" if "?" in referer else "?"
    if n > 0:
        return RedirectResponse(url=f"{referer}{sep}_msg=Lead+escluso+dall'outreach", status_code=303)
    return RedirectResponse(url=f"{referer}{sep}_err=Lead+non+trovato", status_code=303)


# ===========================================================================
# Messaggi (inbox semplificata)
# ===========================================================================

@router.get("/messages", response_class=HTMLResponse)
async def operator_messages(
    request: Request,
    channel: str | None = None,
    status: str | None = None,
):
    """Inbox semplificata: tutti i thread del tenant, filtri minimi (canale +
    stato). Decisione MVP: l'operator vede tutto il tenant — non filtriamo
    per `created_by_user_id` perche' aggiunge complessita' senza valore chiaro.
    """
    threads = db.list_threads(
        channel=channel, status=status, limit=200,
    )
    return templates.TemplateResponse(
        request,
        "operator/messages.html",
        {
            "threads": threads,
            "filter_channel": channel or "",
            "filter_status": status or "",
        },
    )
