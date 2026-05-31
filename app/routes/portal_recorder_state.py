"""Stato del recorder esposto ai template (thin wrapper su agent.portal.recorder).

Tenuto separato dalle route per evitare import pesanti (playwright) al load del
router: `recorder` importa form_fill che importa humanize/playwright lazy, ma qui
restiamo su una funzione pura che legge solo il dict in-memory delle sessioni.
"""
from __future__ import annotations


def recorder_status_one(macro_id: int) -> dict:
    """{'active': bool, 'mode': 'login'|'record'|None} per una macro."""
    try:
        from ..agent.portal import recorder
        return {"active": recorder.is_active(macro_id), "mode": recorder.session_mode(macro_id)}
    except Exception:
        return {"active": False, "mode": None}


def recorder_status_map(macro_ids: list[int]) -> dict[int, dict]:
    return {mid: recorder_status_one(mid) for mid in macro_ids}
