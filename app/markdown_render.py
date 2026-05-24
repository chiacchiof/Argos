"""Rendering markdown server-side per i file .md generati dai task.

Sanitization by construction: `html=False` su MarkdownIt blocca tutti i tag
HTML inline → script injection da report.md user-generated non eseguibile.
Niente bisogno di bleach.
"""
from __future__ import annotations

from markdown_it import MarkdownIt


MD = (
    MarkdownIt(
        "commonmark",
        {
            "linkify": True,
            "html": False,
            "breaks": False,
        },
    )
    .enable("table")
    .enable("strikethrough")
)


def render_markdown(src: str) -> str:
    """Renderizza una stringa markdown a HTML scoped per `.md-prose`."""
    if not src:
        return ""
    return MD.render(src)
