"""B-006: dispatcher dedicato per runner che richiedono ProactorEventLoop su
Windows (Playwright/browser_use). Estratto da `jobs.py:_run_in_proactor_thread`
per:

- **Code organization**: il mega-helper inline era difficile da testare in
  isolamento; ora è un modulo a sé, importato da `jobs.py` (che mantiene un thin
  wrapper retro-compatibile).
- **Timeout opt-in**: parametro `timeout` (o env `ARGOS_PROACTOR_DEFAULT_TIMEOUT_S`)
  cappa il caso "stuck-ma-vivo" (un runner bloccato in loop infinito che il
  watchdog di `jobs.py` non rileva, perché il task asyncio non è morto). Scaduto
  il tempo → `JobTimeout` (subclass di `asyncio.TimeoutError`). Default `None` =
  comportamento legacy invariato.
- **Traceback dump strutturato**: su eccezione non-CancelledError, l'intero
  traceback finisce nel `job.log` via callback `jlog` (se passato). Niente più
  "il runner è morto silenziosamente" come unica diagnosi.

Le rete di sicurezza è data dai test integration di B-007
(`tests/test_workflow_integration.py`): reconcile/watchdog/finalize coprono il
recovery anche se questo modulo regredisce.
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
import threading
import traceback
from typing import Any, Awaitable, Callable

from .. import db


log = logging.getLogger(__name__)

# Default opt-in via env: se settato, applicato quando il caller non passa timeout.
# Es. `ARGOS_PROACTOR_DEFAULT_TIMEOUT_S=5400` cappa a 90 min ogni job.
_ENV_TIMEOUT = "ARGOS_PROACTOR_DEFAULT_TIMEOUT_S"

# Grace per started.wait() prima di iniziare a fare polling del thread (Windows).
_STARTED_GRACE_S = 5.0


class JobTimeout(asyncio.TimeoutError):
    """Sollevata quando scade il timeout globale del proactor.

    Subclass di `asyncio.TimeoutError` per retro-compatibilità: codice esistente
    che cattura TimeoutError continua a funzionare.
    """


def _resolve_timeout(explicit: float | None) -> float | None:
    """Risolve il timeout effettivo: `explicit` ha priorità; altrimenti env var
    `ARGOS_PROACTOR_DEFAULT_TIMEOUT_S` (se settata e parsabile come float > 0);
    altrimenti None (legacy, no cap)."""
    if explicit is not None:
        return float(explicit) if explicit > 0 else None
    raw = (os.environ.get(_ENV_TIMEOUT) or "").strip()
    if not raw:
        return None
    try:
        v = float(raw)
    except ValueError:
        log.warning("%s='%s' non parsabile come float; ignoro.", _ENV_TIMEOUT, raw)
        return None
    return v if v > 0 else None


def _dump_tb(
    jlog: Callable[[str], None] | None,
    job_id: int,
    exc: BaseException,
    label: str,
) -> None:
    """Dump traceback completo nel job_log (o nel logger, se jlog assente).

    Mai solleva: il dumping è best-effort e non deve nascondere l'eccezione
    originale al chiamante.
    """
    try:
        tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        msg = f"[proactor {label}] {type(exc).__name__}: {exc}\n{tb}"
    except Exception:
        msg = f"[proactor {label}] {type(exc).__name__}: {exc} (traceback non formattabile)"
    if jlog is not None:
        try:
            jlog(msg)
            return
        except Exception:
            log.exception("proactor: jlog callback failed; fallback al logger")
    log.error("%s", msg)


async def run_in_proactor_thread(
    coro_factory: Callable[[], Awaitable[Any]],
    job_id: int,
    *,
    timeout: float | None = None,
    jlog: Callable[[str], None] | None = None,
    register: Callable[[int, asyncio.AbstractEventLoop, asyncio.Task], None] | None = None,
    unregister: Callable[[int], None] | None = None,
) -> Any:
    """Esegue una coroutine in un thread con ProactorEventLoop (Windows).

    Su POSIX: gira nel loop chiamante via `asyncio.create_task` — la cancellazione
    si propaga naturalmente e `create_task` copia il ContextVar corrente (tenant ok).

    Su Windows: spawn un thread con ProactorEventLoop. Serve perché uvicorn
    imposta `WindowsSelectorEventLoopPolicy` che **non** supporta
    `asyncio.create_subprocess_exec`, usato da Playwright/browser_use per
    Chromium. I `ContextVar` (tenant_id, user_id) NON si propagano alle
    `threading.Thread`: catturiamo i valori PRIMA nel chiamante e li reiniettiamo
    DENTRO il thread come prima cosa.

    Args:
      coro_factory: callable che produce la coroutine da eseguire. La coro è
        creata DENTRO il thread/loop corretto (lazy).
      job_id: identificatore per log + callback.
      timeout: cap in secondi sull'esecuzione. `None` = legacy (no cap). Fallback
        a `ARGOS_PROACTOR_DEFAULT_TIMEOUT_S` se non passato. Allo scadere alza
        `JobTimeout` (subclass di TimeoutError).
      jlog: callback `(msg: str) -> None` per appendere righe al job_log
        (usato per il traceback dump su crash). Se `None`, log su logger Python.
      register: callback `(job_id, loop, task) -> None` chiamato all'avvio. Il
        caller lo usa per registrare il task in un registro globale dei job
        attivi (per `hard_stop_job` cross-thread). Default no-op.
      unregister: callback `(job_id) -> None` chiamato al cleanup. Default no-op.

    Behaviors invariati rispetto alla versione legacy in `jobs.py`:
      - `CancelledError` propagato/log silenzioso (è lo stop utente).
      - Su POSIX il task gira nel loop esistente.
      - Su Windows si attende `started_event` (max 5s) prima di polling il thread.
    """
    effective_timeout = _resolve_timeout(timeout)

    def _wrap(coro: Awaitable[Any]) -> Awaitable[Any]:
        return asyncio.wait_for(coro, timeout=effective_timeout) if effective_timeout else coro

    # Cattura ContextVar PRIMA del thread (vedi docstring).
    saved_tenant_id = db.current_tenant_id()
    saved_user_id = db.current_user_id()

    def _safe(cb, *args, label):
        if not cb:
            return
        try:
            cb(*args)
        except Exception:
            log.exception("proactor: callback %s failed", label)

    if sys.platform != "win32":
        loop = asyncio.get_running_loop()
        task = asyncio.create_task(_wrap(coro_factory()))
        _safe(register, job_id, loop, task, label="register")
        try:
            return await task
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError as e:
            _dump_tb(jlog, job_id, e, "TIMEOUT")
            raise JobTimeout(
                f"job {job_id} oltre il timeout di {effective_timeout}s"
            ) from e
        except BaseException as e:
            _dump_tb(jlog, job_id, e, "CRASH")
            raise
        finally:
            _safe(unregister, job_id, label="unregister")

    # ---- Windows: thread separato con ProactorEventLoop ----
    result: list[Any] = []
    exc_holder: list[BaseException] = []
    started = threading.Event()

    def runner() -> None:
        # Re-inietta i ContextVar nel thread (il context parte vuoto).
        db.set_current_tenant(saved_tenant_id)
        db.set_current_user(saved_user_id)

        new_loop = asyncio.ProactorEventLoop()  # type: ignore[attr-defined]
        asyncio.set_event_loop(new_loop)
        task = new_loop.create_task(_wrap(coro_factory()))
        _safe(register, job_id, new_loop, task, label="register")
        started.set()
        try:
            result.append(new_loop.run_until_complete(task))
        except asyncio.CancelledError:
            log.info("job %s: task cancelled", job_id)
        except asyncio.TimeoutError as e:
            _dump_tb(jlog, job_id, e, "TIMEOUT")
            exc_holder.append(
                JobTimeout(f"job {job_id} oltre il timeout di {effective_timeout}s")
            )
        except BaseException as e:  # pragma: no cover (path Windows)
            _dump_tb(jlog, job_id, e, "CRASH")
            exc_holder.append(e)
        finally:
            _safe(unregister, job_id, label="unregister")
            try:
                new_loop.close()
            except Exception:
                pass

    t = threading.Thread(target=runner, name=f"proactor-job-{job_id}", daemon=True)
    t.start()
    started.wait(timeout=_STARTED_GRACE_S)
    while t.is_alive():
        await asyncio.sleep(0.5)
    t.join()
    if exc_holder:
        raise exc_holder[0]
    return result[0] if result else None
