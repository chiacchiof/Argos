"""JobManager: lancia run_agent in asyncio.task; APScheduler per cron ricorrenti.

Mantiene anche un registro globale dei job attivi `_active_jobs` (loop+task)
per permettere il **hard stop** (cancellazione del task in corso) e la
**detection di processi morti** dal lato dashboard.
"""
from __future__ import annotations

import asyncio
import logging
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

# Set di job_id per cui triggerare il downstream anche se status='cancelled'.
# Popolato dall'endpoint POST /jobs/{id}/stop?complete_downstream=1 (fix N3).
# Svuotato dopo il trigger nel finally di _run_job.
_trigger_downstream_on_cancel: set[int] = set()


def mark_complete_downstream_on_cancel(job_id: int) -> None:
    """Marca un job: quando viene stoppato/cancellato, il workflow downstream
    parte comunque (qualifier, outreach, ecc.). Usato dall'UI "Stop e completa".
    """
    _trigger_downstream_on_cancel.add(job_id)


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


def register_subjob(job_id: int) -> None:
    """Registra in `_active_jobs` il job_id di un sub-runner (es. sub-job di
    auto_extract → browser_use o bulk_extract).

    Senza questa registrazione, `hard_stop_job` cliccato sul sub-job in UI non
    troverebbe nessun task da cancellare. Punta allo stesso `current_task()` del
    parent: cancellarlo cancella anche il parent (ok, e' il comportamento atteso).
    """
    try:
        loop = asyncio.get_running_loop()
        task = asyncio.current_task()
    except RuntimeError:
        return
    if task is None:
        return
    _active_jobs[job_id] = (loop, task)


def unregister_subjob(job_id: int) -> None:
    _active_jobs.pop(job_id, None)


def reconcile_orphan_jobs() -> int:
    """A startup: ogni job in stato attivo (queued/running/paused) è orfano.

    Il processo che lo eseguiva non esiste più in memoria, quindi è morto.
    Li marchiamo come 'error' con una nota.

    Inoltre: finalizza i workflow_run i cui job sono tutti terminati (incluso
    quelli appena marcati 'error'). Senza questo passo i workflow restano
    "running" in UI per sempre — vedi `partials/workflows_list_table.html`.
    """
    n = 0
    orphan_workflow_run_ids: set[int] = set()
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
        if j.get("workflow_run_id"):
            orphan_workflow_run_ids.add(int(j["workflow_run_id"]))
        n += 1

    # Finalizza i workflow_run dei job orfani (di solito risultano in stato 'error'
    # perche' ALMENO uno dei job e' 'error'). _maybe_finalize_workflow_run
    # decide la politica: error se almeno uno e' error, ecc.
    for wfr_id in orphan_workflow_run_ids:
        try:
            _maybe_finalize_workflow_run(wfr_id)
        except Exception:
            log.exception("reconcile: workflow_run %s finalize failed", wfr_id)

    # Cintura di sicurezza: pulisci workflow_run 'running'/'queued' che NON hanno
    # nessun job attivo (es. job manualmente cancellati senza che _after_job_done
    # girasse, oppure DB sporcato da un crash). Marca come 'error'.
    try:
        stranded = db.find_stranded_workflow_runs()
        for wfr in stranded:
            try:
                _maybe_finalize_workflow_run(int(wfr["id"]))
            except Exception:
                log.exception("reconcile: stranded workflow_run %s finalize failed", wfr.get("id"))
        if stranded:
            log.info("reconcile_orphan_jobs: finalizzati %d workflow_run orfani", len(stranded))
    except Exception:
        log.exception("reconcile: find_stranded_workflow_runs failed")

    if n:
        log.info("reconcile_orphan_jobs: %d job orfani chiusi", n)
    return n


# Quanti secondi di grazia diamo a un job "running" prima di considerarlo zombie
# se il suo task asyncio non e' piu' nel processo. 60s assorbe la finestra fra
# start_job e registrazione in `_active_jobs` (popolata dal thread proactor)
# senza killare runner appena partiti.
_WATCHDOG_GRACE_SECONDS = 60


def watchdog_zombie_jobs() -> int:
    """Watchdog runtime: ogni N secondi (chiamato dallo scheduler) controlla i
    job in stato attivo (queued/running/paused) nel DB e li confronta con il
    registro `_active_jobs` / `_running_tasks` in-process.

    Se un job e' "running" nel DB ma il suo task asyncio NON e' piu' vivo
    (`is_runner_alive(job_id) == False`) E sono passati piu' di `_WATCHDOG_GRACE_SECONDS`
    da started_at, lo riconcilia automaticamente marcandolo come 'error'.

    Cosi' i job zombie (process morto, crash silente del runner, task asyncio
    abbandonato) si auto-riconciliano senza richiedere un riavvio dell'app —
    l'UI smette di mostrare "running da 14 minuti" e l'utente puo' rilanciare.

    Ritorna il numero di zombie riconciliati. Idempotente.
    """
    import datetime as _dt
    n = 0
    now = _dt.datetime.now(_dt.timezone.utc)
    for j in _list_active_jobs_in_db():
        jid = int(j["id"])
        if is_runner_alive(jid):
            continue  # vivo, non toccare
        # Grace period: leggi started_at dal DB, se < N secondi fa salta
        with db.connect() as con:
            row = con.execute(
                "SELECT started_at FROM jobs WHERE id = %s", (jid,)
            ).fetchone()
        started_iso = (row or {}).get("started_at") if row else None
        if started_iso:
            try:
                started = _dt.datetime.fromisoformat(started_iso.replace("Z", "+00:00"))
                if started.tzinfo is None:
                    started = started.replace(tzinfo=_dt.timezone.utc)
                if (now - started).total_seconds() < _WATCHDOG_GRACE_SECONDS:
                    continue  # ancora nel grace period, salta
            except Exception:
                pass
        # Diagnostica: dump dello stato delle registry e dei thread proactor
        # prima di marcare il job come zombie. Aiuta a capire i falsi positivi
        # (job vivo ma is_runner_alive=False) — vedi job 162 del 2026-05-28.
        try:
            import threading as _th
            rt = _running_tasks.get(jid)
            rt_state = (
                f"task={rt!r} done={rt.done() if rt else 'n/a'}"
                if rt is not None else "MISSING"
            )
            aj = _active_jobs.get(jid)
            aj_state = (
                f"task={aj[1]!r} done={aj[1].done()} loop_closed={aj[0].is_closed()}"
                if aj is not None else "MISSING"
            )
            proactor_threads = [
                t.name for t in _th.enumerate()
                if t.name.startswith(f"proactor-job-{jid}")
            ]
            db.append_job_log(
                jid,
                f"Watchdog DIAG: _running_tasks[{jid}]={rt_state} | "
                f"_active_jobs[{jid}]={aj_state} | "
                f"proactor_threads={proactor_threads}",
            )
        except Exception:
            log.exception("watchdog diag dump failed for job %s", jid)
        db.append_job_log(
            jid,
            f"Watchdog: runner asyncio non più attivo dopo grace period "
            f"({_WATCHDOG_GRACE_SECONDS}s). Marco come errore.",
        )
        db.update_job(
            jid,
            status="error",
            error="Runner died (watchdog detected zombie).",
            finished_at=db.now_iso(),
        )
        db.set_control_signal(jid, None)
        n += 1
    if n:
        log.warning("watchdog_zombie_jobs: %d zombi riconciliati", n)
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
    *,
    timeout: float | None = None,
) -> Any:
    """Esegue la coroutine in un thread con ProactorEventLoop (Windows).

    Implementazione: delega a [`app/runtime/proactor.run_in_proactor_thread`](app/runtime/proactor.py)
    (B-006: estratto in modulo dedicato per testabilità + timeout opt-in +
    traceback dump strutturato). Questo wrapper resta in `jobs.py` per:

    - **call-site compatibilità**: i runner chiamano `_run_in_proactor_thread`
      come prima, niente migrazione dei call site.
    - **wiring di `_active_jobs`**: passa register/unregister che mutano il
      registro globale dei job attivi (usato da `hard_stop_job`,
      `is_runner_alive`, watchdog) — il registro vive in `jobs.py` perché
      condiviso con `register_subjob`/`unregister_subjob`/reconcile/ecc.
    - **wiring di `jlog`**: callback verso `db.append_job_log` per il traceback
      dump (sempre attivo, opt-out non previsto).

    Vedi `app/runtime/proactor.py` per la spiegazione tecnica completa (perché
    serve il proactor thread su Windows, propagazione tenant ContextVar, ecc.).

    Args:
      coro_factory: callable lazy che ritorna la coroutine.
      job_id: id del job (per registry + log).
      timeout: cap in secondi (default None = no cap; fallback a env
        `ARGOS_PROACTOR_DEFAULT_TIMEOUT_S`). Allo scadere alza `JobTimeout`.
    """
    from .runtime.proactor import run_in_proactor_thread

    return await run_in_proactor_thread(
        coro_factory,
        job_id,
        timeout=timeout,
        jlog=lambda msg: db.append_job_log(job_id, msg),
        register=lambda jid, loop, task: _active_jobs.__setitem__(jid, (loop, task)),
        unregister=lambda jid: _active_jobs.pop(jid, None),
    )


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
                    "SELECT workflow_id FROM workflow_runs WHERE id = %s",
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
                                "UPDATE tasks SET input_artifact_path = %s WHERE id = %s",
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
    # Carico il task SENZA filtro tenant: il job runner è privileged
    # (chiamato da APScheduler cron o da workflow downstream, dove il
    # context HTTP può non essere settato). `tenant_id=None` esplicito
    # bypassa il filtro del ContextVar.
    task = db.get_task(task_id, tenant_id=None)
    if not task:
        db.update_job(job_id, status="error", error="Task non trovato", finished_at=db.now_iso())
        return

    # Setto il ContextVar tenant_id + user_id per la durata di questo job runner.
    # Serve a:
    # - cron schedulati: APScheduler chiama senza context HTTP → senza questo,
    #   le `db.*` chiamate dal runner agentico salverebbero con tenant_id=NULL
    # - job downstream nei workflow: _after_job_done crea task fuori dal context HTTP
    # - propagazione al thread Playwright via _run_in_proactor_thread (vedi sopra)
    tenant_token = db.set_current_tenant(task.get("tenant_id"))
    user_token = db.set_current_user(task.get("created_by_user_id"))

    mode = task.get("agent_mode") or "react"
    try:
        if mode == "browser_use":
            from .agent.runner_browseruse import run_agent as run_bu
            await _run_in_proactor_thread(lambda: run_bu(task, job_id), job_id)
        elif mode == "bulk_extract":
            from .agent.runner_bulk_extract import run_agent as run_bk
            await run_bk(task, job_id)
        elif mode == "auto_extract":
            from .agent.runner_auto_extract import run_agent as run_ae
            # auto_extract può lanciare browser_use → serve il proactor thread
            await _run_in_proactor_thread(lambda: run_ae(task, job_id), job_id)
        elif mode == "site_explorer":
            from .agent.runner_site_explorer import run_agent as run_se
            await run_se(task, job_id)
        elif mode == "outreach":
            from .agent.runner_outreach import run_agent as run_o
            await run_o(task, job_id)
        elif mode == "outreach_social":
            from .agent.runner_outreach_social import run_agent as run_os
            # Apre browser headed (Playwright) → serve proactor thread su Windows.
            await _run_in_proactor_thread(lambda: run_os(task, job_id), job_id)
        elif mode == "outreach_whatsapp":
            from .agent.runner_outreach_whatsapp import run_agent as run_ow
            # Motore A apre browser (Playwright) → proactor thread anche qui.
            # Motore B-only funziona anche in loop normale, ma il dispatcher
            # non sa a priori se userà A o B: meglio sempre proactor.
            await _run_in_proactor_thread(lambda: run_ow(task, job_id), job_id)
        elif mode == "recon_social":
            from .agent.runner_recon_social import run_agent as run_rs
            # Apre Chromium persistent → proactor thread su Windows.
            await _run_in_proactor_thread(lambda: run_rs(task, job_id), job_id)
        elif mode == "audience_discovery":
            from .agent.runner_audience_discovery import run_agent as run_ad
            # Stesso pattern di recon_social: Chromium persistent loggato →
            # proactor thread su Windows.
            await _run_in_proactor_thread(lambda: run_ad(task, job_id), job_id)
        elif mode == "qualifier":
            from .agent.runner_qualifier import run_agent as run_q
            await run_q(task, job_id)
        elif mode == "responder":
            from .agent.runner_responder import run_agent as run_r
            await run_r(task, job_id)
        else:
            # run_react può fallback a Playwright via fetch_url quando il
            # contenuto HTTP è scarso → serve ProactorEventLoop su Windows.
            await _run_in_proactor_thread(lambda: run_react(task, job_id), job_id)
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
        # Reset ContextVar tenant/user (settati a inizio funzione)
        try:
            db.reset_current_user(user_token)
            db.reset_current_tenant(tenant_token)
        except Exception:
            pass
        _running_tasks.pop(job_id, None)
        cur = db.get_job(job_id, tenant_id=None)
        # Trigger downstream se:
        # - status = done (caso normale), OPPURE
        # - status = cancelled AND job_id e' nel set _trigger_downstream_on_cancel
        #   (utente ha scelto "stop e completa workflow" — fix N3)
        try:
            status = (cur or {}).get("status") or ""
            should_trigger = status == "done" or (
                status == "cancelled" and job_id in _trigger_downstream_on_cancel
            )
            _trigger_downstream_on_cancel.discard(job_id)
            if should_trigger and cur:
                _after_job_done(
                    job_id,
                    task_id,
                    workflow_run_id=cur.get("workflow_run_id"),
                )
        except Exception:
            log.exception("trigger downstream failed")
        # Aggiorna stato del workflow_run se questo era l'ultimo job in pendenza
        try:
            wfr_id = (cur or {}).get("workflow_run_id")
            if wfr_id is not None:
                _maybe_finalize_workflow_run(int(wfr_id))
        except Exception:
            log.exception("workflow_run finalize failed")


def _maybe_finalize_workflow_run(workflow_run_id: int) -> None:
    """Se tutti i job del workflow_run sono in stato finale, aggiorna lo stato del run.

    Politica: any error -> error; any cancelled (e nessun error) -> cancelled; altrimenti done.
    Idempotente: non fa nulla se il run e' gia' in stato finale.
    """
    jobs_in_run = db.list_jobs_for_workflow_run(workflow_run_id)
    if not jobs_in_run:
        return
    finished = {"done", "error", "cancelled"}
    if not all((j.get("status") or "") in finished for j in jobs_in_run):
        return
    statuses = {(j.get("status") or "") for j in jobs_in_run}
    if "error" in statuses:
        new_status = "error"
    elif "cancelled" in statuses:
        new_status = "cancelled"
    else:
        new_status = "done"
    with db.connect() as con:
        row = con.execute(
            "SELECT status FROM workflow_runs WHERE id = %s",
            (workflow_run_id,),
        ).fetchone()
    if not row or (row["status"] or "") in finished:
        return
    db.update_workflow_run_status(workflow_run_id, new_status)
    log.info("workflow_run #%s -> %s", workflow_run_id, new_status)


def start_job(task_id: int, workflow_run_id: int | None = None) -> int:
    """Crea un job in stato queued e lancia il task asincrono. Ritorna job_id.

    IMPORTANTE: il job eredita sempre `created_by_user_id` dal TASK (chi l'ha
    creato), NON dal caller corrente. Motivo: le credenziali LLM, gli account
    email/social/etc. sono possedute dall'architect che ha creato il task. Se
    un operator (utente con UI semplificata) lancia il task, il runner cerca
    le credenziali dell'architect — non dell'operator — perche' il task e' una
    "definizione" il cui contesto e' quello del suo creatore.

    Caso edge: task senza `created_by_user_id` (es. dati pre-multi-tenant) →
    fallback al caller corrente via _resolve_user(_UNSET).
    """
    task = db.get_task(task_id)
    if task is None:
        raise ValueError(f"Task {task_id} non esiste.")
    if task.get("disabled"):
        raise ValueError(
            f"Task #{task_id} '{task.get('name')}' e' disabilitato. "
            f"Riabilitalo prima di lanciarlo."
        )
    # Owner del job = owner del task (per credential lookup, vedi docstring).
    owner_id = task.get("created_by_user_id")
    create_kwargs: dict[str, Any] = {"workflow_run_id": workflow_run_id}
    if owner_id:
        create_kwargs["created_by_user_id"] = int(owner_id)
    job_id = db.create_job(task_id, **create_kwargs)
    t = asyncio.create_task(_run_job(job_id, task_id))
    _running_tasks[job_id] = t
    return job_id


def start_workflow(workflow_id: int) -> dict:
    """Avvia un workflow: trova i task root del DAG e ne lancia un job ciascuno.

    Ritorna {"workflow_run_id": int, "started_jobs": [job_id, ...], "roots": [task_id, ...]}.
    """
    wf = db.get_workflow(workflow_id)
    if wf is None:
        raise ValueError(f"Workflow {workflow_id} non esiste.")
    if wf.get("disabled"):
        raise ValueError(
            f"Workflow #{workflow_id} '{wf.get('name')}' e' disabilitato. "
            f"Riabilitalo prima di lanciarlo."
        )
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
    """Inserisce un InboundMessage in DB: trova/crea asset shadow, thread, messaggio.
    Fase 2D: asset diventa il record canonico del destinatario."""
    asset = None
    if m.channel == "email" and m.sender_address:
        asset = db.find_asset_by_email(m.sender_address)
        if not asset:
            aid = db.upsert_asset({
                "asset_type": "contact_legacy",
                "title": m.sender_name or m.sender_address,
                "display_name": m.sender_name,
                "email": m.sender_address,
                "status": "new",
                "outreach_status": "replied",
                "raw_json": "{}",
            })
            asset = db.get_asset(aid)
    elif m.channel == "telegram" and m.sender_address:
        asset = db.find_asset_by_telegram_chat(m.sender_address)
        if not asset:
            aid = db.upsert_asset({
                "asset_type": "contact_legacy",
                "title": m.sender_name or m.sender_telegram_username or str(m.sender_address),
                "display_name": m.sender_name,
                "telegram_username": m.sender_telegram_username,
                "telegram_chat_id": m.sender_address,
                "status": "new",
                "outreach_status": "replied",
                "raw_json": "{}",
            })
            db.set_asset_telegram_chat(aid, m.sender_address)
            asset = db.get_asset(aid)
    if not asset:
        return

    thread_id = db.get_or_create_thread(
        asset_id=asset["id"], channel=m.channel,
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
    # mantieni outreach_status='optedout'/'contacted' se gia' impostato
    cur = db.get_asset(asset["id"])
    if (cur.get("outreach_status") or "") not in ("optedout", "contacted"):
        db.update_asset_outreach_status(asset["id"], "replied")
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
    # Watchdog zombie: ogni 60s ricontrolla i job "running" che non hanno piu'
    # un task asyncio vivo. Auto-riconcilia senza riavvio app. Vedi
    # `watchdog_zombie_jobs` per i criteri (grace period 60s).
    _scheduler.add_job(
        watchdog_zombie_jobs,
        trigger=IntervalTrigger(seconds=60),
        id="watchdog_zombie_jobs",
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
