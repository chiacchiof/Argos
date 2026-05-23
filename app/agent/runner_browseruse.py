"""Runner basato su browser-use: pilota un browser reale come fa un utente.

Modalità "profile extraction": per ogni seed URL, parte una sessione browser-use
focalizzata sull'estrazione di pagine-profilo (modelle, annunci, schede personali).
Ogni profilo trovato finisce come riga in profiles.jsonl con uno schema strutturato
(username, email, whatsapp, telegram, sitoweb, social, ecc.).

I file di una run finiscono in: data/results/<task_id>/<timestamp>/
- report.md           riepilogo testuale
- profiles.jsonl      consolidato (un JSON per riga)
- seed_NN/...         file per-seed dell'agente (compresi i suoi profiles.jsonl)

browser-use, playwright e openai sono dipendenze obbligatorie (vedi pyproject.toml).
Dopo l'installazione del progetto serve eseguire una volta:
    playwright install chromium
"""
from __future__ import annotations

import asyncio
import logging
import re
import shutil
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .. import db
from ..config import RESULTS_DIR, settings
from .extraction_templates import get_schema
from .llm_providers import get_provider, resolve_api_key, resolve_base_url
from .ollama import maybe_add_keep_alive


log = logging.getLogger(__name__)


_ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
# parole-chiave delle azioni "interessanti" di browser-use che vogliamo nel job log
_INTERESTING_TOKENS = (
    "Step ", "Eval:", "Memory:", "Next goal", "Plan updated",
    "Clicked", "Opened new tab", "navigate", "scroll", "wait",
    "write_file", "read_file", "extract_structured_data",
    "Final result", "Task complete", "Task failed",
)


class _BrowserUseLogHandler(logging.Handler):
    """Cattura i record del logger 'browser_use' e li appende al job log."""

    def __init__(self, job_id: int):
        super().__init__()
        self.job_id = job_id

    def emit(self, record: logging.LogRecord) -> None:  # pragma: no cover
        try:
            msg = self.format(record)
            msg = _ANSI_ESCAPE.sub("", msg).strip()
            if not msg:
                return
            if not any(tok in msg for tok in _INTERESTING_TOKENS):
                return
            db.append_job_log(self.job_id, f"  bu: {msg[:600]}")
        except Exception:
            pass


class JobStopped(Exception):
    """Sollevata quando l'utente richiede stop dal control_signal."""


OPERATIONAL_PREAMBLE = """Sei un agente che naviga il web come un utente reale per raccogliere \
dati strutturati da pagine pubbliche. L'utente vuole poi usare questi dati per analisi e \
content-optimization (ricontattare i proprietari delle pagine, ottimizzare cataloghi, ecc.).

═══════════════════════════════════════════════════════════════════════
⚠️  REGOLA #1 — CRITICA, NON NEGOZIABILE  ⚠️

DEVI chiamare l'action `extract_structured_data` come tool dopo aver aperto OGNI singola \
pagina che corrisponde allo schema. È L'UNICO MODO in cui i dati vengono salvati.

WRONG (mai fare cosi'):
  Memory: "Extracted structured data from 3 profiles: Anna, Bea, Carla"
  → I dati NON sono stati salvati. Sono solo nel tuo pensiero. ANDRANNO PERSI.

CORRECT (sempre fare cosi'):
  1. Apri pagina profilo di Anna
  2. CHIAMA extract_structured_data(query="extract profile_contacts schema fields from this page")
  3. Apri pagina profilo di Bea
  4. CHIAMA extract_structured_data(...)
  5. Apri pagina profilo di Carla
  6. CHIAMA extract_structured_data(...)

REGOLE OPERATIVE:
- Una pagina valida = una chiamata a `extract_structured_data`. Non aggregare piu' profili in \
  una sola chiamata, non saltare la chiamata "perche' tanto sai gia' i dati".
- NON descrivere l'estrazione nella Memory ("Extracted profile X"). La Memory serve solo per \
  pianificare i prossimi step (es. "next: navigate to /page/3"), NON per memorizzare dati estratti.
- Se l'action `extract_structured_data` non e' disponibile come tool, fermati e usa `done` \
  con summary "tool extract_structured_data non disponibile".
═══════════════════════════════════════════════════════════════════════

STRATEGIA OPERATIVA (per ogni seed URL):
1. Apri la pagina di partenza. Gestisci cookie banner / verifica età cliccando il bottone di \
   conferma ("Accetta", "OK", "SONO MAGGIORENNE", "Continue", "I agree", ecc.).
2. Se è una landing/listing: identifica i link che portano alle pagine di dettaglio individuali \
   secondo i criteri dello SCHEMA DI ESTRAZIONE (vedi sotto).
3. Per ogni link che SEMBRA essere una pagina valida, aprilo (anche in nuove tab è OK).
4. Verifica che soddisfi i criteri dello schema.
5. SE LA PAGINA È VALIDA → **chiama extract_structured_data** (vedi REGOLA #1) con una query \
   che chiede tutti i campi dello SCHEMA fornito sotto. L'action ritorna un JSON strutturato \
   che viene salvato su disco automaticamente.
6. Torna alla lista, vai alla prossima pagina. Salta duplicati / categorie / pagine non valide.
7. Continua finché ci sono step disponibili o non trovi più pagine nuove.

VINCOLI:
- NON fare login, registrazione, form-fill, checkout, pagamenti.
- NON inventare dati: se un campo non è in pagina, metti null. Mai allucinare valori.
- Rispetta whitelist/blacklist domini se specificate dall'utente.
- Se la pagina richiede autenticazione o ha anti-bot pesante, NON insistere: scrivi nel summary \
  "skipped: <motivo>" e passa avanti.

ANTI-LOOP:
- Se hai visitato la stessa URL 3 volte senza estrarne dati, NON tornarci una quarta volta. \
  Cerca un'altra strada o termina con `done`.
- Se stai per ripetere lo stesso click/scroll/navigate del turno precedente con Memory invariata, \
  cambia approccio invece di insistere.

OUTPUT FINALE (con l'action `done`):
Un riepilogo MARKDOWN BREVE (max 30 righe) con:
- numero di chiamate `extract_structured_data` eseguite con successo (= profili salvati)
- elenco dei domini coperti con conteggio per dominio
- problemi incontrati (anti-bot, JS-only, login richiesto, struttura imprevista)
- NIENTE ridondanza dei dati estratti: sono già stati salvati tramite `extract_structured_data`.
"""

def _resolve_extraction_schema(task: dict[str, Any]) -> str:
    """Schema effettivo: testo custom del progetto se presente, altrimenti template."""
    custom = (task.get("extraction_schema") or "").strip()
    if custom:
        return custom
    return get_schema(task.get("extraction_template"))


def _build_seed_task(task: dict[str, Any], seed_url: str | None, max_steps: int) -> str:
    schema_block = _resolve_extraction_schema(task)
    parts = [
        OPERATIONAL_PREAMBLE,
        "",
        "═══ SCHEMA DI ESTRAZIONE PER QUESTO PROGETTO ═══",
        schema_block.rstrip(),
        "═══════════════════════════════════════════════",
        "",
        f"OBIETTIVO SPECIFICO DELL'UTENTE:\n{task['objective']}",
    ]
    if seed_url:
        parts.append(f"\nPARTI DA QUESTO URL: {seed_url}")
        parts.append(
            "Concentrati SOLO su questo dominio in questa sessione. "
            "Estrai più profili possibile dal sito a partire da qui."
        )
    else:
        seeds = task.get("seed_queries") or []
        if seeds:
            parts.append("\nURL/QUERY DI PARTENZA:\n- " + "\n- ".join(seeds))
    allowed = task.get("allowed_domains") or []
    blocked = task.get("blocked_domains") or []
    if allowed:
        parts.append("\nDOMINI CONSENTITI: " + ", ".join(allowed))
    if blocked:
        parts.append("\nDOMINI VIETATI: " + ", ".join(blocked))
    parts.append(f"\nHai a disposizione {max_steps} step. Massimizza il numero di profili estratti.")
    return "\n".join(parts)


def _safe_slug(s: str, max_len: int = 40) -> str:
    s = re.sub(r"^https?://", "", s.strip())
    s = re.sub(r"[^A-Za-z0-9._-]+", "_", s)
    return s[:max_len].strip("._-") or "seed"


def _normalize_seed_url(s: str) -> str:
    s = s.strip()
    if not s:
        return s
    if s.startswith(("http://", "https://")):
        return s
    return f"https://{s}"


async def _wait_if_paused_or_stop(job_id: int, jlog: Callable) -> None:
    """Se control_signal == 'pause', sospende il loop fino a 'resume'/'stop'/None."""
    sig = db.get_control_signal(job_id)
    if sig == "stop":
        jlog("Segnale STOP ricevuto — interruzione richiesta dall'utente.")
        raise JobStopped()
    if sig != "pause":
        return
    jlog("Segnale PAUSE ricevuto — attendo resume o stop.")
    db.update_job(job_id, status="paused")
    while True:
        await asyncio.sleep(1.5)
        sig = db.get_control_signal(job_id)
        if sig == "stop":
            jlog("Segnale STOP ricevuto durante pausa — interruzione.")
            raise JobStopped()
        if sig is None or sig == "" or sig == "resume":
            db.set_control_signal(job_id, None)
            db.update_job(job_id, status="running")
            jlog("Segnale RESUME ricevuto — riprendo. Ricarico configurazione progetto.")
            return


async def run_agent(task: dict[str, Any], job_id: int) -> str:
    from .. import jobs as _jobs

    def jlog(line: str) -> None:
        db.append_job_log(job_id, line)

    # Aggancia handler che fa filtrare i log di browser-use nel job log.
    bu_logger = logging.getLogger("browser_use")
    handler = _BrowserUseLogHandler(job_id)
    handler.setLevel(logging.INFO)
    handler.setFormatter(logging.Formatter("%(name)s | %(message)s"))
    bu_logger.addHandler(handler)
    prev_level = bu_logger.level
    if prev_level == logging.NOTSET or prev_level > logging.INFO:
        bu_logger.setLevel(logging.INFO)

    # Registra anche il sub-job in `_active_jobs` cosi' Stop su questo specifico
    # job_id (es. sub-job di auto_extract) lo cancella davvero.
    _jobs.register_subjob(job_id)

    try:
        return await _run_agent_inner(task, job_id, jlog)
    finally:
        _jobs.unregister_subjob(job_id)
        bu_logger.removeHandler(handler)
        if prev_level != logging.NOTSET:
            bu_logger.setLevel(prev_level)


async def _run_agent_inner(task: dict[str, Any], job_id: int, jlog: Callable) -> str:
    from .blocked_domains import assert_no_blocked_seeds

    db.update_job(job_id, status="running", started_at=db.now_iso())
    db.set_control_signal(job_id, None)
    jlog(
        f"Avvio browser-use per task #{task['id']} \"{task['name']}\""
    )

    # POLICY GATE: domini bloccati (vedi memoria feedback_no_mondocamgirl_traffic)
    _blocked = assert_no_blocked_seeds(task.get("seed_queries") or [])
    if _blocked:
        msg = f"Seed bloccati dalla policy locale (no-traffic): {_blocked}. Abort runner."
        jlog(f"⛔ {msg}")
        db.update_job(job_id, status="error", error=msg, finished_at=db.now_iso())
        return ""

    try:
        from browser_use import Agent
        try:
            from browser_use.llm import ChatOpenAI
        except ImportError:
            from langchain_openai import ChatOpenAI  # type: ignore
    except ImportError as e:
        msg = (
            f"Import di browser-use fallito: {e}. "
            "Probabilmente l'ambiente non è aggiornato — riesegui:\n"
            "  pip install -e .\n"
            "  playwright install chromium"
        )
        jlog(msg)
        db.update_job(job_id, status="error", error=msg, finished_at=db.now_iso())
        raise RuntimeError(msg)

    # Browser_use richiede tool-calling complesso + visione: se l'utente ha
    # specificato un LLM dedicato `browser_llm_*` (modello capable, magari diverso
    # dal main extraction), preferiamolo. Altrimenti fallback al main del task.
    # Validation: se provider=cloud E model=ollama-tag (es. provider=openai +
    # model=qwen3-coder:30b), e' un mismatch dal form non resettato → fallback a main.
    from .runner_auto_extract import _looks_like_ollama_model, _CLOUD_PROVIDERS
    browser_provider = (task.get("browser_llm_provider") or "").strip()
    browser_model = (task.get("browser_llm_model") or "").strip()
    if browser_provider and browser_model:
        if browser_provider.lower() in _CLOUD_PROVIDERS and _looks_like_ollama_model(browser_model):
            jlog(
                f"⚠️ Browser LLM incongruente: provider='{browser_provider}' "
                f"ma model='{browser_model}' sembra Ollama. Fallback al main LLM. "
                f"Correggi i campi `browser_llm_*` nel form del task."
            )
            provider_key = task.get("llm_provider") or "ollama"
            model_name = task["model"]
            api_key_override = task.get("llm_api_key")
            role_label = "Provider LLM (fallback dopo mismatch browser_llm)"
        else:
            provider_key = browser_provider
            model_name = browser_model
            api_key_override = task.get("browser_llm_api_key")
            role_label = "🖥️ Browser LLM (override)"
    else:
        provider_key = task.get("llm_provider") or "ollama"
        model_name = task["model"]
        api_key_override = task.get("llm_api_key")
        role_label = "Provider LLM"

    provider_info = get_provider(provider_key)
    try:
        base_url = resolve_base_url(provider_key, task.get("llm_base_url"))
        api_key = resolve_api_key(provider_key, api_key_override)
    except RuntimeError as e:
        jlog(f"ERRORE configurazione provider: {e}")
        db.update_job(job_id, status="error", error=str(e), finished_at=db.now_iso())
        raise
    jlog(
        f"{role_label}: {provider_info['name']} ({provider_key}) "
        f"@ {base_url} — modello {model_name}"
    )
    # timeout per chiamata LLM: limita il "window" di esposizione su completion
    # in volo. Quando lo Stop chiude il TCP, OpenAI smette al token successivo
    # (best effort). 60s e' largo per gpt-4o-mini ma non lascia run da minuti.
    llm_request_timeout = 60
    try:
        llm = ChatOpenAI(
            model=model_name,
            base_url=base_url,
            api_key=api_key,
            temperature=0.2,
            timeout=llm_request_timeout,
        )
    except TypeError:
        try:
            llm = ChatOpenAI(
                model=model_name,
                openai_api_base=base_url,
                openai_api_key=api_key,
                temperature=0.2,
                request_timeout=llm_request_timeout,
            )
        except TypeError:
            # ultimo fallback senza timeout esplicito
            llm = ChatOpenAI(
                model=model_name,
                openai_api_base=base_url,
                openai_api_key=api_key,
                temperature=0.2,
            )

    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_dir = RESULTS_DIR / str(task["id"]) / ts
    run_dir.mkdir(parents=True, exist_ok=True)
    jlog(f"Run dir: {run_dir}")

    raw_seeds = task.get("seed_queries") or []
    seeds = [_normalize_seed_url(s) for s in raw_seeds if s.strip()]

    consolidated_jsonl = run_dir / "profiles.jsonl"
    summaries: list[str] = []
    n_ok = 0
    n_failed = 0
    stopped = False

    # lista frozen al momento dello start (cambiamenti alle seed durante una pausa
    # NON modificano l'iterazione in corso: solo objective/schema/max_iter rileggi)
    seeds_to_run: list[str | None] = seeds if len(seeds) >= 1 else [None]

    for i, seed in enumerate(seeds_to_run, 1):
        # Check stop / pause prima di ogni seed
        try:
            await _wait_if_paused_or_stop(job_id, jlog)
        except JobStopped:
            stopped = True
            break

        # Ricarico il progetto da DB: l'utente potrebbe averlo modificato durante la pausa
        live_task = db.get_task(task["id"]) or task
        # Mantengo i seed originali (frozen) ma uso obiettivo/schema/max_iter aggiornati
        max_steps = int(live_task.get("max_iterations") or 30)

        if len(seeds_to_run) > 1:
            slug = _safe_slug(seed or f"seed{i}")
            sub_dir = run_dir / f"seed_{i:02d}_{slug}"
            sub_dir.mkdir(parents=True, exist_ok=True)
            jlog(f"=== seed {i}/{len(seeds_to_run)}: {seed} ===")
        else:
            sub_dir = run_dir

        task_text = _build_seed_task(live_task, seed, max_steps)
        # Flush incrementale: dopo ogni N step, browser_use chiama questa
        # callback che salva history.extracted_content() in profiles.jsonl.
        # Cosi' anche con kill brusco (uvicorn reload, watchdog stop) i profili
        # gia' estratti sopravvivono — non sono piu' in memoria volatile.
        agent_holder: dict = {}
        new_step_cb = _make_incremental_flush_callback(
            agent_holder, sub_dir, jlog, job_id, flush_every=3, memory_stuck_threshold=8,
        )
        agent = _make_agent(
            Agent,
            task_text,
            llm,
            sub_dir,
            jlog,
            should_stop_cb=_make_stop_signal_callback(job_id),
            new_step_cb=new_step_cb,
        )
        agent_holder["agent"] = agent

        history_obj = None
        try:
            # Timeout difensivo: anche se max_steps e' settato, browser-use puo'
            # entrare in loop di scroll/no-progress che consuma molto tempo (e $).
            # Stima: ~12s/step in media -> max_steps * 15s + overhead 60s.
            timeout_s = max(180, max_steps * 15 + 60)
            history_obj = await asyncio.wait_for(
                agent.run(max_steps=max_steps),
                timeout=timeout_s,
            )
            final = _extract_final(history_obj)
            label = seed or "task generico"
            summaries.append(f"## {label}\n\n{final or '(nessun output)'}")
            n_ok += 1
        except asyncio.CancelledError:
            jlog(f"Seed '{seed}' INTERROTTA (hard stop richiesto dall'utente).")
            summaries.append(f"## {seed}\n\nINTERROTTA dall'utente durante l'esecuzione.")
            stopped = True
            try:
                if history_obj is not None:
                    _collect_extracted_jsonl(history_obj, sub_dir, jlog)
                _maybe_collect_agent_files(agent, sub_dir, jlog)
                _consolidate_jsonl(sub_dir, consolidated_jsonl, jlog)
            except Exception:
                pass
            break
        except asyncio.TimeoutError:
            jlog(
                f"Seed '{seed}' TIMEOUT dopo {timeout_s}s "
                f"(max_steps={max_steps}). Browser-use bloccato in loop senza progresso. "
                f"Salvo quanto raccolto e passo oltre."
            )
            summaries.append(f"## {seed}\n\nTIMEOUT: browser-use bloccato senza progresso.")
            n_failed += 1
            try:
                if history_obj is not None:
                    _collect_extracted_jsonl(history_obj, sub_dir, jlog)
                _maybe_collect_agent_files(agent, sub_dir, jlog)
                _consolidate_jsonl(sub_dir, consolidated_jsonl, jlog)
            except Exception:
                pass
            continue
        except Exception as e:
            tb = traceback.format_exc()
            jlog(f"Seed '{seed}' FALLITA ({type(e).__name__}): {e or repr(e)}")
            for line in tb.splitlines()[-8:]:
                jlog(f"    {line}")
            summaries.append(f"## {seed}\n\nFALLITA: {type(e).__name__}: {e or repr(e)}")
            n_failed += 1
        finally:
            if not stopped:
                # CRITICO: estrai i dati dall'history dell'agente (contenuti dell'action `extract`)
                # e scrivili in profiles.jsonl. Browser-use NON li persiste automaticamente.
                if history_obj is not None:
                    _collect_extracted_jsonl(history_obj, sub_dir, jlog)
                _maybe_collect_agent_files(agent, sub_dir, jlog)
                _consolidate_jsonl(sub_dir, consolidated_jsonl, jlog)

    # ============================================================
    # Ingest finale in DB: ogni profilo del consolidated profiles.jsonl
    # diventa una riga in `contacts` con status='new'. Il qualifier successivo
    # potrà promuoverli a 'qualified' o 'rejected' aggiornando in-place.
    # I dati restano comunque nel file profiles.jsonl come artefatto della run.
    # ============================================================
    n_ingested = _ingest_to_contacts(
        consolidated_jsonl,
        task["id"],
        job_id,
        jlog,
        extraction_template=task.get("extraction_template"),
    )
    n_assets = _ingest_to_assets(
        consolidated_jsonl,
        task["id"],
        job_id,
        jlog,
        extraction_template=task.get("extraction_template"),
    )

    # Stage 2 — Playbook write: se abbiamo estratto >0 asset E c'e' UN solo seed
    # (caso tipico di auto_extract sub-job), chiediamo al LLM un playbook free-form
    # da iniettare nei futuri run di site_explorer su questo dominio.
    try:
        if (
            n_assets > 0
            and not stopped
            and len(seeds_to_run) == 1
            and task.get("extraction_template")
        ):
            await _write_site_playbook(
                seed_url=seeds_to_run[0],
                asset_type=task["extraction_template"],
                n_assets=n_assets,
                summaries_text="\n\n".join(summaries)[:6000],
                task=task,
                job_id=job_id,
                jlog=jlog,
            )
    except Exception as e:
        jlog(f"  ⚠️ Playbook write skipped: {type(e).__name__}: {e}")

    # report finale (anche dopo stop, quello che hai raccolto è già su disco)
    n_profiles = _count_lines(consolidated_jsonl)
    fmt = (task.get("output_format") or "md")
    report_ext = "md" if fmt in ("md", "both") else "txt"

    status_word = "INTERROTTA dall'utente" if stopped else "completata"
    header = (
        f"# Riepilogo run {ts} ({status_word})\n\n"
        f"- **Profili estratti totali**: {n_profiles} "
        f"(vedi `profiles.jsonl` per i dati strutturati)\n"
        f"- **Contatti ingestiti in DB**: {n_ingested} "
        f"(visibili in /inbox/contacts con status `new`)\n"
        f"- **Seed completate**: {n_ok}/{len(seeds_to_run)}\n"
        f"- **Seed fallite**: {n_failed}\n\n---\n\n"
    )
    final_report = header + "\n\n".join(summaries)
    report_path = run_dir / f"report.{report_ext}"
    report_path.write_text(final_report, encoding="utf-8")
    if fmt == "both":
        (run_dir / "report.txt").write_text(final_report, encoding="utf-8")

    final_status = "cancelled" if stopped else "done"
    jlog(
        f"Run {status_word}: {n_profiles} profili, {n_ok} seed ok, "
        f"{n_failed} fallite. Report: {report_path}"
    )
    db.update_job(
        job_id,
        status=final_status,
        finished_at=db.now_iso(),
        result_path=str(report_path),
    )
    db.set_control_signal(job_id, None)
    return str(report_path)


def _make_agent(
    Agent,
    task_text: str,
    llm: Any,
    run_dir: Path,
    jlog: Callable,
    should_stop_cb: Callable[[], Any] | None = None,
    new_step_cb: Callable[..., Any] | None = None,
) -> Any:
    """Inizializza Agent provando i nomi alternativi del kwarg per il file_system_path.

    Se `should_stop_cb` (async, ritorna bool) e' fornita, la registriamo come
    `register_should_stop_callback` cosi' browser-use la chiama tra uno step e
    l'altro: se True, l'agent.run termina graceful.

    Se `new_step_cb` (async (browser_state, model_output, step_n)) e' fornita,
    viene registrata come `register_new_step_callback`: chiamata dopo ogni step
    LLM completato. Usata per flush incrementale dell'history (extracted_content)
    su disco, cosi' se il job viene killato a meta' i dati gia' estratti sopravvivono.
    """
    base: dict[str, Any] = {"task": task_text, "llm": llm}
    if should_stop_cb is not None:
        base["register_should_stop_callback"] = should_stop_cb
    if new_step_cb is not None:
        base["register_new_step_callback"] = new_step_cb
    for kw in ("file_system_path", "files_path", "agent_data_dir"):
        try:
            return Agent(**base, **{kw: str(run_dir)})
        except TypeError:
            continue
    return Agent(**base)


def _make_incremental_flush_callback(
    agent_holder: dict,
    sub_dir: Path,
    jlog: Callable,
    job_id: int,
    flush_every: int = 3,
    memory_stuck_threshold: int = 8,
    extract_fail_threshold: int = 3,
):
    """Costruisce callback async chiamato dopo ogni step browser_use che svolge
    TRE compiti generici e domain-agnostic:

    1. **Flush incrementale**: ogni `flush_every` step, scarica
       history.extracted_content() su profiles.jsonl. Cosi' se il job e' killato
       a meta', i dati gia' estratti via tool sopravvivono.

    2. **Early-stop Memory stuck**: hash della Memory dell'AgentOutput. Se la
       Memory e' identica per `memory_stuck_threshold` step consecutivi → setta
       control_signal=stop sul job_id, terminando graceful l'agente. Evita loop
       di failure che bruciano step e soldi senza fare progressi.

    3. **Early-stop Extract failure repeated**: se la Memory/Eval del model_output
       contiene pattern di "extract_structured_data failed" per
       `extract_fail_threshold` step consecutivi, abort. Senza questo controllo,
       un modello LLM (anche capable come qwen3.6:27b) che fallisce
       l'extract_structured_data sulla prima pagina entra in retry loop e
       brucia 8+ minuti prima del timeout esterno (incidente 2026-05-23).

    `agent_holder` e' un dict mutabile usato per passare l'agente dopo
    l'inizializzazione (circular dependency: la callback ha bisogno dell'agent,
    l'agent ha bisogno della callback).
    """
    state = {
        "count": 0,
        "last_flushed": 0,
        "last_memory_hash": None,
        "memory_stuck_count": 0,
        "extract_fail_count": 0,
        "early_stopped": False,
    }

    # Pattern regex per detection di "extract_structured_data failed" nella
    # Memory/Eval. Comprende varianti del modello su come descrive il problema.
    import re
    _EXTRACT_FAIL_RE = re.compile(
        r"extract_structured_data.{0,40}(failed|fail|error|retry|attempts?\s+failed)",
        re.IGNORECASE,
    )

    async def _on_new_step(browser_state, model_output, step_n: int) -> None:
        state["count"] += 1

        # -- Estrai Memory + Eval text (difensivo: model_output puo' essere
        # pydantic model o dict con strutture leggermente diverse tra versioni
        # di browser-use) --
        mem_text: str | None = None
        eval_text: str | None = None
        try:
            cs = getattr(model_output, "current_state", None) or getattr(model_output, "state", None)
            if cs is not None:
                mem_text = getattr(cs, "memory", None)
                eval_text = getattr(cs, "evaluation_previous_goal", None) or getattr(cs, "evaluation", None)
                if isinstance(cs, dict):
                    mem_text = mem_text or cs.get("memory")
                    eval_text = eval_text or cs.get("evaluation_previous_goal") or cs.get("evaluation")
        except Exception as e:
            log.debug("model_output parse failed at step %s: %s", step_n, e)

        # -- Compito 2: early-stop su Memory identica --
        try:
            if mem_text and isinstance(mem_text, str):
                import hashlib
                h = hashlib.md5(mem_text.encode("utf-8", errors="ignore")).hexdigest()
                if h == state["last_memory_hash"]:
                    state["memory_stuck_count"] += 1
                else:
                    state["last_memory_hash"] = h
                    state["memory_stuck_count"] = 0
                if state["memory_stuck_count"] >= memory_stuck_threshold and not state["early_stopped"]:
                    state["early_stopped"] = True
                    jlog(
                        f"  🛑 early-stop: Memory dell'agente identica per "
                        f"{state['memory_stuck_count']} step consecutivi (step {step_n}). "
                        f"Setto control_signal=stop per uscire dal loop di failure. "
                        f"L'agente raccolto fin qui rimane salvato."
                    )
                    try:
                        db.set_control_signal(job_id, "stop")
                    except Exception as e:
                        log.debug("set_control_signal failed: %s", e)
        except Exception as e:
            log.debug("memory-stuck check failed at step %s: %s", step_n, e)

        # -- Compito 3: early-stop su extract_structured_data fallito ripetuto --
        try:
            combined = " ".join(filter(None, [mem_text, eval_text]))
            if combined and _EXTRACT_FAIL_RE.search(combined):
                state["extract_fail_count"] += 1
            else:
                # Reset solo se non c'e' indizio di fallimento (preserva il count
                # tra step adiacenti dove l'LLM sta ragionando senza menzionarlo).
                if combined:
                    state["extract_fail_count"] = 0
            if (
                state["extract_fail_count"] >= extract_fail_threshold
                and not state["early_stopped"]
            ):
                state["early_stopped"] = True
                jlog(
                    f"  🛑 early-stop: extract_structured_data fallito "
                    f"{state['extract_fail_count']} step consecutivi (step {step_n}). "
                    f"Pattern noto: il modello LLM non riesce a generare JSON strict "
                    f"compatibile con lo schema; browser-use library ritorna errore. "
                    f"Abort precoce per evitare di sprecare step. "
                    f"FIX SUGGERITO: aggiungi un Browser LLM cloud capable "
                    f"(gpt-4o-mini) sul task — vedi /accounts/llm-keys."
                )
                try:
                    db.set_control_signal(job_id, "stop")
                except Exception as e:
                    log.debug("set_control_signal failed: %s", e)
        except Exception as e:
            log.debug("extract-fail check failed at step %s: %s", step_n, e)

        # -- Compito 1: flush incrementale --
        if (state["count"] - state["last_flushed"]) < flush_every:
            return
        agent = agent_holder.get("agent")
        if agent is None or not hasattr(agent, "state") or not hasattr(agent.state, "history"):
            return
        try:
            history = agent.state.history
            n_before = _count_lines(sub_dir / "profiles.jsonl")
            n_written = _collect_extracted_jsonl(history, sub_dir, lambda _s: None)
            n_after = _count_lines(sub_dir / "profiles.jsonl")
            new_lines = max(0, n_after - n_before)
            state["last_flushed"] = state["count"]
            if new_lines > 0:
                jlog(f"  💾 flush incrementale step {step_n}: +{new_lines} profili salvati su disco")
        except Exception as e:
            log.debug("incremental flush failed at step %s: %s", step_n, e)

    return _on_new_step


def _make_stop_signal_callback(job_id: int):
    """Costruisce il callback async che browser-use chiama per sapere se fermarsi."""
    async def _should_stop() -> bool:
        try:
            return (db.get_control_signal(job_id) or "") == "stop"
        except Exception:
            return False
    return _should_stop


def _extract_final(history: Any) -> str:
    if hasattr(history, "final_result"):
        try:
            r = history.final_result()
            if r:
                return str(r)
        except Exception:
            pass
    if hasattr(history, "extracted_content"):
        try:
            ec = history.extracted_content() or []
            if ec:
                return "\n\n".join(str(x) for x in ec)
        except Exception:
            pass
    return str(history) if history is not None else ""


def _count_lines(p: Path) -> int:
    if not p.exists():
        return 0
    try:
        return sum(1 for line in p.read_text(encoding="utf-8").splitlines() if line.strip())
    except Exception:
        return 0


def _consolidate_jsonl(sub_dir: Path, dest: Path, jlog: Callable) -> None:
    """Trova ogni profiles.jsonl dentro sub_dir e ne appende le righe in dest."""
    if not sub_dir.exists():
        return
    appended = 0
    for jsonl in sub_dir.rglob("profiles.jsonl"):
        try:
            if jsonl.resolve() == dest.resolve():
                continue
        except Exception:
            continue
        try:
            with dest.open("a", encoding="utf-8") as out, jsonl.open(encoding="utf-8") as inp:
                for line in inp:
                    line = line.strip()
                    if line:
                        out.write(line + "\n")
                        appended += 1
        except Exception:
            continue
    if appended:
        jlog(f"Consolidate {appended} righe da {sub_dir.name}/.../profiles.jsonl → profiles.jsonl")


CONTACTS_TEMPLATES = {"profile_contacts"}


def _ingest_to_contacts(
    jsonl_path: Path,
    task_id: int,
    job_id: int,
    jlog: Callable,
    extraction_template: str | None = None,
) -> int:
    """Legge profiles.jsonl consolidato e fa upsert su `contacts` con status='new'.

    Idempotente: se un contatto (matching email o telegram_username) esiste già,
    aggiorna i campi descrittivi MA preserva lo `status` corrente — così se è già
    'qualified'/'contacted'/'optedout' non torna indietro a 'new'.

    Filtra righe che non hanno né email né telegram (non sono contattabili).

    L'ingest in tabella `contacts` viene eseguito SOLO se l'extraction_template
    e' incentrato sui contatti (profile_contacts). Per template come real_estate,
    ecommerce_products, events, news_articles, job_listings i dati restano nel
    profiles.jsonl ma non vengono ingestiti come contatti contattabili.
    """
    import json as _json
    from urllib.parse import urlparse

    if not jsonl_path.exists():
        return 0
    if extraction_template and extraction_template not in CONTACTS_TEMPLATES:
        jlog(
            f"  ℹ️ ingest contacts saltato: extraction_template={extraction_template!r} "
            f"non e' centrato sui contatti. I dati restano in profiles.jsonl."
        )
        return 0

    n_ingested = 0
    n_skipped_no_contact = 0
    n_skipped_invalid = 0

    try:
        with jsonl_path.open(encoding="utf-8") as f:
            for raw in f:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    obj = _json.loads(raw)
                except _json.JSONDecodeError:
                    n_skipped_invalid += 1
                    continue
                if not isinstance(obj, dict):
                    n_skipped_invalid += 1
                    continue

                email = obj.get("email") or None
                tg = obj.get("telegram") or obj.get("telegram_username") or None
                if isinstance(tg, str):
                    tg = tg.lstrip("@") or None

                if not email and not tg:
                    n_skipped_no_contact += 1
                    continue

                source_url = obj.get("url") or obj.get("source_url")
                source_domain = obj.get("source_domain")
                if not source_domain and source_url:
                    try:
                        source_domain = (urlparse(source_url).hostname or "").lower() or None
                    except Exception:
                        source_domain = None

                display = (
                    obj.get("display_name")
                    or obj.get("username")
                    or obj.get("nickname")
                )
                db.upsert_asset({
                    "asset_type": "contact_ingest",
                    "source_task_id": task_id,
                    "source_job_id": job_id,
                    "source_url": source_url,
                    "source_domain": source_domain,
                    "title": display or (email or tg) or "(browseruse ingest)",
                    "display_name": display,
                    "email": email,
                    "telegram_username": tg,
                    "raw_json": raw,
                })
                n_ingested += 1
    except Exception as e:
        jlog(f"  WARN errore durante ingest in DB: {type(e).__name__}: {e}")
        return n_ingested

    if n_ingested:
        jlog(
            f"  ingest DB: {n_ingested} asset su tabella `assets` "
            "(asset_type='contact_ingest')"
        )
    if n_skipped_no_contact:
        jlog(f"  {n_skipped_no_contact} righe scartate (no email/telegram)")
    if n_skipped_invalid:
        jlog(f"  ⏭️ {n_skipped_invalid} righe scartate (JSON invalido)")
    return n_ingested


def _ingest_to_assets(
    jsonl_path: Path,
    task_id: int,
    job_id: int,
    jlog: Callable,
    extraction_template: str | None = None,
) -> int:
    """Legge profiles.jsonl consolidato e fa upsert in tabella `assets` con tag derivati.

    Funziona per tutti i template (real_estate, ecommerce_products, events, news_articles,
    job_listings, profile_contacts, ...). Niente filtri "no email": ogni riga estratta
    diventa un asset, con tag derivati dichiarativamente da `derive_tags`.

    **Validation di completezza**: se i campi-chiave del template sono tutti null/empty
    (es. raw_json di una pagina indice processata erroneamente), la riga viene scartata
    invece di finire in DB come asset-fantasma. Per `generic` (template ignoto) si tiene
    tutto.
    """
    import json as _json
    from urllib.parse import urlparse

    from .asset_tags import derive_tags, derive_title

    if not jsonl_path.exists():
        return 0

    asset_type = (extraction_template or "generic").strip() or "generic"
    n_ingested = 0
    n_skipped_invalid = 0
    n_skipped_empty = 0

    try:
        with jsonl_path.open(encoding="utf-8") as f:
            for raw in f:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    obj = _json.loads(raw)
                except _json.JSONDecodeError:
                    n_skipped_invalid += 1
                    continue
                if not isinstance(obj, dict):
                    n_skipped_invalid += 1
                    continue

                if not _has_minimal_data_for(asset_type, obj):
                    n_skipped_empty += 1
                    continue

                source_url = obj.get("url") or obj.get("source_url")
                source_domain = obj.get("source_domain")
                if not source_domain and source_url:
                    try:
                        source_domain = (urlparse(source_url).hostname or "").lower() or None
                    except Exception:
                        source_domain = None

                tags = derive_tags(asset_type, obj)
                title = derive_title(asset_type, obj)

                db.upsert_asset(
                    {
                        "asset_type": asset_type,
                        "source_task_id": task_id,
                        "source_job_id": job_id,
                        "source_url": source_url,
                        "source_domain": source_domain,
                        "title": title,
                        "raw_json": raw,
                    },
                    tags=tags,
                )
                n_ingested += 1
    except Exception as e:
        jlog(f"  ⚠️ errore durante ingest assets: {type(e).__name__}: {e}")
        return n_ingested

    if n_ingested:
        jlog(
            f"  💾 ingest DB: {n_ingested} asset di tipo '{asset_type}' "
            f"(tabella `assets`, status='new')"
        )
    if n_skipped_empty:
        jlog(
            f"  ⏭️ {n_skipped_empty} righe scartate (campi-chiave del template "
            f"'{asset_type}' tutti vuoti: probabilmente pagine indice processate "
            f"per errore dal pattern del crawler)"
        )
    if n_skipped_invalid:
        jlog(f"  ⏭️ {n_skipped_invalid} righe scartate (JSON invalido)")
    return n_ingested


def _is_truthy(v: Any) -> bool:
    """Considera 'vuoto' None, stringhe vuote/whitespace, liste/dict vuoti, 0/False."""
    if v is None:
        return False
    if isinstance(v, str):
        return bool(v.strip())
    if isinstance(v, (list, dict, tuple)):
        return bool(v)
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return v != 0
    return True


def _has_minimal_data_for(asset_type: str, obj: dict) -> bool:
    """Ritorna True se l'oggetto ha i campi minimi popolati per essere un asset
    'reale' del template, altrimenti False (probabilmente pagina indice processata).

    Per `generic` (no template) si tiene sempre.
    """
    if asset_type == "real_estate":
        # Almeno uno dei campi numerici fondamentali: prezzo, mq, locali.
        return any(
            _is_truthy(obj.get(k))
            for k in ("prezzo_eur", "metri_quadri", "locali", "indirizzo", "agenzia")
        )
    if asset_type == "ecommerce_products":
        return _is_truthy(obj.get("name")) or _is_truthy(obj.get("price_amount"))
    if asset_type == "events":
        return _is_truthy(obj.get("title")) or _is_truthy(obj.get("start_datetime"))
    if asset_type == "news_articles":
        return _is_truthy(obj.get("title")) and (
            _is_truthy(obj.get("author"))
            or _is_truthy(obj.get("published_at"))
            or _is_truthy(obj.get("summary"))
        )
    if asset_type == "job_listings":
        return _is_truthy(obj.get("title")) and (
            _is_truthy(obj.get("company")) or _is_truthy(obj.get("apply_url"))
        )
    if asset_type == "profile_contacts":
        # Per lead-gen serve almeno UN contatto reale, NON solo il nome.
        # Display_name/username sono identificatori utili ma senza contatto sono inutilizzabili.
        return any(
            _is_truthy(obj.get(k))
            for k in ("email", "whatsapp", "telegram", "sitoweb", "social")
        )
    return True


def _extract_json_dicts(text: str) -> list[dict]:
    """Estrae tutti i dict JSON validi da una stringa.

    Strategie progressive:
    1. La stringa intera è già un JSON (dict o lista di dict)
    2. La stringa contiene blocchi ```json ... ```
    3. Scan greedy per oggetti `{...}` ben bilanciati nel testo
    """
    import json as _json

    if not text or not isinstance(text, str):
        return []
    text = text.strip()

    # 1. JSON puro
    try:
        obj = _json.loads(text)
        if isinstance(obj, dict):
            return [obj]
        if isinstance(obj, list):
            return [x for x in obj if isinstance(x, dict)]
    except _json.JSONDecodeError:
        pass

    # 2. Blocchi markdown ```json ... ```
    out: list[dict] = []
    for blk in re.findall(r"```(?:json|JSON)?\s*([\s\S]*?)```", text):
        try:
            obj = _json.loads(blk.strip())
            if isinstance(obj, dict):
                out.append(obj)
            elif isinstance(obj, list):
                out.extend(x for x in obj if isinstance(x, dict))
        except _json.JSONDecodeError:
            continue
    if out:
        return out

    # 3. Scan greedy per oggetti `{...}` bilanciati
    i, n = 0, len(text)
    while i < n:
        if text[i] == "{":
            depth = 0
            start = i
            in_str = False
            escape = False
            while i < n:
                c = text[i]
                if escape:
                    escape = False
                elif c == "\\" and in_str:
                    escape = True
                elif c == '"' and not escape:
                    in_str = not in_str
                elif not in_str:
                    if c == "{":
                        depth += 1
                    elif c == "}":
                        depth -= 1
                        if depth == 0:
                            try:
                                obj = _json.loads(text[start : i + 1])
                                if isinstance(obj, dict):
                                    out.append(obj)
                            except _json.JSONDecodeError:
                                pass
                            i += 1
                            break
                i += 1
        else:
            i += 1
    # Fallback markdown: se nessun JSON valido trovato, prova a parsare
    # il testo come profilo in markdown "**Field:** value" (browser_use spesso
    # estrae cosi' invece di JSON puro, perdendo i dati senza questo fallback).
    if not out:
        md_dict = _parse_markdown_profile(text)
        if md_dict:
            out.append(md_dict)
    return out


# Regex per ri-parsare profili che browser_use ha estratto in markdown libero
# anziche' come JSON. Esempi di pattern:
#   **Age:** 26
#   - **Hair Color:** Blonde
#   Nationality: American
# Strategia: matcha "<bullet?> <stars?> <KEY> <stars?>: <VALUE>" su singola riga.
# [^*:\n] evita di sconfinare in altri token (asterischi, colon, newline).
_MD_FIELD_RE = re.compile(
    r"^[ \t]*[-*]?[ \t]*([A-Za-z][^:\n]{0,40})[ \t]*:[ \t]+([^\n]{1,300})$",
    re.MULTILINE,
)
# Asterischi markdown ("**") rimossi PRIMA del match (browser_use produce
# righe tipo "**Display Name:** Mia Melano" che renderebbero il regex fragile).
_MD_ASTERISK_STRIP = re.compile(r"\*\*")

_MD_FIELD_KEY_MAP: dict[str, str] = {
    "name": "display_name",
    "full name": "display_name",
    "real name": "display_name",
    "display name": "display_name",
    "username": "username",
    "user name": "username",
    "alias": "username",
    "handle": "username",
    "nickname": "username",
    "email": "email",
    "e-mail": "email",
    "telegram": "telegram",
    "telegram username": "telegram",
    "whatsapp": "whatsapp",
    "phone": "whatsapp",
    "telefono": "whatsapp",
    "instagram": "instagram",
    "facebook": "facebook",
    "twitter": "twitter",
    "tiktok": "tiktok",
    "onlyfans": "onlyfans",
    "website": "sitoweb",
    "sitoweb": "sitoweb",
    "site": "sitoweb",
    "url": "source_url",
    "profile url": "source_url",
    "source url": "source_url",
    "age": "age",
    "born": "born",
    "birthday": "born",
    "data di nascita": "born",
    "nationality": "nationality",
    "ethnicity": "ethnicity",
    "country": "country",
    "city": "city",
    "location": "city",
    "languages": "languages",
    "profession": "profession",
    "professions": "profession",
    "hair color": "hair_color",
    "eye color": "eye_color",
    "height": "height",
    "weight": "weight",
    "measurements": "measurements",
}


def _parse_markdown_profile(text: str) -> dict | None:
    """Parsea testo markdown con campi 'Key: value' o '**Key:** value' in un dict.

    Usato quando browser_use estrae profili come testo libero invece che JSON.
    Ritorna None se trova meno di 3 campi (dati troppo sparsi per essere utili).
    """
    if not text:
        return None
    # Rimuovi asterischi markdown per semplificare il regex
    text_norm = _MD_ASTERISK_STRIP.sub("", text)
    out: dict[str, Any] = {}
    socials: list[dict[str, str]] = []
    for m in _MD_FIELD_RE.finditer(text_norm):
        raw_key = m.group(1).strip().lower()
        raw_val = m.group(2).strip()
        if not raw_val or raw_val.lower() in ("n/a", "none", "null", "-", "--"):
            continue
        # Tronca valori troppo lunghi (header markdown probabilmente)
        raw_val = raw_val[:500]
        # Mappa al nostro schema
        key = _MD_FIELD_KEY_MAP.get(raw_key)
        if not key:
            # Heuristic: se il valore inizia con "http", trattalo come social
            if raw_val.lower().startswith(("http://", "https://", "www.")):
                socials.append({"platform": raw_key, "url": raw_val})
            continue
        # Social platforms: aggrega in lista invece di chiave singola
        if key in ("instagram", "facebook", "twitter", "tiktok", "onlyfans"):
            socials.append({"platform": key, "url": raw_val})
            continue
        # Skip se gia' presente (manteniamo il primo)
        if key not in out:
            out[key] = raw_val
    if socials:
        out["social"] = socials
    if len(out) < 3:
        return None
    return out


def _collect_extracted_jsonl(history: Any, sub_dir: Path, jlog: Callable) -> int:
    """Recupera i dati estratti dall'history dell'agente e li scrive in sub_dir/profiles.jsonl.

    Browser-use ha un'action `extract` che mette i risultati in `history.extracted_content()`.
    Senza questa funzione i dati sarebbero persi quando il job termina (vivono solo in memoria).
    """
    import json as _json

    profiles_path = sub_dir / "profiles.jsonl"

    contents: list[str] = []
    try:
        ec = history.extracted_content() or []
        contents.extend(str(x) for x in ec if x)
    except Exception as e:
        jlog(f"  history.extracted_content non disponibile: {e}")

    # Anche final_result può contenere il report finale strutturato
    try:
        final = history.final_result()
        if final and isinstance(final, str):
            contents.append(final)
    except Exception:
        pass

    if not contents:
        jlog("  ⚠️ history vuota: l'agente non ha eseguito 'extract' actions o un done() con dati.")
        return 0

    jlog(f"  history: {len(contents)} blocchi di contenuto da analizzare")

    # Dedup: l'agente browser_use può rivisitare uno stesso profilo più volte
    # (loop, "Add to favorites" che torna alla pagina, ecc.). Scartiamo le righe
    # con source_url/url già visto in QUESTO sub_dir (carichiamo gli URL già scritti).
    seen_urls: set[str] = set()
    if profiles_path.exists():
        for line in profiles_path.read_text(encoding="utf-8").splitlines():
            try:
                prev = _json.loads(line)
                u = prev.get("source_url") or prev.get("url")
                if isinstance(u, str) and u:
                    seen_urls.add(u.strip().rstrip("/"))
            except _json.JSONDecodeError:
                continue

    n = 0
    n_dups = 0
    with profiles_path.open("a", encoding="utf-8") as f:
        for text in contents:
            for obj in _extract_json_dicts(text):
                # filtra dict troppo poveri (meno di 2 chiavi non significative)
                if len(obj) < 2:
                    continue
                u = obj.get("source_url") or obj.get("url")
                if isinstance(u, str) and u:
                    key = u.strip().rstrip("/")
                    if key in seen_urls:
                        n_dups += 1
                        continue
                    seen_urls.add(key)
                f.write(_json.dumps(obj, ensure_ascii=False) + "\n")
                n += 1

    if n_dups:
        jlog(f"  🔁 dedup: scartati {n_dups} profili duplicati (stesso source_url/url)")

    if n:
        jlog(f"  ✅ scritte {n} righe in {profiles_path.name} dall'history dell'agente")
    else:
        jlog(
            f"  ⚠️ history aveva contenuto ({len(contents)} blocchi) ma nessun JSON dict estratto. "
            "Probabilmente il modello non ha prodotto dati strutturati con l'action `extract`."
        )
    return n


def _maybe_collect_agent_files(agent: Any, run_dir: Path, jlog: Callable) -> None:
    candidates: list[Path] = []
    fs = getattr(agent, "file_system", None)
    for attr in ("base_dir", "data_dir", "path", "root", "_root"):
        p = getattr(fs, attr, None) if fs is not None else None
        if isinstance(p, (str, Path)):
            candidates.append(Path(p))
    for attr in ("file_system_path", "files_path", "agent_data_dir"):
        p = getattr(agent, attr, None)
        if isinstance(p, (str, Path)):
            candidates.append(Path(p))

    for src in candidates:
        try:
            src_resolved = src.resolve()
            dst_resolved = run_dir.resolve()
            if not src_resolved.exists() or src_resolved == dst_resolved:
                continue
            if src_resolved.is_dir():
                copied = 0
                for child in src_resolved.rglob("*"):
                    if child.is_file():
                        rel = child.relative_to(src_resolved)
                        target = run_dir / rel
                        target.parent.mkdir(parents=True, exist_ok=True)
                        try:
                            shutil.copy2(child, target)
                            copied += 1
                        except Exception:
                            pass
                if copied:
                    jlog(f"Copiati {copied} file dell'agente da {src_resolved}")
                return
        except Exception:
            continue


# ===========================================================================
# Stage 2 — Playbook write: cosa ha imparato browser_use sul sito?
# ===========================================================================

async def _write_site_playbook(
    *,
    seed_url: str,
    asset_type: str,
    n_assets: int,
    summaries_text: str,
    task: dict[str, Any],
    job_id: int,
    jlog: Callable,
) -> None:
    """Chiede al LLM un riassunto operativo di cosa ha imparato browser_use sul
    sito, da iniettare nei futuri run di site_explorer (HTTP-only) sullo stesso
    dominio. Salva in DB tramite db.upsert_site_playbook.

    L'output del LLM e' atteso come JSON con `playbook_text` e `transferable`.
    Su parsing fail: fallback a salvataggio del raw output con transferable=true.
    """
    import json as _json
    from urllib.parse import urlparse
    import httpx
    from .llm_providers import resolve_api_key, resolve_base_url
    from .runner_bulk_extract import _registrable_domain

    host = (urlparse(seed_url).hostname or "").lower()
    reg = _registrable_domain(host)
    if not reg:
        return

    provider = (task.get("browser_llm_provider") or task.get("llm_provider") or "ollama").strip()
    model = (task.get("browser_llm_model") or task.get("model") or "qwen3.5:latest").strip()
    try:
        base_url = resolve_base_url(provider, task.get("llm_base_url"))
        api_key = resolve_api_key(provider, task.get("browser_llm_api_key") or task.get("llm_api_key"))
    except Exception as e:
        jlog(f"  ⚠️ Playbook write: impossibile risolvere LLM ({e}). Skip.")
        return

    sys_prompt = (
        "Sei un'AI che scrive 'playbook' operativi per altri agenti. Hai appena "
        "completato un'estrazione su un sito web con un agente browser-based. "
        "Ora devi scrivere ISTRUZIONI BREVI per un agente HTTP-only "
        "(fetch + readability + LLM extract, NIENTE browser, NIENTE click) che "
        "estraga gli stessi dati sullo stesso sito.\n\n"
        "Output: SOLO un JSON con questi campi (niente prosa fuori dal JSON):\n"
        "{\n"
        '  "playbook_text": "<istruzioni operative in italiano, 5-10 righe max>",\n'
        '  "transferable": true | false,\n'
        '  "blockers": [<lista di motivi che bloccherebbero un agente HTTP-only, '
        'es. \"captcha\", \"login richiesto\", \"contenuti JS-render\">]\n'
        "}\n\n"
        "Regole:\n"
        "- transferable=false se il sito richiede click/scroll/login per vedere i dati: "
        "in quel caso i blockers spiegano perche'.\n"
        "- transferable=true se i dati sono nel HTML statico: il playbook spiega DOVE "
        "(URL pattern, sub-paths utili) e COSA evitare (footer, gating).\n"
        "- Sii concreto: usa pattern URL veri (es. '/<slug>/contatti/'), non frasi vaghe.\n"
        "- Lingua: italiano.\n"
    )
    user_prompt = (
        f"Sito: {reg}\n"
        f"Asset type: {asset_type}\n"
        f"Asset estratti con successo (browser_use): {n_assets}\n\n"
        f"Cosa ho fatto durante l'estrazione (sintesi per-seed):\n{summaries_text}\n\n"
        f"Scrivi ora il JSON del playbook."
    )

    payload: dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "system", "content": sys_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0.2,
        "max_tokens": 600,
        "response_format": {"type": "json_object"},
    }
    maybe_add_keep_alive(payload, base_url)
    api_url = f"{base_url.rstrip('/')}/chat/completions"
    headers = {"Authorization": f"Bearer {api_key}"}

    try:
        async with httpx.AsyncClient(timeout=120) as client:
            r = await client.post(api_url, json=payload, headers=headers)
            r.raise_for_status()
            data = r.json()
        raw = (data.get("choices", [{}])[0].get("message", {}).get("content") or "").strip()
        # strip <think> tags Qwen3
        cleaned = re.sub(r"<think>[\s\S]*?</think>", "", raw, flags=re.IGNORECASE).strip()
        try:
            obj = _json.loads(cleaned)
        except Exception:
            # Fallback: salva raw come playbook_text, transferable=true
            obj = {"playbook_text": cleaned[:2000], "transferable": True, "blockers": []}
    except Exception as e:
        jlog(f"  ⚠️ Playbook write: chiamata LLM fallita ({type(e).__name__}: {e}). Skip.")
        return

    playbook_text = (obj.get("playbook_text") or "").strip()
    transferable = bool(obj.get("transferable", True))
    blockers = obj.get("blockers") or []
    if not playbook_text:
        jlog("  ⚠️ Playbook write: LLM non ha emesso playbook_text. Skip.")
        return

    # Serializza il playbook con metadati come JSON-string nel campo `playbook`
    full = _json.dumps(
        {
            "text": playbook_text,
            "transferable": transferable,
            "blockers": blockers,
            "asset_count_at_creation": n_assets,
        },
        ensure_ascii=False,
    )
    pb_id = db.upsert_site_playbook(
        registrable_domain=reg,
        asset_type=asset_type,
        playbook=full,
        source_runner="browser_use",
        source_job_id=job_id,
        transferable=transferable,
    )
    jlog(
        f"  📚 Playbook salvato (id={pb_id}) per {reg} / {asset_type} — "
        f"transferable={transferable}, blockers={blockers}"
    )
