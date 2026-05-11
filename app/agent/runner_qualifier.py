"""Runner qualifier: legge profiles.jsonl e per ogni record chiede a un LLM
'questo profilo è valido per outreach? scora 0-10' e materializza in `contacts`
con score + status='qualified'/'rejected'.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

from .. import db
from ..config import RESULTS_DIR
from .llm_providers import resolve_api_key, resolve_base_url


log = logging.getLogger(__name__)


DEFAULT_QUALIFIER_PROMPT = (
    "Sei un valutatore di lead per outreach commerciale. Ricevi un profilo in JSON "
    "(estratto da una pagina web pubblica). Devi giudicare se è un lead valido per "
    "essere contattato per offrire 'ottimizzazione dei contenuti'. "
    "Considera valido se ha almeno UN canale di contatto pubblico (email, whatsapp, "
    "telegram, sitoweb), un'identità riconoscibile (nome o handle) e contenuti reali "
    "in pagina. Scarta profili-fantasma, duplicati ovvi, pagine di categoria che "
    "sono finite per errore qui."
)


SCORE_RE = re.compile(r"score\s*[:=]\s*(\d+)", re.IGNORECASE)
KEEP_RE = re.compile(r"\b(keep|valid|qualified|tieni|valido|si)\b", re.IGNORECASE)
REJECT_RE = re.compile(r"\b(reject|skip|drop|invalid|scarta|no)\b", re.IGNORECASE)


async def _judge(
    task: dict[str, Any],
    profile_obj: dict[str, Any],
    extra_prompt: str,
) -> tuple[int, str, str]:
    """Ritorna (score 0-10, decision 'qualified'|'rejected', motivazione)."""
    provider_key = task.get("llm_provider") or "ollama"
    base_url = resolve_base_url(provider_key, task.get("llm_base_url"))
    api_key = resolve_api_key(provider_key, task.get("llm_api_key"))
    model = task["model"]

    sys_prompt = DEFAULT_QUALIFIER_PROMPT
    if extra_prompt:
        sys_prompt += "\n\n--- ISTRUZIONI SPECIFICHE DELL'UTENTE ---\n" + extra_prompt

    user_payload = (
        "Profilo da valutare:\n"
        + json.dumps(profile_obj, ensure_ascii=False, indent=2)[:4000]
        + "\n\nRispondi in due righe ESATTAMENTE in questo formato:\n"
        "score: <0-10>\n"
        "decision: <keep|reject>\n"
        "reason: <una frase breve>"
    )

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": sys_prompt},
            {"role": "user", "content": user_payload},
        ],
        "temperature": 0.1,
        "max_tokens": 200,
    }
    headers = {"Authorization": f"Bearer {api_key}"}
    url = f"{base_url.rstrip('/')}/chat/completions"
    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.post(url, json=payload, headers=headers)
        r.raise_for_status()
        data = r.json()
    text = (data.get("choices", [{}])[0].get("message", {}).get("content", "") or "").strip()

    score_match = SCORE_RE.search(text)
    score = int(score_match.group(1)) if score_match else 5
    score = max(0, min(10, score))

    if REJECT_RE.search(text) and not KEEP_RE.search(text):
        decision = "rejected"
    elif KEEP_RE.search(text):
        decision = "qualified"
    else:
        decision = "qualified" if score >= 5 else "rejected"

    return score, decision, text[:300]


def _domain_of(url: str | None) -> str | None:
    if not url:
        return None
    try:
        return (urlparse(url).hostname or "").lower() or None
    except Exception:
        return None


async def run_agent(task: dict[str, Any], job_id: int) -> str:
    def jlog(line: str) -> None:
        db.append_job_log(job_id, line)

    db.update_job(job_id, status="running", started_at=db.now_iso())
    db.set_control_signal(job_id, None)
    jlog(f"Avvio qualifier per task #{task['id']} \"{task['name']}\" — modello {task['model']}")

    # SORGENTE PRIMARIA: tabella `assets`. Trova i task upstream collegati a
    # questo qualifier (tramite workflow_edges) e prendi i loro asset 'new'.
    # Cosi' il qualifier opera sui dati gia' filtrati dal validator (post-ingest)
    # invece che sul profiles.jsonl raw (che include 90%+ di pagine indice).
    edges_in = db.list_edges(to_task_id=task["id"])
    upstream_ids = sorted({int(e["from_task_id"]) for e in edges_in if e.get("from_task_id")})
    assets_to_judge: list[dict[str, Any]] = []
    if upstream_ids:
        for src_tid in upstream_ids:
            assets_to_judge.extend(
                db.list_assets(source_task_id=src_tid, status="new", limit=10000)
            )
        jlog(
            f"Sorgente: tabella `assets` (task upstream {upstream_ids}): "
            f"{len(assets_to_judge)} asset 'new' da valutare."
        )

    # FALLBACK: se nessun asset (es. task standalone senza workflow), leggi
    # il profiles.jsonl come prima.
    artifact = task.get("input_artifact_path")
    use_fallback_jsonl = not assets_to_judge and bool(artifact)
    if use_fallback_jsonl:
        p = Path(artifact)
        if not p.exists():
            msg = f"Artifact non trovato: {p}"
            jlog(msg)
            db.update_job(job_id, status="error", error=msg, finished_at=db.now_iso())
            raise RuntimeError(msg)
        jlog(f"Sorgente: profiles.jsonl fallback (`{artifact}`).")
    elif not assets_to_judge:
        msg = (
            "Nessun asset upstream da valutare e nessun input_artifact_path. "
            "Collega il qualifier a un task upstream nel workflow, oppure imposta input_artifact_path."
        )
        jlog(msg)
        db.update_job(job_id, status="error", error=msg, finished_at=db.now_iso())
        raise RuntimeError(msg)

    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_dir = RESULTS_DIR / str(task["id"]) / ts
    run_dir.mkdir(parents=True, exist_ok=True)
    qualified_path = run_dir / "qualified.jsonl"
    rejected_path = run_dir / "rejected.jsonl"

    extra_prompt = (task.get("objective") or "").strip()

    n_total = 0
    n_qualified = 0
    n_rejected = 0
    n_failed = 0
    stopped = False

    def _build_obj_from_asset(a: dict[str, Any]) -> dict[str, Any]:
        try:
            return json.loads(a.get("raw_json") or "{}")
        except json.JSONDecodeError:
            return {}

    async def _process_obj(
        obj: dict[str, Any],
        asset_id: int | None,
        raw_str: str,
        fq, fr,
    ) -> None:
        nonlocal n_total, n_qualified, n_rejected, n_failed
        try:
            score, decision, reason = await _judge(task, obj, extra_prompt)
        except Exception as e:
            jlog(f"  ⚠️ judge failed: {type(e).__name__}: {e}")
            n_failed += 1
            return

        # Aggiorna asset (sorgente primaria). Anche se asset_id e' presente,
        # se l'asset e' qualified E ha almeno un canale di contatto reale,
        # materializzalo ANCHE nella tabella `contacts` per renderlo utilizzabile
        # dai task outreach (che leggono da contacts, non da assets).
        if asset_id is not None:
            try:
                db.update_asset_qualifier(asset_id, score, decision, notes=reason[:300])
            except Exception as e:
                jlog(f"  ⚠️ update asset {asset_id} failed: {e}")
            # Materializza in contacts se qualified + contatti reali
            if decision == "qualified":
                email = obj.get("email") or None
                tg_user = obj.get("telegram") or obj.get("telegram_username") or None
                if isinstance(tg_user, str):
                    tg_user = tg_user.lstrip("@") or None
                # Solo se almeno UN canale reale (altrimenti niente da contattare)
                if email or tg_user:
                    try:
                        cid = db.upsert_contact({
                            "source_task_id": task["id"],
                            "source_job_id": job_id,
                            "source_url": obj.get("url") or obj.get("source_url"),
                            "source_domain": obj.get("source_domain") or _domain_of(obj.get("url")),
                            "display_name": (
                                obj.get("display_name") or obj.get("username") or obj.get("nickname")
                            ),
                            "email": email,
                            "telegram_username": tg_user,
                            "raw_json": raw_str,
                        })
                        db.update_contact_qualifier(cid, score, decision)
                    except Exception as e:
                        jlog(f"  ⚠️ materialize contact failed: {e}")
        else:
            email = obj.get("email") or None
            tg_user = obj.get("telegram") or obj.get("telegram_username") or None
            if isinstance(tg_user, str):
                tg_user = tg_user.lstrip("@") or None
            cid = db.upsert_contact({
                "source_task_id": task["id"],
                "source_job_id": job_id,
                "source_url": obj.get("url") or obj.get("source_url"),
                "source_domain": obj.get("source_domain") or _domain_of(obj.get("url")),
                "display_name": (
                    obj.get("display_name") or obj.get("username") or obj.get("nickname")
                ),
                "email": email,
                "telegram_username": tg_user,
                "raw_json": raw_str,
            })
            db.update_contact_qualifier(cid, score, decision)

        line_out = json.dumps(
            {
                **obj,
                "_qualifier_score": score,
                "_qualifier_decision": decision,
                "_qualifier_reason": reason,
                "_asset_id": asset_id,
            },
            ensure_ascii=False,
        )
        if decision == "qualified":
            fq.write(line_out + "\n")
            n_qualified += 1
        else:
            fr.write(line_out + "\n")
            n_rejected += 1

    with qualified_path.open("w", encoding="utf-8") as fq, \
         rejected_path.open("w", encoding="utf-8") as fr:
        if assets_to_judge:
            for asset in assets_to_judge:
                if db.get_control_signal(job_id) == "stop":
                    jlog("STOP richiesto.")
                    stopped = True
                    break
                obj = _build_obj_from_asset(asset)
                if not obj:
                    n_failed += 1
                    continue
                n_total += 1
                await _process_obj(
                    obj,
                    asset_id=int(asset["id"]),
                    raw_str=asset.get("raw_json") or "",
                    fq=fq,
                    fr=fr,
                )
                if (n_total % 10) == 0:
                    jlog(
                        f"  progresso: {n_total} valutati ({n_qualified} qualified, "
                        f"{n_rejected} rejected)"
                    )
        elif use_fallback_jsonl:
            with Path(artifact).open(encoding="utf-8") as fin:
                for raw in fin:
                    if db.get_control_signal(job_id) == "stop":
                        jlog("STOP richiesto.")
                        stopped = True
                        break
                    raw = raw.strip()
                    if not raw:
                        continue
                    try:
                        obj = json.loads(raw)
                    except json.JSONDecodeError:
                        n_failed += 1
                        continue
                    n_total += 1
                    await _process_obj(
                        obj,
                        asset_id=None,
                        raw_str=raw,
                        fq=fq,
                        fr=fr,
                    )
                    if (n_total % 10) == 0:
                        jlog(
                            f"  progresso: {n_total} valutati ({n_qualified} qualified, "
                            f"{n_rejected} rejected)"
                        )

    fmt = task.get("output_format") or "md"
    report_ext = "md" if fmt in ("md", "both") else "txt"
    source_label = (
        f"tabella `assets` (task upstream {upstream_ids})"
        if assets_to_judge
        else f"profiles.jsonl `{artifact}`"
    )
    report = (
        f"# Qualifier run {ts}\n\n"
        f"- **Task**: {task['name']} (#{task['id']})\n"
        f"- **Sorgente**: {source_label}\n"
        f"- **Profili totali valutati**: {n_total}\n"
        f"- **Qualified**: {n_qualified} (vedi `qualified.jsonl`)\n"
        f"- **Rejected**: {n_rejected} (vedi `rejected.jsonl`)\n"
        f"- **Failed**: {n_failed}\n"
        f"- **Stato**: {'INTERROTTO' if stopped else 'Completato'}\n"
    )
    report_path = run_dir / f"report.{report_ext}"
    report_path.write_text(report, encoding="utf-8")

    final_status = "cancelled" if stopped else "done"
    db.update_job(job_id, status=final_status, finished_at=db.now_iso(),
                  result_path=str(report_path))
    db.set_control_signal(job_id, None)
    jlog(f"Qualifier concluso: {n_qualified}/{n_total} qualified.")
    return str(report_path)
