"""Contesto per la modale di condivisione, condiviso tra Fogli e Fascicoli.

Produce un dict `share` uniforme per `templates/share_modal.html`:
  - title, base (URL base: /sheets/{id} o /fascicoli/{id}), kind, visibility
  - rows: persone CON accesso (owner/architetto = fissi; membri = ruolo editabile)
  - addable: utenti del tenant aggiungibili (ricercabili nel pannello "+")

Ruoli: 'viewer' = Sola lettura, 'editor' = Modifica. Per i Fascicoli, 'editor'
abilita anche upload file e creazione fogli nel progetto.
"""
from __future__ import annotations

from typing import Any

_ARCH_ROLES = ("tenant_architect", "super_admin")


def build_share_context(
    *,
    kind: str,
    title: str,
    base: str,
    visibility: str,
    owner_user_id: int | None,
    member_role: dict[int, str],
    tenant_users: list[dict[str, Any]],
) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    addable: list[dict[str, Any]] = []
    for u in tenant_users:
        if not u.get("is_active"):
            continue
        uid = u["id"]
        email = u.get("email") or "—"
        if uid == owner_user_id:
            rows.append({"user_id": uid, "email": email, "tag": "Proprietario", "fixed": "Gestione", "_o": 0})
        elif u.get("role") in _ARCH_ROLES:
            rows.append({"user_id": uid, "email": email, "tag": "Architetto · accesso completo",
                         "fixed": "Gestione", "_o": 1})
        elif uid in member_role:
            rows.append({"user_id": uid, "email": email, "tag": None, "fixed": None,
                         "role": member_role[uid], "_o": 2})
        else:
            addable.append({"user_id": uid, "email": email})
    rows.sort(key=lambda r: (r["_o"], (r["email"] or "").lower()))
    addable.sort(key=lambda r: (r["email"] or "").lower())
    return {
        "kind": kind, "title": title, "base": base, "visibility": visibility,
        "rows": rows, "addable": addable,
    }
