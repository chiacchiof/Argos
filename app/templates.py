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


def _render_md(value: Any) -> str:
    """Filter Jinja: renderizza una stringa markdown a HTML (CommonMark + tables +
    strikethrough). `html=False` in MarkdownIt blocca tag inline → sanitization
    by construction. L'output va wrappato in `<div class="md-prose">…</div>` per
    lo stile, e usato con `| safe` perche' e' HTML.
    """
    if value is None or value == "":
        return ""
    try:
        from .markdown_render import render_markdown
        return render_markdown(str(value))
    except Exception:
        return ""


templates.env.filters["render_md"] = _render_md


def _human_bytes(value: Any) -> str:
    """Filter Jinja: byte -> stringa leggibile (B/KB/MB/GB/TB). Usato dalla
    pagina Spazio occupato. Robusto a None/non-numerico."""
    try:
        n = float(value or 0)
    except (TypeError, ValueError):
        return "0 B"
    if n < 0:
        n = 0
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{int(n)} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


templates.env.filters["human_bytes"] = _human_bytes


def _static_css_mtime() -> int:
    """mtime di static/style.css per cache-busting. Cambia ad ogni modifica del
    file; serializzato come int (secondi). Usato in base.html:
    `<link href="/static/style.css?v={{ static_css_mtime }}">`."""
    try:
        css = Path(__file__).resolve().parent.parent / "static" / "style.css"
        return int(css.stat().st_mtime)
    except Exception:
        return 0


# Esposto come callable (non valore congelato): cosi' viene ricalcolato a ogni
# render del template — utile in dev con uvicorn --reload che NON ricarica
# moduli Python quando solo la CSS cambia. Overhead minimo (1 stat() per page).
templates.env.globals["static_css_mtime"] = _static_css_mtime


def _static_asset_mtime(path: str) -> int:
    """mtime di un asset dentro static/ per cache-busting mirato."""
    try:
        root = Path(__file__).resolve().parent.parent / "static"
        asset = (root / path.lstrip("/")).resolve()
        if root.resolve() not in asset.parents and asset != root.resolve():
            return 0
        return int(asset.stat().st_mtime)
    except Exception:
        return 0


templates.env.globals["static_asset_mtime"] = _static_asset_mtime
