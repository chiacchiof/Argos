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
- `consume_live_instructions(job_id, jlog)` (B-001): drena la coda chat utente
  del job e ritorna un blocco "ISTRUZIONI UTENTE LIVE" da iniettare nel prompt
  LLM. Logga + posta un ack. Chiamato al checkpoint dai runner che supportano
  l'iniezione di suggerimenti (browser_use, site_explorer, bulk_extract).
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
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

# B-001: agent_mode i cui runner CONSUMANO la coda chat al checkpoint (via
# `consume_live_chat` / `consume_live_instructions`). Per questi il route mette in
# coda testo libero e `/skip` (applied=0). Per gli altri mode i COMANDI
# deterministici (/stop, /pause, /resume, /note, /set) funzionano comunque a
# livello route; il testo libero riceve solo un ack che rimanda ai comandi (così
# non resta "pending" all'infinito).
#
# - browser_use / site_explorer: iniettano il testo libero nel prompt LLM e
#   traducono /skip in "salta il seed/target corrente".
# - outreach_whatsapp: consuma tra un DM e l'altro — /skip salta il contatto e il
#   numero viene riletto dall'asset (così un `/set <id> whatsapp …` live ha effetto);
#   il testo libero viene annotato nel log (il messaggio è pre-generato).
#
# bulk_extract (estrazione concorrente, schema fisso) e auto_extract (delega a
# sub-job con job_id diverso) restano fuori da questo set in v1.
MODES_SUPPORTING_LIVE_CHAT: frozenset[str] = frozenset({
    "browser_use",
    "site_explorer",
    "outreach_whatsapp",
})


class RunnerStopped(Exception):
    """Sollevata quando control_signal == 'stop' nel corso del loop.

    Il runner deve catturarla, settare stopped=True, e procedere al cleanup
    (save pending queue, ingest in DB) PRIMA di propagare/ritornare.
    """


@dataclass
class LiveChat:
    """Esito del consumo della coda chat di un job al checkpoint del runner.

    - `skip`: True se l'utente ha inviato almeno un `/skip` (salta target corrente).
    - `instructions`: testo libero dell'utente (per iniezione nel prompt LLM o
      annotazione nel log, a seconda del runner).
    """
    skip: bool = False
    instructions: list[str] = field(default_factory=list)


def consume_live_chat(job_id: int, jlog: Callable[[str], None]) -> LiveChat | None:
    """B-001: estrae e consuma i messaggi chat utente pending per questo job.

    Va chiamato al checkpoint del runner (di solito subito dopo
    `wait_if_paused_or_stop`). Effetti collaterali:
      - marca i messaggi come consumati (`db.consume_pending_chat`),
      - logga ogni riga nel job log (visibile in dashboard),
      - posta UN ack `assistant` nella chat.

    Ritorna un `LiveChat` (skip + instructions) o `None` se la coda è vuota.
    Sincrono e difensivo: non solleva mai (un problema sulla chat non deve far
    cadere un runner).
    """
    try:
        pending = db.consume_pending_chat(job_id)
    except Exception:
        return None
    if not pending:
        return None

    skip = False
    instructions: list[str] = []
    for m in pending:
        body = (m.get("body") or "").strip()
        if not body:
            continue
        if body.lower() == "/skip":
            skip = True
            jlog("💬 istruzione live: /skip (salta target corrente)")
        else:
            instructions.append(body)
            jlog(f"💬 istruzione live dall'utente: {body}")

    if not skip and not instructions:
        return None

    try:
        n = len(instructions) + (1 if skip else 0)
        db.insert_job_chat_message(
            job_id, "assistant",
            f"Ricevuto ({n} istruzione/i). Le applico al prossimo step.",
            kind="reply", applied=1,
        )
    except Exception:
        pass
    return LiveChat(skip=skip, instructions=instructions)


def consume_live_instructions(
    job_id: int, jlog: Callable[[str], None],
) -> str | None:
    """Variante per i runner LLM-driven (browser_use, site_explorer): ritorna un
    blocco "ISTRUZIONI UTENTE LIVE" già formattato da anteporre al prompt, oppure
    `None` se non c'è nulla. `/skip` viene tradotto in un'istruzione esplicita di
    salto nel testo. Wrappa `consume_live_chat` (che fa consumo + log + ack)."""
    lc = consume_live_chat(job_id, jlog)
    if not lc:
        return None
    lines = list(lc.instructions)
    if lc.skip:
        lines.append(
            "L'utente chiede di SALTARE il target/seed corrente e proseguire "
            "con il prossimo."
        )
    if not lines:
        return None
    return (
        "ISTRUZIONI UTENTE LIVE (priorità su tutto, comunicate durante "
        "l'esecuzione):\n- " + "\n- ".join(lines)
    )


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
