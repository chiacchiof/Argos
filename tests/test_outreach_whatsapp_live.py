"""Test B-001 (completamento outreach_whatsapp): chat live tra un DM e l'altro.

Esercita `_run_engine_b` in **dry-run** (nessun invio reale, nessuna API): al
checkpoint per-DM il runner consuma la coda chat → `/skip` salta il contatto e il
numero viene riletto dall'asset (così un `/set <id> whatsapp …` live ha effetto).
Più il gating del route (outreach_whatsapp ora mette in coda /skip).
"""
from __future__ import annotations

import asyncio

from app import db
from app.agent.runner_control import MODES_SUPPORTING_LIVE_CHAT, consume_live_chat


def _mk_job() -> int:
    tid = db.create_task({
        "name": "wa", "objective": "o", "agent_mode": "outreach_whatsapp", "model": "m",
    })
    jid = db.create_job(tid)
    db.update_job(jid, status="running", started_at=db.now_iso())
    return jid


def _mk_asset(whatsapp: str, name: str) -> dict:
    aid = db.upsert_asset({
        "asset_type": "contact_legacy", "title": name,
        "whatsapp": whatsapp, "raw_json": "{}",
    })
    return db.get_asset(aid, tenant_id=None)


def _dm_logs(job_id: int) -> list[dict]:
    with db.connect() as c:
        rows = c.execute(
            "SELECT target_username, reason, ok FROM social_dm_log "
            "WHERE job_id = %s ORDER BY id", (job_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def _run_engine_b(plan, job_id):
    from app.agent.runner_outreach_whatsapp import _run_engine_b as run_b
    return asyncio.run(run_b(
        plan=plan, api=None, api_config_id=None,
        job_id=job_id, dry_run=True, jlog=lambda _s: None,
    ))


def test_outreach_whatsapp_in_live_chat_modes():
    assert "outreach_whatsapp" in MODES_SUPPORTING_LIVE_CHAT


def test_consume_live_chat_skip_and_instructions():
    jid = _mk_job()
    db.insert_job_chat_message(jid, "user", "/skip", kind="command", applied=0)
    db.insert_job_chat_message(jid, "user", "rallenta", kind="suggestion", applied=0)
    lc = consume_live_chat(jid, lambda _s: None)
    assert lc is not None
    assert lc.skip is True
    assert "rallenta" in lc.instructions
    assert db.count_pending_chat(jid) == 0          # coda drenata
    assert consume_live_chat(jid, lambda _s: None) is None  # niente più


def test_engine_b_skip_skips_contact():
    jid = _mk_job()
    a1 = _mk_asset("+391111111111", "Uno")
    a2 = _mk_asset("+392222222222", "Due")
    plan = [
        {"asset": a1, "engine": "B", "phone": a1["whatsapp"], "message": "m1"},
        {"asset": a2, "engine": "B", "phone": a2["whatsapp"], "message": "m2"},
    ]
    # /skip in coda → consumato alla PRIMA iterazione → salta a1
    db.insert_job_chat_message(jid, "user", "/skip", kind="command", applied=0)

    _run_engine_b(plan, jid)
    logs = _dm_logs(jid)
    by_phone = {l["target_username"]: l for l in logs}
    assert by_phone["+391111111111"]["reason"] == "skipped_by_user"
    assert by_phone["+392222222222"]["reason"] == "dry_run"  # a2 inviato (dry)


def test_engine_b_reads_fresh_number_for_live_set():
    jid = _mk_job()
    a = _mk_asset("+390000000000", "Tizio")
    # plan con il numero VECCHIO (snapshot al plan-build)
    plan = [{"asset": a, "engine": "B", "phone": a["whatsapp"], "message": "m"}]
    # simulo un /set live: l'asset viene corretto DOPO la build del plan
    db.update_asset(a["id"], whatsapp="+393331234567")

    _run_engine_b(plan, jid)
    logs = _dm_logs(jid)
    assert len(logs) == 1
    # il runner ha riletto il numero aggiornato dall'asset, non lo snapshot vecchio
    assert logs[0]["target_username"] == "+393331234567"
    assert logs[0]["reason"] == "dry_run"
