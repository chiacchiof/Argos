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


_DEFAULT_MAX_ITER = 30
_DEFAULT_TARGET_PER_SITE = 30
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
    db.update_job(job_id, status="running", started_at=db.now_iso())
    db.set_control_signal(job_id, None)
    jlog(
        f"Avvio site_explorer per task #{task['id']} \"{task['name']}\" — "
        f"agente ReAct su LLM"
    )

    # 1. Risorse e config LLM
    provider_key = task.get("llm_provider") or "ollama"
    try:
        base_url = resolve_base_url(provider_key, task.get("llm_base_url"))
        api_key = resolve_api_key(provider_key, task.get("llm_api_key"))
    except RuntimeError as e:
        jlog(f"ERRORE configurazione provider: {e}")
        db.update_job(job_id, status="error", error=str(e), finished_at=db.now_iso())
        raise
    model = task["model"]
    jlog(f"Provider/Modello: {provider_key} / {model}")

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

    max_steps = int(task.get("max_iterations") or _DEFAULT_MAX_ITER)
    # target_cap_per_site: campo dedicato ai runner che hanno una nozione di "asset
    # estratti per sito" (site_explorer e auto_extract→site_explorer). Fallback:
    # se non valorizzato (task creati prima di 2026-05-10), default 30.
    max_targets = max(1, min(int(task.get("target_cap_per_site") or _DEFAULT_TARGET_PER_SITE), 200))

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

    # 3. Sistema prompt + messaggi iniziali
    system_prompt = _build_system_prompt(
        objective=(task.get("objective") or "").strip(),
        schema_text=schema_text,
        extraction_template=extraction_template,
        seed_reg_domain=seed_reg_domain,
        seed_url=seed_url,
        max_steps=max_steps,
        max_targets=max_targets,
    )
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system_prompt},
        {
            "role": "user",
            "content": (
                f"Inizia esplorando il seed: {seed_url}\n\n"
                f"Strategia consigliata: prima fetch_page(seed) per capire la struttura, "
                f"poi naviga verso le sezioni che probabilmente contengono i target."
            ),
        },
    ]

    payload: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "temperature": 0.1,
        "max_tokens": 600,
        "tools": SITE_EXPLORER_TOOLS_SPEC,
        "tool_choice": "auto",
    }
    headers = {"Authorization": f"Bearer {api_key}"}
    api_url = f"{base_url.rstrip('/')}/chat/completions"

    # 4. Loop ReAct
    async with httpx.AsyncClient(timeout=120) as llm_client, httpx.AsyncClient(
        timeout=_FETCH_TIMEOUT_S,
        follow_redirects=True,
        headers={"User-Agent": settings.http_user_agent},
    ) as fetch_client:
        try:
            for step in range(max_steps):
                if db.get_control_signal(job_id) == "stop":
                    jlog("STOP richiesto dall'utente.")
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
                        )
                        jlog(
                            f"  📄 step {n_steps}: fetch_page({_truncate_url(url_arg)}) "
                            f"→ {_summarize_fetch_output(tool_output)}"
                        )
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
                        )
                        if asset_obj is not None:
                            profiles_f.write(json.dumps(asset_obj, ensure_ascii=False) + "\n")
                            profiles_f.flush()
                            n_assets_collected += 1
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
                    elif name == "done":
                        done_reason = (args.get("reason") or "").strip() or "agente ha terminato"
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

            else:
                jlog(f"  ⚠️ cap step raggiunto ({max_steps}) senza done()")
                done_reason = f"cap step ({max_steps}) raggiunto"

        except asyncio.CancelledError:
            jlog("Cancellato.")
            stopped = True
            raise
        finally:
            profiles_f.close()

    # 5. Ingest in DB (riusa la pipeline asset)
    n_assets_in_db = 0
    if extraction_template:
        n_assets_in_db = _ingest_to_assets(
            profiles_path,
            task["id"],
            job_id,
            jlog,
            extraction_template=extraction_template,
        )

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
    db.update_job(
        job_id, status=final_status, finished_at=db.now_iso(),
        result_path=str(report_path),
    )
    db.set_control_signal(job_id, None)
    return str(report_path)


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
) -> str:
    template_label = extraction_template or "(nessun template specifico)"
    schema_block = (
        f"SCHEMA TARGET ({extraction_template}):\n{schema_text[:2500]}\n\n"
        if schema_text
        else ""
    )
    return (
        "Sei un agente esploratore di siti web. Il tuo compito e' navigare un sito "
        "a partire da un seed URL e ESTRARRE le pagine-dettaglio (annunci, prodotti, "
        "profili, ecc.) coerenti con l'obiettivo dell'utente.\n\n"
        f"OBIETTIVO UTENTE:\n{objective[:1500]}\n\n"
        f"{schema_block}"
        f"DOMINIO DEL SEED: {seed_reg_domain} (resta su questo dominio o sub-dominio)\n"
        f"SEED INIZIALE: {seed_url}\n"
        f"TEMPLATE TARGET: {template_label}\n\n"
        "STRATEGIA OPERATIVA:\n"
        "1. Inizia con fetch_page(seed) per capire la struttura del sito.\n"
        "2. Identifica nei link interni le SEZIONI che probabilmente linkano i target. "
        "Es. per immobili: link tipo /vendita-case/<citta>/, /immobili-vendita/, /annunci/.\n"
        "3. Scendi nelle sezioni piu' coerenti con l'obiettivo (es. la zona richiesta).\n"
        "4. Quando vedi una pagina che SEMBRA una scheda-dettaglio, prova "
        "extract_target(url). Se ok=true, hai trovato un target valido.\n"
        "5. Continua a estrarre target dalla stessa listing finche' raggiungi il cap "
        f"di {max_targets} asset, oppure scendi in altre sotto-sezioni se hai esaurito.\n"
        "6. Termina con done(reason) quando hai abbastanza target o capisci che il "
        "sito non contiene quello che cerchi.\n\n"
        "REGOLE FONDAMENTALI:\n\n"
        "**1. USA SOLO URL VISTI** (CRITICO):\n"
        "Quando chiami extract_target(url) o fetch_page(url), l'URL DEVE essere uno "
        "che hai effettivamente VISTO ritornato da una chiamata fetch_page precedente, "
        "in `link_patterns[i].urls` o `link_patterns[i].examples`.\n"
        "**MAI inventare URL** decrementando o incrementando id (es. se vedi /annuncio/659632/, "
        "NON provare /annuncio/659631/, /annuncio/658565/, ecc. a meno che siano nella lista). "
        "Inventare URL ti porta a estrarre annunci di altre città/zone, fuori dall'obiettivo.\n"
        "Per il TOP pattern di una listing, fetch_page ritorna `urls` con la lista completa: "
        "iteragli sopra in ordine, senza inventare.\n\n"
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
        "listing finisce prima. Conta correttamente: ogni log `[totale: N/M]` ti dice il vero "
        "N. Non chiamare done() prima di aver esaurito la listing della zona o raggiunto il cap.\n\n"
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
        "ESEMPIO DI TRAIETTORIA (per orientarti, NON copiare gli URL):\n"
        "  step 1: fetch_page(\"https://example-immobili.it/\")\n"
        "    → vede link /vendita-case/{citta}/ con 30+ URL, fra cui /vendita-case/acireale/\n"
        "  step 2: fetch_page(\"https://example-immobili.it/vendita-case/acireale/\")\n"
        "    → top pattern: /annuncio/{int} con `urls`: ['.../12345/', '.../12346/',\n"
        "       '.../12347/', '.../12348/', ... 25 URL totali]  ← QUESTI sono i link veri.\n"
        "  step 3: extract_target(\"https://example-immobili.it/annuncio/12345/\")  ← preso dal urls[0]\n"
        "    → ok=true, asset estratto (qualunque sia il prezzo, lo prendi)\n"
        "  step 4: extract_target(\"https://example-immobili.it/annuncio/12346/\")  ← urls[1]\n"
        "    → ok=true, asset estratto\n"
        "  step 5: extract_target(\"https://example-immobili.it/annuncio/12347/\")  ← urls[2]\n"
        "    → no_data (annuncio scaduto). Prosegui con urls[3], NON inventare /annuncio/12344/.\n"
        "  ... continua iterando sugli `urls` della listing in ordine\n"
        f"  step N: done(\"esauriti gli URL della listing acireale, totale {max_targets} estratti\")\n\n"
        "ESEMPIO TRAIETTORIA #2 — profilo persona con contatti in sotto-pagina:\n"
        "  step 1: fetch_page(\"https://www.example-cam.com/\")\n"
        "    → vede pattern /{slug}/ con 20 URL profilo (alice.example-cam.com, ...)\n"
        "  step 2: fetch_page(\"https://alice.example-cam.com/\")\n"
        "    → vede 50 link, fra cui /contatti/ e /social-links/\n"
        "  step 3: extract_target(\"https://alice.example-cam.com/\")\n"
        "    → ok=true MA fields_missing=[email, whatsapp, telegram, social] (INCOMPLETO)\n"
        "  step 4: l'asset e' incompleto → NON passare al prossimo profilo. Esploro:\n"
        "    fetch_page(\"https://alice.example-cam.com/social-links/\")\n"
        "    → vede instagram + telegram URL nei link esterni\n"
        "  step 5: extract_target(\"https://alice.example-cam.com/social-links/\")\n"
        "    → ok=true, fields_filled include 'social', is_complete=true, asset valido!\n"
        "  step 6+: passa al prossimo profilo (bob.example-cam.com)\n\n"
        "ANTI-PATTERN da EVITARE:\n"
        "- ❌ extract_target(\"/annuncio/12344/\") quando NON era in `urls`/`examples` → URL inventato\n"
        "- ❌ skip annuncio perche' il prezzo e' fuori range → quello lo fa il qualifier dopo\n"
        "- ❌ fermarsi dopo 2 target se la listing ne ha 25 → continua\n"
        "- ❌ accettare un profile_contacts incompleto (solo nome, no email/social) senza\n"
        "  prima esplorare le sotto-pagine /contatti, /social, /info\n"
    )


async def _tool_fetch_page(
    url: str,
    *,
    seed_reg_domain: str,
    client: httpx.AsyncClient,
    visited_urls: set[str],
    learned_subpath: str | None = None,
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
        # Per il TOP pattern (quello con piu' URL), ritorniamo TUTTI gli URL (cap 50):
        # questo permette all'agente di chiamare extract_target sui link veri della
        # listing senza dover inventare/indovinare id decrementando.
        # Per i pattern secondari, solo 3 esempi (basta per orientamento).
        link_summary = []
        for idx, (pat, urls) in enumerate(list(groups.items())[:_FETCH_LINK_PATTERN_TOP]):
            entry: dict[str, Any] = {
                "pattern": pat,
                "count": len(urls),
            }
            if idx == 0:
                # top pattern: lista completa (cap 50)
                entry["urls"] = urls[:50]
            else:
                entry["examples"] = urls[:3]
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
    # Hint subpath imparato: se sappiamo che su questo sito i target completi
    # vivono in /social-links/ e l'URL appena fetchata non lo include, suggerisci
    # di andare diretto la' invece di esplorare ulteriormente il listing.
    if learned_subpath and learned_subpath not in url:
        out["hint"] = (
            f"Su questo sito target completi sono stati estratti dal subpath "
            f"'{learned_subpath}/'. Per i prossimi profili, vai diretto a "
            f"<profile_url>{learned_subpath}/ invece di esplorare la home del profilo."
        )
    return json.dumps(out, ensure_ascii=False)


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
            )
        ct = (r.headers.get("content-type") or "").lower()
        if "html" not in ct and "xml" not in ct:
            return (
                json.dumps({"ok": False, "url": url, "reason": f"content-type non HTML: {ct}"}),
                None,
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
