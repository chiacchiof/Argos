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
from .llm_providers import resolve_credential
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
        "  Causa: il sito blocca scrapers via IP rate-limit, geo-blocking, o "
        "WAF (Cloudflare entry-level). Lo User-Agent di Argos e' gia' "
        "browser-like (Chrome 120) dal 2026-05-23 — NON suggerire di "
        "cambiarlo.\n"
        "  Fix utente: (1) se il task e' su `auto_extract` o `bulk_extract`, "
        "il fallback browser_use Playwright spesso bypassa 403 base — "
        "verifica che sia abilitato; (2) se neanche browser_use funziona, il "
        "sito ha anti-bot avanzato (vedi P3) — target non realizzabile senza "
        "proxy residenziali + headed browser.\n\n"
        "**P3 — Anti-bot avanzato (Cloudflare/DataDome) o DOM vuoto al browser**\n"
        "  Sintomo (1): log contiene 'Cloudflare', 'verification', 'challenge' "
        "o '_cf_turnstile'. Spesso anche errori 'Just a moment...'.\n"
        "  Sintomo (2 — variante silenziosa, piu' frequente): nel log del "
        "browser_use compaiono ripetutamente 'The page is currently empty', "
        "'encountered an empty DOM', 'no interactive elements available', "
        "'Page remained empty after waiting'. Il LLM continua a fare "
        "`scroll`/`wait` senza mai chiamare `extract_structured_data` perche' "
        "il DOM e' davvero vuoto (challenge bloccante). Spesso termina con "
        "TIMEOUT dopo 510s.\n"
        "  IMPORTANTE: NON confondere con P1. In P1 il DOM e' pieno e l'LLM "
        "non genera JSON strict; in P3 il DOM e' VUOTO e l'LLM non ha nulla "
        "da estrarre. Il fix di P1 (Browser LLM cloud) NON risolve P3.\n"
        "  Causa: anti-bot ML-based che rileva e blocca browser headless. "
        "Spesso si attiva dopo un primo run di scraping riuscito (il sito "
        "memorizza il fingerprint e blocca i tentativi successivi).\n"
        "  Fix utente: serve browser headed (visibile) con stealth maxed, "
        "proxy residenziali, o cambiare IP. Non risolvibile lato Argos senza "
        "setup specifico. NON suggerire 'cambia main LLM' o 'usa gpt-4o-mini' "
        "per P3 — sono no-op irrilevanti.\n\n"
        "**P9 — Sito non e' una directory di profili**\n"
        "  Sintomo: il profiler riconosce il sito come `promising=yes/maybe`, "
        "il site_explorer mappa con successo (fetch_page OK, link/pattern "
        "rilevati), ma 0 asset estratti perche' i link 'profilo' che trova "
        "puntano a pagine non-profilo (autori di articoli, sezioni staff con "
        "5-6 redattori, pagine /contattaci/, ecc.). Il template "
        "`profile_contacts` torna 'campi-chiave tutti vuoti'.\n"
        "  Causa: l'utente ha messo come seed un sito generico di settore "
        "(news, forum) sperando di trovare profili contattabili, ma il sito "
        "NON e' una directory esposta di membri. GDPR ha chiuso quel pattern "
        "su tutti i siti seri. Solo siti SPECIFICAMENTE pensati come "
        "directory pubbliche espongono email/social di utenti.\n"
        "  Fix utente: il problema NON e' la configurazione del task. E' la "
        "STRATEGIA. Devi cambiare i seed_queries: cercare directory verticali "
        "del settore (es. albo professionale, listing di settore con email "
        "obbligatorie), usare `recon_social` (Instagram/Facebook con keyword "
        "settoriale), oppure importare lead esistenti via CSV `/import` e "
        "qualificarli. Lo scraping cieco di portali/news/forum non porta "
        "lead utili.\n\n"
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
        "## 📍 Esito per sub-job\n"
        "UNA bullet per CIASCUN sub-job ricevuto, NELL'ORDINE in cui sono "
        "elencati nel prompt. Formato esatto:\n"
        "- **<display_id>** <host o nome sub-job> — <✅ OK | ⚠️ parziale | "
        "❌ fallito>: <strategia usata> → <n profili>. <1 frase di cosa e' "
        "andato bene o male, citando l'errore osservato>.\n"
        "Devi coprire TUTTI i sub-job: non ometterne nessuno.\n\n"
        "## 🔍 Pattern problematici rilevati\n"
        "Per ogni pattern del catalogo riconosciuto, una bullet:\n"
        "- **[Codice pattern es. P1]** <nome breve>\n"
        "  - Sintomo osservato in questo run: <citazione log o 1 riga>\n"
        "  - Sub-job affetti: <lista di display_id>\n"
        "  - Causa: <riga dal catalogo>\n"
        "  - **Fix consigliato**: <riga dal catalogo, adattata al contesto>\n\n"
        "## ⚙️ Tuning configurazione consigliato\n"
        "Concretamente: cosa cambierebbe l'esito del PROSSIMO run? Confronta "
        "la configurazione attuale (vedi blocco 'Task' nel prompt: main LLM, "
        "discovery LLM, browser LLM, agent_mode, max_iterations) con quello "
        "che i pattern rilevati suggeriscono.\n"
        "\n"
        "REGOLA TASSATIVA (no-op detection): se il `<valore attuale>` LETTO "
        "DAL BLOCCO TASK e' GIA' uguale al `<valore consigliato>`, NON "
        "elencare quella riga. Significa che lo slot e' gia' configurato "
        "bene, non serve cambiarlo. Esempi di errori da evitare:\n"
        "- Task ha `browser LLM: openai/gpt-4o-mini`, NON scrivere 'da "
        "openai/gpt-4o-mini a openai/gpt-4o-mini'. Tacere quello slot.\n"
        "- Task ha `main LLM: openai/gpt-4o-mini` con OpenAI key attiva, NON "
        "suggerire 'aggiungi Browser LLM cloud' — e' gia' attivo.\n"
        "- Se TUTTI gli slot pertinenti sono adeguati, scrivi: "
        "'_Configurazione gia' adeguata: la causa non e' nella config del "
        "task. Vedi sezione Raccomandazioni concrete._'.\n"
        "\n"
        "REGOLA: se la diagnosi e' P3 (anti-bot/DOM vuoto) o P9 (sito non "
        "directory di profili), il main LLM NON e' la causa. Non suggerire "
        "main LLM cloud per quei pattern: e' un no-op che spreca soldi. Spiega "
        "invece nella sezione Raccomandazioni che serve cambiare la "
        "STRATEGIA, non il modello.\n"
        "\n"
        "Per ogni cambio EFFETTIVAMENTE diverso dal valore attuale, scrivi "
        "UNA riga:\n"
        "- **<slot>** (es. `main LLM`, `browser LLM`, `agent_mode`, "
        "`max_iterations`): da `<valore attuale ESATTO dal blocco Task>` a "
        "`<valore consigliato>` — <perche', massimo 1 frase>.\n"
        "Solo cambiamenti AZIONABILI dalla UI Argos (slot del task).\n\n"
        "## 💡 Raccomandazioni concrete per il prossimo run\n"
        "Azioni che esulano dalla sola configurazione: rivedere seed_queries, "
        "escludere domini non scrapabili, aggiungere proxy residenziali, "
        "segnalare bug Argos, ecc. Lista ordinata per priorita' (alta → "
        "bassa). Se nulla da aggiungere oltre il tuning, scrivi: '_Nessuna "
        "azione ulteriore — vedi sezione tuning sopra._'.\n\n"
        "═══════════════════════════════════════════════════════════════\n\n"
        "REGOLE:\n"
        "- Scrivi in italiano, tecnico ma comprensibile a un non-developer.\n"
        "- Se NON rilevi pattern problematici (task andato bene), scrivi nella "
        "sezione 'Pattern problematici rilevati': '_Nessun pattern noto. Run "
        "pulito._'.\n"
        "- Non inventare codici pattern non in catalogo (P1-P8). Se vedi un "
        "errore nuovo, mettilo come 'P? — <nome>' con sintomo/causa/fix dedotti.\n"
        "- NEL TUNING CONFIGURAZIONE: cita ESATTAMENTE i nomi degli slot del "
        "task (`main LLM`, `discovery LLM`, `browser LLM`, `agent_mode`, "
        "`max_iterations`) cosi' l'utente sa dove cliccare. Non dire 'il "
        "modello': dilli quale slot.\n"
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

        # Risoluzione provider/model con fallback a 2 livelli (2026-05-23):
        #   1. PREFERENZA: se il task ha `browser_llm_*` configurato (cloud
        #      capable, es. openai/gpt-4o-mini), USALO per il summary. Il
        #      modello cloud rispetta meglio le regole anti-allucinazione del
        #      prompt rispetto ai modelli ollama che tendono a rigurgitare il
        #      catalogo P1-P9.
        #   2. FALLBACK: se browser_llm non e' configurato OPPURE la sua
        #      chiave non e' risolvibile (vault key cancellata, env var
        #      mancante, provider sconosciuto), usa il main LLM del task
        #      (tipicamente ollama qwen3-coder, gratis ma meno preciso). Cosi'
        #      l'utente non resta MAI senza summary per un missing key.
        # Costo: ~$0.003/summary su gpt-4o-mini, $0 su ollama.
        def _resolve_summary_llm():
            browser_provider = (task.get("browser_llm_provider") or "").strip()
            browser_model = (task.get("browser_llm_model") or "").strip()
            if browser_provider and browser_model:
                try:
                    api_key, base_url, _ = resolve_credential(
                        task.get("browser_llm_credential_id"),
                        browser_provider,
                        project_key=task.get("browser_llm_api_key"),
                        custom_base_url=(
                            task.get("browser_llm_base_url")
                            or task.get("llm_base_url")
                        ),
                    )
                    return browser_provider, browser_model, api_key, base_url
                except Exception as e:
                    log.warning(
                        "master_summary: browser_llm %s/%s non risolvibile (%s). "
                        "Fallback al main LLM.",
                        browser_provider, browser_model, e,
                    )
            # Fallback main LLM
            main_provider = (task.get("llm_provider") or "ollama").strip()
            main_model = task.get("model") or "qwen3-coder:30b"
            api_key, base_url, _ = resolve_credential(
                task.get("llm_credential_id"),
                main_provider,
                project_key=task.get("llm_api_key"),
                custom_base_url=task.get("llm_base_url"),
            )
            return main_provider, main_model, api_key, base_url

        provider_key, model, api_key, base_url = _resolve_summary_llm()

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
