"""JobManager: lancia run_agent in asyncio.task; APScheduler per cron ricorrenti.

Mantiene anche un registro globale dei job attivi `_active_jobs` (loop+task)
per permettere il **hard stop** (cancellazione del task in corso) e la
**detection di processi morti** dal lato dashboard.
"""
from __future__ import annotations

import asyncio
import logging
import sys
import threading
from typing import Any, Awaitable, Callable

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from . import db
from .agent.runner import run_agent as run_react

log = logging.getLogger(__name__)

_scheduler: AsyncIOScheduler | None = None
_running_tasks: dict[int, asyncio.Task] = {}

# Mappa job_id -> (event_loop, asyncio.Task) per cancellazione cross-thread.
# Popolata DA dentro il thread proactor; svuotata quando il task termina.
_active_jobs: dict[int, tuple[asyncio.AbstractEventLoop, asyncio.Task]] = {}


def is_runner_alive(job_id: int) -> bool:
    """Il runner per questo job è ancora vivo in questo processo?

    `_running_tasks` viene popolato sincronicamente in `start_job`, mentre
    `_active_jobs` lo popola il thread proactor con un piccolo ritardo: senza
    il primo controllo, la dashboard renderizzata subito dopo il POST /run
    vede runner_alive=False e disabilita polling+controlli finché l'utente
    non ricarica la pagina.
    """
    t = _running_tasks.get(job_id)
    if t and not t.done():
        return True
    entry = _active_jobs.get(job_id)
    if entry:
        _, task = entry
        return not task.done()
    return False


def hard_stop_job(job_id: int) -> bool:
    """Cancella il task asyncio del job (cross-thread). Ritorna True se richiesto."""
    entry = _active_jobs.get(job_id)
    if not entry:
        return False
    loop, task = entry
    if task.done():
        return False
    try:
        loop.call_soon_threadsafe(task.cancel)
        return True
    except Exception:
        log.exception("hard_stop_job %s failed", job_id)
        return False


def reconcile_orphan_jobs() -> int:
    """A startup: ogni job in stato attivo (queued/running/paused) è orfano.

    Il processo che lo eseguiva non esiste più in memoria, quindi è morto.
    Li marchiamo come 'error' con una nota.
    """
    n = 0
    for j in _list_active_jobs_in_db():
        db.append_job_log(
            j["id"],
            "Job orfano rilevato all'avvio del server: il processo che lo "
            "eseguiva non è più attivo. Marco come errore.",
        )
        db.update_job(
            j["id"],
            status="error",
            error="Server riavviato: runner perso.",
            finished_at=db.now_iso(),
        )
        db.set_control_signal(j["id"], None)
        n += 1
    if n:
        log.info("reconcile_orphan_jobs: %d job orfani chiusi", n)
    return n


def _list_active_jobs_in_db() -> list[dict[str, Any]]:
    with db.connect() as con:
        rows = con.execute(
            "SELECT id FROM jobs WHERE status IN ('queued', 'running', 'paused')"
        ).fetchall()
    return [dict(r) for r in rows]


async def _run_in_proactor_thread(
    coro_factory: Callable[[], Awaitable[Any]],
    job_id: int,
) -> Any:
    """Esegue la coroutine in un thread con ProactorEventLoop (Windows).

    Espone (loop, task) tramite `_active_jobs[job_id]` così che `hard_stop_job`
    possa cancellare il task da qualunque thread chiamando `task.cancel()` via
    `loop.call_soon_threadsafe`.

    Serve perché uvicorn imposta WindowsSelectorEventLoopPolicy che NON supporta
    asyncio.create_subprocess_exec — usato da Playwright/browser-use per avviare
    Chromium. Su Linux/macOS la coroutine gira nel loop chiamante.
    """
    if sys.platform != "win32":
        # POSIX: la cancellazione si propaga naturalmente nel loop chiamante
        loop = asyncio.get_running_loop()
        task = asyncio.create_task(coro_factory())
        _active_jobs[job_id] = (loop, task)
        try:
            return await task
        finally:
            _active_jobs.pop(job_id, None)

    result: list[Any] = []
    exc_holder: list[BaseException] = []
    started = threading.Event()

    def runner() -> None:
        new_loop = asyncio.ProactorEventLoop()  # type: ignore[attr-defined]
        asyncio.set_event_loop(new_loop)
        task = new_loop.create_task(coro_factory())
        _active_jobs[job_id] = (new_loop, task)
        started.set()
        try:
            result.append(new_loop.run_until_complete(task))
        except asyncio.CancelledError:
            # Il task è stato cancellato dall'esterno: non lo trattiamo come errore.
            log.info("job %s: task cancelled", job_id)
        except BaseException as e:  # pragma: no cover
            exc_holder.append(e)
        finally:
            _active_jobs.pop(job_id, None)
            try:
                new_loop.close()
            except Exception:
                pass

    t = threading.Thread(target=runner, name=f"proactor-job-{job_id}", daemon=True)
    t.start()
    started.wait(timeout=5)
    while t.is_alive():
        await asyncio.sleep(0.5)
    t.join()
    if exc_holder:
        raise exc_holder[0]
    return result[0] if result else None


def _after_job_done(job_id: int, task_id: int, workflow_run_id: int | None = None) -> None:
    """Triggera i job downstream secondo workflow_edges quando un job completa con successo.

    Se workflow_run_id è impostato, segue solo gli edge di quel workflow.
    Se è None, segue gli edge legacy (workflow_id IS NULL) per retro-compat.
    """
    workflow_id = None
    if workflow_run_id is not None:
        try:
            run = None
            with db.connect() as con:
                row = con.execute(
                    "SELECT workflow_id FROM workflow_runs WHERE id = ?",
                    (workflow_run_id,),
                ).fetchone()
                if row:
                    workflow_id = int(row["workflow_id"])
        except Exception:
            log.exception("after_job_done: failed to resolve workflow_id from run")

    try:
        if workflow_id is not None:
            edges = db.list_edges(workflow_id=workflow_id, from_task_id=task_id)
        else:
            edges = db.list_edges(from_task_id=task_id)
    except Exception:
        log.exception("after_job_done: list_edges failed")
        return

    for e in edges:
        if not e.get("enabled"):
            continue
        downstream_tid = e["to_task_id"]
        downstream = db.get_task(downstream_tid)
        if not downstream:
            continue
        # Se l'edge specifica un pass_artifact, lo passiamo come override per il downstream
        artifact = e.get("pass_artifact")
        if artifact:
            upstream_job = db.get_job(job_id)
            result_path = (upstream_job or {}).get("result_path") or ""
            try:
                from pathlib import Path
                rp = Path(result_path)
                if rp.exists():
                    candidate = rp.parent / artifact
                    if candidate.exists():
                        with db.connect() as con:
                            con.execute(
                                "UPDATE tasks SET input_artifact_path = ? WHERE id = ?",
                                (str(candidate), downstream_tid),
                            )
                        log.info("DAG: passato artifact %s a task #%s", candidate, downstream_tid)
            except Exception:
                log.exception("after_job_done: artifact passing failed")
        # Lancia il job downstream
        try:
            new_job_id = db.create_job(
                downstream_tid,
                triggered_by_job_id=job_id,
                workflow_run_id=workflow_run_id,
            )
            db.append_job_log(
                new_job_id,
                f"Job triggered da workflow_edge: task upstream #{task_id} job #{job_id}",
            )
            t = asyncio.create_task(_run_job(new_job_id, downstream_tid))
            _running_tasks[new_job_id] = t
            log.info("DAG: triggered downstream job #%s (task %s)", new_job_id, downstream_tid)
        except Exception:
            log.exception("after_job_done: failed to start downstream job")


async def _run_job(job_id: int, task_id: int) -> None:
    task = db.get_task(task_id)
    if not task:
        db.update_job(job_id, status="error", error="Task non trovato", finished_at=db.now_iso())
        return
    mode = task.get("agent_mode") or "react"
    try:
        if mode == "browser_use":
            from .agent.runner_browseruse import run_agent as run_bu
            await _run_in_proactor_thread(lambda: run_bu(task, job_id), job_id)
        elif mode == "bulk_extract":
            from .agent.runner_bulk_extract import run_agent as run_bk
            await run_bk(task, job_id)
        elif mode == "outreach":
            from .agent.runner_outreach import run_agent as run_o
            await run_o(task, job_id)
        elif mode == "qualifier":
            from .agent.runner_qualifier import run_agent as run_q
            await run_q(task, job_id)
        elif mode == "responder":
            from .agent.runner_responder import run_agent as run_r
            await run_r(task, job_id)
        else:
            await run_react(task, job_id)
    except asyncio.CancelledError:
        log.info("job %s cancelled by user", job_id)
        db.append_job_log(job_id, "Job cancellato dall'utente (hard stop).")
        cur = db.get_job(job_id)
        if cur and (cur.get("status") or "") not in ("done", "cancelled", "error"):
            db.update_job(job_id, status="cancelled", finished_at=db.now_iso())
            db.set_control_signal(job_id, None)
        return
    except Exception as e:
        log.exception("job %s failed", job_id)
        detail = str(e) or repr(e) or type(e).__name__
        db.append_job_log(job_id, f"ERRORE ({type(e).__name__}): {detail}")
        db.update_job(job_id, status="error", error=detail, finished_at=db.now_iso())
    finally:
        _running_tasks.pop(job_id, None)
        # Trigger downstream solo se job done
        try:
            cur = db.get_job(job_id)
            if cur and (cur.get("status") or "") == "done":
                _after_job_done(
                    job_id,
                    task_id,
                    workflow_run_id=cur.get("workflow_run_id"),
                )
        except Exception:
            log.exception("trigger downstream failed")


def start_job(task_id: int, workflow_run_id: int | None = None) -> int:
    """Crea un job in stato queued e lancia il task asincrono. Ritorna job_id."""
    job_id = db.create_job(task_id, workflow_run_id=workflow_run_id)
    t = asyncio.create_task(_run_job(job_id, task_id))
    _running_tasks[job_id] = t
    return job_id


def start_workflow(workflow_id: int) -> dict:
    """Avvia un workflow: trova i task root del DAG e ne lancia un job ciascuno.

    Ritorna {"workflow_run_id": int, "started_jobs": [job_id, ...], "roots": [task_id, ...]}.
    """
    roots = db.find_workflow_roots(workflow_id)
    if not roots:
        # Se il workflow non ha edge, non c'è nulla da lanciare
        raise ValueError("Workflow vuoto o senza task: aggiungi almeno un edge.")
    run_id = db.create_workflow_run(workflow_id)
    started: list[int] = []
    for root_tid in roots:
        try:
            jid = start_job(root_tid, workflow_run_id=run_id)
            started.append(jid)
        except Exception:
            log.exception("start_workflow: failed to start root task %s", root_tid)
    return {"workflow_run_id": run_id, "started_jobs": started, "roots": roots}


def _scheduled_run(task_id: int) -> None:
    """Trigger callback per APScheduler (sync wrapper)."""
    try:
        start_job(task_id)
    except Exception:
        log.exception("scheduled run failed for task %s", task_id)


def _refresh_schedules() -> None:
    if _scheduler is None:
        return
    _scheduler.remove_all_jobs()
    for t in db.list_tasks():
        cron_expr = t.get("cron")
        if not cron_expr:
            continue
        try:
            trigger = CronTrigger.from_crontab(cron_expr)
            _scheduler.add_job(
                _scheduled_run,
                trigger=trigger,
                args=[t["id"]],
                id=f"task-{t['id']}",
                replace_existing=True,
            )
        except Exception as e:
            log.warning("cron non valido per task %s: %s (%s)", t["id"], cron_expr, e)


async def _poll_email_inbound() -> None:
    """Job APScheduler: legge IMAP, materializza messaggi inbound, aggiorna thread."""
    from .channels import email as ch_email
    from .channels.base import is_enabled

    if not is_enabled("email"):
        return
    try:
        msgs = await ch_email.fetch_inbound(limit=50)
    except Exception:
        log.exception("poll email failed")
        return
    if not msgs:
        return
    for m in msgs:
        try:
            _ingest_inbound(m)
        except Exception:
            log.exception("ingest email failed")


async def _poll_telegram_inbound() -> None:
    from .channels import telegram as ch_tg
    from .channels.base import is_enabled

    if not is_enabled("telegram"):
        return
    try:
        msgs = await ch_tg.fetch_inbound(limit=100)
    except Exception:
        log.exception("poll telegram failed")
        return
    if not msgs:
        return
    for m in msgs:
        try:
            _ingest_inbound(m)
        except Exception:
            log.exception("ingest telegram failed")


def _ingest_inbound(m) -> None:
    """Inserisce un InboundMessage in DB: trova/crea contact, thread, messaggio."""
    contact = None
    if m.channel == "email" and m.sender_address:
        contact = db.find_contact_by_email(m.sender_address)
        if not contact:
            cid = db.upsert_contact({
                "email": m.sender_address,
                "display_name": m.sender_name,
                "status": "replied",  # ci ha scritto, ovvio che è "vivo"
            })
            contact = db.get_contact(cid)
    elif m.channel == "telegram" and m.sender_address:
        contact = db.find_contact_by_telegram_chat(m.sender_address)
        if not contact:
            # nuovo utente Telegram → crea contact
            cid = db.upsert_contact({
                "telegram_username": m.sender_telegram_username,
                "telegram_chat_id": m.sender_address,
                "display_name": m.sender_name,
                "status": "replied",
            })
            db.set_contact_telegram_chat(cid, m.sender_address)
            contact = db.get_contact(cid)
    if not contact:
        return

    thread_id = db.get_or_create_thread(
        contact["id"], m.channel,
        external_id=(m.in_reply_to or m.external_id) if m.channel == "email" else m.sender_address,
        subject=m.subject,
    )
    # check duplicato
    existing = db.list_messages(thread_id)
    if any(em.get("external_id") == m.external_id and em.get("direction") == "in" for em in existing):
        return  # già inseriti

    db.insert_message(
        thread_id, "in", m.body,
        external_id=m.external_id, status="received",
    )
    db.touch_thread(thread_id)
    db.update_thread_status(thread_id, "replied")
    # mantieni status='optedout' se già impostato
    cur = db.get_contact(contact["id"])
    if (cur.get("status") or "") not in ("optedout", "contacted"):
        db.update_contact_status(contact["id"], "replied")
    log.info("Ingested inbound %s msg from %s", m.channel, m.sender_address)


def start_scheduler() -> None:
    global _scheduler
    if _scheduler is not None:
        return
    _scheduler = AsyncIOScheduler()
    _scheduler.start()
    _refresh_schedules()
    # Polling periodico canali
    _scheduler.add_job(
        _poll_email_inbound,
        trigger=IntervalTrigger(seconds=60),
        id="poll_email_inbound",
        replace_existing=True,
        max_instances=1,
    )
    _scheduler.add_job(
        _poll_telegram_inbound,
        trigger=IntervalTrigger(seconds=30),
        id="poll_telegram_inbound",
        replace_existing=True,
        max_instances=1,
    )


def stop_scheduler() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None


def reload_schedules() -> None:
    """Da chiamare dopo create/update/delete progetto."""
    _refresh_schedules()
