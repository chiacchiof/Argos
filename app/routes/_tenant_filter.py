"""Helper "View as tenant" per le list-page dei super-admin.

Espone `?as_tenant_id=N` sulle list-page: quando un super-admin lo passa, le
query `db.list_*` filtrano per quel tenant invece di vedere tutto. I tenant
normali ignorano il parametro (il loro tenant_id viene gia' dal middleware).

Tre helper:
- `parse_as_tenant_id(request)`: estrae il parametro (validato).
- `tenant_query_arg(request)`: cosa passare alle `db.list_*(tenant_id=...)`.
- `picker_context(request)`: contesto template per il dropdown <select>.

Il template `partials/tenant_picker.html` consuma `picker_context`.
"""
from __future__ import annotations

from typing import Any

from fastapi import Request

from .. import db, db_cloud


def parse_as_tenant_id(request: Request) -> int | None:
    """Estrae `?as_tenant_id=N` dalla querystring se l'utente e' super_admin
    e il valore e' un int valido. Restituisce None altrimenti (o se manca,
    o se vale 'all')."""
    user = getattr(request.state, "current_user", None)
    if not (user and getattr(user, "is_super_admin", False)):
        return None
    raw = (request.query_params.get("as_tenant_id") or "").strip()
    if not raw or raw.lower() == "all":
        return None
    try:
        return int(raw)
    except (ValueError, TypeError):
        return None


def tenant_query_arg(request: Request) -> Any:
    """Cosa passare alle query `db.list_*(tenant_id=...)`:
    - int -> filtra a quel tenant specifico (override del context).
    - `db._UNSET` -> la query usa il default = ContextVar (super_admin
      senza override => None => no filter; tenant_user => il suo).
    """
    val = parse_as_tenant_id(request)
    return val if val is not None else db._UNSET


def picker_context(request: Request) -> dict[str, Any]:
    """Variabili per `partials/tenant_picker.html`. Restituisce sempre il
    flag `tenant_picker_visible` (False per non-super-admin), in modo che il
    template possa fare un include incondizionato senza errori."""
    user = getattr(request.state, "current_user", None)
    if not (user and getattr(user, "is_super_admin", False)):
        return {"tenant_picker_visible": False}
    try:
        tenants = db_cloud.list_tenants()
    except Exception:
        tenants = []
    return {
        "tenant_picker_visible": True,
        "tenant_picker_tenants": tenants,
        "tenant_picker_current": parse_as_tenant_id(request),
    }
