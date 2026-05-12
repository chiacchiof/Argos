"""Runner auto_extract — il dispatcher "intelligente" che decide la strategia per ogni sito.

Per ogni URL della lista `seed_queries`:
  1. profiler analizza la home (1 chiamata HTTP + 1 LLM "capable")
  2. dispatcha al runner appropriato (bulk_extract / browser_use / skip)
  3. fallback automatico: se la strategia produce 0 profili e non era già browser_use,
     ritenta con browser_use
  4. aggrega i profiles in un unico jsonl + report.md + auto_extract_report.json

Il task auto_extract eredita tutta la configurazione (schema, modello, browser_llm_*,
discovery_llm_*, crawler, ecc.) — viene applicata ai sub-runner secondo la strategia
scelta dal profiler.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .. import db
from ..config import RESULTS_DIR
from .blocked_domains import is_blocked as _is_blocked
from .llm_providers import resolve_api_key, resolve_base_url
from .runner_bulk_extract import _registrable_domain
from .site_profiler import profile_site
from .site_recon import recon_site


log = logging.getLogger(__name__)


async def _execute_strategy(
    task: dict[str, Any],
    strategy: str,
    site_url: str,
    parent_job_id: int,
    jlog,
    prepopulated_urls: list[str] | None = None,
) -> tuple[int, Path | None]:
    """Esegue il sub-runner per quella strategia su quel singolo URL.

    `prepopulated_urls` (opzionale): URL di pagine listing gia' pronte
    (es. paginated `?page=1..N`) — passate al sub-runner che le carica nella
    propria exploration_queue saltando l'auto-discovery.

    Ritorna (n_profili_estratti, path_report_sub).
    """
    sub_task = dict(task)
    sub_task["seed_queries"] = [site_url]
    sub_task["agent_mode"] = strategy
    if prepopulated_urls:
        sub_task["prepopulated_listing_urls"] = list(prepopulated_urls)
    # Cap max_iterations per i sub-job che hanno semantica "step LLM" invece di
    # "URL processati": auto_extract puo' avere max_iterations=200/500 (URL crawled),
    # ma per browser_use e site_explorer e' il numero di step LLM e va cappato.
    if strategy == "browser_use":
        inherited = int(sub_task.get("max_iterations") or 25)
        sub_task["max_iterations"] = min(inherited, 25)
    elif strategy == "site_explorer":
        # Cap anti-loop alto: site_explorer e' un agente ReAct controllato (1 step LLM
        # = 1 fetch_page o 1 extract_target), quindi 200 step bastano per estrarre
        # ~150 profili da una directory grande. Cap minimo 50 per non scendere sotto
        # il default sensato anche se l'utente ha messo max_iterations=10 nel task.
        inherited = int(sub_task.get("max_iterations") or 50)
        sub_task["max_iterations"] = max(50, min(inherited, 200))
    # Estendi allowed_domains con il registrable domain del sito (per supportare
    # sub-domini come profili). Se l'utente ha già messo qualcosa, mantienilo
    # ma aggiungi il registrable se mancante.
    host = (urlparse(site_url).hostname or "").lower()
    reg = _registrable_domain(host)
    existing_allowed = list(sub_task.get("allowed_domains") or [])
    if reg and reg not in existing_allowed and host not in existing_allowed:
        existing_allowed.append(reg)
    sub_task["allowed_domains"] = existing_allowed

    sub_job_id = db.create_job(task["id"])
    db.append_job_log(
        sub_job_id,
        f"Sub-job di auto_extract job#{parent_job_id} per {site_url} (strategy={strategy})",
    )
    jlog(f"  → lancio strategy={strategy} (sub-job #{sub_job_id})")

    try:
        if strategy == "bulk_extract":
            from .runner_bulk_extract import run_agent as run_bk
            await run_bk(sub_task, sub_job_id)
        elif strategy in ("site_explorer", "http_llm_guided"):
            from .runner_site_explorer import run_agent as run_se
            await run_se(sub_task, sub_job_id)
        elif strategy == "browser_use":
            from .runner_browseruse import run_agent as run_bu
            # Già dentro ProactorEventLoop (auto_extract è chiamato così), ok.
            await run_bu(sub_task, sub_job_id)
        else:
            jlog(f"  ⚠️ strategy '{strategy}' sconosciuta, salto")
            return 0, None
    except asyncio.CancelledError:
        jlog(f"  ⏹ sub-runner cancellato (stop richiesto)")
        raise
    except Exception as e:
        jlog(f"  ❌ sub-runner crashato: {type(e).__name__}: {e}")
        return 0, None

    sub_job = db.get_job(sub_job_id)
    rp = (sub_job or {}).get("result_path") or ""
    if not rp:
        return 0, None
    sub_run_dir = Path(rp).parent
    profiles_file = sub_run_dir / "profiles.jsonl"
    n = 0
    if profiles_file.exists():
        n = sum(
            1 for line in profiles_file.read_text(encoding="utf-8").splitlines()
            if line.strip()
        )
    jlog(f"  ← sub-runner #{sub_job_id} OK: {n} profili")
    return n, Path(rp)


def _aggregate_profiles(report_path: Path | None, main_profiles: Path) -> int:
    """Append delle righe del sub-runner al profiles.jsonl consolidato. Ritorna n righe."""
    if not report_path:
        return 0
    sub_dir = report_path.parent
    sub_profiles = sub_dir / "profiles.jsonl"
    if not sub_profiles.exists():
        return 0
    n = 0
    with main_profiles.open("a", encoding="utf-8") as out:
        for line in sub_profiles.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                out.write(line + "\n")
                n += 1
    return n


def _count_jsonl_lines(p: Path) -> int:
    """Conta righe non vuote nel jsonl consolidato (per re-arm intra-job)."""
    try:
        return sum(1 for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip())
    except Exception:
        return 0


async def _maybe_rearm_site_explorer(
    *,
    task: dict[str, Any],
    site_url: str,
    parent_job_id: int,
    jlog,
    main_profiles: Path,
    n_already_extracted: int,
    target_cap: int,
) -> bool:
    """Stage 2 intra-job: dopo che browser_use ha estratto N>0 profili E ha salvato
    un playbook in DB, rilanciamo site_explorer ARMATO del playbook per finire il
    lavoro fino al cap target. Cap di sicurezza: max 1 re-arming per sito.

    Ritorna True se il re-arm e' stato eseguito (e ha aggiunto profili al jsonl
    consolidato), False se saltato (no playbook, transferable=false, ecc.).
    """
    asset_type = task.get("extraction_template")
    host = (urlparse(site_url).hostname or "").lower()
    reg = _registrable_domain(host)
    if not asset_type or not reg:
        return False

    playbook = db.get_site_playbook(reg, asset_type)
    if not playbook:
        jlog("  ℹ️ Re-arm saltato: nessun playbook trovato in DB.")
        return False
    # transferable e' gia' filtrato da get_site_playbook (ritorna solo active+transferable=1)
    jlog(
        f"  💡 Playbook fresco per {reg} (id={playbook['id']}, source="
        f"{playbook['source_runner']}). Rilancio site_explorer armato per finire "
        f"il lavoro ({n_already_extracted}/{target_cap} gia' raccolti)."
    )
    n_rearm, rearm_report = await _execute_strategy(
        task, "site_explorer", site_url, parent_job_id, jlog,
    )
    n_added = _aggregate_profiles(rearm_report, main_profiles)
    jlog(f"  ← re-arm site_explorer: +{n_added} profili (totale per sito: {n_already_extracted + n_added})")
    return n_added > 0


_OLLAMA_MODEL_MARKERS = ("qwen", "llama", "mistral", "deepseek", "phi", "gemma", "codestral", "granite")
_CLOUD_PROVIDERS = {"openai", "anthropic", "gemini", "grok"}


def _looks_like_ollama_model(model: str) -> bool:
    """Heuristic: True se il nome del modello sembra un tag Ollama (qwen3:30b,
    llama3.1:8b, ecc.) — usato per detectare incongruenze provider/modello."""
    if not model:
        return False
    m = model.lower()
    if any(mk in m for mk in _OLLAMA_MODEL_MARKERS):
        return True
    if ":" in m and not m.startswith(("gpt-", "claude-", "o1-", "o3-")):
        return True
    return False


def _resolve_profiler_llm(task: dict[str, Any]) -> tuple[str, str, str, str | None]:
    """Risolve provider per il profiler: discovery_llm_* se compilato, altrimenti main.

    Validation: se discovery_provider e' cloud ma discovery_model sembra Ollama
    (incongruenza tipica dovuta al form che non resetta il modello al cambio di
    provider), fallback automatico al main LLM con un warning. Evita 404 sicuri.

    Ritorna (base_url, api_key, model, warning_message_or_None).
    """
    discovery_provider = (task.get("discovery_llm_provider") or "").strip()
    discovery_model = (task.get("discovery_llm_model") or "").strip()
    main_provider = (task.get("llm_provider") or "ollama").strip()
    main_model = (task.get("model") or "").strip()
    warning: str | None = None

    if discovery_provider and discovery_model:
        # Detect incongruenza provider/model
        if discovery_provider.lower() in _CLOUD_PROVIDERS and _looks_like_ollama_model(discovery_model):
            warning = (
                f"⚠️ Discovery LLM incongruente: provider='{discovery_provider}' "
                f"ma model='{discovery_model}' sembra Ollama. Fallback al main LLM "
                f"({main_provider}/{main_model}) per evitare errori 404. "
                f"Correggi i campi `discovery_llm_*` nel form del task."
            )
            provider_key = main_provider
            model = main_model
            api_key_input = task.get("llm_api_key")
        else:
            provider_key = discovery_provider
            model = discovery_model
            api_key_input = task.get("discovery_llm_api_key")
    elif discovery_provider:
        # Provider impostato ma modello vuoto → usa modello main col discovery provider
        # (puo' funzionare se il main_model e' compatibile, altrimenti fallira' a runtime)
        provider_key = discovery_provider
        model = main_model
        api_key_input = task.get("discovery_llm_api_key")
    else:
        provider_key = main_provider or "ollama"
        model = main_model
        api_key_input = task.get("llm_api_key")

    base_url = resolve_base_url(provider_key, task.get("llm_base_url"))
    api_key = resolve_api_key(provider_key, api_key_input)
    return base_url, api_key, model, warning


async def run_agent(task: dict[str, Any], job_id: int) -> str:
    def jlog(line: str) -> None:
        db.append_job_log(job_id, line)

    db.update_job(job_id, status="running", started_at=db.now_iso())
    db.set_control_signal(job_id, None)
    jlog(
        f"Avvio auto_extract per task #{task['id']} \"{task['name']}\". "
        "Strategia per ogni sito decisa da un profiler LLM."
    )

    # 1. Risolvi LLM per il profiler
    try:
        prof_base_url, prof_api_key, prof_model, prof_warning = _resolve_profiler_llm(task)
    except RuntimeError as e:
        jlog(f"ERRORE configurazione provider profiler: {e}")
        db.update_job(job_id, status="error", error=str(e), finished_at=db.now_iso())
        raise
    if prof_warning:
        jlog(prof_warning)
    jlog(f"Profiler LLM: {prof_model} (riusa discovery_llm_* se compilato, altrimenti main)")

    # 2. Setup run dir consolidata
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_dir = RESULTS_DIR / str(task["id"]) / ts
    run_dir.mkdir(parents=True, exist_ok=True)
    main_profiles = run_dir / "profiles.jsonl"
    main_profiles.touch()
    report_json_path = run_dir / "auto_extract_report.json"

    # 3. Lista siti
    sites: list[str] = [u.strip() for u in (task.get("seed_queries") or []) if u and u.strip()]
    if not sites:
        jlog("⚠️ Lista siti vuota (campo 'seed_queries'). Aborto.")
        db.update_job(
            job_id, status="error",
            error="Lista siti vuota: compila 'URL da processare' nel task.",
            finished_at=db.now_iso(),
        )
        return ""

    jlog(f"Lista siti da processare: {len(sites)}")
    for i, s in enumerate(sites, 1):
        jlog(f"  {i}. {s}")

    site_results: list[dict[str, Any]] = []
    total_profiles = 0
    schema_text = task.get("extraction_schema") or ""
    objective_text = task.get("objective") or ""

    target_type = (task.get("extraction_template") or "profile_contacts").strip() or "profile_contacts"

    for i, site_url in enumerate(sites, start=1):
        jlog(f"\n[{i}/{len(sites)}] {site_url}")

        if db.get_control_signal(job_id) == "stop":
            jlog("STOP richiesto dall'utente: interrompo il loop siti.")
            break

        # GATE: domini bloccati (vedi memoria feedback_no_mondocamgirl_traffic)
        if _is_blocked(site_url):
            jlog(f"  ⛔ dominio bloccato (no-traffic policy): skip {site_url}")
            site_results.append({
                "url": site_url, "strategy": "skip",
                "promising": "no", "reason": "Dominio bloccato dalla policy locale.",
                "n_profiles": 0, "fallback_used": None,
            })
            continue

        # RECON: cerca la directory canonica del sito prima del profiler.
        # Se il seed e' una home curata (es. tryst.link/) ma il sito ha una
        # directory paginata vera (/escorts, /products, ...), recon_site
        # promuove al best_seed. Costo ~2-5s, indipendente da LLM.
        effective_url = site_url
        prepop_urls: list[str] = []
        try:
            recon = await recon_site(site_url, target_type=target_type)
        except Exception as e:
            jlog(f"  ⚠️ recon crashato (continuo con seed originale): {type(e).__name__}: {e}")
            recon = None
        if recon is not None:
            for ev in recon.evidence:
                jlog(f"  RECON: {ev}")
            if recon.seed_changed and not _is_blocked(recon.best_seed_url):
                effective_url = recon.best_seed_url
                jlog(f"  ↪ seed effettivo: {effective_url}")
            if recon.sitemap_urls_total > 0:
                jlog(f"  RECON: sitemap ha {recon.sitemap_urls_total} URL del target (non ancora usati dal runner)")
            if recon.prepopulated_urls:
                # Filtra eventuali URL bloccati e capping a 200 per il sub-runner
                # Cap match con site_recon (2000): copre directory grandi senza
                # esplodere memoria. target_cap_per_site del task ferma comunque
                # l'estrazione molto prima del cap di pagine se basta.
                prepop_urls = [u for u in recon.prepopulated_urls if not _is_blocked(u)][:2000]
                if prepop_urls:
                    jlog(f"  RECON: {len(prepop_urls)} URL pre-popolati pronti per runner (paginazione/sitemap)")

        # PROFILER
        try:
            decision = await profile_site(
                effective_url, objective_text, schema_text,
                prof_base_url, prof_api_key, prof_model,
            )
        except Exception as e:
            jlog(f"  ⚠️ profiler crashato: {type(e).__name__}: {e}")
            site_results.append({
                "url": effective_url, "strategy": "skip",
                "promising": "no", "reason": f"profiler error: {e}",
                "n_profiles": 0, "fallback_used": None,
            })
            continue

        sigs = decision.get("signals", {})
        jlog(
            f"  PROFILER → strategy={decision['strategy']}  "
            f"promising={decision['promising']}  "
            f"http={decision.get('http_status')}  "
            f"text_ratio={sigs.get('text_to_html_ratio')}  "
            f"login_form={sigs.get('has_login_form')}  "
            f"signup_kw={sigs.get('has_signup_keywords')}  "
            f"recurring_pattern={sigs.get('has_recurring_target_pattern')}  "
            f"top_pattern_count={sigs.get('top_pattern_count')}"
        )
        jlog(f"           reason: {decision['reason']}")
        if decision.get("target_hint"):
            jlog(f"           target hint: {decision['target_hint']}")

        # === OVERRIDE: se recon ha gia' trovato URL paginati statici, forza
        # site_explorer ignorando il profiler. Il recon ha materialmente
        # scaricato le pagine e contato i profili, quindi sa che il sito e'
        # accessibile via HTTP statico — il profiler che dice "JS-heavy" per
        # via di text_ratio basso (es. CSS inline gigante) e' un falso allarme.
        if prepop_urls and decision["strategy"] in ("browser_use", "skip"):
            old = decision["strategy"]
            decision["strategy"] = "site_explorer"
            jlog(
                f"  ↪ OVERRIDE strategy: {old} → site_explorer perche' recon "
                f"ha gia' validato {len(prepop_urls)} URL paginati HTTP-accessibili "
                f"(text_ratio basso era falso allarme da HEAD gonfio)."
            )

        if decision["strategy"] == "skip":
            jlog(f"  ⏭️  skip (profiler)")
            site_results.append({
                "url": effective_url, "original_seed": site_url, "strategy": "skip",
                "promising": decision["promising"],
                "reason": decision["reason"],
                "n_profiles": 0, "fallback_used": None,
                "expected_yield": decision.get("expected_yield", 0),
            })
            continue

        # SUB-RUNNER (strategia primaria)
        n_primary, sub_report = await _execute_strategy(
            task, decision["strategy"], effective_url, job_id, jlog,
            prepopulated_urls=prepop_urls,
        )
        n_aggregated = _aggregate_profiles(sub_report, main_profiles)
        n_primary = n_aggregated  # uso il count effettivo dopo aggregazione

        # FALLBACK: se 0 profili, prova la strategia complementare (1 retry max per sito).
        # bulk_extract  -> site_explorer (agente ReAct: trova listing nascoste, drill-down)
        # site_explorer -> browser_use   (HTTP statico non e' bastato: serve JS/click reveal)
        # browser_use   -> site_explorer (agente HTTP intelligente, piu' rapido di un altro browser)
        fallback_map = {
            "bulk_extract": "site_explorer",
            "site_explorer": "browser_use",
            "browser_use": "site_explorer",
        }
        fallback_used = None
        rearm_used = False
        n_final = n_primary
        fb_strategy = fallback_map.get(decision["strategy"])
        if n_primary == 0 and fb_strategy:
            jlog(
                f"  ⚠️ {decision['strategy']} ha prodotto 0 profili. "
                f"Tento fallback → {fb_strategy} (1 retry max per sito)."
            )
            n_fb, fb_report = await _execute_strategy(
                task, fb_strategy, effective_url, job_id, jlog,
                prepopulated_urls=prepop_urls,
            )
            n_aggregated_fb = _aggregate_profiles(fb_report, main_profiles)
            fallback_used = fb_strategy
            n_final = n_aggregated_fb

            # Stage 2 — Re-arm intra-job: se il fallback era browser_use ed ha estratto >0,
            # browser_use ha appena salvato un playbook in DB. Se transferable=true E
            # abbiamo ancora budget rispetto al cap target, rilanciamo site_explorer
            # ARMATO del playbook per finire il lavoro a costo basso.
            target_cap = max(1, int(task.get("target_cap_per_site") or 30))
            if (
                fb_strategy == "browser_use"
                and n_final > 0
                and n_final < target_cap
                and task.get("extraction_template")
            ):
                rearm_used = await _maybe_rearm_site_explorer(
                    task=task,
                    site_url=effective_url,
                    parent_job_id=job_id,
                    jlog=jlog,
                    main_profiles=main_profiles,
                    n_already_extracted=n_final,
                    target_cap=target_cap,
                )
                if rearm_used:
                    n_final = _count_jsonl_lines(main_profiles)

        total_profiles += n_final
        site_results.append({
            "url": effective_url,
            "original_seed": site_url,
            "strategy": decision["strategy"],
            "promising": decision["promising"],
            "reason": decision["reason"],
            "expected_yield": decision.get("expected_yield", 0),
            "fallback_used": fallback_used,
            "rearm_used": rearm_used,
            "n_profiles_primary": n_primary,
            "n_profiles_final": n_final,
            "target_hint": decision.get("target_hint", ""),
        })

    # 4. Scrivi report finale
    job_now = db.get_job(job_id) or {}
    report_data = {
        "task_id": task["id"],
        "task_name": task["name"],
        "started_at": job_now.get("started_at"),
        "finished_at": db.now_iso(),
        "n_sites_total": len(sites),
        "n_sites_processed": len(site_results),
        "total_profiles": total_profiles,
        "sites": site_results,
    }
    report_json_path.write_text(
        json.dumps(report_data, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    # report.md leggibile
    md_lines = [
        f"# auto_extract — task #{task['id']} \"{task['name']}\"",
        "",
        f"- Siti processati: **{len(site_results)}/{len(sites)}**",
        f"- Profili estratti totali: **{total_profiles}**",
        "",
        "## Per sito",
        "",
        "| # | Sito | Strategia | Fallback | Profili | Promising | Reason |",
        "|---|---|---|---|---|---|---|",
    ]
    for i, r in enumerate(site_results, start=1):
        md_lines.append(
            f"| {i} | {r['url']} | {r.get('strategy', '?')} | "
            f"{r.get('fallback_used') or '—'} | {r.get('n_profiles_final', 0)} | "
            f"{r.get('promising', '?')} | {(r.get('reason') or '')[:120]} |"
        )
    md_lines.extend([
        "",
        "## Output",
        f"- `profiles.jsonl` consolidato: {total_profiles} righe",
        "- `auto_extract_report.json`: dettaglio strutturato per sito",
        "- I sub-job (uno per ogni strategia/sito) restano nei propri timestamp accanto a questa run.",
    ])
    md_path = run_dir / "report.md"
    md_path.write_text("\n".join(md_lines), encoding="utf-8")

    jlog(
        f"\n✅ auto_extract completato: {total_profiles} profili totali "
        f"da {len(site_results)} siti. Report: {md_path}"
    )

    db.update_job(
        job_id, status="done", finished_at=db.now_iso(),
        result_path=str(md_path),
    )
    db.set_control_signal(job_id, None)
    return str(md_path)
