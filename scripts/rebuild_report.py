"""Ricostruisce `report.md` arricchito per un job già finito.

Uso:
    python scripts/rebuild_report.py <task_id> [job_id]

Se `job_id` non è specificato, ricostruisce il report dell'ultimo job del task.
Se ce ne sono di più, mostra la lista e chiede quale.

Strategia:
- Carica task + job dal DB (per task config + log testuale del job)
- Risolve il `run_dir` dal `job.result_path` (oppure dall'ultima dir presente
  in `data/results/<task_id>/`)
- Parsa il job log via heuristiche per estrarre metriche-per-fase (audience_discovery v1)
- Istanzia `RunReporter`, popola fasi/metriche, chiama `write()` → produce
  `<run_dir>/report.md` arricchito SOVRASCRIVENDO il vecchio (se esiste)

Output sul terminale: path del report e n. di insight prodotti.

Limiti v1: supporta solo audience_discovery. Per gli altri runner: aggiungi
parser specifici in `_parse_<mode>_log(log_text) -> dict` e mappa qui sotto.
"""
from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Forza stdout/stderr a UTF-8 per non rompere su Windows cp1252 quando
# stampiamo path con caratteri unicode (la console Argos su Windows può
# essere lanciata con `chcp 65001` opzionale, ma reconfigure() qui è safe).
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

# Assicura che `app/` sia importabile quando lanci dallo scripts/
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import db
from app.config import RESULTS_DIR
from app.agent.run_reporter import RunReporter


def _parse_audience_discovery_log(log_text: str) -> dict[str, Any]:
    """Ritorna dict con `phases` (per-fase metrics) + `runtime_overrides`
    (campi che il job ha usato in pratica — model, ecc. — che potrebbero
    essere diversi dallo stato attuale del task in DB)."""
    state: dict[str, dict[str, Any]] = {
        "keywords": {"items_in": 0, "items_out": 0, "status": "ok", "details": {}},
        "anchor_friends": {"items_in": 0, "items_out": 0, "status": "skipped",
                           "details": {"reason": "non rilevato dal parser"}},
        "search_groups": {"items_in": 0, "items_out": 0, "status": "ok", "details": {}},
        "open_groups": {"items_in": 0, "items_out": 0, "status": "ok",
                        "details": {"zero_author_groups": 0}},
        "scoring": {"items_in": 0, "items_out": 0, "status": "ok", "details": {
            "saved": 0, "skipped_low_score": 0, "skipped_dedup": 0, "extract_fail": 0,
            "score_distribution": [],
        }},
    }
    overrides: dict[str, Any] = {}

    for ln in log_text.splitlines():
        # Estrai model effettivamente usato dal job (può differire da task.model
        # attuale se l'utente ha modificato il campo dopo il run)
        m = re.search(r"LLM:\s*(\S+)\s*/\s*(\S+)", ln)
        if m:
            overrides["llm_provider"] = m.group(1)
            overrides["model"] = m.group(2)
            continue
        # ── Fase 1: keywords ──
        m = re.search(r"Keyword dedotte \((\d+)\):\s*(\[.+?\])", ln)
        if m:
            state["keywords"]["items_in"] = 1
            state["keywords"]["items_out"] = int(m.group(1))
            state["keywords"]["status"] = "ok"
            try:
                state["keywords"]["details"]["keywords"] = ast.literal_eval(m.group(2))
            except Exception:
                pass
            continue

        if ("LLM non ha prodotto array JSON" in ln
                or "Nessuna keyword dedotta" in ln
                or "sembra thinking-mode" in ln):
            state["keywords"]["status"] = "empty"
            state["keywords"]["items_in"] = 1
            state["keywords"]["items_out"] = 0

        # ── Fase 2: anchor_friends ──
        m = re.search(r"Fase 2: friends-of per (\d+) anchor", ln)
        if m:
            state["anchor_friends"]["status"] = "ok"
            state["anchor_friends"]["items_in"] = int(m.group(1))
            state["anchor_friends"]["details"] = {}
        if "Fase 2: nessun anchor" in ln:
            state["anchor_friends"]["status"] = "skipped"
            state["anchor_friends"]["details"] = {"reason": "nessun anchor profile nel seed"}

        # ── Fase 3: search_groups ──
        if re.search(r"search_groups: → \d+ gruppi", ln):
            state["search_groups"]["items_in"] += 1
        m = re.search(r"Gruppi totali dedup:\s*(\d+)", ln)
        if m:
            state["search_groups"]["items_out"] = int(m.group(1))

        # ── Fase 4: open_groups ──
        m = re.search(r"open_group: → (\d+) autori unici", ln)
        if m:
            n = int(m.group(1))
            state["open_groups"]["items_in"] += 1
            state["open_groups"]["items_out"] += n
            if n == 0:
                state["open_groups"]["details"]["zero_author_groups"] += 1

        # ── Fase 5: scoring ──
        m = re.search(r"score:\s*(\d+)/10", ln)
        if m:
            score = int(m.group(1))
            state["scoring"]["details"]["score_distribution"].append(score)
            state["scoring"]["items_in"] += 1
        if "✅ SAVE" in ln:
            state["scoring"]["details"]["saved"] += 1
        if "skip: score" in ln and "< threshold" in ln:
            state["scoring"]["details"]["skipped_low_score"] += 1
            state["scoring"]["items_in"] += 1
        if "skip: già in DB" in ln:
            state["scoring"]["details"]["skipped_dedup"] += 1
            state["scoring"]["items_in"] += 1
        if "extract_profile error" in ln:
            state["scoring"]["details"]["extract_fail"] += 1
            state["scoring"]["items_in"] += 1

    # Status finale per scoring
    sc = state["scoring"]
    sc["items_out"] = sc["details"]["saved"]
    if sc["items_in"] == 0:
        sc["status"] = "empty"

    # Search_groups empty se 0 gruppi
    if state["search_groups"]["items_in"] > 0 and state["search_groups"]["items_out"] == 0:
        state["search_groups"]["status"] = "empty"
    if state["search_groups"]["items_in"] == 0:
        state["search_groups"]["status"] = "skipped"
        state["search_groups"]["details"]["reason"] = "nessuna keyword da Fase 1"

    # Open_groups empty se 0 autori
    if state["open_groups"]["items_in"] > 0 and state["open_groups"]["items_out"] == 0:
        state["open_groups"]["status"] = "empty"
    if state["open_groups"]["items_in"] == 0:
        state["open_groups"]["status"] = "skipped"
        state["open_groups"]["details"]["reason"] = "nessun gruppo da Fase 3"

    return {"phases": state, "overrides": overrides}


_PARSERS = {
    "audience_discovery": _parse_audience_discovery_log,
}


def _resolve_run_dir(task_id: int, job: dict) -> Path | None:
    """Risolve la directory del run: prima da job.result_path, poi cerca la
    cartella timestamp più recente in data/results/<task_id>/."""
    rp = job.get("result_path") or ""
    if rp:
        p = Path(rp)
        if p.is_file():
            return p.parent
        if p.is_dir():
            return p
    # Fallback: ultima dir sotto data/results/<task_id>/
    task_dir = Path(RESULTS_DIR) / str(task_id)
    if not task_dir.exists():
        return None
    subdirs = sorted([d for d in task_dir.iterdir() if d.is_dir()], reverse=True)
    return subdirs[0] if subdirs else None


def _pick_job(task_id: int, job_id: int | None) -> dict | None:
    if job_id is not None:
        return db.get_job(int(job_id))
    with db.connect() as con:
        rows = list(con.execute(
            "SELECT id, status, started_at, finished_at FROM jobs "
            "WHERE task_id = %s ORDER BY id DESC LIMIT 10",
            (task_id,),
        ))
    if not rows:
        print(f"Nessun job per task #{task_id}", file=sys.stderr)
        return None
    if len(rows) == 1:
        return db.get_job(int(dict(rows[0])["id"]))
    print(f"Job recenti per task #{task_id}:")
    for r in rows:
        d = dict(r) if hasattr(r, "keys") else r
        print(f"  #{d['id']:>5} status={d.get('status')!r:12} started={d.get('started_at')}")
    print()
    raw = input(f"Quale job ricostruire? (default: #{dict(rows[0])['id']}): ").strip()
    if not raw:
        return db.get_job(int(dict(rows[0])["id"]))
    return db.get_job(int(raw))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Rebuild a job report.md arricchito (con diagnostica) "
                    "a partire dal job log + audit jsonl."
    )
    ap.add_argument("task_id", type=int, help="ID del task")
    ap.add_argument("job_id", type=int, nargs="?", default=None,
                    help="ID del job (opzionale: default = ultimo del task)")
    ap.add_argument("--output", type=str, default=None,
                    help="Override path del report (default: <run_dir>/report.md)")
    args = ap.parse_args(argv)

    task = db.get_task(args.task_id)
    if not task:
        print(f"Task #{args.task_id} non trovato", file=sys.stderr)
        return 2

    job = _pick_job(args.task_id, args.job_id)
    if not job:
        return 2
    job_id = int(job["id"])

    agent_mode = (task.get("agent_mode") or "").strip()
    parser_fn = _PARSERS.get(agent_mode)
    if not parser_fn:
        print(
            f"Parser per agent_mode={agent_mode!r} non implementato. "
            f"v1 supporta: {sorted(_PARSERS.keys())}.",
            file=sys.stderr,
        )
        return 3

    run_dir = _resolve_run_dir(args.task_id, job)
    if not run_dir:
        print(f"Nessuna run dir per task #{args.task_id} job #{job_id}", file=sys.stderr)
        return 4

    log_text = job.get("log") or ""
    if not log_text:
        print(f"⚠️ Job #{job_id} non ha log testuale.", file=sys.stderr)

    parsed = parser_fn(log_text)
    phases_parsed: dict[str, dict[str, Any]] = parsed["phases"]
    overrides: dict[str, Any] = parsed.get("overrides", {})

    # Applica gli override dal log al task così la diagnostica vede i valori
    # ACTUALLY USATI dal job (non quelli attuali del task in DB, che potrebbero
    # essere stati modificati dopo).
    task_at_run = dict(task)
    for k, v in overrides.items():
        task_at_run[k] = v

    # Costruisci il reporter manualmente popolando phases & metriche
    reporter = RunReporter(task_at_run, job_id, run_dir)
    # Timestamp dal job
    if job.get("started_at"):
        try:
            reporter.started_at = datetime.fromisoformat(job["started_at"].replace("Z", "+00:00"))
        except Exception:
            pass
    if job.get("finished_at"):
        try:
            reporter.finished_at = datetime.fromisoformat(job["finished_at"].replace("Z", "+00:00"))
        except Exception:
            pass

    # Popola fasi nell'ordine canonico
    for name in ("keywords", "anchor_friends", "search_groups", "open_groups", "scoring"):
        st = phases_parsed.get(name)
        if not st:
            continue
        p = reporter.start_phase(name)
        reporter.end_phase(
            p,
            status=st["status"],
            items_in=st["items_in"],
            items_out=st["items_out"],
            **st.get("details", {}),
        )
        # Sovrascrivi i timestamp DOPO end_phase (che li sovrascrive con now()):
        # nel rebuild non abbiamo timestamp per-fase, quindi azzeriamo la durata
        # mettendo start=end. Più onesto che mostrare 1h sballate.
        p.started_at = reporter.started_at or datetime.now(timezone.utc)
        p.finished_at = p.started_at

    # Metriche aggregate (da phases_parsed, non `parsed`)
    sc = phases_parsed.get("scoring", {})
    sc_details = sc.get("details", {})
    reporter.add_metric("saved_count", sc_details.get("saved", 0))
    reporter.add_metric("candidates_scoperti", sc.get("items_in", 0))
    reporter.add_metric("keyword_dedotte", phases_parsed.get("keywords", {}).get("details", {}).get("keywords", []))
    reporter.add_metric("gruppi_esplorati", phases_parsed.get("open_groups", {}).get("items_in", 0))
    reporter.add_metric("speed_profile", task_at_run.get("speed_profile") or "safe")
    reporter.add_metric("account_fb", f"#{task_at_run.get('recon_social_account_id')} (vedi /social/accounts)")

    # Status finale = status job
    final_status = "ok" if (job.get("status") in ("done",) and sc_details.get("saved", 0) > 0) else (
        "empty" if job.get("status") == "done" else "error"
    )
    reporter.set_final_status(final_status)

    # File output (best-effort lookup)
    profiles_path = run_dir / "profiles.jsonl"
    if profiles_path.exists():
        n_lines = sum(1 for _ in profiles_path.open(encoding="utf-8"))
        reporter.add_output("profiles.jsonl", f"{n_lines} righe (1 per match)")
    audit_path = run_dir / "recon_audit_log.jsonl"
    if audit_path.exists():
        reporter.add_output("recon_audit_log.jsonl", "log eventi del run")
    screenshots_dir = run_dir / "screenshots"
    if screenshots_dir.exists():
        n_pngs = len(list(screenshots_dir.glob("*.png")))
        reporter.add_output("screenshots/", f"{n_pngs} screenshot")

    # Scrivi report
    out_path = Path(args.output) if args.output else (run_dir / "report.md")
    written = reporter.write(out_path)

    # Conta insight per stampa
    from app.agent.run_reporter import diagnose
    insights = diagnose(agent_mode, reporter)
    n_critical = sum(1 for i in insights if i.severity == "critical")
    n_warning = sum(1 for i in insights if i.severity == "warning")
    n_info = sum(1 for i in insights if i.severity == "info")
    n_success = sum(1 for i in insights if i.severity == "success")

    print(f"[OK] Report ricostruito: {written}")
    print(f"     Job #{job_id} status={job.get('status')!r}, durata "
          f"{(reporter.finished_at - reporter.started_at).total_seconds() if reporter.finished_at else 0:.0f}s")
    print(f"     Insight prodotti: {n_critical} critical, {n_warning} warning, "
          f"{n_info} info, {n_success} success")
    return 0


if __name__ == "__main__":
    sys.exit(main())
