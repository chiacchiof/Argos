"""Rendering markdown server-side per i file .md generati dai task.

Sanitization by construction: `html=False` su MarkdownIt blocca tutti i tag
HTML inline → script injection da report.md user-generated non eseguibile.
Niente bisogno di bleach.
"""
from __future__ import annotations

import re
import unicodedata
from html import unescape

from markdown_it import MarkdownIt
from mdit_py_plugins.anchors import anchors_plugin


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


def _slugify_heading(text: str) -> str:
    """Slug stabile per heading delle guide, compatibile con i link del TOC."""
    normalized = unicodedata.normalize("NFKD", unescape(text or ""))
    ascii_text = "".join(ch for ch in normalized if not unicodedata.combining(ch))
    ascii_text = ascii_text.lower().replace("'", "")
    ascii_text = re.sub(r"[^a-z0-9]+", "-", ascii_text)
    return ascii_text.strip("-") or "section"


MD_DOCS = (
    MarkdownIt(
        "commonmark",
        {
            "linkify": True,
            "html": True,
            "breaks": False,
        },
    )
    .enable("table")
    .enable("strikethrough")
    .use(
        anchors_plugin,
        min_level=1,
        max_level=6,
        slug_func=_slugify_heading,
        permalink=False,
    )
)

_GUIDE_LINK_RE = re.compile(r'href="(?:\./)?(USER_GUIDE|ADMIN_GUIDE)\.md(#[^"]*)?"')
_HEADING_RE = re.compile(r"<h([1-6]) id=\"([^\"]+)\">(.*?)</h\1>", re.S)
_CODE_RE = re.compile(r"<code>.*?</code>", re.S)
_TAG_RE = re.compile(r"<[^>]+>")
_ID_RE = re.compile(r'\bid="([^"]+)"')
_SHORT_ALIAS_SUFFIXES = {"operativa", "operative", "operativi", "operativo"}


def _rewrite_docs_links(html: str) -> str:
    def repl(match: re.Match[str]) -> str:
        guide = match.group(1)
        anchor = match.group(2) or ""
        return f'href="/docs/{guide}{anchor}"'

    return _GUIDE_LINK_RE.sub(repl, html)


def _add_heading_aliases(html: str) -> str:
    """Aggiunge alias corti agli heading usati dai TOC storici."""
    seen_ids = set(_ID_RE.findall(html))

    def repl(match: re.Match[str]) -> str:
        level, heading_id, inner = match.groups()
        without_code = _CODE_RE.sub("", inner)
        alias_text = _TAG_RE.sub("", without_code).strip()
        alias_texts = [alias_text]
        words = alias_text.split()
        if len(words) > 2 and _slugify_heading(words[-1]) in _SHORT_ALIAS_SUFFIXES:
            alias_texts.append(" ".join(words[:-1]))

        prefixes = []
        for alias in dict.fromkeys(_slugify_heading(text) for text in alias_texts):
            if alias and alias != heading_id and alias not in seen_ids:
                seen_ids.add(alias)
                prefixes.append(f'<span id="{alias}" class="docs-anchor-alias"></span>')
        prefix = "".join(prefixes)
        return f'{prefix}<h{level} id="{heading_id}">{inner}</h{level}>'

    return _HEADING_RE.sub(repl, html)


def render_docs_markdown(src: str) -> str:
    """Renderizza markdown trusted delle guide in-app con anchor navigabili."""
    if not src:
        return ""
    html = MD_DOCS.render(src)
    html = _rewrite_docs_links(html)
    html = _add_heading_aliases(html)
    return html
