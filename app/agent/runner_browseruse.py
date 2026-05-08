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

STRATEGIA OPERATIVA (per ogni seed URL):
1. Apri la pagina di partenza. Gestisci cookie banner / verifica età cliccando il bottone di \
   conferma ("Accetta", "OK", "SONO MAGGIORENNE", "Continue", "I agree", ecc.).
2. Se è una landing/listing: identifica i link che portano alle pagine di dettaglio individuali \
   secondo i criteri dello SCHEMA DI ESTRAZIONE (vedi sotto).
3. Per ogni link che SEMBRA essere una pagina valida, aprilo (anche in nuove tab è OK).
4. Verifica che soddisfi i criteri dello schema.
5. SE LA PAGINA È VALIDA → chiama l'action `extract` (extract_structured_data) descrivendo \
   precisamente i campi che vuoi estrarre secondo lo SCHEMA fornito sotto. L'action ritorna \
   un JSON strutturato che viene memorizzato per te.
   IMPORTANTE: ogni chiamata a `extract` deve produrre UN oggetto JSON che corrisponda allo \
   schema (con i campi richiesti, NULL se assenti). Non riassumere a parole, USA L'ACTION.
6. Torna alla lista, vai alla prossima pagina. Salta duplicati / categorie / pagine non valide.
7. Continua finché ci sono step disponibili o non trovi più pagine nuove.

VINCOLI:
- NON fare login, registrazione, form-fill, checkout, pagamenti.
- NON inventare dati: se un campo non è in pagina, metti null. Mai allucinare valori.
- Rispetta whitelist/blacklist domini se specificate dall'utente.
- Se la pagina richiede autenticazione o ha anti-bot pesante, NON insistere: scrivi nel summary \
  "skipped: <motivo>" e passa avanti.

OUTPUT FINALE (con l'action `done`):
Un riepilogo MARKDOWN BREVE (max 30 righe) con:
- numero di pagine estratte in questa sessione (= numero di chiamate `extract` eseguite con successo)
- elenco dei domini coperti con conteggio per dominio
- problemi incontrati (anti-bot, JS-only, login richiesto, struttura imprevista)
- NIENTE ridondanza dei dati estratti: sono già stati salvati tramite l'action `extract`.
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

    try:
        return await _run_agent_inner(task, job_id, jlog)
    finally:
        bu_logger.removeHandler(handler)
        if prev_level != logging.NOTSET:
            bu_logger.setLevel(prev_level)


async def _run_agent_inner(task: dict[str, Any], job_id: int, jlog: Callable) -> str:
    db.update_job(job_id, status="running", started_at=db.now_iso())
    db.set_control_signal(job_id, None)
    jlog(
        f"Avvio browser-use per task #{task['id']} \"{task['name']}\""
    )

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
    browser_provider = (task.get("browser_llm_provider") or "").strip()
    browser_model = (task.get("browser_llm_model") or "").strip()
    if browser_provider and browser_model:
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
    try:
        llm = ChatOpenAI(
            model=model_name,
            base_url=base_url,
            api_key=api_key,
            temperature=0.2,
        )
    except TypeError:
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
        agent = _make_agent(Agent, task_text, llm, sub_dir, jlog)

        history_obj = None
        try:
            history_obj = await agent.run(max_steps=max_steps)
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
    n_ingested = _ingest_to_contacts(consolidated_jsonl, task["id"], job_id, jlog)

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


def _make_agent(Agent, task_text: str, llm: Any, run_dir: Path, jlog: Callable) -> Any:
    """Inizializza Agent provando i nomi alternativi del kwarg per il file_system_path."""
    base = {"task": task_text, "llm": llm}
    for kw in ("file_system_path", "files_path", "agent_data_dir"):
        try:
            return Agent(**base, **{kw: str(run_dir)})
        except TypeError:
            continue
    return Agent(**base)


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


def _ingest_to_contacts(jsonl_path: Path, task_id: int, job_id: int, jlog: Callable) -> int:
    """Legge profiles.jsonl consolidato e fa upsert su `contacts` con status='new'.

    Idempotente: se un contatto (matching email o telegram_username) esiste già,
    aggiorna i campi descrittivi MA preserva lo `status` corrente — così se è già
    'qualified'/'contacted'/'optedout' non torna indietro a 'new'.

    Filtra righe che non hanno né email né telegram (non sono contattabili).
    """
    import json as _json
    from urllib.parse import urlparse

    if not jsonl_path.exists():
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

                db.upsert_contact({
                    "source_task_id": task_id,
                    "source_job_id": job_id,
                    "source_url": source_url,
                    "source_domain": source_domain,
                    "display_name": (
                        obj.get("display_name")
                        or obj.get("username")
                        or obj.get("nickname")
                    ),
                    "email": email,
                    "telegram_username": tg,
                    "raw_json": raw,
                    # status NON viene mai degradato dall'upsert: per nuovi record
                    # parte da 'new', per esistenti viene preservato.
                })
                n_ingested += 1
    except Exception as e:
        jlog(f"  ⚠️ errore durante ingest in DB: {type(e).__name__}: {e}")
        return n_ingested

    if n_ingested:
        jlog(
            f"  💾 ingest DB: {n_ingested} contatti su tabella `contacts` "
            f"(status='new' per i nuovi, preservato per già esistenti)"
        )
    if n_skipped_no_contact:
        jlog(f"  ⏭️ {n_skipped_no_contact} righe scartate (no email/telegram)")
    if n_skipped_invalid:
        jlog(f"  ⏭️ {n_skipped_invalid} righe scartate (JSON invalido)")
    return n_ingested


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
