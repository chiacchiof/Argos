"""Master summary: aggrega i report dei sub-job di un parent auto_extract e
genera un riassunto LLM unico per l'utente.

Trigger: alla fine di `runner_auto_extract.run_agent`, dentro un try/finally
che cattura anche `done`, `error`, `cancelled`. Cosi' l'utente vede SEMPRE un
overview di "com'e' andato il task", anche quando e' fallito.

Output: `master_summary.md` salvato nella run_dir del parent job. Visibile dalla
UI tramite la card del parent (`partials/job_dashboard.html`).

Modello LLM: eredita dal main del task (stesso provider/model). Per task con
qwen3-coder:30b locale, il summary e' gratis. Per task cloud (gpt-4o-mini)
costa ~$0.001 per summary.

Sicurezza tenant: la funzione legge il parent job + sub via `db.list_subjobs()`
che e' gia' tenant-filtered. Niente leak cross-tenant nel summary.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from .. import db
from .llm_providers import resolve_api_key, resolve_base_url
from .ollama import chat_openai_compat


log = logging.getLogger(__name__)

# Limit sui contenuti che mettiamo nel prompt LLM. Senza limite, un task su
# 100 siti satura il context e produce summary scadenti.
_MAX_REPORT_CHARS = 1500   # singolo report (parent o sub)
_MAX_LOG_TAIL_LINES = 30   # tail del job log


def _read_report(run_dir: Path) -> str:
    """Legge `report.md` dalla run_dir, troncato a max chars. Vuoto se mancante."""
    if not run_dir or not run_dir.exists():
        return ""
    report = run_dir / "report.md"
    if not report.exists():
        return ""
    try:
        text = report.read_text(encoding="utf-8", errors="replace").strip()
    except Exception:
        return ""
    if len(text) > _MAX_REPORT_CHARS:
        text = text[:_MAX_REPORT_CHARS] + "\n...[truncated]"
    return text


def _read_log_tail(job: dict[str, Any]) -> str:
    """Tail delle ultime N righe del job log dal DB."""
    log_blob = job.get("log") or ""
    if not log_blob:
        return ""
    lines = log_blob.splitlines()
    if len(lines) > _MAX_LOG_TAIL_LINES:
        lines = lines[-_MAX_LOG_TAIL_LINES:]
    return "\n".join(lines)


def _count_jsonl_lines(p: Path) -> int:
    if not p or not p.exists():
        return 0
    try:
        return sum(1 for ln in p.read_text(encoding="utf-8", errors="replace").splitlines() if ln.strip())
    except Exception:
        return 0


def _parent_run_dir(parent_job: dict[str, Any]) -> Path | None:
    """Risolve la run_dir del parent dal `result_path` (se valorizzato)."""
    rp = parent_job.get("result_path")
    if not rp:
        return None
    p = Path(rp)
    return p if p.is_dir() else p.parent


def _build_prompt(
    task: dict[str, Any],
    parent_job: dict[str, Any],
    sub_jobs: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Costruisce il prompt LLM per il summary aggregato."""
    parent_run_dir = _parent_run_dir(parent_job)
    parent_report = _read_report(parent_run_dir) if parent_run_dir else ""
    parent_log_tail = _read_log_tail(parent_job)
    n_total_profiles = (
        _count_jsonl_lines(parent_run_dir / "profiles.jsonl")
        if parent_run_dir else 0
    )

    sub_blocks: list[str] = []
    for idx, sj in enumerate(sub_jobs, start=1):
        display_id = f"#{parent_job['id']}.{idx}"
        sub_rp = sj.get("result_path") or ""
        sub_dir = Path(sub_rp).parent if sub_rp else None
        sub_report = _read_report(sub_dir) if sub_dir else ""
        sub_log_tail = _read_log_tail(sj)
        sub_n = _count_jsonl_lines(sub_dir / "profiles.jsonl") if sub_dir else 0
        sub_blocks.append(
            f"### Sub-job {display_id} (DB id={sj['id']}, status={sj.get('status')})\n"
            f"- profili estratti: {sub_n}\n"
            f"- error: {sj.get('error') or '—'}\n"
            f"- report.md:\n```\n{sub_report or '(no report)'}\n```\n"
            f"- ultime righe log:\n```\n{sub_log_tail or '(no log)'}\n```\n"
        )

    system = (
        "Sei un assistente che analizza l'esito di un task agentico multi-step "
        "di scraping/estrazione dati di Argos. Ricevi: la configurazione del task, "
        "il report del job principale (auto_extract orchestrator), e i report di "
        "ogni sub-job (un sub per sito, ognuno con la sua strategia: "
        "site_explorer / browser_use / bulk_extract).\n\n"
        "L'output di questo summary viene letto dall'orchestrator chat di Argos "
        "per spiegare all'utente cosa e' successo e suggerire mitigazioni "
        "specifiche. Quindi DEVE essere strutturato in sezioni chiare e contenere "
        "informazioni AZIONABILI, non solo descrittive.\n\n"
        "═══════════════════════════════════════════════════════════════\n"
        "CATALOGO DEI PATTERN PROBLEMATICI NOTI (usa per riconoscere e diagnosticare)\n"
        "═══════════════════════════════════════════════════════════════\n\n"
        "**P1 — `extract_structured_data failed` loop**\n"
        "  Sintomo: log contiene 'extract_structured_data call failed' ripetuto 2-3+ "
        "volte sullo stesso sito. Spesso seguito da 'TIMEOUT browser-use bloccato'.\n"
        "  Causa: il modello LLM (di solito qwen/llama locali) non genera JSON "
        "strict che browser-use library riesce a parsare. Il modello e' capable "
        "ma non strict-mode.\n"
        "  Fix utente: aggiungi un Browser LLM cloud capable (gpt-4o-mini "
        "raccomandato, ~$0.001/step) sul task. Vai a /accounts/llm-keys per "
        "aggiungere la chiave, poi sul task slot 'Browser LLM' scegli gpt-4o-mini.\n\n"
        "**P2 — HTTP 403 / 401 / 429 ripetuti**\n"
        "  Sintomo: 'FAIL: HTTP 403' su tutti gli URL del sito, anche dopo retry.\n"
        "  Causa: il sito blocca scrapers via User-Agent blacklist, IP rate-limit, "
        "geo-blocking, o WAF (Cloudflare entry-level).\n"
        "  Fix utente: (1) verifica che HTTP_USER_AGENT in .env sia 'Mozilla/5.0 ...' "
        "(default da 2026-05-23), non 'Argos/0.1'; (2) se gia' browser-like, passa "
        "la modalita' del task a browser_use (Playwright reale bypassa 403 base); "
        "(3) se neanche browser_use funziona, il sito ha anti-bot avanzato "
        "(Cloudflare Turnstile, DataDome) — target non realizzabile senza proxy "
        "residenziali + headed browser.\n\n"
        "**P3 — Cloudflare Turnstile / DataDome challenge**\n"
        "  Sintomo: log contiene 'Cloudflare', 'verification', 'challenge' o "
        "'_cf_turnstile'. Spesso anche errori 'Just a moment...'.\n"
        "  Causa: anti-bot ML-based, browser headless detectabile.\n"
        "  Fix utente: serve browser headed (visibile) con stealth maxed, o "
        "considerare un servizio anti-bot dedicato. Non risolvibile lato Argos "
        "senza setup specifico.\n\n"
        "**P4 — Memory stuck / LLM loop**\n"
        "  Sintomo: log contiene 'Memory dell'agente identica per N step' o "
        "'early-stop'. Il modello continua a dire 'Partial Success — need to retry' "
        "ma non chiama mai il tool finale.\n"
        "  Causa: modello sotto-dimensionato per tool-calling agentico complesso "
        "(tipicamente <20B parametri, o chat-tuned invece di code-tuned).\n"
        "  Fix utente: cambia il modello a qwen3-coder:30b (locale, free, "
        "code-tuned) o cloud capable (gpt-4o-mini, claude-haiku-4-5). Per "
        "browser_use serve frontier (vedi P1).\n\n"
        "**P5 — Login wall / no profili pubblici**\n"
        "  Sintomo: l'agente naviga il sito, accetta cookie, esplora forum, "
        "ma 'non ho trovato link a profili' o 'profili richiedono login'.\n"
        "  Causa: il target richiede autenticazione per esporre i dati.\n"
        "  Fix utente: target NON realizzabile via scraping pubblico. Considera "
        "(a) sostituire con un sito alternativo che espone profili pubblici, "
        "(b) escludere il sito dal task.\n\n"
        "**P6 — TIMEOUT senza profili (max_iterations raggiunto)**\n"
        "  Sintomo: 'TIMEOUT dopo N secondi (max_steps=N)' senza nessun extract.\n"
        "  Causa: di solito combo P1 + P4 (LLM in loop + extract fails) oppure "
        "sito molto profondo (servirebbero piu' step LLM).\n"
        "  Fix utente: prima diagnostica se P1 o P4 (vedi sopra). Se ne' P1 ne' "
        "P4, alza max_iterations del task da 30 a 60-100.\n\n"
        "**P7 — Errore tecnico Argos (bug codice/setup)**\n"
        "  Sintomo: AttributeError, ImportError, 'chromium not found', "
        "'Playwright non installato', stack trace Python.\n"
        "  Causa: bug nostro o installazione rotta.\n"
        "  Fix utente: questo NON e' colpa tua. Segnala al maintainer Argos: "
        "copia il messaggio di errore esatto.\n\n"
        "**P8 — Sito non scrapabile per design (es. solo SPA con dati lato server)**\n"
        "  Sintomo: HTML della pagina non contiene i dati visibili a video, "
        "tutto e' caricato via fetch JSON post-render.\n"
        "  Causa: SPA pesante (React/Vue) con dati in API non documentate.\n"
        "  Fix utente: prova browser_use mode (rendering full JS), oppure "
        "intercetta le chiamate API del sito e usa bulk_extract con quegli "
        "endpoint diretti.\n\n"
        "═══════════════════════════════════════════════════════════════\n"
        "FORMATO OBBLIGATORIO DELL'OUTPUT (markdown, sezioni esatte)\n"
        "═══════════════════════════════════════════════════════════════\n\n"
        "## 🎯 Esito globale\n"
        "Una riga: status (success / partial / failed) + N profili totali estratti "
        "+ N siti processati su N totali.\n\n"
        "## 📍 Per sito\n"
        "Una bullet per sito, formato `- **<host>**: <strategia usata> → "
        "<n profili>. <1 frase di cosa e' andato>`.\n\n"
        "## 🔍 Pattern problematici rilevati\n"
        "Per ogni pattern del catalogo riconosciuto, una bullet:\n"
        "- **[Codice pattern es. P1]** <nome breve>\n"
        "  - Sintomo osservato in questo run: <citazione log o 1 riga>\n"
        "  - Causa: <riga dal catalogo>\n"
        "  - **Fix consigliato**: <riga dal catalogo, adattata al contesto>\n\n"
        "## 💡 Raccomandazioni concrete per il prossimo run\n"
        "Lista ordinata per priorita' (alta → bassa), una per riga, AZIONABILE "
        "(es. 'Cambia main LLM da X a Y in /tasks/N/edit', non 'considera un "
        "modello migliore').\n\n"
        "═══════════════════════════════════════════════════════════════\n\n"
        "REGOLE:\n"
        "- Scrivi in italiano, tecnico ma comprensibile a un non-developer.\n"
        "- Se NON rilevi pattern problematici (task andato bene), scrivi nella "
        "sezione 'Pattern problematici rilevati': '_Nessun pattern noto. Run "
        "pulito._' e nella sezione 'Raccomandazioni': '_Nessuna azione "
        "richiesta._'.\n"
        "- Non inventare codici pattern non in catalogo (P1-P8). Se vedi un "
        "errore nuovo, mettilo come 'P? — <nome>' con sintomo/causa/fix dedotti.\n"
        "- NON ripetere i dati grezzi (numeri di step, timestamps, ecc.): "
        "sintetizza."
    )

    task_brief = (
        f"## Task #{task['id']} '{task.get('name')}'\n"
        f"- agent_mode: {task.get('agent_mode')}\n"
        f"- objective: {task.get('objective') or '—'}\n"
        f"- seed_queries: {task.get('seed_queries') or '—'}\n"
        f"- main LLM: {task.get('llm_provider')}/{task.get('model')}\n"
        f"- discovery LLM: {task.get('discovery_llm_provider')}/{task.get('discovery_llm_model')}\n"
        f"- browser LLM: {task.get('browser_llm_provider')}/{task.get('browser_llm_model')}\n"
    )

    parent_block = (
        f"## Parent job #{parent_job['id']} (status={parent_job.get('status')})\n"
        f"- profili totali aggregati: {n_total_profiles}\n"
        f"- error: {parent_job.get('error') or '—'}\n"
        f"- report.md (parent):\n```\n{parent_report or '(no report)'}\n```\n"
        f"- ultime righe log parent:\n```\n{parent_log_tail or '(no log)'}\n```\n"
    )

    user = task_brief + "\n" + parent_block + "\n" + "\n".join(sub_blocks)
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


async def generate_master_summary(parent_job_id: int) -> str | None:
    """Genera (e salva) il master_summary.md per un parent job.

    Ritorna il path del file (str) o None se non generabile (parent senza
    sub-job, LLM error, ecc.). Idempotente: se il file esiste gia' lo
    sovrascrive (= rigenerazione voluta dopo update dei sub).

    Catch-all sugli errori: se LLM crashia o il salvataggio fallisce, ritorna
    None ma NON solleva — il task chiamante non deve fallire per questo.
    """
    try:
        parent_job = db.get_job(parent_job_id)
        if not parent_job:
            log.warning("master_summary: parent #%s non trovato", parent_job_id)
            return None

        sub_jobs = db.list_subjobs(parent_job_id)
        if not sub_jobs:
            # Niente sub → nulla da aggregare. Lasciamo il report.md del parent
            # come unica fonte (non duplichiamo informazione).
            log.info("master_summary: parent #%s senza sub, skip.", parent_job_id)
            return None

        task = db.get_task(int(parent_job["task_id"]))
        if not task:
            return None

        parent_run_dir = _parent_run_dir(parent_job)
        if not parent_run_dir or not parent_run_dir.exists():
            log.info(
                "master_summary: parent #%s senza run_dir, skip.", parent_job_id
            )
            return None

        # Risolvi provider/model dal main del task (stesso pattern di
        # _resolve_profiler_llm: se vuoi qualita' migliore, in futuro
        # introduci summary_llm_* dedicati).
        provider_key = (task.get("llm_provider") or "ollama").strip()
        model = task.get("model") or "qwen3-coder:30b"
        base_url = resolve_base_url(provider_key, task.get("llm_base_url"))
        api_key = resolve_api_key(provider_key, task.get("llm_api_key"))

        messages = _build_prompt(task, parent_job, sub_jobs)

        try:
            msg = await chat_openai_compat(
                base_url=base_url,
                api_key=api_key,
                model=model,
                messages=messages,
                temperature=0.3,
            )
        except Exception as e:
            log.warning("master_summary: LLM error per parent #%s: %s", parent_job_id, e)
            return None

        content = (msg.get("content") or "").strip()
        if not content:
            log.warning("master_summary: LLM ha risposto vuoto per parent #%s", parent_job_id)
            return None

        # Header con metadata per audit
        header = (
            f"# Master summary — job #{parent_job_id}\n\n"
            f"_Generato automaticamente al termine del job. "
            f"Modello: {provider_key}/{model}. "
            f"Sub-job analizzati: {len(sub_jobs)}._\n\n"
            "---\n\n"
        )

        out_path = parent_run_dir / "master_summary.md"
        out_path.write_text(header + content + "\n", encoding="utf-8")
        log.info(
            "master_summary: salvato %s (%d sub-job analizzati)",
            out_path, len(sub_jobs),
        )
        return str(out_path)
    except Exception as e:
        log.exception("master_summary: errore generico per parent #%s: %s", parent_job_id, e)
        return None
