"""Helper comuni per gestione control_signal (pause/stop/resume) condivisi
da tutti i runner.

Estratto da runner_browseruse.py per essere riusato da site_explorer, bulk_extract,
qualifier, ecc. — cosi' il pulsante Pause della UI funziona uniformemente.

API:
- `wait_if_paused_or_stop(job_id, jlog)`: chiamato all'inizio di ogni step del
  loop principale del runner. Se signal == 'pause', sospende; se 'stop', alza
  RunnerStopped.
- `RunnerStopped`: eccezione semaforica, catturata dal runner e gestita come
  stop graceful (segnando stopped=True, ingest dei dati raccolti finora).
- `MODES_SUPPORTING_PAUSE`: set di agent_mode che supportano la pausa, usato
  dall'UI per disabilitare il bottone quando non applicabile.
"""
from __future__ import annotations

import asyncio
from typing import Callable

from .. import db


# Insieme di agent_mode che chiamano wait_if_paused_or_stop nel loop interno.
# La UI puo' usare questo per disabilitare il bottone Pause con tooltip
# esplicativo quando la modalita' non lo supporta.
MODES_SUPPORTING_PAUSE: frozenset[str] = frozenset({
    "browser_use",
    "site_explorer",
    "bulk_extract",
    "auto_extract",  # delega ai sub-runner ma riceve il signal di fermo
})


class RunnerStopped(Exception):
    """Sollevata quando control_signal == 'stop' nel corso del loop.

    Il runner deve catturarla, settare stopped=True, e procedere al cleanup
    (save pending queue, ingest in DB) PRIMA di propagare/ritornare.
    """


async def wait_if_paused_or_stop(job_id: int, jlog: Callable[[str], None]) -> None:
    """Helper centralizzato per gestione control_signal.

    - signal == 'stop'   → alza `RunnerStopped` (runner deve catturare)
    - signal == 'pause'  → sospende fino a 'resume'/'stop'/None
    - altri              → ritorna subito
    """
    sig = db.get_control_signal(job_id)
    if sig == "stop":
        jlog("Segnale STOP ricevuto — interruzione richiesta dall'utente.")
        raise RunnerStopped()
    if sig != "pause":
        return
    jlog("Segnale PAUSE ricevuto — attendo resume o stop.")
    db.update_job(job_id, status="paused")
    while True:
        await asyncio.sleep(1.5)
        sig = db.get_control_signal(job_id)
        if sig == "stop":
            jlog("Segnale STOP ricevuto durante pausa — interruzione.")
            raise RunnerStopped()
        if sig is None or sig == "" or sig == "resume":
            db.set_control_signal(job_id, None)
            db.update_job(job_id, status="running")
            jlog("Segnale RESUME ricevuto — riprendo.")
            return
