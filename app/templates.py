"""Jinja2 templates singleton."""
import json
from pathlib import Path
from typing import Any

from fastapi.templating import Jinja2Templates


TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


def _safe_loads(value: Any) -> Any:
    """Filter Jinja: parse JSON string in modo sicuro. Su input invalido o non-str
    ritorna None senza esplodere. Usato per colonne JSON come `contacts.social_json`.
    """
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except (json.JSONDecodeError, ValueError):
        return None


templates.env.filters["safe_loads"] = _safe_loads


def _owner_display(row: Any) -> str:
    """Filter Jinja: dato un row (dict) con campi owner_first_name/owner_last_name/
    owner_email (eventualmente NULL), ritorna 'Nome Cognome' se popolati,
    altrimenti email, altrimenti '—'. Usato per la colonna 'Tenant Owner'."""
    if not row:
        return "—"
    fn = (row.get("owner_first_name") or "").strip() if hasattr(row, "get") else ""
    ln = (row.get("owner_last_name") or "").strip() if hasattr(row, "get") else ""
    if fn or ln:
        return f"{fn} {ln}".strip()
    return (row.get("owner_email") or "—") if hasattr(row, "get") else "—"


templates.env.filters["owner_display"] = _owner_display
