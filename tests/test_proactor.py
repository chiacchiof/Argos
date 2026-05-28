"""Test B-006: app/runtime/proactor.run_in_proactor_thread.

Su Windows il modulo spawn-a un thread con ProactorEventLoop; in CI POSIX usa
asyncio.create_task nel loop corrente. I test esercitano il path POSIX (path
Windows è testato dall'esecuzione dei runner reali sull'OS del dev). Coperture:
- esecuzione no-op (ritorno valore + register/unregister chiamati),
- timeout opt-in via param e fallback env var,
- traceback dump strutturato su crash (jlog riceve TB completo),
- CancelledError silenzioso (non triggera dump),
- `_resolve_timeout` priorità param > env > None.
"""
from __future__ import annotations

import asyncio
import os

import pytest

from app.runtime.proactor import (
    JobTimeout,
    _resolve_timeout,
    run_in_proactor_thread,
)


# ---------------------------------------------------------------------------
# _resolve_timeout (puro)
# ---------------------------------------------------------------------------

def test_resolve_timeout_priority(monkeypatch):
    monkeypatch.delenv("ARGOS_PROACTOR_DEFAULT_TIMEOUT_S", raising=False)
    assert _resolve_timeout(None) is None
    assert _resolve_timeout(30) == 30.0
    # explicit param ha priorità sull'env
    monkeypatch.setenv("ARGOS_PROACTOR_DEFAULT_TIMEOUT_S", "999")
    assert _resolve_timeout(5) == 5.0
    # fallback all'env quando explicit è None
    assert _resolve_timeout(None) == 999.0
    # env malformata → ignorata
    monkeypatch.setenv("ARGOS_PROACTOR_DEFAULT_TIMEOUT_S", "abc")
    assert _resolve_timeout(None) is None
    # env ≤ 0 → ignorata
    monkeypatch.setenv("ARGOS_PROACTOR_DEFAULT_TIMEOUT_S", "0")
    assert _resolve_timeout(None) is None


# ---------------------------------------------------------------------------
# run_in_proactor_thread (POSIX path)
# ---------------------------------------------------------------------------

def _run(coro_factory, job_id=1, **kw):
    return asyncio.run(run_in_proactor_thread(coro_factory, job_id, **kw))


def test_run_returns_value_and_callbacks_invoked():
    seen_register: list = []
    seen_unregister: list = []

    async def work():
        return "done"

    out = _run(
        work, job_id=42,
        register=lambda jid, loop, task: seen_register.append(jid),
        unregister=lambda jid: seen_unregister.append(jid),
    )
    assert out == "done"
    assert seen_register == [42]
    assert seen_unregister == [42]


def test_timeout_raises_jobtimeout_and_dumps():
    logs: list[str] = []

    async def hang():
        await asyncio.sleep(5)
        return "never"

    with pytest.raises(JobTimeout):
        _run(hang, job_id=7, timeout=0.1, jlog=logs.append)
    # traceback dump nel jlog
    assert any("TIMEOUT" in m for m in logs)


def test_crash_dumps_traceback_then_reraises():
    logs: list[str] = []

    async def boom():
        raise RuntimeError("kaboom")

    with pytest.raises(RuntimeError, match="kaboom"):
        _run(boom, job_id=8, jlog=logs.append)
    # il dump contiene il traceback e il marker CRASH
    joined = "\n".join(logs)
    assert "CRASH" in joined
    assert "RuntimeError: kaboom" in joined
    assert "boom" in joined  # frame della funzione nel traceback


def test_cancelled_is_silent_no_dump():
    """CancelledError = stop utente: NON deve generare traceback dump.

    Nota: asyncio.run() può consumare CancelledError sollevata dal main coro
    (interpretata come shutdown del loop), quindi non assertiamo l'exception
    type che riemerge dall'esterno — l'unico contratto è "niente dump".
    """
    logs: list[str] = []

    async def cancelled():
        raise asyncio.CancelledError()

    try:
        _run(cancelled, job_id=9, jlog=logs.append)
    except BaseException:
        pass
    assert logs == [], f"atteso nessun dump su CancelledError, ricevuto: {logs}"


def test_callbacks_safe_on_exception():
    """unregister deve essere chiamato anche se il task crasha (finally)."""
    seen_unregister: list = []

    async def boom():
        raise RuntimeError("x")

    with pytest.raises(RuntimeError):
        _run(boom, job_id=11, unregister=lambda jid: seen_unregister.append(jid))
    assert seen_unregister == [11]


def test_callback_failure_doesnt_break_run():
    """Una eccezione nel callback register/unregister NON deve abortire il job."""
    def bad_register(*a):
        raise RuntimeError("registry down")

    async def work():
        return 42

    out = _run(work, job_id=12, register=bad_register)
    assert out == 42
