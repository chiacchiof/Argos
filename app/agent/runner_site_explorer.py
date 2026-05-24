"""Runner site_explorer: agent ReAct vero per navigazione di siti web.

A differenza di bulk_extract (pipeline rigida con discovery+crawler) e auto_extract
(dispatcher di sub-runner), questo agent USA un LLM per decidere step-per-step
come navigare il sito a partire da un seed URL e dall'obiettivo del task.

Loop ReAct con tool minimi:
- fetch_page(url): scarica pagina, ritorna title + link interni raggruppati per
  pattern + preview testo. Pensato per LLM (output compatto, niente HTML grezzo).
- extract_target(url): tenta l'estrazione strutturata sulla pagina con il template
  configurato; ritorna ok/no_data per dire all'agente "questa e' un target o no".
- done(reason): termina graceful.

L'agente memorizza implicitamente nei messaggi gli URL gia' visitati e gli asset
gia' estratti — il context window di gpt-4o-mini regge facilmente 30 step.

Cap: max_iterations (default 30 step). Costo tipico: 0.05-0.20 USD per sito.

Output: profiles.jsonl + report.md, compatibile con qualifier downstream.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

from .. import db
from ..config import RESULTS_DIR, settings
from .extraction_templates import get_schema
from .llm_providers import resolve_api_key, resolve_base_url
from .ollama import maybe_add_keep_alive
from .runner_bulk_extract import (
    _extract_links,
    _group_urls_by_pattern,
    _normalize_url,
    _registrable_domain,
)


log = logging.getLogger(__name__)


SITE_EXPLORER_TOOLS_SPEC: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "fetch_page",
            "description": (
                "Scarica una pagina HTTP e ritorna title + link interni raggruppati "
                "per pattern strutturale + preview del testo principale (max ~2KB). "
                "Usalo per esplorare la struttura del sito e capire dove andare. "
                "Stesso registrable_domain del seed: link esterni vengono filtrati."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "URL completo http(s)."},
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "enqueue_listings",
            "description": (
                "Accoda URL di LISTING/CATEGORIA/PAGINAZIONE da esplorare in seguito. "
                "Usalo SUBITO dopo il primo fetch_page per dichiarare i punti d'ingresso "
                "alle pagine che CONTENGONO target (es. /donne/, /categoria/cucina/, "
                "?p=2, /page/2, sub-sezioni). Il runner consumera' la queue uno per uno "
                "facendo fetch_page automatico, dopo che hai esaurito gli URL del fetch "
                "corrente. Puoi chiamarlo piu' volte (es. quando un fetch_page rivela "
                "paginazione nuova). NON usarlo per URL profilo singoli (per quelli usa "
                "extract_target)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "urls": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Lista di URL listing/categoria/paginazione (max 10 per chiamata).",
                    },
                    "reason": {
                        "type": "string",
                        "description": "Una frase che spiega perche' questi URL sono listing.",
                    },
                },
                "required": ["urls", "reason"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "discover_via_browser",
            "description": (
                "Apre il seed in un browser HEADLESS (Playwright), scrolla N volte "
                "per attivare l'infinite scroll del sito, raccoglie tutti gli URL "
                "del DOM finale che matchano `target_pattern_hint` e li accoda "
                "automaticamente al runner per estrazione. Usa SOLO quando: "
                "(a) un fetch_page sulla listing principale mostra POCHI target "
                "(es. <10 sub-domain o <10 URL del pattern atteso), E "
                "(b) l'objective utente menziona 'tutti', 'centinaia', 'tutto' o "
                "il sito ha indicatori JS-load (infinite scroll, lazy load). "
                "NON usa LLM: navigation puramente deterministica, gratis come "
                "token. Ritorna n_urls trovati, automaticamente accodati al "
                "direct_target_queue (estrazione via HTTP+LLM extract dal runner)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "URL della pagina listing con infinite scroll (es. /donne/, /prodotti/, /annunci/).",
                    },
                    "scrolls": {
                        "type": "integer",
                        "description": "Numero di scroll (default 20, max 100). Piu' scroll = piu' URL raccolti.",
                    },
                    "target_pattern_hint": {
                        "type": "string",
                        "description": "Regex o substring per filtrare i link raccolti. Es. 'mondocamgirls.com/' per sub-domain profili, 'annuncio/' per pagine annuncio. Lascia vuoto per tutti i link interni.",
                    },
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "start_extraction",
            "description": (
                "Segnala che la FASE 1 (mapping) e' completa: hai gia' chiamato "
                "enqueue_listings con tutti i listing che vuoi esplorare. Da questo "
                "momento il RUNNER prendera' il controllo: poppera' la queue uno per "
                "uno, fara' fetch_page automatico su ciascun listing, identifichera' "
                "il pattern dei profili, e fara' extract_target su ogni URL fresco. "
                "Tu non devi fare piu' nulla. Chiamalo SOLO quando hai mappato tutto "
                "il sito (almeno 1 chiamata enqueue_listings)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "summary": {
                        "type": "string",
                        "description": "Breve riassunto della struttura rilevata (1-2 frasi).",
                    },
                },
                "required": ["summary"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "extract_target",
            "description": (
                "Tenta l'estrazione strutturata sulla pagina seguendo lo schema target "
                "(configurato sul task). Ritorna ok=true e l'asset estratto se la "
                "pagina e' un target valido (campi-chiave del template popolati), "
                "ok=false se la pagina non e' un target (es. e' un indice o non ha "
                "i dati richiesti). Usa questo tool quando pensi di essere arrivato "
                "su una pagina-dettaglio."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "URL della pagina-dettaglio da cui estrarre."},
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "done",
            "description": (
                "Termina l'esplorazione di questo sito. Usalo quando hai estratto "
                "abbastanza target, oppure quando capisci che il sito non contiene "
                "i target richiesti, oppure quando hai esaurito le opzioni."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "reason": {"type": "string", "description": "Breve motivazione (1-2 frasi)."},
                },
                "required": ["reason"],
            },
        },
    },
]


from .url_canonical import (
    canonical_url as _canonical_url,
    looks_like_service_path as _looks_like_service_path,
)


_DEFAULT_MAX_ITER = 30
_DEFAULT_TARGET_PER_SITE = 30
_UNBOUNDED_TARGET_CAP = 20000  # cap di sicurezza per modalita' unbounded.
# Alzato da 5000 a 20000 il 2026-05-11 dopo aver visto tryst.link con 32k profili
# reali e babepedia /top100 con 21k profili. 5000 lasciava troppo sul tavolo.
_FETCH_TIMEOUT_S = 25
_FETCH_TEXT_PREVIEW = 2000  # caratteri da mostrare al LLM
_FETCH_LINK_PATTERN_TOP = 12  # top-N pattern di link da mostrare al LLM


async def run_agent(task: dict[str, Any], job_id: int) -> str:
    from .. import jobs as _jobs
    from .runner_browseruse import _has_minimal_data_for, _ingest_to_assets

    def jlog(line: str) -> None:
        db.append_job_log(job_id, line)

    _jobs.register_subjob(job_id)
    try:
        return await _run_agent_inner(task, job_id, jlog, _has_minimal_data_for, _ingest_to_assets)
    finally:
        _jobs.unregister_subjob(job_id)


async def _run_agent_inner(
    task: dict[str, Any],
    job_id: int,
    jlog,
    _has_minimal_data_for,
    _ingest_to_assets,
) -> str:
    from .blocked_domains import assert_no_blocked_seeds

    db.update_job(job_id, status="running", started_at=db.now_iso())
    db.set_control_signal(job_id, None)
    jlog(
        f"Avvio site_explorer per task #{task['id']} \"{task['name']}\" — "
        f"agente ReAct su LLM"
    )

    # POLICY GATE: domini bloccati (vedi memoria feedback_no_mondocamgirl_traffic)
    _blocked = assert_no_blocked_seeds(task.get("seed_queries") or [])
    if _blocked:
        msg = f"Seed bloccati dalla policy locale (no-traffic): {_blocked}. Abort runner."
        jlog(f"⛔ {msg}")
        db.update_job(job_id, status="error", error=msg, finished_at=db.now_iso())
        return ""

    # 1. Risorse e config LLM
    provider_key = task.get("llm_provider") or "ollama"
    try:
        # resolve_credential supporta vault (FK credential_id) + fallback legacy.
        from .llm_providers import resolve_credential
        api_key, base_url, _ = resolve_credential(
            task.get("llm_credential_id"),
            provider_key,
            project_key=task.get("llm_api_key"),
            custom_base_url=task.get("llm_base_url"),
        )
    except RuntimeError as e:
        jlog(f"ERRORE configurazione provider: {e}")
        db.update_job(job_id, status="error", error=str(e), finished_at=db.now_iso())
        raise
    model = task["model"]
    jlog(f"Provider/Modello: {provider_key} / {model}")

    # Configurazione cap + refresh policy (deve essere risolta PRIMA dei log
    # informativi che ne dipendono).
    max_steps = int(task.get("max_iterations") or _DEFAULT_MAX_ITER)
    raw_cap = int(task.get("target_cap_per_site") if task.get("target_cap_per_site") is not None else _DEFAULT_TARGET_PER_SITE)
    if raw_cap <= 0:
        max_targets = _UNBOUNDED_TARGET_CAP
        unbounded_mode = True
    else:
        max_targets = max(1, min(raw_cap, _UNBOUNDED_TARGET_CAP))
        unbounded_mode = False
    refresh_policy_days = int(task.get("refresh_policy_days") if task.get("refresh_policy_days") is not None else 7)

    # Log informativi
    if refresh_policy_days == 0:
        jlog("♻️ Refresh policy: MAI (skip URL gia' in DB)")
    elif refresh_policy_days < 0:
        jlog("♻️ Refresh policy: SEMPRE (re-extract tutti, anche freschi)")
    else:
        jlog(f"♻️ Refresh policy: re-extract dopo {refresh_policy_days} giorni")
    if unbounded_mode:
        jlog(
            f"♾️ Modalita' UNBOUNDED: cap target = 0 → estraggo TUTTI i target del sito "
            f"(cap interno di sicurezza: {_UNBOUNDED_TARGET_CAP}). Costo proporzionale al sito."
        )

    # 2. Setup
    seed_queries = [u for u in (task.get("seed_queries") or []) if u]
    if not seed_queries:
        msg = "site_explorer richiede almeno un seed URL."
        jlog(msg)
        db.update_job(job_id, status="error", error=msg, finished_at=db.now_iso())
        raise RuntimeError(msg)
    seed_url = _normalize_url(seed_queries[0]) or seed_queries[0]
    seed_host = (urlparse(seed_url).hostname or "").lower()
    seed_reg_domain = _registrable_domain(seed_host)

    extraction_template = (task.get("extraction_template") or "").strip() or None
    schema_text = get_schema(extraction_template) if extraction_template else ""

    # Stage 2 — Playbook read: c'e' un playbook persistente per questo (dominio, asset_type)?
    # Se si', lo iniettiamo nel system prompt come "intelligence da run precedente".
    # Bumpamo `hits` ora; alla fine bumpiamo successes/failures in base al risultato.
    playbook_data: dict[str, Any] | None = None
    playbook_id: int | None = None
    if extraction_template and seed_reg_domain:
        try:
            pb_row = db.get_site_playbook(seed_reg_domain, extraction_template)
            if pb_row:
                playbook_id = int(pb_row["id"])
                # Il playbook in DB e' JSON serializzato (text + transferable + blockers).
                try:
                    playbook_data = json.loads(pb_row["playbook"])
                except Exception:
                    playbook_data = {"text": pb_row["playbook"], "transferable": True}
                db.bump_playbook_hits(playbook_id)
                jlog(
                    f"📚 Playbook trovato per {seed_reg_domain}/{extraction_template} "
                    f"(id={playbook_id}, source={pb_row['source_runner']}, "
                    f"hits={pb_row['hits']+1}, successes={pb_row['successes']}). "
                    f"Lo inietto nel system prompt."
                )
        except Exception as e:
            log.debug("Playbook read failed for %s: %s", seed_reg_domain, e)

    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_dir = RESULTS_DIR / str(task["id"]) / ts
    run_dir.mkdir(parents=True, exist_ok=True)
    profiles_path = run_dir / "profiles.jsonl"
    profiles_f = profiles_path.open("w", encoding="utf-8")

    n_assets_collected = 0
    n_steps = 0
    visited_urls: set[str] = set()
    extracted_urls: set[str] = set()
    stopped = False
    done_reason: str | None = None
    # Pattern learning per il sito: se per un primo target i contatti sono in
    # /social-links/, registriamo learned_subpath="/social-links" e lo suggeriamo
    # ai tool successivi cosi' l'agente non rifa il drill-down per ogni profilo.
    last_incomplete_url: str | None = None
    learned_subpath: str | None = None
    # Anti-loop: se gli ultimi N extract_target consecutivi falliscono perche'
    # l'URL e' gia' stato fatto (o la pagina e' gia' visitata) significa che il
    # LLM si e' "incartato" sugli URL del context invece di pescare URL nuovi.
    # A soglia raggiunta, forziamo il break con done_reason esplicito.
    consecutive_already_done_extracts = 0
    consecutive_already_visited_fetches = 0
    _ANTILOOP_THRESHOLD = 4
    _FETCH_LOOP_THRESHOLD = 3
    # Multi-phase planning (Fix H + I): l'agente prima MAPPA il sito tramite
    # enqueue_listings(), poi (chiamando start_extraction) cede il controllo al
    # RUNNER che fa il loop deterministico fetch_page → extract_target su ogni
    # URL fresco di ogni listing. Niente piu' decisioni LLM nel loop di estrazione
    # (che era la causa del "il modello smette dopo 5 estrazioni").
    exploration_queue: list[str] = []
    explored_listings: set[str] = set()
    enqueue_call_count = 0  # quante volte il LLM ha chiamato enqueue_listings
    _MAX_QUEUE_SIZE = 30  # cap di sicurezza anti-runaway
    _MAX_MAPPING_STEPS = 6  # cap per la fase MAPPING; oltre forziamo extraction
    extraction_phase_started = False  # True dopo start_extraction o auto-trigger
    # Direct target queue: URL profilo gia' identificati (es. via discover_via_browser
    # su sito infinite-scroll). Il runner-driven extraction li processa con
    # extract_target diretto (no fetch_page intermedio per identificare il pattern).
    direct_target_queue: list[str] = []

    # 3. Sistema prompt + messaggi iniziali
    system_prompt = _build_system_prompt(
        objective=(task.get("objective") or "").strip(),
        schema_text=schema_text,
        extraction_template=extraction_template,
        seed_reg_domain=seed_reg_domain,
        seed_url=seed_url,
        max_steps=max_steps,
        max_targets=max_targets,
        playbook_data=playbook_data,
    )

    # === Prepopulated listing queue (da site_recon pagination expansion) ===
    # Se l'auto_extract ha gia' identificato URL paginati validi (es. ?page=1..N
    # con paginazione visibile nel HTML del seed), li trovo in
    # task["prepopulated_listing_urls"]. Li carico direttamente in
    # exploration_queue saltando l'auto-discovery (i prepopulated coprono gia' tutto).
    prepop_listings = list(task.get("prepopulated_listing_urls") or [])
    if prepop_listings:
        added_pp = 0
        for u in prepop_listings:
            if not u:
                continue
            if u in explored_listings or u in exploration_queue:
                continue
            canonical_u = _canonical_url(u)
            if any(_canonical_url(q) == canonical_u for q in exploration_queue):
                continue
            exploration_queue.append(u)
            added_pp += 1
            # Per i prepopulated NON applichiamo _MAX_QUEUE_SIZE (cap anti-runaway
            # contro enqueue_listings del LLM): questi URL sono pre-validati dal
            # recon, devono passare tutti per coprire la directory paginata.
        if added_pp:
            jlog(
                f"📥 Prepopulated listing queue: +{added_pp} URL paginati da recon "
                f"(saltato auto-discovery via browser scroll)."
            )

    # === Forced auto-discovery (deterministico, NON LLM-driven) ===
    # Se l'objective contiene keyword "infinite scroll/tutti/centinaia/..."
    # OPPURE target_cap_per_site=0 (unbounded), il runner chiama discover_via_browser
    # DA SOLO sul seed prima del primo turno LLM. Il LLM trova la queue gia' piena
    # e deve solo chiamare start_extraction.
    #
    # SKIP se exploration_queue gia' popolata dai prepopulated_listing_urls
    # (siamo gia' coperti, non serve scrollare il browser).
    obj_lower = (task.get("objective") or "").lower()
    _AUTO_DISCOVER_KEYWORDS = (
        "infinite scroll", "infinite scrolling", "scroll infinito",
        "tutti i profili", "tutti i target", "tutti gli annunci",
        "tutti i prodotti", "tutto il sito", "centinaia", "migliaia",
        "tutti i contatti", "tutta la lista",
    )
    auto_discover_triggered = (
        unbounded_mode
        or any(k in obj_lower for k in _AUTO_DISCOVER_KEYWORDS)
    )
    if auto_discover_triggered and exploration_queue:
        jlog(
            "🤖 Auto-discovery via browser saltato: exploration_queue gia' popolata "
            f"({len(exploration_queue)} URL) da prepopulated listing."
        )
        auto_discover_triggered = False
    if auto_discover_triggered:
        # Resume: prova a caricare la queue persistita da un run precedente
        # interrotto/cancellato. Se valida, salta la discovery (~30s + 0 token).
        persisted = _load_pending_queue(task["id"], seed_reg_domain, refresh_policy_days)
        if persisted:
            recovered = persisted.get("queue_remaining") or []
            already = set(persisted.get("already_extracted") or [])
            # Filtra: skip URL gia' estratti / service-path / dedup canonical
            for u in recovered:
                if u in extracted_urls or u in direct_target_queue:
                    continue
                if _looks_like_service_path(u):
                    continue
                canonical_u = _canonical_url(u)
                if any(_canonical_url(t) == canonical_u for t in direct_target_queue):
                    continue
                direct_target_queue.append(u)
            extracted_urls.update(already)
            jlog(
                f"♻️ Resume: caricata queue persistita ({len(direct_target_queue)} URL "
                f"residui, {len(already)} gia' estratti). SALTO la discovery via browser."
            )
        else:
            from .url_discovery_browser import discover_urls_via_scroll
            scrolls_n = 30
            # Pattern hint: sub-domain dello stesso registrable_domain (caso piu' comune
            # per profili e simili). Fallback a "tutto il dominio" se non match.
            # Pattern_hint: matcha sia URL con subdomain (es. `cam.example.com`,
            # tipico per profili camgirl, OnlyFans-like) sia senza (es. `tryst.link/escort/X`,
            # `babepedia.com/babe/X`). Il subdomain e' OPZIONALE — non richiederlo per
            # default era il bug che azzerava il yield su tryst/babepedia.
            pattern_hint = rf"^https?://([a-z0-9_-]+\.)?{re.escape(seed_reg_domain)}/?"
            jlog(
                f"🤖 Auto-discovery FORZATA (trigger: {'unbounded' if unbounded_mode else 'keyword objective'}): "
                f"chiamo discover_via_browser({seed_url}, scrolls={scrolls_n}) PRIMA del primo turno LLM."
            )
            try:
                disc = await discover_urls_via_scroll(
                    url=seed_url,
                    scrolls=scrolls_n,
                    pattern_hint=pattern_hint,
                    seed_reg_domain=seed_reg_domain,
                )
                if disc.get("ok"):
                    added = 0
                    for u in (disc.get("urls") or []):
                        if u in extracted_urls or u in direct_target_queue:
                            continue
                        if _looks_like_service_path(u):
                            continue
                        canonical_u = _canonical_url(u)
                        if any(_canonical_url(t) == canonical_u for t in direct_target_queue):
                            continue
                        direct_target_queue.append(u)
                        added += 1
                    jlog(
                        f"  ✅ auto-discovery: {disc.get('scrolls_done')} scroll, "
                        f"{disc.get('n_urls_filtered')} URL filtrati, "
                        f"+{added} accodati al direct_target_queue."
                    )
                    # Persisti la queue subito dopo la discovery: se il run viene
                    # cancellato a metà, il prossimo run può fare resume senza ri-scroll.
                    try:
                        _save_pending_queue(
                            task["id"], direct_target_queue, extracted_urls,
                            seed_url, seed_reg_domain,
                        )
                    except Exception as e:
                        log.debug("Save pending queue failed: %s", e)
                else:
                    jlog(f"  ⚠️ auto-discovery FAIL: {disc.get('error')}. Procedo con flusso classico.")
            except Exception as e:
                jlog(f"  ⚠️ auto-discovery EXCEPTION: {type(e).__name__}: {e}. Procedo con flusso classico.")

    # User message: piu' diretto se auto-discover ha popolato la queue
    if direct_target_queue:
        first_user_msg = (
            f"Auto-discovery ha gia' raccolto {len(direct_target_queue)} URL target dal sito "
            f"(via browser headless con scroll, infinite-scroll detectato). Tu adesso devi "
            f"SEMPLICEMENTE chiamare start_extraction(summary='Auto-discovery completata, "
            f"queue popolata') per cedere il controllo al runner che estrarra' tutto. "
            f"NON fare fetch_page, NON fare enqueue_listings: la queue e' gia' pronta."
        )
    elif exploration_queue:
        first_user_msg = (
            f"La exploration_queue contiene gia' {len(exploration_queue)} URL listing "
            f"paginati (es. ?page=1..N) identificati dal recon. Chiama "
            f"start_extraction(summary='Pagination expansion, queue popolata') per cedere "
            f"il controllo al runner che processera' tutte le pagine. NON fare fetch_page "
            f"o enqueue_listings: la queue e' gia' pronta."
        )
    else:
        first_user_msg = (
            f"Inizia esplorando il seed: {seed_url}\n\n"
            f"Strategia consigliata: prima fetch_page(seed) per capire la struttura, "
            f"poi naviga verso le sezioni che probabilmente contengono i target."
        )
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": first_user_msg},
    ]

    payload: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "temperature": 0.1,
        "max_tokens": 600,
        "tools": SITE_EXPLORER_TOOLS_SPEC,
        "tool_choice": "auto",
    }
    maybe_add_keep_alive(payload, base_url)
    headers = {"Authorization": f"Bearer {api_key}"}
    api_url = f"{base_url.rstrip('/')}/chat/completions"

    # 4. Loop ReAct
    # llm_client = httpx (parla con OpenAI/Ollama, no anti-bot)
    # fetch_client = HttpFetcher con TLS impersonation (parla con siti scrapati,
    # bypassa Cloudflare e altri anti-bot)
    from .http_fetcher import HttpFetcher as _HttpFetcher
    async with httpx.AsyncClient(timeout=120) as llm_client, _HttpFetcher(
        timeout=_FETCH_TIMEOUT_S,
        follow_redirects=True,
        user_agent=settings.http_user_agent,
    ) as fetch_client:
        try:
            from .runner_control import wait_if_paused_or_stop, RunnerStopped
            for step in range(max_steps):
                # Gestisce pause + stop in modo uniforme con runner_browseruse.
                # Se signal='pause' sospende; se 'stop' alza RunnerStopped che
                # viene catturata sotto e segna stopped=True.
                try:
                    await wait_if_paused_or_stop(job_id, jlog)
                except RunnerStopped:
                    stopped = True
                    break

                if n_assets_collected >= max_targets:
                    jlog(
                        f"  raggiunto cap target ({max_targets}). "
                        f"Termino senza chiamare done()."
                    )
                    done_reason = f"raggiunto cap target ({max_targets})"
                    break

                n_steps += 1
                try:
                    r = await llm_client.post(api_url, json=payload, headers=headers)
                    r.raise_for_status()
                    data = r.json()
                except Exception as e:
                    jlog(f"  ⚠️ step {n_steps}/{max_steps}: errore LLM: {type(e).__name__}: {e}")
                    break

                msg = (data.get("choices") or [{}])[0].get("message") or {}
                tool_calls = msg.get("tool_calls") or []
                content = (msg.get("content") or "").strip()

                # Logga "thoughts" del modello (alcuni emettono content + tool_calls)
                if content:
                    jlog(f"  💭 step {n_steps}: {content[:300]}")

                if not tool_calls:
                    jlog(
                        f"  ↩ step {n_steps}/{max_steps}: nessun tool_call emesso "
                        f"(il modello ha smesso di chiamare tool). Termino."
                    )
                    done_reason = "LLM ha smesso di chiamare tool"
                    break

                # Append assistant message (con tool_calls) al payload
                normalized: list[dict[str, Any]] = []
                for c in tool_calls:
                    cp = dict(c)
                    cp.setdefault("id", f"call_{uuid.uuid4().hex[:8]}")
                    normalized.append(cp)
                payload["messages"].append(
                    {"role": "assistant", "content": content, "tool_calls": normalized}
                )

                for call in normalized:
                    fn = (call.get("function") or {})
                    name = fn.get("name") or ""
                    args_raw = fn.get("arguments")
                    args = _decode_args(args_raw)

                    if name == "fetch_page":
                        url_arg = (args.get("url") or "").strip()
                        tool_output = await _tool_fetch_page(
                            url_arg,
                            seed_reg_domain=seed_reg_domain,
                            client=fetch_client,
                            visited_urls=visited_urls,
                            learned_subpath=learned_subpath,
                            extracted_urls=extracted_urls,
                        )
                        jlog(
                            f"  📄 step {n_steps}: fetch_page({_truncate_url(url_arg)}) "
                            f"→ {_summarize_fetch_output(tool_output)}"
                        )
                        # F3 originale rimosso (rev. 5): la euristica "URL nel
                        # next_extract_targets = target, non listing" era troppo fragile.
                        # I top pattern di una pagina possono contenere URL listing
                        # legittimi (es. /categoria/donne/) che il LLM correttamente
                        # vuole accodare. La dedup e' gia' garantita da:
                        # - _canonical_url (cross-lingua)
                        # - extracted_urls set (no re-extract)
                        # - explored_listings set (no re-pop)
                        # - _looks_like_service_path (no privacy/faq/...)
                        pass
                        # Fix D: anti-loop su fetch_page ripetuti su URL gia' visitati.
                        if _is_already_visited_fetch_output(tool_output):
                            consecutive_already_visited_fetches += 1
                            if consecutive_already_visited_fetches >= _FETCH_LOOP_THRESHOLD:
                                done_reason = (
                                    f"anti-loop fetch_page: {consecutive_already_visited_fetches} "
                                    f"fetch_page consecutivi su URL gia' visitati. Il LLM non sta "
                                    f"pescando URL nuovi nemmeno dai response recenti."
                                )
                                jlog(f"  🛑 step {n_steps}: {done_reason}")
                        else:
                            consecutive_already_visited_fetches = 0
                    elif name == "extract_target":
                        url_arg = (args.get("url") or "").strip()
                        tool_output, asset_obj, is_complete = await _tool_extract_target(
                            url_arg,
                            seed_reg_domain=seed_reg_domain,
                            llm_client=llm_client,
                            fetch_client=fetch_client,
                            base_url=base_url,
                            api_key=api_key,
                            model=model,
                            schema_text=schema_text,
                            extraction_template=extraction_template,
                            extracted_urls=extracted_urls,
                            has_min_data_fn=_has_minimal_data_for,
                            learned_subpath=learned_subpath,
                            n_assets_collected=n_assets_collected,
                            max_targets=max_targets,
                            exploration_queue=exploration_queue,
                        )
                        if asset_obj is not None:
                            profiles_f.write(json.dumps(asset_obj, ensure_ascii=False) + "\n")
                            profiles_f.flush()
                            n_assets_collected += 1
                            consecutive_already_done_extracts = 0
                            jlog(
                                f"  ✅ step {n_steps}: extract_target({_truncate_url(url_arg)}) "
                                f"→ ok: {_summarize_extract_output(tool_output)} "
                                f"[totale: {n_assets_collected}/{max_targets}]"
                            )
                            # Pattern learning: se prima avevamo un incompleto e ora un
                            # completo, e il URL completo estende quello incompleto,
                            # registra il subpath che ha portato al completamento.
                            if is_complete and last_incomplete_url and not learned_subpath:
                                derived = _derive_subpath(last_incomplete_url, url_arg)
                                if derived:
                                    learned_subpath = derived
                                    jlog(
                                        f"  💡 PATTERN IMPARATO: target completi su questo "
                                        f"sito vivono in '{learned_subpath}/'. Lo suggerisco "
                                        f"all'agente sui prossimi profili."
                                    )
                            if is_complete:
                                last_incomplete_url = None
                            else:
                                last_incomplete_url = url_arg
                        else:
                            jlog(
                                f"  ⛔ step {n_steps}: extract_target({_truncate_url(url_arg)}) "
                                f"→ {_summarize_extract_output(tool_output)}"
                            )
                            # Anti-loop: se l'output indica "URL gia' fatto/visitato",
                            # bumpa il counter; altrimenti resetta.
                            if _is_already_done_output(tool_output):
                                consecutive_already_done_extracts += 1
                                if consecutive_already_done_extracts >= _ANTILOOP_THRESHOLD:
                                    done_reason = (
                                        f"anti-loop: {consecutive_already_done_extracts} extract_target "
                                        f"consecutivi su URL gia' fatti. Il LLM si e' incartato sul "
                                        f"context invece di pescare URL nuovi dai fetch_page recenti."
                                    )
                                    jlog(f"  🛑 step {n_steps}: {done_reason}")
                            else:
                                consecutive_already_done_extracts = 0
                    elif name == "discover_via_browser":
                        # Tool ibrido: browser headless per scoprire URL via infinite scroll
                        from .url_discovery_browser import discover_urls_via_scroll
                        d_url = (args.get("url") or "").strip() or seed_url
                        d_scrolls = int(args.get("scrolls") or 20)
                        d_pattern = (args.get("target_pattern_hint") or "").strip() or None
                        # Validation: stesso registrable_domain
                        try:
                            d_host = (urlparse(d_url).hostname or "").lower()
                            d_reg = _registrable_domain(d_host)
                        except Exception:
                            d_reg = None
                        if d_reg != seed_reg_domain:
                            tool_output = json.dumps({
                                "ok": False,
                                "reason": f"URL fuori dominio (atteso *.{seed_reg_domain})",
                            })
                            jlog(f"  ❌ step {n_steps}: discover_via_browser → URL fuori dominio")
                        else:
                            jlog(
                                f"  🌐 step {n_steps}: discover_via_browser({_truncate_url(d_url)}) "
                                f"scrolls={d_scrolls} pattern={d_pattern!r} → headless browser..."
                            )
                            try:
                                disc = await discover_urls_via_scroll(
                                    url=d_url,
                                    scrolls=d_scrolls,
                                    pattern_hint=d_pattern,
                                    seed_reg_domain=seed_reg_domain,
                                )
                            except Exception as e:
                                disc = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                            if disc.get("ok"):
                                # Accoda automaticamente al direct_target_queue (dedup)
                                added = 0
                                for u in (disc.get("urls") or []):
                                    if u in extracted_urls or u in direct_target_queue:
                                        continue
                                    if _looks_like_service_path(u):
                                        continue
                                    canonical_u = _canonical_url(u)
                                    if any(_canonical_url(t) == canonical_u for t in direct_target_queue) \
                                       or any(_canonical_url(e) == canonical_u for e in extracted_urls):
                                        continue
                                    direct_target_queue.append(u)
                                    added += 1
                                tool_output = json.dumps({
                                    "ok": True,
                                    "scrolls_done": disc.get("scrolls_done"),
                                    "n_urls_total_dom": disc.get("n_urls_total"),
                                    "n_urls_filtered": disc.get("n_urls_filtered"),
                                    "n_added_to_queue": added,
                                    "direct_target_queue_size": len(direct_target_queue),
                                    "preview": (disc.get("urls") or [])[:5],
                                    "warning": disc.get("error"),
                                    "next_step_suggestion": (
                                        f"Aggiunti {added} URL target alla coda diretta. "
                                        "Adesso chiama start_extraction() per cedere al runner: "
                                        "estrarra' uno per uno via HTTP+LLM (no piu' browser)."
                                    ),
                                })
                                jlog(
                                    f"  ✅ step {n_steps}: discover_via_browser → "
                                    f"{disc.get('scrolls_done')} scroll, {disc.get('n_urls_filtered')} URL "
                                    f"filtrati, +{added} accodati al direct_target_queue"
                                )
                            else:
                                tool_output = json.dumps({
                                    "ok": False,
                                    "reason": disc.get("error", "errore sconosciuto"),
                                })
                                jlog(f"  ❌ step {n_steps}: discover_via_browser FAIL: {disc.get('error')}")
                    elif name == "start_extraction":
                        summary = (args.get("summary") or "").strip() or "(no summary)"
                        # Accetta start_extraction se almeno una delle due queue ha contenuto
                        total_pending = len(exploration_queue) + len(direct_target_queue)
                        if total_pending == 0:
                            tool_output = json.dumps(
                                {
                                    "ok": False,
                                    "reason": (
                                        "start_extraction RIFIUTATO: nessun lavoro accodato. "
                                        "Prima chiama enqueue_listings (per listing/categorie) "
                                        "e/o discover_via_browser (per siti infinite-scroll)."
                                    ),
                                },
                                ensure_ascii=False,
                            )
                            jlog(f"  ⚠️ step {n_steps}: start_extraction rifiutato (queue vuote)")
                        else:
                            extraction_phase_started = True
                            tool_output = json.dumps(
                                {
                                    "ok": True,
                                    "ack": "extraction_started",
                                    "exploration_queue_size": len(exploration_queue),
                                    "direct_target_queue_size": len(direct_target_queue),
                                    "message": (
                                        f"Mapping completato. Cedo il controllo al runner: "
                                        f"{len(direct_target_queue)} target diretti + "
                                        f"{len(exploration_queue)} listing da esplorare."
                                    ),
                                },
                                ensure_ascii=False,
                            )
                            jlog(
                                f"  🎯 step {n_steps}: start_extraction → "
                                f"runner-driven: {len(direct_target_queue)} target diretti + "
                                f"{len(exploration_queue)} listing. Summary: {summary[:120]}"
                            )
                    elif name == "enqueue_listings":
                        urls_arg = args.get("urls") or []
                        reason_arg = (args.get("reason") or "").strip() or "(no reason)"
                        if not isinstance(urls_arg, list):
                            urls_arg = []
                        # Filtra: stesso registrable_domain, http(s), non-vuoti, non duplicati,
                        # NON e' un URL target (era nei next_extract_targets di un fetch precedente)
                        valid: list[str] = []
                        skipped_reasons: list[tuple[str, str]] = []
                        for raw_u in urls_arg[:10]:
                            u = (str(raw_u) or "").strip()
                            if not u or not u.startswith(("http://", "https://")):
                                skipped_reasons.append((u or "(empty)", "non-URL"))
                                continue
                            host = (urlparse(u).hostname or "").lower()
                            if _registrable_domain(host) != seed_reg_domain:
                                skipped_reasons.append((u, "fuori-dominio"))
                                continue
                            if u in explored_listings or u in exploration_queue:
                                skipped_reasons.append((u, "duplicato"))
                                continue
                            # Dedup canonical: stesso contenuto cross-lingua = un solo accodamento
                            canonical_u = _canonical_url(u)
                            if any(_canonical_url(q) == canonical_u for q in exploration_queue) \
                               or any(_canonical_url(e) == canonical_u for e in explored_listings):
                                skipped_reasons.append((u, "duplicato-canonical"))
                                continue
                            # Skip URL gia' estratti come asset (sarebbe target, non listing)
                            if u in extracted_urls or canonical_u in {_canonical_url(t) for t in extracted_urls}:
                                skipped_reasons.append((u, "url-gia-estratto"))
                                continue
                            # F1: scarta path di servizio (privacy, faq, terms, ecc.)
                            if _looks_like_service_path(u):
                                skipped_reasons.append((u, "service-path"))
                                continue
                            if len(exploration_queue) + len(valid) >= _MAX_QUEUE_SIZE:
                                break
                            valid.append(u)
                        skipped = [u for u, _ in skipped_reasons]
                        for u in valid:
                            exploration_queue.append(u)
                        enqueue_call_count += 1
                        # Compatta i motivi degli skip per il LLM (cosi' impara cosa NON accodare)
                        skip_summary: dict[str, int] = {}
                        for _, reason in skipped_reasons:
                            skip_summary[reason] = skip_summary.get(reason, 0) + 1
                        tool_output = json.dumps(
                            {
                                "ok": True,
                                "n_added": len(valid),
                                "n_skipped": len(skipped),
                                "skipped_reasons": skip_summary,
                                "queue_size": len(exploration_queue),
                                "queue_preview": exploration_queue[:5],
                                "reason_received": reason_arg[:200],
                                "next_step_suggestion": (
                                    "Mapping in corso. Quando hai accodato TUTTI i listing "
                                    "che vuoi esplorare (categorie, paginazione, sub-sezioni), "
                                    "chiama start_extraction(summary='...') per cedere il "
                                    "controllo al runner che fara' il loop di estrazione "
                                    "deterministicamente. NON chiamare extract_target tu in "
                                    "FASE 1: il runner lo fara' meglio. NON accodare URL "
                                    "che sono target singoli (profili, annunci, prodotti) — "
                                    "accoda solo LISTING/CATEGORIE/PAGINAZIONE."
                                ),
                            },
                            ensure_ascii=False,
                        )
                        skip_log = (
                            f", scartati: {skip_summary}" if skip_summary else ""
                        )
                        jlog(
                            f"  📋 step {n_steps}: enqueue_listings(+{len(valid)} URL"
                            f"{skip_log}) → queue size={len(exploration_queue)} "
                            f"[reason: {reason_arg[:80]}]"
                        )
                    elif name == "done":
                        # Done() guard: se la queue di esplorazione non e' vuota E il cap
                        # target NON e' raggiunto, RIFIUTIAMO il done() e diciamo al LLM
                        # di consumare prima la queue. Eccezione: se l'agente non ha mai
                        # chiamato enqueue_listings dopo il primo fetch_page, probabilmente
                        # ha solo lavoro single-livello → done() valido.
                        proposed_reason = (args.get("reason") or "").strip() or "agente ha terminato"
                        if (
                            exploration_queue
                            and n_assets_collected < max_targets
                            and enqueue_call_count > 0
                        ):
                            tool_output = json.dumps(
                                {
                                    "ok": False,
                                    "reason": (
                                        f"done() RIFIUTATO: la queue ha ancora "
                                        f"{len(exploration_queue)} listing da esplorare e hai "
                                        f"estratto solo {n_assets_collected}/{max_targets} target. "
                                        f"Continua: il runner fara' fetch_page sul prossimo "
                                        f"listing della queue automaticamente. Chiama done() "
                                        f"SOLO quando la queue e' vuota O hai raggiunto il cap."
                                    ),
                                    "queue_remaining": exploration_queue[:5],
                                    "estratti_finora": n_assets_collected,
                                    "cap_target": max_targets,
                                },
                                ensure_ascii=False,
                            )
                            jlog(
                                f"  ⏸️  step {n_steps}: done() rifiutato → queue ha "
                                f"{len(exploration_queue)} listing, estratti {n_assets_collected}/{max_targets}"
                            )
                        else:
                            done_reason = proposed_reason
                            tool_output = json.dumps({"ok": True, "ack": "terminating"})
                            jlog(f"  🏁 step {n_steps}: done → {done_reason}")
                    else:
                        tool_output = json.dumps({"ok": False, "reason": f"tool sconosciuto {name!r}"})
                        jlog(
                            f"  ⚠️ step {n_steps}: tool sconosciuto {name!r} con args={args}. "
                            f"Lo segnalo al modello."
                        )

                    payload["messages"].append(
                        {
                            "role": "tool",
                            "tool_call_id": call["id"],
                            "name": name,
                            "content": tool_output[:8000],
                        }
                    )

                if done_reason:
                    break

                # Transizione FASE 1 → FASE 2: il LLM ha ceduto il controllo
                # esplicitamente (start_extraction) o ha esaurito i mapping step.
                if extraction_phase_started:
                    jlog(
                        f"  ➡️ Transizione a FASE 2 (runner-driven extraction). "
                        f"Queue: {len(exploration_queue)} listing."
                    )
                    break
                # Auto-trigger della FASE 2 se l'agente ha mappato (queue non vuota
                # OPPURE direct_target_queue popolata da discover_via_browser) e ha
                # esaurito il budget mapping. Salva-vita anti-runaway.
                if (
                    n_steps >= _MAX_MAPPING_STEPS
                    and (exploration_queue or direct_target_queue)
                    and not extraction_phase_started
                ):
                    extraction_phase_started = True
                    jlog(
                        f"  ⏱️ Auto-trigger FASE 2: cap mapping step "
                        f"({_MAX_MAPPING_STEPS}) raggiunto, queue ha "
                        f"{len(exploration_queue)} listing. Cedo al runner."
                    )
                    break

            else:
                jlog(f"  ⚠️ cap step raggiunto ({max_steps}) senza done()")
                done_reason = f"cap step ({max_steps}) raggiunto"

            # FASE 2 — Runner-driven extraction (deterministica, nessuna decisione LLM)
            # Il LLM extractor viene comunque chiamato dentro extract_target per
            # ogni profilo, ma il LOOP (quale URL fetchare, quale URL estrarre,
            # quando passare al prossimo listing) e' gestito dal runner.
            if extraction_phase_started and not stopped and (exploration_queue or direct_target_queue):
                cap_state = await _runner_driven_extraction(
                    queue=exploration_queue,
                    direct_target_queue=direct_target_queue,
                    explored_listings=explored_listings,
                    extracted_urls=extracted_urls,
                    visited_urls=visited_urls,
                    seed_reg_domain=seed_reg_domain,
                    fetch_client=fetch_client,
                    llm_client=llm_client,
                    base_url=base_url,
                    api_key=api_key,
                    model=model,
                    schema_text=schema_text,
                    extraction_template=extraction_template,
                    has_min_data_fn=_has_minimal_data_for,
                    profiles_f=profiles_f,
                    n_assets_collected_start=n_assets_collected,
                    max_targets=max_targets,
                    learned_subpath_initial=learned_subpath,
                    job_id=job_id,
                    jlog=jlog,
                    refresh_policy_days=refresh_policy_days,
                    task_id=task["id"],
                    seed_url=seed_url,
                )
                n_assets_collected = cap_state["n_assets_collected"]
                if not done_reason:
                    done_reason = cap_state["done_reason"]

        except asyncio.CancelledError:
            jlog("Cancellato.")
            stopped = True
            # NON raise immediato: lascia che il finally faccia ingest+save_queue
            # prima di propagare. Senza questo, hard_stop_job perdeva i profili
            # gia' nel jsonl (bug N1 incidente 2026-05-12).
        finally:
            try:
                profiles_f.close()
            except Exception:
                pass
            # Persisti la queue residua per resume al prossimo run.
            if direct_target_queue or exploration_queue:
                try:
                    _save_pending_queue(
                        task["id"], direct_target_queue, extracted_urls,
                        seed_url, seed_reg_domain,
                    )
                except Exception:
                    pass

    # 5. Ingest in DB — DEVE girare anche con stop graceful per non perdere
    # i profili gia' estratti. Fix N1 (incidente 2026-05-12: hard_stop saltava ingest).
    n_assets_in_db = 0
    if extraction_template:
        try:
            n_assets_in_db = _ingest_to_assets(
                profiles_path,
                task["id"],
                job_id,
                jlog,
                extraction_template=extraction_template,
            )
        except Exception as e:
            jlog(f"⚠️ ingest fail in finally: {type(e).__name__}: {e}")

    # 6. Report finale
    fmt = task.get("output_format") or "md"
    report_ext = "md" if fmt in ("md", "both") else "txt"
    status_word = "INTERROTTO" if stopped else "completato"
    report = (
        f"# Riepilogo site_explorer {ts} ({status_word})\n\n"
        f"- **Seed**: {seed_url}\n"
        f"- **Step LLM eseguiti**: {n_steps}/{max_steps}\n"
        f"- **Pagine visitate (fetch_page)**: {len(visited_urls)}\n"
        f"- **Asset estratti**: {n_assets_collected}\n"
        f"- **Asset ingestati in DB**: {n_assets_in_db}\n"
        f"- **Modello**: {provider_key} / {model}\n"
        f"- **Motivo terminazione**: {done_reason or 'n/a'}\n\n"
        f"Vedi `profiles.jsonl` per i dati strutturati.\n"
    )
    report_path = run_dir / f"report.{report_ext}"
    report_path.write_text(report, encoding="utf-8")

    final_status = "cancelled" if stopped else "done"
    jlog(
        f"site_explorer {status_word}: {n_assets_collected} asset estratti, "
        f"{n_assets_in_db} ingestati in DB. Report: {report_path}"
    )

    # Fix 1 — site_explorer scrive il PROPRIO playbook a fine job riuscito.
    # Cosi' i prossimi run sullo stesso dominio partono "armati" con la mappa
    # gia' nota: in fase MAPPING il LLM legge il playbook e fa subito enqueue
    # senza ri-esplorare. Stage 2 esteso a site_explorer.
    if (
        not stopped
        and n_assets_in_db > 0
        and extraction_template
        and seed_reg_domain
    ):
        try:
            pb_lines = [
                f"Mapping rilevato su {seed_reg_domain} (asset_type={extraction_template}).",
            ]
            if explored_listings:
                pb_lines.append(
                    f"Listing esplorate ({len(explored_listings)}): "
                    + ", ".join(sorted(list(explored_listings))[:8])
                )
            if learned_subpath:
                pb_lines.append(f"Subpath che completa i target: {learned_subpath}")
            pb_lines.append(
                f"Estrazione: {n_assets_in_db} asset / {len(visited_urls)} pagine visitate."
            )
            pb_text = "\n".join(pb_lines)
            pb_payload = json.dumps(
                {
                    "text": pb_text,
                    "transferable": True,
                    "blockers": [],
                    "asset_count_at_creation": n_assets_in_db,
                    "explored_listings": sorted(list(explored_listings))[:20],
                    "learned_subpath": learned_subpath,
                },
                ensure_ascii=False,
            )
            new_pb_id = db.upsert_site_playbook(
                registrable_domain=seed_reg_domain,
                asset_type=extraction_template,
                playbook=pb_payload,
                source_runner="site_explorer",
                source_job_id=job_id,
                transferable=True,
            )
            jlog(
                f"  📚 Playbook salvato (id={new_pb_id}) per {seed_reg_domain} / "
                f"{extraction_template} — listing esplorate={len(explored_listings)}"
            )
        except Exception as e:
            log.debug("Site_explorer playbook write failed: %s", e)

    # Stage 2 — Playbook outcome: se avevamo applicato un playbook, registra esito
    # (success se >0 asset, failure altrimenti). Auto-stale a 1 failure (soglia
    # abbassata 2026-05-23: un playbook che produce 0 asset al riapplicarsi e'
    # quasi sempre stantio, meglio invalidarlo subito).
    if playbook_id is not None and not stopped:
        try:
            outcome = db.bump_playbook_outcome(
                playbook_id, success=(n_assets_in_db > 0)
            )
            if outcome == "stale":
                jlog(
                    f"  ⚠️ Playbook id={playbook_id} marcato STALE (0 asset "
                    f"prodotti al riuso). Il prossimo run su questo sito "
                    f"ripartira' vergine."
                )
        except Exception as e:
            log.debug("Playbook outcome bump failed: %s", e)

    # Site intelligence: registra l'esito per (dominio, tenant) cosi' che
    # l'orchestrator possa consultarlo pre-task nei run successivi.
    # Best-effort: errori qui non devono compromettere il return del runner.
    if seed_reg_domain and not stopped:
        try:
            success = n_assets_in_db > 0
            db.upsert_site_intelligence(
                registrable_domain=seed_reg_domain,
                status="accessible" if success else "low_yield",
                strategy_worked="site_explorer" if success else None,
                job_id=job_id,
                success=success,
                notes=(
                    f"site_explorer estratti={n_assets_in_db}, "
                    f"step={n_steps}, target_cap={target_cap}"
                ),
            )
            if not success:
                # Auto-promote check: se N tenant indipendenti hanno fail
                # sullo stesso dominio, promuovi a community pool.
                try:
                    promo = db.auto_promote_to_community_pool(seed_reg_domain)
                    if promo.get("promoted"):
                        jlog(
                            f"  🌐 Pool community: {seed_reg_domain} promosso "
                            f"a shared ({promo['n_tenants']} tenant negativi), "
                            f"policy id={promo['policy_id']}"
                        )
                except Exception as ee:
                    log.debug("auto_promote check failed: %s", ee)
        except Exception as e:
            log.debug("site_intelligence upsert failed: %s", e)

    db.update_job(
        job_id, status=final_status, finished_at=db.now_iso(),
        result_path=str(report_path),
    )
    db.set_control_signal(job_id, None)
    return str(report_path)


async def _runner_driven_extraction(
    *,
    queue: list[str],
    direct_target_queue: list[str] | None,
    explored_listings: set[str],
    extracted_urls: set[str],
    visited_urls: set[str],
    seed_reg_domain: str,
    fetch_client: httpx.AsyncClient,
    llm_client: httpx.AsyncClient,
    base_url: str,
    api_key: str,
    model: str,
    schema_text: str,
    extraction_template: str | None,
    has_min_data_fn,
    profiles_f,
    n_assets_collected_start: int,
    max_targets: int,
    learned_subpath_initial: str | None,
    job_id: int,
    jlog,
    refresh_policy_days: int = 7,
    task_id: int | None = None,
    seed_url: str = "",
) -> dict[str, Any]:
    """Loop deterministico runner-driven sulla queue di esplorazione.

    Per ogni listing della queue:
      1. fetch_page (programmatico, niente LLM decisional)
      2. identifica il "target pattern" (top pattern con count>=3 + filter)
      3. extract_target su ogni URL fresco (chiama LLM extractor)
      4. detect paginazione → append automaticamente alla queue
      5. detect drill-down via learned_subpath
      6. termina su cap_target raggiunto

    Il LLM viene chiamato SOLO per estrarre il JSON di ogni profilo (la parte
    inevitabile). Niente piu' chiamate decisionali "next URL?" che facevano
    perdere il filo.
    """
    n_assets_collected = n_assets_collected_start
    learned_subpath = learned_subpath_initial
    last_incomplete_url: str | None = None
    pagination_appended_per_listing: dict[str, int] = {}
    cap_pagination = 5
    cap_per_listing = 30  # max URL fresh estratti da un singolo listing
    direct_target_queue = direct_target_queue or []

    # === FASE A — Direct target extraction (URL noti, niente fetch_page intermedio) ===
    # Questi URL provengono tipicamente da discover_via_browser su siti infinite-scroll.
    # Vanno estratti direttamente uno per uno con extract_target (HTTP+LLM).
    if direct_target_queue:
        from .. import db
        jlog(
            f"  📥 [runner] FASE A: {len(direct_target_queue)} target diretti da estrarre "
            f"(da discover_via_browser)"
        )
        n_skipped_recent_direct = 0
        # Save periodico ogni N estrazioni: sopravvive a cancel/crash.
        # Usiamo pop(0) cosi' direct_target_queue mostra sempre lo stato residuo
        # (i caller che leggono la queue dopo il return hanno la versione corretta).
        _save_period = 50
        n_processed = 0
        while direct_target_queue:
            target_url = direct_target_queue.pop(0)

            # Save periodico: ogni N URL processati, persisti la queue residua
            if task_id is not None and n_processed > 0 and n_processed % _save_period == 0:
                try:
                    _save_pending_queue(
                        task_id, direct_target_queue, extracted_urls, seed_url, seed_reg_domain,
                    )
                except Exception:
                    pass

            if n_assets_collected >= max_targets:
                jlog(f"  🎯 [runner] cap target ({max_targets}) raggiunto in FASE A. Stop.")
                # Reinserisci l'URL appena pop-pato come residuo (non l'abbiamo processato)
                direct_target_queue.insert(0, target_url)
                if task_id is not None:
                    try:
                        _save_pending_queue(task_id, direct_target_queue, extracted_urls, seed_url, seed_reg_domain)
                    except Exception:
                        pass
                return {
                    "n_assets_collected": n_assets_collected,
                    "done_reason": f"runner-driven (direct): cap {max_targets} raggiunto",
                }
            # Gestisce pause + stop tramite helper centralizzato.
            from .runner_control import wait_if_paused_or_stop, RunnerStopped
            try:
                await wait_if_paused_or_stop(job_id, lambda _s: None)
            except RunnerStopped:
                direct_target_queue.insert(0, target_url)
                if task_id is not None:
                    try:
                        _save_pending_queue(task_id, direct_target_queue, extracted_urls, seed_url, seed_reg_domain)
                    except Exception:
                        pass
                return {"n_assets_collected": n_assets_collected, "done_reason": "STOP utente"}
            n_processed += 1
            # Refresh policy
            if extraction_template and db.has_recent_asset(
                target_url, extraction_template, refresh_policy_days
            ):
                n_skipped_recent_direct += 1
                extracted_urls.add(target_url)
                continue

            tool_output, asset_obj, is_complete = await _tool_extract_target(
                target_url,
                seed_reg_domain=seed_reg_domain,
                llm_client=llm_client,
                fetch_client=fetch_client,
                base_url=base_url,
                api_key=api_key,
                model=model,
                schema_text=schema_text,
                extraction_template=extraction_template,
                extracted_urls=extracted_urls,
                has_min_data_fn=has_min_data_fn,
                learned_subpath=learned_subpath,
                n_assets_collected=n_assets_collected,
                max_targets=max_targets,
                exploration_queue=[],
            )
            if asset_obj is not None:
                profiles_f.write(json.dumps(asset_obj, ensure_ascii=False) + "\n")
                profiles_f.flush()
                n_assets_collected += 1
                jlog(
                    f"  ✅ [runner-direct] extract({_truncate_url(target_url)}) "
                    f"→ {_summarize_extract_output(tool_output)} "
                    f"[totale: {n_assets_collected}/{max_targets}]"
                )
                if is_complete and last_incomplete_url and not learned_subpath:
                    derived = _derive_subpath(last_incomplete_url, target_url)
                    if derived:
                        learned_subpath = derived
                        jlog(f"  💡 [runner-direct] PATTERN: subpath '{learned_subpath}'")
                last_incomplete_url = None if is_complete else target_url
            else:
                jlog(
                    f"  ⛔ [runner-direct] extract({_truncate_url(target_url)}) "
                    f"→ {_summarize_extract_output(tool_output)}"
                )
        if n_skipped_recent_direct > 0:
            jlog(f"  ⏩ [runner-direct] skip {n_skipped_recent_direct} URL gia' in DB freschi")
        jlog(f"  📤 [runner] FASE A completata: {n_assets_collected}/{max_targets} estratti finora")
        # FASE A esaurita interamente (loop terminato senza early return) → la
        # queue persistita non serve piu'. La cancelliamo per evitare resume su
        # un set di URL gia' processato.
        if task_id is not None:
            try:
                p = _pending_queue_path(task_id)
                if p.exists():
                    p.unlink()
                    jlog("  🧹 [runner] queue persistita rimossa (FASE A completa)")
            except Exception:
                pass

    # === FASE B — Listing exploration (loop sulle listing della exploration_queue) ===

    # Helper inline per detection paginazione
    def _detect_pagination_links(link_summary: list[dict[str, Any]]) -> list[str]:
        out: list[str] = []
        for entry in link_summary:
            for u in (entry.get("urls") or []) + (entry.get("examples") or []):
                if any(p in u for p in ("?p=", "?page=", "/page/", "&p=", "&page=")):
                    out.append(u)
        # dedupe preservando ordine
        seen: set[str] = set()
        uniq: list[str] = []
        for u in out:
            if u not in seen:
                seen.add(u)
                uniq.append(u)
        return uniq

    while queue and n_assets_collected < max_targets:
        # Pause/Stop signal check (helper centralizzato — supporta pause).
        from .. import db
        from .runner_control import wait_if_paused_or_stop, RunnerStopped
        try:
            await wait_if_paused_or_stop(job_id, lambda _s: None)
        except RunnerStopped:
            return {"n_assets_collected": n_assets_collected, "done_reason": "STOP utente"}

        listing_url = queue.pop(0)
        if listing_url in explored_listings:
            continue
        explored_listings.add(listing_url)
        jlog(
            f"  🔁 [runner] pop listing: {_truncate_url(listing_url)} "
            f"(queue={len(queue)}, estratti={n_assets_collected}/{max_targets})"
        )

        fetch_out_str = await _tool_fetch_page(
            listing_url,
            seed_reg_domain=seed_reg_domain,
            client=fetch_client,
            visited_urls=visited_urls,
            learned_subpath=learned_subpath,
            extracted_urls=extracted_urls,
        )
        try:
            fetch_data = json.loads(fetch_out_str)
        except Exception:
            jlog(f"  ⚠️ [runner] fetch_page output non parseable, skip listing")
            continue
        if not fetch_data.get("ok"):
            jlog(
                f"  ⚠️ [runner] fetch_page fail per {_truncate_url(listing_url)}: "
                f"{fetch_data.get('reason', '')[:100]}"
            )
            continue
        link_summary = fetch_data.get("link_patterns") or []
        if not link_summary:
            jlog(f"  ⚠️ [runner] nessun pattern in {_truncate_url(listing_url)}, skip")
            continue

        # Identifica target URLs: top pattern (cap 30) con filtri agnostici al dominio:
        # - F1: skip URL con segmenti di "service path" (privacy, faq, terms, ecc.)
        # - F2: skip URL la cui canonical form e' gia' stata estratta (cross-lingua dedup)
        # - skip self-listing e paginazione (gestita separatamente)
        target_urls: list[str] = []
        seen_canonicals: set[str] = {_canonical_url(u) for u in extracted_urls}
        for entry in link_summary[:3]:
            for u in (entry.get("urls") or []):
                # skip self-listing-like (path ridondante)
                if u.rstrip("/") == listing_url.rstrip("/"):
                    continue
                # skip paginazione (verra' append separatamente alla queue)
                if any(p in u for p in ("?p=", "?page=", "/page/")):
                    continue
                # F1: skip pagine di sistema (privacy/terms/faq/...)
                if _looks_like_service_path(u):
                    continue
                # F2: skip duplicati cross-lingua (URL diverse, stesso contenuto logico)
                canonical = _canonical_url(u)
                if canonical in seen_canonicals:
                    continue
                seen_canonicals.add(canonical)
                target_urls.append(u)
                if len(target_urls) >= cap_per_listing:
                    break
            if len(target_urls) >= cap_per_listing:
                break

        # Append paginazione alla queue (cap per listing)
        pagination_urls = _detect_pagination_links(link_summary)
        appended_pag = 0
        for pag_u in pagination_urls:
            if pag_u in explored_listings or pag_u in queue:
                continue
            if appended_pag >= cap_pagination:
                break
            queue.append(pag_u)
            appended_pag += 1
        if appended_pag > 0:
            jlog(f"  ➕ [runner] +{appended_pag} URL paginazione accodati alla queue")

        if not target_urls:
            jlog(f"  ℹ️ [runner] {_truncate_url(listing_url)}: nessun target URL identificato")
            continue
        jlog(f"  📦 [runner] {len(target_urls)} target URL identificati. Estraggo...")

        # Estrai ogni URL del listing
        n_skipped_recent = 0
        for target_url in target_urls:
            if n_assets_collected >= max_targets:
                jlog(f"  🎯 [runner] cap target ({max_targets}) raggiunto. Stop.")
                return {
                    "n_assets_collected": n_assets_collected,
                    "done_reason": f"runner-driven: cap target {max_targets} raggiunto",
                }
            try:
                await wait_if_paused_or_stop(job_id, lambda _s: None)
            except RunnerStopped:
                return {"n_assets_collected": n_assets_collected, "done_reason": "STOP utente"}

            # Fix 3: skip se l'asset esiste in DB ed e' fresco entro refresh_policy_days.
            # Risparmia chiamate LLM extractor + fetch HTTP sui re-run incrementali.
            if extraction_template and db.has_recent_asset(
                target_url, extraction_template, refresh_policy_days
            ):
                n_skipped_recent += 1
                extracted_urls.add(target_url)  # marca come "fatto" per i set
                continue

            tool_output, asset_obj, is_complete = await _tool_extract_target(
                target_url,
                seed_reg_domain=seed_reg_domain,
                llm_client=llm_client,
                fetch_client=fetch_client,
                base_url=base_url,
                api_key=api_key,
                model=model,
                schema_text=schema_text,
                extraction_template=extraction_template,
                extracted_urls=extracted_urls,
                has_min_data_fn=has_min_data_fn,
                learned_subpath=learned_subpath,
                n_assets_collected=n_assets_collected,
                max_targets=max_targets,
                exploration_queue=queue,
            )
            if asset_obj is not None:
                profiles_f.write(json.dumps(asset_obj, ensure_ascii=False) + "\n")
                profiles_f.flush()
                n_assets_collected += 1
                jlog(
                    f"  ✅ [runner] extract({_truncate_url(target_url)}) "
                    f"→ {_summarize_extract_output(tool_output)} "
                    f"[totale: {n_assets_collected}/{max_targets}]"
                )
                # Pattern learning: incomplete → complete = subpath imparato
                if is_complete and last_incomplete_url and not learned_subpath:
                    derived = _derive_subpath(last_incomplete_url, target_url)
                    if derived:
                        learned_subpath = derived
                        jlog(f"  💡 [runner] PATTERN IMPARATO: subpath '{learned_subpath}'")
                last_incomplete_url = None if is_complete else target_url
            else:
                jlog(
                    f"  ⛔ [runner] extract({_truncate_url(target_url)}) "
                    f"→ {_summarize_extract_output(tool_output)}"
                )
                # Drill-down opzionale se learned_subpath: prova <url>/<subpath>/
                if learned_subpath and learned_subpath not in target_url:
                    drilled_url = target_url.rstrip("/") + learned_subpath + "/"
                    if drilled_url not in extracted_urls:
                        d_out, d_obj, d_complete = await _tool_extract_target(
                            drilled_url,
                            seed_reg_domain=seed_reg_domain,
                            llm_client=llm_client,
                            fetch_client=fetch_client,
                            base_url=base_url,
                            api_key=api_key,
                            model=model,
                            schema_text=schema_text,
                            extraction_template=extraction_template,
                            extracted_urls=extracted_urls,
                            has_min_data_fn=has_min_data_fn,
                            learned_subpath=learned_subpath,
                            n_assets_collected=n_assets_collected,
                            max_targets=max_targets,
                            exploration_queue=queue,
                        )
                        if d_obj is not None:
                            profiles_f.write(json.dumps(d_obj, ensure_ascii=False) + "\n")
                            profiles_f.flush()
                            n_assets_collected += 1
                            jlog(
                                f"  ✅ [runner-drilldown] {_truncate_url(drilled_url)} "
                                f"→ {_summarize_extract_output(d_out)} "
                                f"[totale: {n_assets_collected}/{max_targets}]"
                            )
        if n_skipped_recent > 0:
            jlog(
                f"  ⏩ [runner] skip {n_skipped_recent} URL gia' in DB freschi "
                f"(refresh_policy_days={refresh_policy_days}). Token risparmiati."
            )

    return {
        "n_assets_collected": n_assets_collected,
        "done_reason": (
            f"runner-driven completato: queue esaurita "
            f"({n_assets_collected}/{max_targets} estratti)"
            if not queue
            else f"runner-driven: cap target raggiunto"
        ),
    }


# ---------------------------------------------------------------------------
# Persistenza della direct_target_queue (resume incrementale dopo cancel/error)
# ---------------------------------------------------------------------------
# La queue dei target scoperti via discover_via_browser viene salvata su disco
# in `data/results/<task_id>/_pending_queue.json` (un solo file per task,
# sovrascritto ad ogni save). Al run successivo sullo stesso task, se il file
# esiste ed e' fresco (eta' < refresh_policy_days), viene caricato e SI SALTA
# la discovery via browser → resume diretto da dove eravamo.
#
# Perche' a livello task e non job: vogliamo che il run #N+1 possa riprendere
# dal job #N anche se sono job_id diversi. Un solo file per task semplifica.

def _pending_queue_path(task_id: int) -> "Path":
    from pathlib import Path
    return RESULTS_DIR / str(task_id) / "_pending_queue.json"


def _save_pending_queue(
    task_id: int,
    direct_target_queue: list[str],
    extracted_urls: set[str],
    seed_url: str,
    seed_reg_domain: str,
) -> None:
    """Salva la queue residua + URL gia' estratti su disco (atomic write)."""
    if not direct_target_queue and not extracted_urls:
        return
    from pathlib import Path
    import tempfile, os
    path = _pending_queue_path(task_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "saved_at": datetime.now(timezone.utc).isoformat(),
        "seed_url": seed_url,
        "seed_reg_domain": seed_reg_domain,
        "queue_remaining": list(direct_target_queue),
        "already_extracted": sorted(extracted_urls),
    }
    # Atomic write: tmpfile + rename
    fd, tmp = tempfile.mkstemp(prefix="_pending_queue_", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        os.replace(tmp, str(path))
    except Exception:
        try:
            os.unlink(tmp)
        except Exception:
            pass
        raise


def _load_pending_queue(
    task_id: int,
    seed_reg_domain: str,
    refresh_policy_days: int,
) -> dict[str, Any] | None:
    """Carica la queue persistita se valida.

    Ritorna None se:
    - Il file non esiste
    - Eta' > refresh_policy_days (per coerenza con refresh policy: i target
      "vecchi" verrebbero scartati comunque dal has_recent_asset)
    - Dominio diverso (sicurezza: queue di un altro sito)
    - Parsing fail
    """
    path = _pending_queue_path(task_id)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if payload.get("seed_reg_domain") != seed_reg_domain:
        return None
    # TTL: se refresh_policy >= 0, controlla eta'. -1 = sempre fresca.
    if refresh_policy_days >= 0:
        try:
            saved_at = datetime.fromisoformat(payload["saved_at"].replace("Z", "+00:00"))
            if saved_at.tzinfo is None:
                saved_at = saved_at.replace(tzinfo=timezone.utc)
            age_days = (datetime.now(timezone.utc) - saved_at).days
            # Eta' max = max(refresh_policy_days, 1) — almeno 1 giorno di validita'
            if age_days > max(refresh_policy_days, 1):
                return None
        except Exception:
            return None
    return payload


def _decode_args(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if not raw:
        return {}
    if isinstance(raw, str):
        try:
            obj = json.loads(raw)
            return obj if isinstance(obj, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


def _build_system_prompt(
    *,
    objective: str,
    schema_text: str,
    extraction_template: str | None,
    seed_reg_domain: str,
    seed_url: str,
    max_steps: int,
    max_targets: int,
    playbook_data: dict[str, Any] | None = None,
) -> str:
    template_label = extraction_template or "(nessun template specifico)"
    schema_block = (
        f"SCHEMA TARGET ({extraction_template}):\n{schema_text[:2500]}\n\n"
        if schema_text
        else ""
    )
    # Playbook injection: blocco prominente all'inizio dopo schema, con "📚".
    playbook_block = ""
    if playbook_data and (playbook_data.get("text") or "").strip():
        blockers = playbook_data.get("blockers") or []
        blockers_str = ", ".join(blockers) if blockers else "(nessuno noto)"
        playbook_block = (
            "📚 INTELLIGENCE DA RUN PRECEDENTE (playbook persistito da un agente piu' "
            "potente che ha gia' lavorato su questo sito):\n"
            f"{playbook_data['text'].strip()[:2000]}\n"
            f"Blockers noti: {blockers_str}\n"
            "USA QUESTE INDICAZIONI come PRIMA strategia, prima di esplorare a vuoto. "
            "NB: se il playbook NON menziona discover_via_browser ma il sito e' a infinite "
            "scroll (o l'objective dice 'tutti'/'centinaia'), il playbook potrebbe essere "
            "obsoleto: in tal caso usa comunque discover_via_browser.\n\n"
        )

    # Trigger keyword detection sull'objective (forza il MODO 🅱 quando applicabile)
    obj_lower = (objective or "").lower()
    infinite_scroll_keywords = (
        "infinite scroll", "infinite scrolling", "scroll infinito",
        "tutti i profili", "tutti i target", "tutti gli annunci",
        "tutti i prodotti", "tutto il sito", "centinaia", "migliaia",
        "tutti i contatti", "tutta la lista",
    )
    matched_keywords = [k for k in infinite_scroll_keywords if k in obj_lower]
    is_unbounded = max_targets >= _UNBOUNDED_TARGET_CAP
    trigger_block = ""
    if matched_keywords or is_unbounded:
        reasons = []
        if matched_keywords:
            reasons.append(f"objective contiene keyword '{matched_keywords[0]}'")
        if is_unbounded:
            reasons.append("target_cap_per_site=0 (UNBOUNDED)")
        trigger_block = (
            "🚨 TRIGGER ATTIVATO PER MODO 🅱 (DISCOVER_VIA_BROWSER):\n"
            f"  Motivo: {' E '.join(reasons)}.\n"
            "  ⇒ DEVI chiamare discover_via_browser sulla home o sulla listing principale "
            "DOPO il primo fetch_page (non saltare il fetch_page: ti serve a capire il "
            "registrable_domain e i pattern target da passare in target_pattern_hint).\n"
            "  Esempio per camgirl con sub-domain profili:\n"
            "    discover_via_browser(\n"
            "      url='<seed o listing principale>',\n"
            "      scrolls=30,\n"
            "      target_pattern_hint='^https?://[a-z0-9_-]+\\\\.<dominio>/'\n"
            "    )\n"
            "  Dopo questo step chiama subito start_extraction.\n\n"
        )
    return (
        f"{playbook_block}"
        f"{trigger_block}"
        "Sei un agente esploratore di siti web. Il tuo compito e' navigare un sito "
        "a partire da un seed URL e ESTRARRE le pagine-dettaglio (annunci, prodotti, "
        "profili, ecc.) coerenti con l'obiettivo dell'utente.\n"
        "Lavori in DUE fasi distinte: MAPPING (capire la struttura del sito) e "
        "EXTRACTION (estrarre i target). Una buona mappatura iniziale e' la "
        "differenza fra estrarre 13 profili vs 80 profili.\n\n"
        f"OBIETTIVO UTENTE:\n{objective[:1500]}\n\n"
        f"{schema_block}"
        f"DOMINIO DEL SEED: {seed_reg_domain} (resta su questo dominio o sub-dominio)\n"
        f"SEED INIZIALE: {seed_url}\n"
        f"TEMPLATE TARGET: {template_label}\n\n"
        "═══ IL TUO COMPITO: SOLO FASE 1 — MAPPING ═══\n"
        "Il tuo lavoro e' SEMPLICE: mappare la struttura del sito. Ci sono DUE "
        "modi di mappare, scegli quello giusto in base al sito:\n\n"
        "🅰 MODO STANDARD (siti con HTML statico):\n"
        "  fetch_page(seed) → enqueue_listings([categorie, paginazioni]) → start_extraction\n"
        "  Il runner fara' fetch_page+extract_target su ogni listing/categoria.\n\n"
        "🅱 MODO INFINITE-SCROLL (siti che caricano profili via JS scroll):\n"
        "  fetch_page(seed) → discover_via_browser(seed_o_listing, scrolls=20-50, target_pattern_hint='...')\n"
        "    → start_extraction\n"
        "  Il browser headless apre la pagina, scrolla N volte, raccoglie TUTTI i\n"
        "  link che matchano il pattern, e li accoda al runner per extract_target diretto.\n\n"
        "  ⚠️ TRIGGER OBBLIGATORI per usare discover_via_browser (anche subito):\n"
        "  - L'objective utente contiene 'infinite scroll', 'infinite scrolling', "
        "'tutti i profili', 'tutti i target', 'centinaia', 'migliaia', 'tutto il sito'\n"
        "  - target_cap_per_site = 0 (modalita' UNBOUNDED esplicita)\n"
        "  - fetch_page sulla home/listing mostra <15 link nel pattern target atteso\n"
        "  - Il sito espone profili come sub-domain (`<slug>.dominio.com/`) e fetch_page\n"
        "    mostra <10 sub-domain (sintomo classico di lazy load)\n\n"
        "  Se UNO di questi e' vero → DEVI chiamare discover_via_browser. Niente scrupoli\n"
        "  sul costo: discover_via_browser e' GRATIS in token (browser deterministico,\n"
        "  zero LLM). NON usare invece il MODO 🅰 standard, fallirebbe a recuperare i\n"
        "  contenuti dietro lo scroll.\n\n"
        "Puoi anche COMBINARE i due modi (es. discover_via_browser per la home +\n"
        "enqueue_listings per le paginazioni statiche). Dopo qualunque combinazione,\n"
        "chiama start_extraction.\n\n"
        "⚠️ DEFINIZIONI (non confonderle):\n"
        "- LISTING/CATEGORIA/PAGINAZIONE = pagina che CONTIENE PIU' link a target. "
        "Es. /donne/, /vendita-case/roma/, /products/electronics/, ?p=2.\n"
        "- TARGET = pagina di DETTAGLIO singolo (1 profilo, 1 annuncio, 1 prodotto). "
        "Es. <slug>.dominio.com/, /annuncio/12345/, /profile/alice/.\n"
        "Accoda con enqueue_listings SOLO le listing. I target li estrae il runner "
        "automaticamente quando processa la listing che li contiene. Se per errore "
        "accodi un target, non e' un dramma: il runner usa il pattern del fetch_page "
        "per identificare i target del listing, e i tuoi target accidentali "
        "potrebbero generare estrazioni in piu'.\n\n"
        "STEP TIPICO (3-5 step totali):\n"
        "1. fetch_page(seed) — vedi la struttura della home.\n"
        "2. (opzionale) fetch_page(<sezione>) — se serve approfondire un punto specifico.\n"
        "3. enqueue_listings(urls=[...], reason=\"...\") — accoda TUTTI i listing che "
        "vuoi esplorare:\n"
        "   - sotto-categorie nel menu (es. `/donne/`, `/trans/`, `/categoria/cucina/`,\n"
        "     `/vendita-case/acireale/`, `/products/electronics/`)\n"
        "   - sezioni del sito (es. `/listing/`, `/annunci/`, `/people/`, `/profili/`)\n"
        "   - paginazione esplicita (es. `?p=2`, `/page/2`) — il runner trova quelle\n"
        "     successive automaticamente.\n"
        "4. start_extraction(summary=\"...\") — cedi il controllo al runner.\n\n"
        "⚠️ NON CHIAMARE extract_target IN FASE 1: lascialo fare al runner. Tu mappa "
        "soltanto. Se sbagli e chiami extract_target, e' OK ma stai sprecando token: "
        "il runner avrebbe fatto la stessa cosa meglio.\n\n"
        f"⚠️ NON CHIAMARE done(): lo fa il runner quando raggiunge {max_targets} target "
        "o esaurisce la queue. Tu non hai responsabilita' sul terminare.\n\n"
        "ESEMPIO DI INTERAZIONE TIPICA:\n"
        "  step 1: fetch_page(seed) → vedi link a /donne/, /trans/, /video/, /annunci/\n"
        "  step 2: enqueue_listings(\n"
        "    urls=[\"<seed>/donne/\", \"<seed>/trans/\", \"<seed>/video/\"],\n"
        "    reason=\"3 categorie nel menu del sito\")\n"
        "  step 3: start_extraction(summary=\"Sito con 3 categorie. Profili sub-domain.\")\n"
        "  → fine del tuo turno. Il runner ora estrae 100 profili da solo.\n\n"
        "REGOLE FONDAMENTALI:\n\n"
        "**1. USA SOLO URL VISTI** (CRITICO):\n"
        "Quando chiami extract_target(url) o fetch_page(url), l'URL DEVE essere uno "
        "che hai effettivamente VISTO ritornato da una chiamata fetch_page precedente, "
        "in `link_patterns[i].urls` o `link_patterns[i].examples`.\n"
        "**MAI inventare URL** decrementando o incrementando id (es. se vedi /annuncio/659632/, "
        "NON provare /annuncio/659631/, /annuncio/658565/, ecc. a meno che siano nella lista). "
        "Inventare URL ti porta a estrarre annunci di altre città/zone, fuori dall'obiettivo.\n"
        "Per il TOP pattern di una listing, fetch_page ritorna `urls` con la lista completa: "
        "iteragli sopra in ordine, senza inventare.\n"
        "**REGOLA ANTI-LOOP**: dopo che hai esaurito gli URL del PRIMO fetch_page, fai un "
        "fetch_page su una listing/sezione DIVERSA (es. paginazione `?p=2`, sotto-categoria, "
        "altra sezione di profili). Il response del nuovo fetch_page contiene URL FRESCHI "
        "nel campo `urls`/`examples`: USA QUELLI per i prossimi extract_target.\n"
        "**NON ritentare extract_target su URL gia' visitati**: il framework ritorna `URL gia' "
        "estratto` e dopo 4 tentativi consecutivi cosi' il runner termina automaticamente. "
        "Se vedi `gia' estratto`/`gia' visitato` come reason, **smetti subito**: prendi un "
        "URL DIVERSO dal latest fetch_page o chiama done().\n\n"
        "**2. ESTRAI TUTTO, NON FILTRARE in vivo**:\n"
        "Il tuo compito e' raccogliere asset coerenti con la ZONA/CATEGORIA dell'obiettivo. "
        "**NON applicare filtri quantitativi** (prezzo > X, mq > Y, ecc.) durante l'estrazione: "
        "lascia tutto il filtraggio al qualifier downstream. Se l'obiettivo dice 'prezzo > 200k', "
        "TU estrai TUTTI gli annunci della zona; il qualifier scartera' poi quelli sotto 200k.\n"
        "L'unica cosa che decidi tu: la SEZIONE/ZONA del sito da cui estrarre. "
        "Una volta sulla listing giusta, estrai TUTTO quello che vedi (senza giudicare prezzo/mq).\n\n"
        "**3. RESTA SULLA LISTING DELLA ZONA RICHIESTA**:\n"
        "Se l'obiettivo menziona una zona specifica (es. 'Acireale'), DOPO aver navigato alla "
        "listing della zona (es. /vendita-case/acireale/), USA SOLO gli URL di quella listing. "
        "Non saltare ad altre zone. Se la listing finisce, chiama done() invece di pescare URL "
        "da altri pattern.\n\n"
        "**4. CAP target = LIMITE MASSIMO, non obiettivo**:\n"
        f"Hai max {max_targets} target. Questo e' un TETTO, non un goal: estrai meno se la "
        "listing finisce prima. **Il cap REALE e' SEMPRE quello del runner (= "
        f"{max_targets})**: ignora qualsiasi numero diverso scritto nell'objective dell'utente "
        "(es. 'voglio almeno 50 profili'). L'objective e' una preferenza, il cap e' la regola.\n"
        "Ogni `extract_target` di successo ti riporta nel response un campo `_runner_state` "
        f"con `cap_target_reale`={max_targets} e `rimanenti`: quello e' il NUMERO AUTORITATIVO "
        "su cui basarti. Non chiamare done() prima di aver esaurito la listing o raggiunto il cap.\n\n"
        "**5. ARRICCHIMENTO TARGET INCOMPLETI** (CRITICO):\n"
        "Quando extract_target ritorna `ok=true`, controlla `is_complete` e `fields_missing`.\n"
        "**`ok=true` E NON `is_complete=true` ≠ scartabile**: il framework salva GIA' l'asset "
        "(perche' ok=true significa 'soglia minima superata'). `is_complete=false` ti dice "
        "solo che potresti arricchire andando in una sotto-pagina.\n"
        "  - Se `is_complete=true`: profilo gia' valido, **vai DIRETTO al prossimo profilo**.\n"
        "  - Se `is_complete=false`: opzionale tentare 1 sotto-pagina di arricchimento (es.\n"
        "    `/contatti/`, `/social/`, `/info/`, `/links/`). MAX 1 tentativo per profilo, poi\n"
        "    passa avanti.\n"
        "  - Per `profile_contacts` la soglia di 'completo' e' GIA' BASSA: basta display_name +\n"
        "    UN solo canale fra email/whatsapp/telegram/social/sitoweb. Se `is_complete=false`\n"
        "    significa che manca anche quel singolo canale, quindi una sotto-pagina /social\n"
        "    PUO' realmente arricchire.\n"
        "**NON chiamare done() solo perche' i profili sono 'incompleti'**: gli asset salvati\n"
        "valgono comunque, il qualifier downstream filtra. Continua finche' non raggiungi\n"
        "il cap target o non esaurisci la listing.\n\n"
        "**6. Altre regole**:\n"
        f"- Hai max {max_steps} step totali (LLM round trip).\n"
        "- DEVI sempre emettere un tool_call ad ogni turno (mai solo testo). Tool: "
        "fetch_page, extract_target, done.\n"
        "- Resta sul dominio del seed (i tool filtrano comunque).\n"
        "- Non rifare fetch su URL gia' visitate (te le ricordi nel context).\n"
        "- Non chiamare extract_target su URL che SEMBRANO non-dettaglio (home, listing, "
        "account, /privacy/, /chi-siamo/, ecc.).\n"
        "- Se 3 extract_target consecutivi ritornano no_data sulla stessa listing, prova "
        "una sezione diversa (es. paginazione `?p=2`) o chiama done().\n\n"
        "ESEMPIO DI TRAIETTORIA con MAPPING + EXTRACTION (per orientarti, NON copiare gli URL):\n\n"
        "  ─── FASE 1 — MAPPING ───\n"
        "  step 1: fetch_page(\"https://example-immobili.it/\")\n"
        "    → menu: link a /vendita-case/acireale/, /vendita-case/catania/, /affitto/,\n"
        "       /commerciale/. Anche `?p=2` come paginazione.\n"
        "  step 2: enqueue_listings(\n"
        "    urls=[\"/vendita-case/acireale/\", \"/vendita-case/catania/\",\n"
        "          \"/vendita-case/acireale/?p=2\", \"/vendita-case/catania/?p=2\"],\n"
        "    reason=\"sotto-categorie citta + paginazione\")\n"
        "    → queue size=4. Posso iniziare l'estrazione.\n\n"
        "  ─── FASE 2 — EXTRACTION ───\n"
        "  step 3-N: extract_target sui profili visibili dal fetch_page corrente (se la\n"
        "    home espone qualche annuncio diretto). Quando esauriti, fai fetch_page sul\n"
        "    prossimo URL della queue (vedi _runner_state.queue_preview).\n"
        "  step N+1: fetch_page(\"https://example-immobili.it/vendita-case/acireale/\")\n"
        "    → 25 URL annunci nuovi nel campo `urls`/`next_extract_targets`.\n"
        "  step N+2 ... extract_target su ognuno → 18 estratti, 7 vuoti/JSON-fail.\n"
        "  step M: fetch_page sul prossimo della queue (catania) → idem.\n"
        f"  step Z: cap raggiunto ({max_targets}) O queue vuota → done(reason).\n\n"
        "ESEMPIO TRAIETTORIA #2 — profilo persona con contatti in sotto-pagina:\n"
        "  step 1: fetch_page(\"https://www.example-cam.com/\")\n"
        "    → vede pattern /{slug}/ con 20 URL profilo (alice.example-cam.com, ...).\n"
        "      Anche link `/donne/`, `/trans/`, `/video/` nel menu.\n"
        "  step 2: enqueue_listings([\"/donne/\", \"/trans/\", \"/video/\"], reason=\"categorie\")\n"
        "  step 3: extract_target sui 20 profili visibili dal fetch del seed.\n"
        "  step 23: queue.pop → fetch_page(\"/donne/\") → altri 30 profili nuovi.\n"
        "  step 24-N: extract_target su ognuno.\n"
        f"  step Z: queue vuota e {max_targets} estratti → done.\n\n"
        "ANTI-PATTERN da EVITARE:\n"
        "- ❌ chiamare done() dopo aver estratto solo i profili del PRIMO fetch_page\n"
        "  senza aver mai chiamato enqueue_listings → il runner ti rifiutera' il done()\n"
        "- ❌ extract_target(\"/annuncio/12344/\") quando NON era in `urls`/`examples` → inventato\n"
        "- ❌ skip annuncio perche' il prezzo e' fuori range → lo fa il qualifier dopo\n"
        "- ❌ accettare un profile_contacts incompleto (solo nome) senza esplorare\n"
        "  /contatti, /social, /info se hint_subpath e' presente\n"
        "- ❌ enqueue_listings con URL profilo singoli (es. `/annuncio/12345/`) → quelli\n"
        "  non sono listing, vanno in extract_target\n"
    )


async def _tool_fetch_page(
    url: str,
    *,
    seed_reg_domain: str,
    client: httpx.AsyncClient,
    visited_urls: set[str],
    learned_subpath: str | None = None,
    extracted_urls: set[str] | None = None,
) -> str:
    """Fetch + readability + link extraction. Output compatto per LLM."""
    if not url or not url.startswith(("http://", "https://")):
        return json.dumps({"ok": False, "reason": "URL non valido (serve http(s))"})
    host = (urlparse(url).hostname or "").lower()
    if _registrable_domain(host) != seed_reg_domain:
        return json.dumps(
            {"ok": False, "reason": f"URL fuori dominio (atteso *.{seed_reg_domain})"}
        )
    if url in visited_urls:
        # Soft-warn invece di FAIL: il modello a volte vuole tornare a una listing
        # per pescare URL che non aveva preso. Diciamoglielo che e' gia' stata vista,
        # MA gli ricordiamo che gli URL veri li ha gia' nel context (negli `urls` del
        # primo fetch_page) — cosi' non resta in stallo e prosegue con quegli URL.
        return json.dumps(
            {
                "ok": False,
                "reason": (
                    "URL gia' visitato in questa sessione. Gli URL della listing/seed "
                    "sono gia' nel context (campo `urls` o `examples` del primo fetch_page): "
                    "pescali da li' invece di ri-fetchare. Oppure prova un URL diverso "
                    "(paginazione `?p=2`, sotto-categoria, paginazione successiva)."
                ),
                "hint_already_visited": True,
            }
        )

    try:
        from readability import Document
        from selectolax.parser import HTMLParser

        r = await client.get(url)
        status = r.status_code
        if status >= 400:
            return json.dumps({"ok": False, "url": url, "status": status, "reason": f"HTTP {status}"})
        ct = (r.headers.get("content-type") or "").lower()
        if "html" not in ct and "xml" not in ct:
            return json.dumps(
                {"ok": False, "url": url, "status": status, "reason": f"content-type non HTML: {ct}"}
            )
        html = r.text
        visited_urls.add(url)

        try:
            doc = Document(html)
            title = (doc.short_title() or "").strip()[:200]
            summary = doc.summary(html_partial=True)
            body = HTMLParser(summary).body if summary else None
            text = body.text(separator=" ", strip=True) if body else ""
        except Exception:
            title = ""
            text = ""
        if not text:
            try:
                text = HTMLParser(html).body.text(separator=" ", strip=True)
            except Exception:
                text = ""
        text_preview = text[:_FETCH_TEXT_PREVIEW]

        links = _extract_links(html, url, same_origin_only=True)
        groups = _group_urls_by_pattern(links)
        # FIX E: per i TOP 3 pattern, ritorniamo `urls` (cap 30) FILTRATI dagli URL
        # gia' estratti in questa sessione → l'agente vede SOLO URL freschi e non
        # rifa il loop su quelli gia' fatti. Per i pattern secondari (idx>=3), solo
        # 3 esempi per orientamento.
        ext_urls_lower = {u.lower() for u in (extracted_urls or set())}
        link_summary = []
        urls_with_per_pattern = 30
        for idx, (pat, urls) in enumerate(list(groups.items())[:_FETCH_LINK_PATTERN_TOP]):
            # filtra URL gia' estratti
            fresh_urls = [u for u in urls if u.lower() not in ext_urls_lower]
            entry: dict[str, Any] = {
                "pattern": pat,
                "count": len(urls),  # totale (per orientamento)
                "n_fresh": len(fresh_urls),  # quanti effettivamente nuovi
            }
            if idx < 3:
                # top 3 pattern: lista completa di URL freschi (cap 30)
                entry["urls"] = fresh_urls[:urls_with_per_pattern]
            else:
                # secondari: 3 esempi (anche fra i freschi se possibile)
                entry["examples"] = (fresh_urls or urls)[:3]
            link_summary.append(entry)
    except Exception as e:
        return json.dumps({"ok": False, "url": url, "reason": f"errore: {type(e).__name__}: {e}"})

    out: dict[str, Any] = {
        "ok": True,
        "url": url,
        "status": status,
        "title": title,
        "text_preview": text_preview,
        "n_internal_links": len(links),
        "link_patterns": link_summary,
    }
    # Fix B+E: esponi `next_extract_targets` con un MIX di URL freschi presi dai
    # top 3 pattern (3 URL per pattern). Cosi' anche se il top pattern e' navigation
    # (es. /it/camgirls-donne.html), i sub-domain dei profili (pattern secondario)
    # sono comunque visibili al modello. Aiuta i modelli sotto-8B che altrimenti
    # tenterebbero solo gli URL del primo pattern.
    next_targets: list[str] = []
    for entry in link_summary[:3]:
        pattern_urls = entry.get("urls") or []
        for u in pattern_urls[:3]:
            if u not in next_targets:
                next_targets.append(u)
    if next_targets:
        out["next_extract_targets"] = next_targets
        out["DIRETTIVA"] = (
            "Il prossimo extract_target DEVE essere uno di `next_extract_targets` "
            "(mix dai top 3 pattern). NON ritentare URL gia' estratti: i campi `urls` "
            "contengono SOLO URL freschi. Scegli quello che SEMBRA piu' simile a una "
            "scheda profilo/dettaglio (NON pagine /en/, /areacamgirl/, /info/ ecc.)."
        )
    # Hint subpath imparato: se sappiamo che su questo sito i target completi
    # vivono in /social-links/ e l'URL appena fetchata non lo include, suggerisci
    # di andare diretto la' invece di esplorare ulteriormente il listing.
    if learned_subpath and learned_subpath not in url:
        out["hint_subpath"] = (
            f"Su questo sito target completi sono stati estratti dal subpath "
            f"'{learned_subpath}/'. Per i prossimi profili, vai diretto a "
            f"<profile_url>{learned_subpath}/ invece di esplorare la home del profilo."
        )
    return json.dumps(out, ensure_ascii=False)


# Pattern testuali (case-insensitive) che indicano pagina vuota/placeholder/errore.
# Cross-domain: stringhe generiche che appaiono identiche su molti siti.
_EMPTY_PLACEHOLDER_TITLE_PATTERNS: tuple[str, ...] = (
    "free pics, galleries",        # babepedia placeholder (slug senza dati)
    "free sexy pics, galleries",
    "free nude pics, galleries",
    "page not found", "404 not found", "not found - 404",
    "pagina non trovata", "pagina non disponibile",
    "this page is unavailable",
    "account suspended", "suspended profile",
    "profilo sospeso", "profilo non disponibile",
    "user not found", "utente non trovato",
    "no results", "nessun risultato",
    "access denied", "accesso negato",
)

_EMPTY_PLACEHOLDER_BODY_PATTERNS: tuple[str, ...] = (
    "the page you requested could not be found",
    "the page you are looking for",
    "il profilo che stai cercando non esiste",
    "this profile does not exist",
    "questo profilo non esiste",
    "this account has been suspended",
    "questo account e' stato sospeso",
    "404 error",
)


def _validate_critical_fields(obj: dict, *, raw_text: str, raw_html: str) -> dict:
    """Post-validation anti-hallucination: per i campi critici di contatto,
    verifica che il valore esista nel raw text/HTML. Altrimenti nullify.

    Cross-domain: applicabile a qualsiasi sito + schema. Non rimuove l'asset,
    solo i singoli campi inventati.

    Regole:
      - `email`: must appear substring (lowercase) in raw_text o raw_html
      - `whatsapp`: cerca la sequenza di cifre piu' lunga (almeno 6) nel raw
      - `telegram`: cerca l'handle (`@xxx` o segment dopo `t.me/`) nel raw
      - `social[]`: ogni `url` deve apparire (sottostringa case-insensitive) nel raw_html
    """
    if not isinstance(obj, dict):
        return obj

    haystack_text = (raw_text or "").lower()
    haystack_html = (raw_html or "").lower()

    def _appears(needle: str) -> bool:
        n = (needle or "").strip().lower()
        return bool(n) and (n in haystack_text or n in haystack_html)

    # email
    email = obj.get("email")
    if isinstance(email, str) and email.strip():
        if not _appears(email.strip()):
            obj["email"] = None

    # whatsapp — estraggo le cifre, cerco la sequenza nel raw
    wa = obj.get("whatsapp")
    if isinstance(wa, str) and wa.strip():
        digits = re.sub(r"\D", "", wa)
        if len(digits) < 6 or (digits not in haystack_text and digits not in haystack_html):
            obj["whatsapp"] = None

    # telegram — estraggo l'handle (parte dopo @ o t.me/)
    tg = obj.get("telegram")
    if isinstance(tg, str) and tg.strip():
        m = re.search(r"(?:@|t\.me/|telegram\.me/)([A-Za-z0-9_]{3,})", tg)
        handle = m.group(1).lower() if m else tg.strip().lstrip("@").lower()
        if not handle or (handle not in haystack_text and handle not in haystack_html):
            obj["telegram"] = None

    # social[] — ogni URL deve esistere nel HTML grezzo
    social = obj.get("social")
    if isinstance(social, list):
        filtered = []
        for s in social:
            if not isinstance(s, dict):
                continue
            url_s = (s.get("url") or "").strip()
            if not url_s:
                continue
            if _appears(url_s) or _appears(url_s.rstrip("/")):
                filtered.append(s)
        obj["social"] = filtered

    # sitoweb
    site = obj.get("sitoweb")
    if isinstance(site, str) and site.strip():
        if not _appears(site.strip()) and not _appears(site.strip().rstrip("/")):
            obj["sitoweb"] = None

    return obj


def _looks_like_empty_or_placeholder(
    title: str | None, text: str | None
) -> str | None:
    """Detection generica di pagina vuota / placeholder / errore. Ritorna la
    ragione (string) se la pagina va skippata, None se sembra reale.

    Logica conservativa: skippa solo con segnali forti, per evitare falsi
    positivi che farebbero perdere dati legittimi.

    Cross-domain: nessun pattern e' specifico di un sito. Funziona su qualsiasi
    pagina che usa stringhe inglesi/italiane standard di errore.
    """
    title_l = (title or "").strip().lower()
    text_l = (text or "").strip().lower()

    # Segnale 1: title contiene placeholder noto
    for pat in _EMPTY_PLACEHOLDER_TITLE_PATTERNS:
        if pat in title_l:
            return f"title placeholder: '{pat}'"

    # Segnale 2: body contiene messaggio di errore esplicito
    for pat in _EMPTY_PLACEHOLDER_BODY_PATTERNS:
        if pat in text_l:
            return f"body error pattern: '{pat}'"

    # Segnale 3: body troppo corto (< 80 char) E nessun marker di contenuto reale
    # (es. presenza di email, telefono, username, age, ecc.)
    if len(text_l) < 80:
        has_content_marker = any(
            m in text_l for m in (
                "@", "+1", "+39", "whatsapp", "telegram",
                "age", "anni", "born", "nat", "city", "città",
            )
        )
        if not has_content_marker:
            return f"body too short ({len(text_l)} char) e senza marker di contenuto"

    return None


async def _tool_extract_target(
    url: str,
    *,
    seed_reg_domain: str,
    llm_client: httpx.AsyncClient,
    fetch_client: httpx.AsyncClient,
    base_url: str,
    api_key: str,
    model: str,
    schema_text: str,
    extraction_template: str | None,
    extracted_urls: set[str],
    has_min_data_fn,
    learned_subpath: str | None = None,
    n_assets_collected: int = 0,
    max_targets: int = 0,
    exploration_queue: list[str] | None = None,
) -> tuple[str, dict[str, Any] | None, bool]:
    """Fetch + LLM extract con schema + validation post-template.

    Ritorna (tool_output_string, asset_obj_or_None, is_complete).
    asset_obj_or_None viene scritto nel profiles.jsonl se non None.
    is_complete = True se tutti i campi-chiave del template sono popolati.
    """
    if not url or not url.startswith(("http://", "https://")):
        return json.dumps({"ok": False, "reason": "URL non valido"}), None, False
    host = (urlparse(url).hostname or "").lower()
    if _registrable_domain(host) != seed_reg_domain:
        return (
            json.dumps({"ok": False, "reason": f"URL fuori dominio (atteso *.{seed_reg_domain})"}),
            None,
            False,
        )
    if url in extracted_urls:
        return json.dumps({"ok": False, "reason": "URL gia' estratto in questa sessione"}), None, False
    if not extraction_template or not schema_text:
        return (
            json.dumps(
                {"ok": False, "reason": "task senza extraction_template: extract_target non disponibile"}
            ),
            None,
            False,
        )

    # Fetch raw text via readability
    try:
        from readability import Document
        from selectolax.parser import HTMLParser

        r = await fetch_client.get(url)
        if r.status_code >= 400:
            return (
                json.dumps({"ok": False, "url": url, "reason": f"HTTP {r.status_code}"}),
                None,
                False,
            )
        ct = (r.headers.get("content-type") or "").lower()
        if "html" not in ct and "xml" not in ct:
            return (
                json.dumps({"ok": False, "url": url, "reason": f"content-type non HTML: {ct}"}),
                None,
                False,
            )
        html = r.text
        summary_html = ""  # main-content extracted by readability (no header/footer/nav)
        try:
            doc = Document(html)
            title_page = (doc.short_title() or "").strip()
            summary_html = doc.summary(html_partial=True) or ""
            text = (
                HTMLParser(summary_html).body.text(separator="\n", strip=True)
                if summary_html
                else ""
            )
        except Exception:
            title_page = ""
            text = ""
        if not text:
            try:
                text = HTMLParser(html).body.text(separator="\n", strip=True)
            except Exception:
                text = ""
    except Exception as e:
        return (
            json.dumps({"ok": False, "url": url, "reason": f"fetch error: {type(e).__name__}: {e}"}),
            None,
            False,
        )

    # Pre-check pagina vuota / placeholder: skip LLM extract se la pagina
    # e' chiaramente un fallback del sito (profilo sospeso, 404, registration wall).
    # Risparmio token significativo su siti tipo babepedia con molti placeholder.
    skip_reason = _looks_like_empty_or_placeholder(title_page, text)
    if skip_reason:
        return (
            json.dumps({
                "ok": False,
                "url": url,
                "reason": f"pre-check empty page: {skip_reason}",
                "skipped_pre_llm": True,
            }),
            None,
            False,
        )

    extracted_urls.add(url)

    # LLM extract — riuso _llm_extract_json di runner_bulk_extract.
    # 1 retry su JSON invalido: i modelli code-tuned a volte emettono prosa al primo
    # giro (specie con prompt lungo); un secondo tentativo con stessa input quasi
    # sempre lo riallinea. Costo: +1 chiamata LLM nei casi di fail (raro).
    from .runner_bulk_extract import _llm_extract_json

    obj = None
    raw = ""
    for attempt in range(2):
        obj, raw = await _llm_extract_json(
            llm_client,
            base_url=base_url,
            api_key=api_key,
            model=model,
            text=text,
            url=url,
            schema=schema_text,
        )
        if obj is not None:
            break
        if attempt == 0:
            log.debug("extract_target: JSON invalido al primo tentativo per %s, retry", url)
    if obj is None:
        return (
            json.dumps(
                {"ok": False, "url": url, "reason": "LLM non ha emesso JSON valido (anche dopo retry)"}
            ),
            None,
            False,
        )

    # DOM-search enrichment: cerca pattern di contatti (mailto, wa.me, t.me, social)
    # nel raw HTML PRIVATO di header/footer/nav. Razionale:
    # - readability (`summary_html`) butta i blocchi link social del profilo perche'
    #   non sono "narrativa" → perdiamo i contatti veri.
    # - raw HTML grezzo include footer/header con contatti GLOBALI del sito (info@dominio,
    #   twitter del brand) → false attribuzioni.
    # La via di mezzo: rimuoviamo selettivamente le zone "globali" (header/footer/nav)
    # e teniamo tutto il resto (sidebar, blocchi link, attributi href).
    try:
        clean_html = _strip_global_chrome(html)
        obj = _enrich_obj_from_dom(obj, clean_html, extraction_template, source_url=url)
    except Exception as e:
        log.debug("DOM enrich failed for %s: %s", url, e)

    # Post-validation anti-hallucination: per i campi critici (contatti) verifico
    # che il valore prodotto dall'LLM esista letteralmente nel testo o HTML
    # grezzo. Se l'LLM ha "inventato" un'email/whatsapp/telegram non presente
    # nella pagina → nullify. Generica cross-domain, riduce hallucinations su
    # modelli che decidono di "inferire" contatti dal slug URL o dal nome.
    try:
        obj = _validate_critical_fields(obj, raw_text=text, raw_html=html)
    except Exception as e:
        log.debug("post-validation failed for %s: %s", url, e)

    # Validation post-template (riusa _has_minimal_data_for)
    if not has_min_data_fn(extraction_template, obj):
        out_no = {
            "ok": False,
            "url": url,
            "reason": (
                f"campi-chiave del template '{extraction_template}' tutti vuoti: "
                f"questa pagina non e' un target valido"
            ),
            "title_page": title_page[:120],
        }
        # Hint subpath imparato: se sappiamo che su questo sito i target completi
        # vivono in /social-links/ (o simile), suggerisci di provarlo prima di
        # passare al prossimo profilo.
        if learned_subpath and learned_subpath not in url:
            out_no["hint"] = (
                f"Su questo sito target completi sono stati estratti dal subpath "
                f"'{learned_subpath}/'. Prova fetch_page('{url.rstrip('/')}{learned_subpath}/') "
                f"prima di scartare questo profilo."
            )
        return (json.dumps(out_no, ensure_ascii=False), None, False)

    # Arricchisci con metadati standard
    obj.setdefault("source_url", url)
    obj.setdefault("source_domain", (urlparse(url).hostname or "").lower())
    obj.setdefault("page_title", title_page[:200])
    obj.setdefault("crawled_at", datetime.now(timezone.utc).isoformat())

    # display_name fallback: il LLM extract spesso lascia vuoto questo campo per
    # profile_contacts; ricostruiamolo dal title_page (priorita') o dal subdomain.
    if extraction_template == "profile_contacts" and not _is_field_filled(obj.get("display_name")):
        derived = _derive_display_name(title_page, url)
        if derived:
            obj["display_name"] = derived

    # Diagnostica per il LLM: quali campi-chiave sono popolati e quali no.
    # Questo guida l'agente a esplorare sotto-pagine se i contatti / dati
    # critici sono mancanti, invece di passare subito al prossimo profilo.
    fields_filled, fields_missing = _describe_extracted_fields(extraction_template, obj)
    is_complete = _compute_is_complete(extraction_template, obj)

    out_ok: dict[str, Any] = {
        "ok": True,
        "url": url,
        "asset_summary": _summarize_asset(extraction_template, obj),
        "fields_filled": fields_filled,
        "fields_missing": fields_missing,
        "is_complete": is_complete,
    }
    # Runner state hint: il modello a volte allucina sul cap target (lo confonde
    # con un numero scritto nell'objective dall'utente). Glielo ricordiamo qui
    # con i veri numeri ufficiali del runner. Da considerare AUTORITATIVO.
    if max_targets > 0:
        # +1 perche' questo asset sara' contato dopo il return
        new_total = n_assets_collected + 1 if is_complete or fields_filled else n_assets_collected
        rimanenti = max(0, max_targets - new_total)
        queue_size = len(exploration_queue) if exploration_queue else 0
        # Fix F + H: reminder esplicito + queue exploration awareness.
        directive_parts = [
            f"Hai estratto {new_total}/{max_targets}.",
            "IGNORA qualunque numero diverso scritto nell'objective utente: "
            f"il cap REALE e' {max_targets}.",
        ]
        if queue_size > 0:
            directive_parts.append(
                f"⚠️ La queue di esplorazione ha ancora {queue_size} listing da visitare: "
                f"{(exploration_queue or [])[:3]}. Quando esaurisci gli URL freschi del "
                f"latest fetch_page, fai fetch_page sul prossimo URL della queue per "
                f"trovare altri profili. NON chiamare done() finche' la queue non e' vuota."
            )
        elif rimanenti > 30:
            directive_parts.append(
                f"⚠️ Hai ancora {rimanenti} target da raccogliere e la queue di esplorazione "
                "e' vuota. PRIMA di chiamare done(), considera se ci sono ALTRE listing del "
                "sito non ancora esplorate (paginazione `?p=2`, sub-categorie es. "
                "`/donne/`, `/trans/`, `/categoria/`). Se si', usa enqueue_listings()."
            )
        else:
            directive_parts.append("Continua finche' rimanenti=0 o esaurite tutte le listing.")
        out_ok["⚠️_RUNNER_STATE_AUTORITATIVO"] = {
            "estratti_finora": new_total,
            "cap_target_REALE": max_targets,
            "rimanenti": rimanenti,
            "queue_size": queue_size,
            "queue_preview": (exploration_queue or [])[:3],
            "DIRETTIVA": " ".join(directive_parts),
        }
    # Hint sul subpath imparato: se l'estrazione e' incompleta e sappiamo gia'
    # che target completi vivono in un certo subpath, suggeriscilo invece di
    # lasciare che l'agente passi subito al prossimo profilo.
    if not is_complete and learned_subpath and learned_subpath not in url:
        out_ok["hint"] = (
            f"Profilo incompleto. Su questo sito target completi sono stati estratti "
            f"dal subpath '{learned_subpath}/'. Prova fetch_page("
            f"'{url.rstrip('/')}{learned_subpath}/') prima di passare al prossimo."
        )
    return (json.dumps(out_ok, ensure_ascii=False), obj, is_complete)


# Pattern regex per estrarre contatti dal raw HTML (anche dentro data-*, href, JSON-LD,
# zone non-readability). Ogni pattern ha una "dimensione": prendiamo solo il primo match
# significativo per non inquinare con duplicati.
_RE_MAILTO = re.compile(r"mailto:\s*([A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,})", re.IGNORECASE)
_RE_EMAIL_INLINE = re.compile(r"\b([A-Za-z0-9._%+\-]{2,}@[A-Za-z0-9.\-]+\.[A-Za-z]{2,})\b")
_RE_TEL = re.compile(r"tel:\s*(\+?\d[\d\s\.\-/]{6,}\d)", re.IGNORECASE)
_RE_PHONE_IT = re.compile(r"(\+?39[\s\.\-]?)?(3\d{2}[\s\.\-]?\d{6,7})")
_RE_WHATSAPP = re.compile(
    r"https?://(?:wa\.me|api\.whatsapp\.com/send)(?:\?phone=|/)(\+?\d{6,15})",
    re.IGNORECASE,
)
_RE_TELEGRAM = re.compile(r"https?://t\.me/([A-Za-z0-9_]{3,32})\b", re.IGNORECASE)
_RE_INSTAGRAM = re.compile(r"https?://(?:www\.)?instagram\.com/([A-Za-z0-9_.]{2,30})/?", re.IGNORECASE)
_RE_TIKTOK = re.compile(r"https?://(?:www\.)?tiktok\.com/@([A-Za-z0-9_.]{2,30})", re.IGNORECASE)
_RE_TWITTER = re.compile(r"https?://(?:www\.)?(?:twitter|x)\.com/([A-Za-z0-9_]{2,30})/?", re.IGNORECASE)
_RE_FACEBOOK = re.compile(r"https?://(?:www\.)?facebook\.com/([A-Za-z0-9.\-]{2,50})/?", re.IGNORECASE)
_RE_YOUTUBE = re.compile(
    r"https?://(?:www\.)?youtube\.com/(?:c/|channel/|@)([A-Za-z0-9_\-]{2,50})/?",
    re.IGNORECASE,
)
_RE_LINKEDIN = re.compile(r"https?://(?:www\.)?linkedin\.com/in/([A-Za-z0-9\-_]{2,50})/?", re.IGNORECASE)
_RE_ONLYFANS = re.compile(r"https?://(?:www\.)?onlyfans\.com/([A-Za-z0-9_]{2,30})", re.IGNORECASE)
_RE_LINKTREE = re.compile(r"https?://(?:www\.)?linktr\.ee/([A-Za-z0-9_.]{2,50})", re.IGNORECASE)

# Email "junk" da ignorare (sviluppatori, mailing list, generiche)
_EMAIL_JUNK_HOSTS = {
    "sentry.io", "wixpress.com", "example.com", "example.org", "test.com",
    "domain.com", "yourdomain.com", "email.com",
}
_EMAIL_JUNK_LOCAL_PARTS = {"noreply", "no-reply", "do-not-reply", "donotreply"}


def _is_real_email(addr: str) -> bool:
    addr = (addr or "").strip().lower()
    if "@" not in addr:
        return False
    local, _, host = addr.partition("@")
    if local in _EMAIL_JUNK_LOCAL_PARTS:
        return False
    if host in _EMAIL_JUNK_HOSTS:
        return False
    if "." not in host:
        return False
    return True


def _strip_global_chrome(html: str) -> str:
    """Rimuove header/footer/nav (zone 'globali del sito') dal HTML, mantenendo
    tutto il resto del body (incluse sidebar, blocchi link social del profilo,
    attributi href). Usato come pre-processing al DOM enrichment per evitare di
    attribuire al profilo contatti del sito (footer info@dominio, twitter brand).
    """
    if not html:
        return ""
    try:
        from selectolax.parser import HTMLParser
        parser = HTMLParser(html)
        # Selettori "chrome globale": tag semantici + role ARIA + class names comuni.
        for sel in (
            "header", "footer", "nav",
            "[role='banner']", "[role='navigation']", "[role='contentinfo']",
            ".site-header", ".site-footer", ".main-nav", ".main-header", ".main-footer",
            "#header", "#footer", "#nav", "#site-header", "#site-footer",
        ):
            try:
                for el in parser.css(sel):
                    el.decompose()
            except Exception:
                continue
        return parser.html or ""
    except Exception:
        return html  # fallback: meglio raw che vuoto


def _is_brand_handle(handle: str, source_url: str | None) -> bool:
    """Euristica anti-brand: se l'handle social e' simile al dominio del sito
    (es. handle='MondoCamGirls' su sito 'mondocamgirls.com'), e' del SITO,
    non del profilo. Scarta.

    Match case-insensitive, normalizzato (rimuovi `_`, `-`, `.`).
    """
    if not handle or not source_url:
        return False
    try:
        host = (urlparse(source_url).hostname or "").lower()
        reg = _registrable_domain(host)  # 'mondocamgirls.com'
        if not reg:
            return False
        brand = reg.split(".")[0]  # 'mondocamgirls'
        h_norm = re.sub(r"[_\-\.]", "", handle.lower())
        b_norm = re.sub(r"[_\-\.]", "", brand)
        return h_norm == b_norm
    except Exception:
        return False


def _enrich_obj_from_dom(
    obj: dict[str, Any],
    html: str,
    asset_type: str,
    source_url: str | None = None,
) -> dict[str, Any]:
    """Estrae pattern di contatto dal raw HTML e popola campi LASCIATI VUOTI dal LLM.

    Non sovrascrive mai campi gia' popolati dal LLM. Cerca anche dentro:
    - href= (link mailto, tel, wa.me, t.me, social)
    - data-* attributi (es. data-whatsapp, data-phone)
    - testo libero del body (regex inline)
    - JSON-LD strutturato (es. <script type="application/ld+json">)

    Solo per i campi rilevanti del template:
    - profile_contacts: email, whatsapp, telegram, social[], sitoweb
    - real_estate: email_agenzia, telefono_agenzia
    - ecommerce_products: (niente di specifico, il LLM e' affidabile)
    - events / news_articles / job_listings: (idem)
    """
    if not html:
        return obj

    # --- Helpers ---
    def _set_if_empty(key: str, value: Any) -> None:
        if not _is_field_filled(obj.get(key)) and _is_field_filled(value):
            obj[key] = value

    # Brand del sito (per filtro anti-brand su email/social).
    site_brand_domain: str | None = None
    if source_url:
        try:
            site_host = (urlparse(source_url).hostname or "").lower()
            site_brand_domain = _registrable_domain(site_host)  # 'mondocamgirls.com'
        except Exception:
            site_brand_domain = None

    def _is_site_email(addr: str) -> bool:
        """True se l'email ha lo stesso dominio del sito (es. info@mondocamgirls.com
        su mondocamgirls.com → e' del sito, non del profilo)."""
        if not site_brand_domain or "@" not in addr:
            return False
        return addr.lower().endswith("@" + site_brand_domain)

    # Email: prima i mailto:, poi inline
    if asset_type in ("profile_contacts", "real_estate") and not _is_field_filled(obj.get("email" if asset_type == "profile_contacts" else "email_agenzia")):
        emails: list[str] = []
        for m in _RE_MAILTO.finditer(html):
            e = m.group(1).strip().lower()
            if _is_real_email(e) and not _is_site_email(e) and e not in emails:
                emails.append(e)
        if not emails:
            for m in _RE_EMAIL_INLINE.finditer(html):
                e = m.group(1).strip().lower()
                if _is_real_email(e) and not _is_site_email(e) and e not in emails:
                    emails.append(e)
                if len(emails) >= 3:
                    break
        if emails:
            target_field = "email" if asset_type == "profile_contacts" else "email_agenzia"
            _set_if_empty(target_field, emails[0])

    # WhatsApp (solo profile_contacts)
    if asset_type == "profile_contacts":
        for m in _RE_WHATSAPP.finditer(html):
            num = m.group(1).strip()
            if num and len(num) >= 8:
                _set_if_empty("whatsapp", num)
                break

    # Telegram (solo profile_contacts) — con filtro anti-brand
    if asset_type == "profile_contacts":
        for m in _RE_TELEGRAM.finditer(html):
            handle = m.group(1).strip()
            if not handle or handle.lower() in {"share", "joinchat", "joinchatlink"}:
                continue
            if _is_brand_handle(handle, source_url):
                continue  # es. t.me/MondoCamGirls su mondocamgirls.com → del sito
            _set_if_empty("telegram", handle)
            break

    # Telefono (mailto-like tel:)
    if asset_type == "real_estate" and not _is_field_filled(obj.get("telefono_agenzia")):
        for m in _RE_TEL.finditer(html):
            num = m.group(1).strip()
            if num and len(re.sub(r"[^\d]", "", num)) >= 8:
                _set_if_empty("telefono_agenzia", num)
                break

    # Social: instagram, tiktok, twitter/x, facebook, youtube, linkedin, onlyfans, linktree
    # Solo per profile_contacts (per altri template lo schema non ha social[]).
    if asset_type == "profile_contacts":
        existing_social = obj.get("social") if isinstance(obj.get("social"), list) else []
        seen_urls = {(s.get("url") or "").lower() for s in existing_social if isinstance(s, dict)}
        seen_platforms = {(s.get("platform") or "").lower() for s in existing_social if isinstance(s, dict)}

        def _add_social(platform: str, url: str) -> None:
            url = url.strip()
            if not url:
                return
            url_l = url.lower()
            if url_l in seen_urls:
                return
            if platform.lower() in seen_platforms:
                # gia' presente per quella piattaforma, non duplicare
                return
            existing_social.append({"platform": platform, "url": url})
            seen_urls.add(url_l)
            seen_platforms.add(platform.lower())

        # Per ogni piattaforma social: scarta junk path + scarta handle = brand del sito.
        for m in _RE_INSTAGRAM.finditer(html):
            handle = m.group(1).strip()
            if not handle or handle in {"p", "explore", "reel", "stories", "accounts"}:
                continue
            if _is_brand_handle(handle, source_url):
                continue
            _add_social("instagram", f"https://instagram.com/{handle}")
            break
        for m in _RE_TIKTOK.finditer(html):
            handle = m.group(1).strip()
            if _is_brand_handle(handle, source_url):
                continue
            _add_social("tiktok", f"https://tiktok.com/@{handle}")
            break
        for m in _RE_TWITTER.finditer(html):
            handle = m.group(1).strip()
            if handle.lower() in {"share", "intent", "search"}:
                continue
            if _is_brand_handle(handle, source_url):
                continue
            _add_social("twitter", f"https://twitter.com/{handle}")
            break
        for m in _RE_FACEBOOK.finditer(html):
            handle = m.group(1).strip()
            if handle.lower() in {"sharer.php", "tr", "dialog"}:
                continue
            if _is_brand_handle(handle, source_url):
                continue
            _add_social("facebook", f"https://facebook.com/{handle}")
            break
        for m in _RE_YOUTUBE.finditer(html):
            handle = m.group(1).strip()
            if _is_brand_handle(handle, source_url):
                continue
            _add_social("youtube", f"https://youtube.com/@{handle}")
            break
        for m in _RE_LINKEDIN.finditer(html):
            handle = m.group(1).strip()
            if _is_brand_handle(handle, source_url):
                continue
            _add_social("linkedin", f"https://linkedin.com/in/{handle}")
            break
        for m in _RE_ONLYFANS.finditer(html):
            handle = m.group(1).strip()
            if _is_brand_handle(handle, source_url):
                continue
            _add_social("onlyfans", f"https://onlyfans.com/{handle}")
            break
        for m in _RE_LINKTREE.finditer(html):
            handle = m.group(1).strip()
            if _is_brand_handle(handle, source_url):
                continue
            _add_social("linktree", f"https://linktr.ee/{handle}")
            break

        if existing_social:
            obj["social"] = existing_social

    return obj


# Campi "chiave" per template (quelli che vogliamo davvero popolati per dichiarare
# l'asset "completo"). I campi non chiave (lang, crawled_at, ecc.) non contano.
_KEY_FIELDS_BY_TEMPLATE: dict[str, list[str]] = {
    "profile_contacts": ["display_name", "email", "whatsapp", "telegram", "social", "sitoweb"],
    "real_estate": ["prezzo_eur", "metri_quadri", "locali", "categoria", "citta", "indirizzo", "agenzia"],
    "ecommerce_products": ["name", "price_amount", "brand", "category", "availability"],
    "events": ["title", "start_datetime", "venue", "city"],
    "news_articles": ["title", "author", "published_at", "summary"],
    "job_listings": ["title", "company", "location", "salary_min_eur", "salary_max_eur"],
}


def _is_field_filled(value: Any) -> bool:
    """True se il valore e' significativamente popolato (non None, non '', non []/{} vuoti, non 0)."""
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, dict, tuple)):
        return bool(value)
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    return True


def _describe_extracted_fields(asset_type: str, obj: dict[str, Any]) -> tuple[list[str], list[str]]:
    """Per il template indicato, ritorna (filled, missing) sui campi-chiave.
    Se template non e' nella mappa, ritorna ([], []) — niente check."""
    keys = _KEY_FIELDS_BY_TEMPLATE.get(asset_type) or []
    filled: list[str] = []
    missing: list[str] = []
    for k in keys:
        if _is_field_filled(obj.get(k)):
            filled.append(k)
        else:
            missing.append(k)
    return filled, missing


# Campi "contact channel" per profile_contacts: basta UNO popolato per dichiarare
# il profilo "completo" (insieme al display_name). Razionale: un profilo reale ha
# 1-3 canali di contatto, mai tutti e 5 (email + whatsapp + telegram + social + sitoweb).
# Pretendere TUTTI bloccava il pattern learning e induceva il LLM a chiudere troppo presto.
_PROFILE_CONTACT_CHANNELS = ("email", "whatsapp", "telegram", "social", "sitoweb")


def _compute_is_complete(asset_type: str, obj: dict[str, Any]) -> bool:
    """Soglia 'completo' tarata per template (NON richiede tutti i campi-chiave).

    - profile_contacts: display_name + ALMENO 1 contact channel.
    - real_estate: prezzo_eur + (citta o indirizzo) + (categoria o tipo).
    - ecommerce_products: name + price_amount.
    - events: title + (start_datetime o (city e venue)).
    - news_articles: title + (author o published_at).
    - job_listings: title + company.
    - default: tutti i campi-chiave popolati (fallback strict).
    """
    g = lambda k: _is_field_filled(obj.get(k))
    if asset_type == "profile_contacts":
        return g("display_name") and any(g(k) for k in _PROFILE_CONTACT_CHANNELS)
    if asset_type == "real_estate":
        return g("prezzo_eur") and (g("citta") or g("indirizzo")) and (g("categoria") or g("tipo"))
    if asset_type == "ecommerce_products":
        return g("name") and g("price_amount")
    if asset_type == "events":
        return g("title") and (g("start_datetime") or (g("city") and g("venue")))
    if asset_type == "news_articles":
        return g("title") and (g("author") or g("published_at"))
    if asset_type == "job_listings":
        return g("title") and g("company")
    # fallback: come prima (tutti i campi-chiave popolati)
    keys = _KEY_FIELDS_BY_TEMPLATE.get(asset_type) or []
    return bool(keys) and all(g(k) for k in keys)


_DISPLAY_NAME_NOISE = re.compile(
    r"\s*[-–—|•·]+\s*(cam ?girl|profile|profilo|home|page|onlyfans|escort|annuncio|annunci|model|modella).*$",
    re.IGNORECASE,
)


def _derive_display_name(title_page: str, url: str) -> str | None:
    """Ricostruisce un display_name plausibile per profile_contacts.

    Strategia:
    1. Title della pagina (priorita'): tipicamente "Miss Giadina - Cam Girl" o
       "SARA ❤ LOVE - Cam Girl". Tagliamo tutto dopo il primo "-" o "|" che
       introduce noise (cam girl, profilo, ecc.).
    2. Subdomain prettificato: "missgiadina.mondocamgirls.com" → "Missgiadina".
       Fallback se title vuoto/inutile.
    """
    # 1. Title cleaning
    if title_page:
        t = title_page.strip()
        # Rimuove suffix tipo " - Cam Girl", " | Profilo", ecc.
        cleaned = _DISPLAY_NAME_NOISE.sub("", t).strip()
        # Se dopo la pulizia resta qualcosa di sensato (>=2 caratteri, non solo simboli),
        # usalo. Altrimenti fallback subdomain.
        if cleaned and len(cleaned) >= 2 and re.search(r"[A-Za-z]", cleaned):
            return cleaned[:120]

    # 2. Subdomain
    try:
        host = (urlparse(url).hostname or "").lower()
        reg = _registrable_domain(host)
        if host and reg and host != reg:
            # es. host="missgiadina.mondocamgirls.com" reg="mondocamgirls.com"
            sub = host[: -(len(reg) + 1)]  # "missgiadina"
            if sub and sub != "www":
                # prettifica: capitalizza e rimuovi numeri trailing
                pretty = re.sub(r"\d+$", "", sub).strip()
                if pretty:
                    return pretty[:1].upper() + pretty[1:]
                return sub[:1].upper() + sub[1:]
    except Exception:
        pass
    return None


def _is_already_done_output(tool_output: str) -> bool:
    """True se il tool_output di extract_target indica che l'URL e' gia' stato
    estratto/visitato in questa sessione (anti-loop trigger)."""
    try:
        d = json.loads(tool_output)
    except Exception:
        return False
    reason = (d.get("reason") or "").lower()
    return "gia' estratto" in reason or "gia' visitato" in reason


def _is_already_visited_fetch_output(tool_output: str) -> bool:
    """True se il tool_output di fetch_page indica che la pagina e' gia' stata
    visitata. Anti-loop trigger per fetch_page (Fix D)."""
    try:
        d = json.loads(tool_output)
    except Exception:
        return False
    if d.get("ok"):
        return False
    reason = (d.get("reason") or "").lower()
    return "gia' visitato" in reason


def _derive_subpath(incomplete_url: str, complete_url: str) -> str | None:
    """Se complete_url e' incomplete_url + un suffisso di path (stesso host),
    ritorna il suffisso (es. '/social-links'). Altrimenti None.

    Esempi:
      ('https://alice.example.com/', 'https://alice.example.com/social-links/')
        → '/social-links'
      ('https://alice.example.com/profile/', 'https://alice.example.com/profile/contacts/')
        → '/contacts'
      ('https://alice.example.com/', 'https://bob.example.com/social-links/') → None
    """
    try:
        a = urlparse(incomplete_url)
        b = urlparse(complete_url)
    except Exception:
        return None
    if a.netloc != b.netloc:
        return None
    a_path = (a.path or "/").rstrip("/")
    b_path = (b.path or "/").rstrip("/")
    if not b_path.startswith(a_path):
        return None
    suffix = b_path[len(a_path):]
    if not suffix or not suffix.startswith("/"):
        return None
    # Sanity: non vogliamo subpath come "/123/456" (numerici) — sono ID, non pattern.
    # Vogliamo subpath testuali tipo "/social-links", "/contatti", "/info".
    seg = suffix.strip("/").split("/")[0]
    if seg.isdigit():
        return None
    return suffix


def _truncate_url(url: str, max_len: int = 70) -> str:
    if not url:
        return "(vuoto)"
    if len(url) <= max_len:
        return url
    return url[: max_len - 3] + "..."


def _summarize_fetch_output(tool_output: str) -> str:
    """Compatta l'output JSON di fetch_page in una riga leggibile per i log."""
    try:
        d = json.loads(tool_output)
    except Exception:
        return "(output non parseable)"
    if not d.get("ok"):
        return f"FAIL: {d.get('reason') or 'unknown'}"
    n_links = d.get("n_internal_links", 0)
    patterns = d.get("link_patterns") or []
    top_pat = ""
    if patterns:
        p0 = patterns[0]
        top_urls = p0.get("urls") or p0.get("examples") or []
        suffix = f" [{len(top_urls)} URL esposti]" if top_urls else ""
        top_pat = f", top: {p0.get('pattern')} ({p0.get('count')} URL){suffix}"
    title = (d.get("title") or "").strip()
    title_str = f' "{title[:50]}"' if title else ""
    return f"{n_links} link, {len(patterns)} pattern{top_pat}{title_str}"


def _summarize_extract_output(tool_output: str) -> str:
    """Compatta l'output JSON di extract_target in una riga leggibile."""
    try:
        d = json.loads(tool_output)
    except Exception:
        return "(output non parseable)"
    if d.get("ok"):
        summary = d.get("asset_summary") or "(senza summary)"
        missing = d.get("fields_missing") or []
        if missing:
            # Mostra i primi 3 campi mancanti come hint per l'utente che legge il log
            missing_preview = ", ".join(missing[:3])
            if len(missing) > 3:
                missing_preview += f", +{len(missing)-3}"
            return f"{summary} | INCOMPLETO (mancano: {missing_preview})"
        return summary
    reason = d.get("reason") or "unknown"
    return f"no: {reason[:120]}"


def _summarize_asset(template: str, obj: dict[str, Any]) -> str:
    """Riepilogo umano-leggibile per il LLM (1-2 righe), evita di re-iniettare
    tutto il raw_json nel context window."""
    bits: list[str] = []
    if template == "real_estate":
        for k in ("categoria", "citta", "tipo"):
            v = obj.get(k)
            if v:
                bits.append(str(v))
        prezzo = obj.get("prezzo_eur")
        if prezzo:
            bits.append(f"€{prezzo}")
    elif template == "ecommerce_products":
        for k in ("name", "brand", "price_amount"):
            v = obj.get(k)
            if v:
                bits.append(str(v))
    elif template == "events":
        for k in ("title", "city"):
            v = obj.get(k)
            if v:
                bits.append(str(v))
    elif template == "news_articles":
        bits.append(str(obj.get("title") or "")[:80])
    elif template == "job_listings":
        bits.append(str(obj.get("title") or "")[:80])
        if obj.get("company"):
            bits.append(str(obj["company"]))
    elif template == "profile_contacts":
        for k in ("display_name", "username"):
            v = obj.get(k)
            if v:
                bits.append(str(v))
    return " · ".join(b for b in bits if b)[:200] or "(senza summary)"
