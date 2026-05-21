"""Template del runner outreach_social (da spostare in app/agent/runner_outreach_social.py).

Schema simmetrico agli altri runner: `async def run_agent(task, job_id) -> str`.

Flusso operativo:
  1. Verifica `AGENTSCRAPER_SECRET` settata (cifratura credenziali)
  2. Carica account social attivi dal DB (task.social_platform = 'instagram'|'tiktok')
  3. Carica target contacts: quelli con social[platform] popolato e status='qualified'
  4. Genera messaggi personalizzati via LLM per ogni target (Qwen locale)
  5. Engine.run_session() apre browser headed con stealth, fa DM
  6. Log risultati su `social_dm_log`, update contacts.status -> 'contacted' se ok
  7. Report finale

NON ANCORA INTEGRATO: serve la migration DB applicata + spostamento social/ in app/.
Vedi `dev/NEXT_STEPS.md`.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Quando questo file sarà in app/agent/runner_outreach_social.py,
# gli import diventano:
# from .. import db
# from ..config import RESULTS_DIR
# from .social.platform_base import SocialAccount, HealthStatus
# from .social.crypto_creds import decrypt, is_configured
# from .social.engine import OutreachEngine
# from .social.message_generator import MessageRequest, generate_batch

log = logging.getLogger(__name__)


# === Template del runner (DA SPOSTARE / RINOMINARE IMPORT) ===

async def run_agent(task: dict[str, Any], job_id: int) -> str:
    """Esegue una sessione outreach social.

    Campi del task usati (TUTTI dal task standard, niente file separati):
      - social_platform: "instagram" | "tiktok"
      - max_dms_per_run: int (default 30, cap totale per questa sessione)
      - max_dms_per_session: int (default 5, max DM in una singola sessione browser)
      - target_status_in: str (default "qualified", filtro contacts)
      - outreach_intent: str (descrizione user dello scopo, va al prompt LLM)
      - message_template_variants: str (esempi di stile separati da "---",
        usati come ispirazione per l'LLM. Esempio:
          "Ciao Sara, ho visto il tuo profilo...
          ---
          Hey, mi piace molto come...
          ---
          Buongiorno, sto cercando creator come te per..."
      - llm_model_for_messages: str (default "qwen3-coder:30b")
      - headed: bool (default True — raccomandato)

    Riusa il campo esistente `message_template` se l'utente vuole un singolo
    esempio (backward compat con outreach email): se message_template_variants
    e' vuoto MA message_template e' valorizzato, viene usato come singolo esempio.
    """
    # PSEUDO-CODICE strutturato. Quando integrato, riempire import.
    raise NotImplementedError(
        "Questo è un template. Sposta in app/agent/runner_outreach_social.py "
        "e fixa gli import. Vedi dev/NEXT_STEPS.md per la procedura completa."
    )

    # === STEP 1: validazioni iniziali ===
    """
    from ..agent.social.crypto_creds import is_configured
    from .. import db

    db.update_job(job_id, status="running", started_at=db.now_iso())
    db.set_control_signal(job_id, None)

    def jlog(s: str) -> None:
        db.append_job_log(job_id, s)

    jlog(f"Avvio outreach_social per task #{task['id']} \"{task['name']}\"")

    if not is_configured():
        msg = "ARGOS_SECRET non settata in .env: niente cifratura credenziali → abort"
        jlog(f"❌ {msg}")
        db.update_job(job_id, status="error", error=msg, finished_at=db.now_iso())
        return ""

    platform_name = (task.get("social_platform") or "instagram").lower()
    if platform_name not in ("instagram", "tiktok"):
        msg = f"social_platform '{platform_name}' non supportata"
        db.update_job(job_id, status="error", error=msg, finished_at=db.now_iso())
        return ""
    """

    # === STEP 2: carica account attivi ===
    """
    from .social.platform_base import SocialAccount
    from .social.crypto_creds import decrypt

    rows = db.list_social_accounts(platform=platform_name, status="active")
    if not rows:
        msg = f"Nessun account social '{platform_name}' attivo nel DB"
        jlog(f"❌ {msg}")
        db.update_job(job_id, status="error", error=msg, finished_at=db.now_iso())
        return ""

    accounts: list[SocialAccount] = []
    for r in rows:
        try:
            password = decrypt(r["encrypted_password"])
        except Exception as e:
            jlog(f"⚠️ decrypt failed for account {r['username']}: {e} — skip")
            continue
        accounts.append(SocialAccount(
            uuid=r["uuid"],
            platform=r["platform"],
            username=r["username"],
            password=password,
            proxy_label=r.get("proxy_label"),
            daily_dm_cap=r.get("daily_dm_cap", 10),
            status=r["status"],
        ))
    jlog(f"Caricati {len(accounts)} account {platform_name} attivi")
    """

    # === STEP 3: carica target dal DB ===
    """
    target_status_in = task.get("target_status_in") or "qualified"
    max_dms_per_run = int(task.get("max_dms_per_run") or 30)
    contacts = db.list_contacts(status=target_status_in, limit=max_dms_per_run * 3)

    # Filtra contacts con social[platform_name] popolato
    targets = []
    for c in contacts:
        soc_raw = c.get("social_json")
        if not soc_raw: continue
        try:
            socials = json.loads(soc_raw)
        except Exception: continue
        for s in socials:
            if isinstance(s, dict) and s.get("platform", "").lower() == platform_name:
                username = _extract_username_from_url(s.get("url", ""), platform_name)
                if username:
                    targets.append({"contact": c, "username": username})
                    break

    jlog(f"Target con {platform_name}: {len(targets)} contacts")
    if not targets:
        db.update_job(job_id, status="done", finished_at=db.now_iso())
        return ""

    # Limita a max_dms_per_run
    targets = targets[:max_dms_per_run]
    """

    # === STEP 4: genera messaggi personalizzati via LLM ===
    """
    from .social.message_generator import MessageRequest, generate_batch

    intent = (task.get("outreach_intent") or "Outreach commerciale per ottimizzazione contenuti del profilo").strip()
    llm_model_for_messages = task.get("llm_model_for_messages") or "qwen3-coder:30b"
    llm_base_url = task.get("llm_base_url") or "http://localhost:11434/v1"
    llm_api_key = task.get("llm_api_key") or ""

    reqs = []
    for t in targets:
        c = t["contact"]
        reqs.append(MessageRequest(
            target_display_name=c.get("display_name") or t["username"],
            target_username=t["username"],
            target_platform=platform_name,
            target_profile_url=c.get("source_url") or "",
            target_raw_data=json.loads(c.get("raw_json") or "{}") if isinstance(c.get("raw_json"), str) else (c.get("raw_json") or {}),
            intent=intent,
        ))

    jlog(f"Generazione messaggi personalizzati via {llm_model_for_messages}...")
    msg_results = await generate_batch(reqs, llm_base_url=llm_base_url, llm_api_key=llm_api_key, llm_model=llm_model_for_messages)

    # Filtra solo quelli generati con successo
    targets_with_msg: list[tuple[str, str]] = []
    target_contact_map: dict[str, int] = {}
    for (req, msg), t in zip(msg_results, targets):
        if msg:
            targets_with_msg.append((t["username"], msg))
            target_contact_map[t["username"]] = t["contact"]["id"]
    jlog(f"Generati {len(targets_with_msg)} messaggi su {len(targets)} target")
    """

    # === STEP 5: engine.run_session ===
    """
    from .social.engine import OutreachEngine
    from .social.platform_base import HealthStatus

    engine = OutreachEngine(accounts, headed=task.get("headed", True), use_patchright=True)
    max_per_session = int(task.get("max_dms_per_session") or 5)

    all_results = []
    # Possiamo splittare in piu' sessioni (uno per account) per distribuire load
    while targets_with_msg:
        batch = targets_with_msg[:max_per_session]
        targets_with_msg = targets_with_msg[max_per_session:]
        results = await engine.run_session(
            platform_name=platform_name,
            targets=batch,
            warmup_min=5.0,
            max_dms_per_session=max_per_session,
        )
        all_results.extend(results)
        if not results:
            jlog("⚠️ Sessione vuota (account esauriti o off-hours). Stop.")
            break
    """

    # === STEP 6: log su DB ===
    """
    n_ok = sum(1 for r in all_results if r.ok)
    for r in all_results:
        contact_id = target_contact_map.get(r.target_username)
        db.insert_social_dm_log(
            account_id=...,  # dell'account che ha fatto il DM (engine deve esporlo)
            job_id=job_id,
            target_contact_id=contact_id,
            target_platform=platform_name,
            target_username=r.target_username,
            message=...,  # idem
            ok=r.ok,
            reason=r.reason,
            health_post=r.health.value,
        )
        if r.ok and contact_id:
            db.update_contact_status(contact_id, "contacted")

    jlog(f"✅ outreach_social completato: {n_ok}/{len(all_results)} DM inviati con successo")
    db.update_job(job_id, status="done", finished_at=db.now_iso())
    """


def _parse_message_template_variants(task: dict) -> list[str]:
    """Estrae la lista di esempi dal campo task `message_template_variants`.

    Formato accettato (textarea-friendly): righe separate da una riga `---`.
    Fallback: se variants vuoto, usa `message_template` come singolo esempio.
    """
    variants_raw = (task.get("message_template_variants") or "").strip()
    if variants_raw:
        # Split su righe `---` (con eventuali spazi)
        chunks = [c.strip() for c in variants_raw.split("\n---") if c.strip()]
        # First chunk può non avere il sep iniziale; clean trailing markers
        cleaned: list[str] = []
        for c in chunks:
            c = c.lstrip("-").strip()
            if c:
                cleaned.append(c)
        if cleaned:
            return cleaned[:10]  # cap a 10 esempi
    # Fallback al campo legacy
    single = (task.get("message_template") or "").strip()
    if single:
        return [single]
    return []


def _extract_username_from_url(url: str, platform: str) -> str | None:
    """Estrae l'username dalla URL social. None se non riconosciuto."""
    import re
    from urllib.parse import urlparse

    if not url:
        return None
    try:
        p = urlparse(url)
        path = (p.path or "").strip("/")
    except Exception:
        return None
    if not path:
        return None
    # Pattern semplici per Instagram/TikTok
    if platform == "instagram":
        # instagram.com/<username>(/post/...)?
        m = re.match(r"^([A-Za-z0-9._]+)(?:/.*)?$", path)
        if m:
            return m.group(1)
    elif platform == "tiktok":
        # tiktok.com/@<username>(/video/...)?
        m = re.match(r"^@?([A-Za-z0-9._-]+)(?:/.*)?$", path)
        if m:
            return m.group(1).lstrip("@")
    return None
