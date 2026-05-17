"""Runner outreach: legge asset dal profiles.jsonl upstream (o dalla
tabella `assets`) e invia messaggi su email + telegram.

Step:
1. Risolve la sorgente degli asset:
   - se task.input_artifact_path è impostato → ingest profiles.jsonl in `assets`
   - altrimenti usa asset già in DB con source_task_id == task.id
   - oppure asset da un task upstream (prima riga numerica in seed_queries)
2. Per ogni asset non optedout/contacted/replied (su `outreach_status`):
   - instanzia message_subject + message_template con placeholder
   - per ogni canale abilitato in task.message_channels, invia
   - rispetta rate_limit_per_minute
3. Aggiorna asset.outreach_status = 'contacted'
4. Scrive outreach_log.jsonl nella run dir + summary
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .. import db
from ..channels import email as ch_email
from ..channels import telegram as ch_telegram
from ..channels.base import is_enabled as channel_enabled
from ..config import RESULTS_DIR


log = logging.getLogger(__name__)
DEFAULT_RATE_LIMIT = 10  # msg/min


def _domain_of(url: str | None) -> str | None:
    if not url:
        return None
    try:
        return (urlparse(url).hostname or "").lower() or None
    except Exception:
        return None


def _ingest_artifact(
    task_id: int,
    job_id: int,
    artifact_path: str,
    jlog,
) -> int:
    """Legge profiles.jsonl e materializza asset (asset_type='contact_ingest').
    Ritorna numero asset ingestiti."""
    p = Path(artifact_path)
    if not p.exists():
        jlog(f"Artifact non trovato: {p}")
        return 0
    n = 0
    skipped = 0
    with p.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                skipped += 1
                continue
            email = obj.get("email") or None
            tg_user = obj.get("telegram") or obj.get("telegram_username") or None
            if isinstance(tg_user, str):
                tg_user = tg_user.lstrip("@") or None
            if not email and not tg_user:
                skipped += 1
                continue
            url = obj.get("url") or obj.get("source_url")
            display = obj.get("display_name") or obj.get("username") or obj.get("nickname")
            db.upsert_asset({
                "asset_type": "contact_ingest",
                "source_task_id": task_id,
                "source_job_id": job_id,
                "source_url": url,
                "source_domain": obj.get("source_domain") or _domain_of(url),
                "title": display or (email or tg_user) or "(ingest)",
                "display_name": display,
                "email": email,
                "telegram_username": tg_user,
                "raw_json": line,
                "status": "qualified",
                "outreach_status": "pending",
            })
            n += 1
    if skipped:
        jlog(f"Ingest: {n} asset, {skipped} righe scartate (no email/telegram o JSON invalido)")
    else:
        jlog(f"Ingest: {n} asset dal profiles.jsonl")
    return n


_PLACEHOLDER_RE = re.compile(r"\{(\w+)\}")


def _instantiate(template: str, asset: dict[str, Any]) -> str:
    """Sostituisce {display_name}, {source_url}, {source_domain}, {email}, ecc."""
    if not template:
        return ""
    safe_data = {
        "display_name": asset.get("display_name") or asset.get("title") or "",
        "source_url": asset.get("source_url") or "",
        "source_domain": asset.get("source_domain") or "",
        "email": asset.get("email") or "",
        "telegram_username": asset.get("telegram_username") or "",
    }
    def repl(m):
        return str(safe_data.get(m.group(1), m.group(0)))
    return _PLACEHOLDER_RE.sub(repl, template)


def _resolve_source_task_for_assets(task: dict[str, Any]) -> int | None:
    """Se l'utente ha messo come prima riga di seed_queries un numero, è il task upstream."""
    seeds = task.get("seed_queries") or []
    for s in seeds:
        s = s.strip()
        if s.isdigit():
            return int(s)
    return None


async def run_agent(task: dict[str, Any], job_id: int) -> str:
    def jlog(line: str) -> None:
        db.append_job_log(job_id, line)

    db.update_job(job_id, status="running", started_at=db.now_iso())
    db.set_control_signal(job_id, None)
    jlog(f"Avvio outreach per task #{task['id']} \"{task['name']}\"")

    # 1. canali validi
    requested_channels = list(task.get("message_channels") or [])
    if not requested_channels:
        msg = "Nessun canale specificato in 'message_channels'. Aborto."
        jlog(msg)
        db.update_job(job_id, status="error", error=msg, finished_at=db.now_iso())
        raise RuntimeError(msg)

    active_channels: list[str] = []
    for ch in requested_channels:
        if channel_enabled(ch):
            active_channels.append(ch)
        else:
            jlog(f"Canale '{ch}' disabilitato in /settings — skip.")
    if not active_channels:
        msg = "Nessun canale abilitato. Configura /settings prima di lanciare outreach."
        jlog(msg)
        db.update_job(job_id, status="error", error=msg, finished_at=db.now_iso())
        raise RuntimeError(msg)
    jlog(f"Canali attivi: {active_channels}")

    # 2. run dir
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_dir = RESULTS_DIR / str(task["id"]) / ts
    run_dir.mkdir(parents=True, exist_ok=True)
    log_path = run_dir / "outreach_log.jsonl"

    # 3. ingest artifact se specificato
    artifact = task.get("input_artifact_path")
    if artifact:
        _ingest_artifact(task["id"], job_id, artifact, jlog)

    # 4. seleziona asset target.
    # Priorità:
    #   0. `target_asset_ids` (snapshot esplicito dal task — vince su tutto) o
    #      `target_contact_ids` (legacy, retrocompat: i runner social/wa lo
    #      onorano gia' allo stesso modo)
    #   1. `outreach_filter_source_task_id` (filtro esplicito dal form)
    #   2. `outreach_filter_source_follower_of` -tradotto in asset_tag filter
    #   3. fallback upstream da workflow_edges o source_task_id=current
    explicit_ids_raw = task.get("target_asset_ids") or task.get("target_contact_ids") or []
    explicit_ids: list[int] = []
    for x in explicit_ids_raw:
        try:
            i = int(x)
            if i > 0:
                explicit_ids.append(i)
        except (TypeError, ValueError):
            continue

    candidates: list[dict[str, Any]] = []
    seen_ids: set[int] = set()
    n_skip_missing = 0  # asset_id esplicito non trovato in DB (eliminato post-snapshot)

    if explicit_ids:
        jlog(f"📌 Audience snapshot: {len(explicit_ids)} asset_id espliciti (filtri legacy ignorati).")
        for aid in explicit_ids:
            if aid in seen_ids:
                continue
            a = db.get_asset(aid)
            if not a:
                n_skip_missing += 1
                continue
            seen_ids.add(aid)
            candidates.append(a)
        if n_skip_missing:
            jlog(f"  ⚠ {n_skip_missing} asset_id non trovati (eliminati post-snapshot)")
    else:
        # Branch legacy: filtri dinamici outreach_filter_*
        filter_tid_raw = task.get("outreach_filter_source_task_id")
        filter_tid = int(filter_tid_raw) if str(filter_tid_raw or "").strip().isdigit() else None
        filter_fof = (task.get("outreach_filter_source_follower_of") or "").strip() or None
        filter_tags_raw = task.get("outreach_filter_tags") or []
        if isinstance(filter_tags_raw, str):
            try:
                import json as _json
                filter_tags_raw = _json.loads(filter_tags_raw) or []
            except Exception:
                filter_tags_raw = []
        filter_tags = [
            (t.get("key"), t.get("value")) for t in filter_tags_raw
            if isinstance(t, dict) and t.get("key") and t.get("value")
        ]
        # follower_of viene espresso come tag filter su asset_tags
        if filter_fof:
            filter_tags.append(("source_follower_of", filter_fof))
        if filter_tid or filter_fof or filter_tags:
            jlog(
                f"Filtri destinatari: source_task_id={filter_tid}, "
                f"source_follower_of={filter_fof!r}, tags={filter_tags}"
            )

        upstream_tid = filter_tid if filter_tid is not None else _resolve_source_task_for_assets(task)
        # Asset qualified, non optedout, non contacted, che abbiano almeno email o telegram_username.
        for a in db.list_assets_for_email_outreach(
            source_task_id=upstream_tid,
            asset_tag_filters=filter_tags or None,
            only_qualified=True,
            exclude_optedout=True,
            exclude_contacted=True,
            limit=10000,
        ):
            if a["id"] in seen_ids:
                continue
            seen_ids.add(a["id"])
            candidates.append(a)
        # Quando non c'è upstream esplicito né filtro, considera asset ingestiti col job corrente
        if not upstream_tid and not filter_fof and not filter_tags and not candidates:
            for a in db.list_assets_for_email_outreach(
                source_task_id=task["id"],
                only_qualified=False,  # asset appena ingestiti possono essere 'new'
                exclude_optedout=True,
                exclude_contacted=True,
                limit=10000,
            ):
                if a["id"] in seen_ids:
                    continue
                seen_ids.add(a["id"])
                candidates.append(a)

    jlog(f"Candidati outreach: {len(candidates)}")
    if not candidates:
        jlog("Nessun asset da contattare. Verifica input_artifact_path, snapshot o filtri.")

    # 5. setup rate limit
    email_cfg = db.get_channel_config("email") or {}
    rate_per_min = int((email_cfg.get("config") or {}).get("rate_limit_per_minute") or DEFAULT_RATE_LIMIT)
    sleep_between = 60.0 / max(1, rate_per_min)

    # 6. invio
    subject_tpl = task.get("message_subject") or "Una proposta per te"
    body_tpl = task.get("message_template") or ""
    if not body_tpl.strip():
        msg = "message_template vuoto: l'outreach non può inviare nulla."
        jlog(msg)
        db.update_job(job_id, status="error", error=msg, finished_at=db.now_iso())
        raise RuntimeError(msg)

    n_sent = 0
    n_failed = 0
    n_skip_optedout = 0
    n_skip_already_contacted = 0
    n_skip_no_email = 0
    n_skip_no_telegram = 0
    stopped = False

    def _log_skip(logf, asset_id: int, channel: str, reason: str) -> None:
        logf.write(json.dumps({
            "ts": db.now_iso(), "asset_id": asset_id,
            "channel": channel, "status": "skipped", "reason": reason,
        }) + "\n")

    with log_path.open("w", encoding="utf-8") as logf:
        for i, asset in enumerate(candidates, 1):
            sig = db.get_control_signal(job_id)
            if sig == "stop":
                jlog("STOP richiesto, interrompo outreach.")
                stopped = True
                break

            os_ = asset.get("outreach_status") or ""
            if os_ == "optedout":
                n_skip_optedout += 1
                _log_skip(logf, asset["id"], "asset", "optedout")
                continue
            if os_ in ("contacted", "replied"):
                n_skip_already_contacted += 1
                _log_skip(logf, asset["id"], "asset", "already_contacted")
                continue

            subject = _instantiate(subject_tpl, asset)
            body = _instantiate(body_tpl, asset)

            sent_any = False
            for channel in active_channels:
                try:
                    if channel == "email":
                        if not ch_email.is_valid_email(asset.get("email")):
                            n_skip_no_email += 1
                            _log_skip(logf, asset["id"], "email", "no_email")
                            continue
                        thread_id = db.get_or_create_thread(
                            asset_id=asset["id"], channel="email",
                            subject=subject, task_id=task["id"],
                        )
                        msg_id = await ch_email.send_email(
                            asset["email"], subject, body
                        )
                        db.insert_message(
                            thread_id, "out", body, llm_generated=False,
                            external_id=msg_id, status="sent", sent_at=db.now_iso(),
                        )
                        # Salva il Message-ID come external_id del thread (se non già presente)
                        t = db.get_thread(thread_id)
                        if t and not t.get("external_id"):
                            with db.connect() as con:
                                con.execute(
                                    "UPDATE threads SET external_id = %s WHERE id = %s",
                                    (msg_id, thread_id),
                                )
                        db.touch_thread(thread_id)
                        sent_any = True
                        logf.write(json.dumps({
                            "ts": db.now_iso(), "asset_id": asset["id"],
                            "channel": "email", "to": asset["email"],
                            "external_id": msg_id, "status": "sent",
                        }) + "\n")
                        jlog(f"  email -> {asset['email']} (msg-id {msg_id[:30]})")
                    elif channel == "telegram":
                        chat_id = asset.get("telegram_chat_id")
                        if not chat_id:
                            n_skip_no_telegram += 1
                            _log_skip(logf, asset["id"], "telegram", "no_telegram_chat_id")
                            continue
                        thread_id = db.get_or_create_thread(
                            asset_id=asset["id"], channel="telegram",
                            external_id=str(chat_id), task_id=task["id"],
                        )
                        msg_id = await ch_telegram.send_message(chat_id, body)
                        db.insert_message(
                            thread_id, "out", body, llm_generated=False,
                            external_id=msg_id, status="sent", sent_at=db.now_iso(),
                        )
                        db.touch_thread(thread_id)
                        sent_any = True
                        logf.write(json.dumps({
                            "ts": db.now_iso(), "asset_id": asset["id"],
                            "channel": "telegram", "to": chat_id,
                            "external_id": msg_id, "status": "sent",
                        }) + "\n")
                        jlog(f"  telegram -> chat {chat_id} (msg {msg_id})")
                except Exception as e:
                    n_failed += 1
                    err = f"{type(e).__name__}: {e}"
                    jlog(f"  WARN {channel} -> {asset.get('email') or asset.get('telegram_chat_id')}: {err}")
                    logf.write(json.dumps({
                        "ts": db.now_iso(), "asset_id": asset["id"],
                        "channel": channel, "status": "failed", "error": err,
                    }) + "\n")

            if sent_any:
                db.update_asset_outreach_status(asset["id"], "contacted")
                n_sent += 1

            # rate limit
            if i < len(candidates):
                await asyncio.sleep(sleep_between)

    n_skipped = n_skip_optedout + n_skip_already_contacted + n_skip_no_email + n_skip_no_telegram

    # 7. report con breakdown skip per canale + stato
    fmt = task.get("output_format") or "md"
    report_ext = "md" if fmt in ("md", "both") else "txt"
    status_text = "INTERROTTO dall'utente" if stopped else "Completato"

    audience_size = len(explicit_ids) if explicit_ids else len(candidates)
    skip_lines: list[str] = []
    if "email" in active_channels and n_skip_no_email:
        skip_lines.append(f"  - **{n_skip_no_email}** asset senza email valida")
    if "telegram" in active_channels and n_skip_no_telegram:
        skip_lines.append(f"  - **{n_skip_no_telegram}** asset senza `telegram_chat_id` (non hanno scritto al bot)")
    if n_skip_optedout:
        skip_lines.append(f"  - **{n_skip_optedout}** outreach_status=optedout")
    if n_skip_already_contacted:
        skip_lines.append(f"  - **{n_skip_already_contacted}** outreach_status=contacted/replied (run precedente)")
    if n_skip_missing:
        skip_lines.append(f"  - **{n_skip_missing}** asset eliminati post-snapshot (id non piu' presenti in DB)")

    report_lines = [
        f"# Outreach run {ts}",
        "",
        f"- **Task**: {task['name']} (#{task['id']})",
        f"- **Canali**: {', '.join(active_channels)}",
        f"- **Audience**: {audience_size} asset" + (" (snapshot esplicito)" if explicit_ids else " (filtri dinamici)"),
        f"- **Inviati**: {n_sent}",
        f"- **Falliti**: {n_failed}",
        f"- **Skippati totali**: {n_skipped}",
    ]
    if skip_lines:
        report_lines.append("- **Breakdown skip**:")
        report_lines.extend(skip_lines)
    report_lines.extend([
        f"- **Stato**: {status_text}",
        "",
        "Vedi `outreach_log.jsonl` per il dettaglio per messaggio.",
        "",
    ])
    report = "\n".join(report_lines)

    report_path = run_dir / f"report.{report_ext}"
    report_path.write_text(report, encoding="utf-8")
    jlog(
        f"Outreach concluso: {n_sent} inviati, {n_failed} falliti, {n_skipped} skippati "
        f"(no_email={n_skip_no_email}, no_telegram={n_skip_no_telegram}, "
        f"optedout={n_skip_optedout}, already={n_skip_already_contacted}, missing={n_skip_missing})."
    )

    final_status = "cancelled" if stopped else "done"
    db.update_job(
        job_id,
        status=final_status,
        finished_at=db.now_iso(),
        result_path=str(report_path),
    )
    db.set_control_signal(job_id, None)
    return str(report_path)
