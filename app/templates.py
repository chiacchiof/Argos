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
