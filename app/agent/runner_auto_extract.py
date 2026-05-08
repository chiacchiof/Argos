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
from .llm_providers import resolve_api_key, resolve_base_url
from .runner_bulk_extract import _registrable_domain
from .site_profiler import profile_site


log = logging.getLogger(__name__)


async def _execute_strategy(
    task: dict[str, Any],
    strategy: str,
    site_url: str,
    parent_job_id: int,
    jlog,
) -> tuple[int, Path | None]:
    """Esegue il sub-runner per quella strategia su quel singolo URL.

    Ritorna (n_profili_estratti, path_report_sub).
    """
    sub_task = dict(task)
    sub_task["seed_queries"] = [site_url]
    sub_task["agent_mode"] = strategy
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
        elif strategy == "browser_use":
            from .runner_browseruse import run_agent as run_bu
            # Già dentro ProactorEventLoop (auto_extract è chiamato così), ok.
            await run_bu(sub_task, sub_job_id)
        elif strategy == "http_llm_guided":
            jlog(
                "  ⚠️ strategy 'http_llm_guided' non ancora implementata: "
                "uso 'bulk_extract' al suo posto."
            )
            from .runner_bulk_extract import run_agent as run_bk
            await run_bk(sub_task, sub_job_id)
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


def _resolve_profiler_llm(task: dict[str, Any]) -> tuple[str, str, str]:
    """Risolve provider per il profiler: discovery_llm_* se compilato, altrimenti main."""
    provider_key = (
        (task.get("discovery_llm_provider") or "").strip()
        or (task.get("llm_provider") or "").strip()
        or "ollama"
    )
    model = (
        (task.get("discovery_llm_model") or "").strip()
        or task.get("model")
        or ""
    )
    api_key_input = (
        task.get("discovery_llm_api_key")
        if task.get("discovery_llm_provider")
        else task.get("llm_api_key")
    )
    base_url = resolve_base_url(provider_key, task.get("llm_base_url"))
    api_key = resolve_api_key(provider_key, api_key_input)
    return base_url, api_key, model


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
        prof_base_url, prof_api_key, prof_model = _resolve_profiler_llm(task)
    except RuntimeError as e:
        jlog(f"ERRORE configurazione provider profiler: {e}")
        db.update_job(job_id, status="error", error=str(e), finished_at=db.now_iso())
        raise
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

    for i, site_url in enumerate(sites, start=1):
        jlog(f"\n[{i}/{len(sites)}] {site_url}")

        if db.get_control_signal(job_id) == "stop":
            jlog("STOP richiesto dall'utente: interrompo il loop siti.")
            break

        # PROFILER
        try:
            decision = await profile_site(
                site_url, objective_text, schema_text,
                prof_base_url, prof_api_key, prof_model,
            )
        except Exception as e:
            jlog(f"  ⚠️ profiler crashato: {type(e).__name__}: {e}")
            site_results.append({
                "url": site_url, "strategy": "skip",
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
            f"signup_kw={sigs.get('has_signup_keywords')}"
        )
        jlog(f"           reason: {decision['reason']}")
        if decision.get("target_hint"):
            jlog(f"           target hint: {decision['target_hint']}")

        if decision["strategy"] == "skip":
            jlog(f"  ⏭️  skip (profiler)")
            site_results.append({
                "url": site_url, "strategy": "skip",
                "promising": decision["promising"],
                "reason": decision["reason"],
                "n_profiles": 0, "fallback_used": None,
                "expected_yield": decision.get("expected_yield", 0),
            })
            continue

        # SUB-RUNNER (strategia primaria)
        n_primary, sub_report = await _execute_strategy(
            task, decision["strategy"], site_url, job_id, jlog,
        )
        n_aggregated = _aggregate_profiles(sub_report, main_profiles)
        n_primary = n_aggregated  # uso il count effettivo dopo aggregazione

        # FALLBACK: se 0 profili e la strategia non era già browser_use, ritenta
        fallback_used = None
        n_final = n_primary
        if n_primary == 0 and decision["strategy"] != "browser_use":
            jlog(
                f"  ⚠️ {decision['strategy']} ha prodotto 0 profili. "
                f"Tento fallback → browser_use (1 retry max per sito)."
            )
            n_fb, fb_report = await _execute_strategy(
                task, "browser_use", site_url, job_id, jlog,
            )
            n_aggregated_fb = _aggregate_profiles(fb_report, main_profiles)
            fallback_used = "browser_use"
            n_final = n_aggregated_fb

        total_profiles += n_final
        site_results.append({
            "url": site_url,
            "strategy": decision["strategy"],
            "promising": decision["promising"],
            "reason": decision["reason"],
            "expected_yield": decision.get("expected_yield", 0),
            "fallback_used": fallback_used,
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
