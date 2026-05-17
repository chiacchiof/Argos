"""Runner responder: legge messaggi inbound non ancora processati,
genera reply via LLM e invia. Auto-detect opt-out keywords.
"""
from __future__ import annotations

import asyncio
import logging
import re
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from .. import db
from ..channels import email as ch_email
from ..channels import telegram as ch_telegram
from ..channels.base import is_enabled as channel_enabled
from ..config import RESULTS_DIR, settings
from .llm_providers import get_provider, resolve_api_key, resolve_base_url
from .ollama import maybe_add_keep_alive


log = logging.getLogger(__name__)


OPT_OUT_PATTERNS = [
    r"\bSTOP\b",
    r"\bunsubscribe\b",
    r"\bdisiscriv",
    r"\brimuovimi\b",
    r"\bremove\s+me\b",
    r"\bnon\s+contattarmi\b",
    r"\bcancella(mi)?\s+",
    r"\bopt[- ]?out\b",
]
OPT_OUT_RE = re.compile("|".join(OPT_OUT_PATTERNS), re.IGNORECASE)


def _is_opt_out(body: str) -> bool:
    return bool(OPT_OUT_RE.search(body or ""))


async def _generate_reply(
    task: dict[str, Any],
    thread: dict[str, Any],
    history: list[dict[str, Any]],
    incoming_body: str,
) -> str:
    """Chiama l'LLM via OpenAI-compat REST per generare una risposta."""
    provider_key = task.get("llm_provider") or "ollama"
    base_url = resolve_base_url(provider_key, task.get("llm_base_url"))
    api_key = resolve_api_key(provider_key, task.get("llm_api_key"))
    model = task["model"]

    system_prompt = task.get("responder_system_prompt") or (
        "Sei un assistente cordiale che risponde in italiano alle email/messaggi che "
        "ricevi. Mantieni il tono professionale e amichevole. Se l'utente fa una domanda "
        "specifica, rispondi nel merito; altrimenti ringrazia per la risposta e proponi "
        "di organizzare una breve call la prossima settimana. Mai inventare dati."
    )

    msgs: list[dict[str, str]] = [{"role": "system", "content": system_prompt}]
    for m in history[-10:]:
        role = "assistant" if m["direction"] == "out" else "user"
        msgs.append({"role": role, "content": m["body"][:4000]})
    # last incoming è già in history; ridondante ma esplicito
    if not msgs[-1]["content"].startswith(incoming_body[:50]):
        msgs.append({"role": "user", "content": incoming_body[:4000]})

    payload: dict[str, Any] = {
        "model": model,
        "messages": msgs,
        "temperature": 0.4,
        "max_tokens": 600,
    }
    maybe_add_keep_alive(payload, base_url)
    headers = {"Authorization": f"Bearer {api_key}"} if api_key and api_key != "ollama-local" else {}
    if api_key == "ollama-local":
        headers["Authorization"] = "Bearer ollama-local"  # alcuni gateway lo richiedono comunque

    url = f"{base_url.rstrip('/')}/chat/completions"
    async with httpx.AsyncClient(timeout=120) as client:
        r = await client.post(url, json=payload, headers=headers)
        r.raise_for_status()
        data = r.json()
    try:
        return (data["choices"][0]["message"]["content"] or "").strip()
    except Exception:
        return "(LLM non ha prodotto una risposta valida)"


async def run_agent(task: dict[str, Any], job_id: int) -> str:
    def jlog(line: str) -> None:
        db.append_job_log(job_id, line)

    db.update_job(job_id, status="running", started_at=db.now_iso())
    db.set_control_signal(job_id, None)
    jlog(f"Avvio responder per task #{task['id']} \"{task['name']}\" — modello {task['model']}")

    pending = db.find_unprocessed_inbound()
    jlog(f"Inbound non processati: {len(pending)}")

    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_dir = RESULTS_DIR / str(task["id"]) / ts
    run_dir.mkdir(parents=True, exist_ok=True)

    n_replied = 0
    n_optouts = 0
    n_failed = 0
    n_skipped = 0
    stopped = False

    for inbound in pending:
        sig = db.get_control_signal(job_id)
        if sig == "stop":
            jlog("STOP richiesto, interrompo responder.")
            stopped = True
            break

        thread_id = inbound["thread_id"]
        thread = db.get_thread(thread_id)
        if not thread:
            n_skipped += 1
            continue
        # Risolvi destinatario: preferisci asset_id (Fase 2D); fallback su
        # contact->asset_id durante la transizione.
        asset = None
        if thread.get("asset_id"):
            asset = db.get_asset(thread["asset_id"])
        if asset is None and thread.get("contact_id"):
            with db.connect() as con:
                row = con.execute(
                    "SELECT asset_id FROM contacts WHERE id = %s",
                    (thread["contact_id"],),
                ).fetchone()
            if row and row.get("asset_id"):
                asset = db.get_asset(row["asset_id"])
        if not asset:
            n_skipped += 1
            continue

        # se asset gia' optedout, skip
        if (asset.get("outreach_status") or "") == "optedout":
            jlog(f"  asset #{asset['id']} optedout: skip")
            n_skipped += 1
            continue

        # opt-out detection
        if _is_opt_out(inbound["body"]):
            jlog(f"  opt-out detected da asset #{asset['id']} (msg #{inbound['id']}): NON rispondo")
            db.update_asset_outreach_status(asset["id"], "optedout", notes="Auto-detected opt-out keyword")
            db.update_thread_status(thread_id, "optedout")
            # marca message come processato (status='received' diventa 'received_processed' implicito
            # tramite presenza di un 'out' successivo... qui non ne mandiamo, quindi inseriamo un msg
            # outbound vuoto con status='skipped_optout' per "consumarlo")
            db.insert_message(thread_id, "out", "(opt-out: nessuna risposta inviata)",
                              llm_generated=False, status="skipped_optout")
            n_optouts += 1
            continue

        # canale attivo?
        if not channel_enabled(thread["channel"]):
            jlog(f"  ⏭️ canale {thread['channel']} non abilitato: skip")
            n_skipped += 1
            continue

        # genera reply
        history = db.list_messages(thread_id)
        try:
            reply = await _generate_reply(task, thread, history, inbound["body"])
            if not reply.strip():
                raise RuntimeError("LLM ha prodotto reply vuota")
            jlog(f"  🤖 generata reply ({len(reply)} char) per thread #{thread_id}")
        except Exception as e:
            n_failed += 1
            err = f"LLM: {type(e).__name__}: {e}"
            jlog(f"  ⚠️ {err}")
            db.insert_message(thread_id, "out", "(generazione reply fallita)",
                              llm_generated=True, status="failed", error=err)
            continue

        # invia
        try:
            if thread["channel"] == "email":
                subject = thread.get("subject") or "(senza oggetto)"
                if not subject.lower().startswith("re:"):
                    subject = f"Re: {subject}"
                in_reply_to = inbound.get("external_id") or thread.get("external_id")
                msg_id = await ch_email.send_email(
                    asset["email"], subject, reply, in_reply_to=in_reply_to,
                )
                db.insert_message(thread_id, "out", reply, llm_generated=True,
                                  external_id=msg_id, status="sent", sent_at=db.now_iso())
            elif thread["channel"] == "telegram":
                chat_id = asset.get("telegram_chat_id") or thread.get("external_id")
                if not chat_id:
                    raise RuntimeError("chat_id mancante")
                msg_id = await ch_telegram.send_message(chat_id, reply)
                db.insert_message(thread_id, "out", reply, llm_generated=True,
                                  external_id=msg_id, status="sent", sent_at=db.now_iso())
            db.touch_thread(thread_id)
            db.update_thread_status(thread_id, "replied")
            n_replied += 1
        except Exception as e:
            n_failed += 1
            err = f"send: {type(e).__name__}: {e}"
            jlog(f"  ⚠️ {err}")
            db.insert_message(thread_id, "out", reply, llm_generated=True,
                              status="failed", error=err)

        # piccolo delay per evitare burst
        await asyncio.sleep(1.0)

    fmt = task.get("output_format") or "md"
    report_ext = "md" if fmt in ("md", "both") else "txt"
    report = (
        f"# Responder run {ts}\n\n"
        f"- **Task**: {task['name']} (#{task['id']})\n"
        f"- **Inbound trovati**: {len(pending)}\n"
        f"- **Risposte inviate**: {n_replied}\n"
        f"- **Opt-out detectati**: {n_optouts}\n"
        f"- **Falliti**: {n_failed}\n"
        f"- **Skippati**: {n_skipped}\n"
        f"- **Stato**: {'INTERROTTO' if stopped else 'Completato'}\n"
    )
    report_path = run_dir / f"report.{report_ext}"
    report_path.write_text(report, encoding="utf-8")

    final_status = "cancelled" if stopped else "done"
    db.update_job(job_id, status=final_status, finished_at=db.now_iso(),
                  result_path=str(report_path))
    db.set_control_signal(job_id, None)
    jlog(f"Responder concluso: {n_replied} risposte, {n_optouts} opt-out, {n_failed} falliti.")
    return str(report_path)
