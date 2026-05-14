"""Runner outreach: legge contatti dal profiles.jsonl upstream (o dalla
tabella contacts) e invia messaggi su email + telegram.

Step:
1. Risolve la sorgente dei contatti:
   - se task.input_artifact_path è impostato → ingest profiles.jsonl in `contacts`
   - altrimenti usa contacts già in DB con source_task_id == task.id
   - oppure contacts da un task upstream (prima riga numerica in seed_queries)
2. Per ogni contatto non `optedout`/`contacted`/`replied`:
   - instanzia message_subject + message_template con placeholder
   - per ogni canale abilitato in task.message_channels, invia
   - rispetta rate_limit_per_minute
3. Aggiorna contact.status = 'contacted'
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
    """Legge profiles.jsonl e materializza contatti. Ritorna numero ingestiti."""
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
            db.upsert_contact({
                "source_task_id": task_id,
                "source_job_id": job_id,
                "source_url": obj.get("url") or obj.get("source_url"),
                "source_domain": obj.get("source_domain") or _domain_of(obj.get("url")),
                "display_name": obj.get("display_name") or obj.get("username") or obj.get("nickname"),
                "email": email,
                "telegram_username": tg_user,
                "raw_json": line,
            })
            n += 1
    if skipped:
        jlog(f"Ingest: {n} contatti, {skipped} righe scartate (no email/telegram o JSON invalido)")
    else:
        jlog(f"Ingest: {n} contatti dal profiles.jsonl")
    return n


_PLACEHOLDER_RE = re.compile(r"\{(\w+)\}")


def _instantiate(template: str, contact: dict[str, Any]) -> str:
    """Sostituisce {display_name}, {source_url}, {source_domain}, {email}, ecc."""
    if not template:
        return ""
    safe_data = {
        "display_name": contact.get("display_name") or "",
        "source_url": contact.get("source_url") or "",
        "source_domain": contact.get("source_domain") or "",
        "email": contact.get("email") or "",
        "telegram_username": contact.get("telegram_username") or "",
    }
    def repl(m):
        return str(safe_data.get(m.group(1), m.group(0)))
    return _PLACEHOLDER_RE.sub(repl, template)


def _resolve_source_task_for_contacts(task: dict[str, Any]) -> int | None:
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

    # 4. seleziona contatti target.
    # Priorità delle sorgenti dei contatti candidati:
    #   1. `outreach_filter_source_task_id` (filtro esplicito dal form) — vince su upstream
    #   2. `outreach_filter_source_follower_of` (asset_tag, JOIN) — applicato AND con 1
    #   3. fallback upstream da workflow_edges o source_task_id=current
    filter_tid_raw = task.get("outreach_filter_source_task_id")
    filter_tid = int(filter_tid_raw) if str(filter_tid_raw or "").strip().isdigit() else None
    filter_fof = (task.get("outreach_filter_source_follower_of") or "").strip() or None
    if filter_tid or filter_fof:
        jlog(
            f"📤 Filtri destinatari: source_task_id={filter_tid}, "
            f"source_follower_of={filter_fof!r}"
        )

    upstream_tid = filter_tid if filter_tid is not None else _resolve_source_task_for_contacts(task)
    target_status_eligible = ("qualified", "new")
    candidates: list[dict[str, Any]] = []
    seen_ids: set[int] = set()
    for st in target_status_eligible:
        for c in db.list_contacts(
            status=st,
            source_task_id=upstream_tid,
            source_follower_of=filter_fof,
        ):
            if c["id"] in seen_ids:
                continue
            seen_ids.add(c["id"])
            candidates.append(c)
    # Quando non c'è upstream esplicito né filtro, considera contatti ingestiti col job corrente
    if not upstream_tid and not filter_fof and not candidates:
        for st in target_status_eligible:
            for c in db.list_contacts(status=st, source_task_id=task["id"]):
                if c["id"] in seen_ids:
                    continue
                seen_ids.add(c["id"])
                candidates.append(c)

    jlog(f"Candidati outreach: {len(candidates)}")
    if not candidates:
        jlog("Nessun contatto da contattare. Verifica input_artifact_path o task upstream.")

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
    n_skipped = 0
    stopped = False

    with log_path.open("w", encoding="utf-8") as logf:
        for i, contact in enumerate(candidates, 1):
            sig = db.get_control_signal(job_id)
            if sig == "stop":
                jlog("STOP richiesto, interrompo outreach.")
                stopped = True
                break

            if contact.get("status") in ("optedout", "contacted", "replied"):
                n_skipped += 1
                continue

            subject = _instantiate(subject_tpl, contact)
            body = _instantiate(body_tpl, contact)

            sent_any = False
            for channel in active_channels:
                try:
                    if channel == "email":
                        if not ch_email.is_valid_email(contact.get("email")):
                            continue
                        thread_id = db.get_or_create_thread(
                            contact["id"], "email", subject=subject, task_id=task["id"]
                        )
                        msg_id = await ch_email.send_email(
                            contact["email"], subject, body
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
                                    "UPDATE threads SET external_id = ? WHERE id = ?",
                                    (msg_id, thread_id),
                                )
                        db.touch_thread(thread_id)
                        sent_any = True
                        logf.write(json.dumps({
                            "ts": db.now_iso(), "contact_id": contact["id"],
                            "channel": "email", "to": contact["email"],
                            "external_id": msg_id, "status": "sent",
                        }) + "\n")
                        jlog(f"  ✉️ → {contact['email']} (msg-id {msg_id[:30]})")
                    elif channel == "telegram":
                        chat_id = contact.get("telegram_chat_id")
                        if not chat_id:
                            jlog(
                                f"  ⏭️ skip telegram per {contact.get('telegram_username') or contact['id']}: "
                                "il contatto non ha ancora scritto al bot (chat_id mancante)"
                            )
                            continue
                        thread_id = db.get_or_create_thread(
                            contact["id"], "telegram", external_id=str(chat_id),
                            task_id=task["id"]
                        )
                        msg_id = await ch_telegram.send_message(chat_id, body)
                        db.insert_message(
                            thread_id, "out", body, llm_generated=False,
                            external_id=msg_id, status="sent", sent_at=db.now_iso(),
                        )
                        db.touch_thread(thread_id)
                        sent_any = True
                        logf.write(json.dumps({
                            "ts": db.now_iso(), "contact_id": contact["id"],
                            "channel": "telegram", "to": chat_id,
                            "external_id": msg_id, "status": "sent",
                        }) + "\n")
                        jlog(f"  💬 → chat {chat_id} (msg {msg_id})")
                except Exception as e:
                    n_failed += 1
                    err = f"{type(e).__name__}: {e}"
                    jlog(f"  ⚠️ {channel} → {contact.get('email') or contact.get('telegram_chat_id')}: {err}")
                    logf.write(json.dumps({
                        "ts": db.now_iso(), "contact_id": contact["id"],
                        "channel": channel, "status": "failed", "error": err,
                    }) + "\n")

            if sent_any:
                db.update_contact_status(contact["id"], "contacted")
                n_sent += 1
            else:
                n_skipped += 1

            # rate limit
            if i < len(candidates):
                await asyncio.sleep(sleep_between)

    # 7. report
    fmt = task.get("output_format") or "md"
    report_ext = "md" if fmt in ("md", "both") else "txt"
    status_text = "INTERROTTO dall'utente" if stopped else "Completato"
    report = (
        f"# Outreach run {ts}\n\n"
        f"- **Task**: {task['name']} (#{task['id']})\n"
        f"- **Canali**: {', '.join(active_channels)}\n"
        f"- **Candidati**: {len(candidates)}\n"
        f"- **Inviati**: {n_sent}\n"
        f"- **Falliti**: {n_failed}\n"
        f"- **Skippati**: {n_skipped}\n"
        f"- **Stato**: {status_text}\n\n"
        f"Vedi `outreach_log.jsonl` per il dettaglio per messaggio.\n"
    )
    report_path = run_dir / f"report.{report_ext}"
    report_path.write_text(report, encoding="utf-8")
    jlog(f"Outreach concluso: {n_sent} inviati, {n_failed} falliti, {n_skipped} skippati.")

    final_status = "cancelled" if stopped else "done"
    db.update_job(
        job_id,
        status=final_status,
        finished_at=db.now_iso(),
        result_path=str(report_path),
    )
    db.set_control_signal(job_id, None)
    return str(report_path)
