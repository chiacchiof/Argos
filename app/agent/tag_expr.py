"""Parser/validatore per espressioni boolean sui filtri tag in /qualified.

Esempi di expression accettate:
    F1
    F1 AND F2
    F1 OR F2
    (F1 AND F2) OR F3
    ((F1 OR F2) AND F3) OR (F4 AND F5)

Token validi:
    F<n>   — riferimento al filtro #n (1-based, n in [1, n_filters])
    AND    — congiunzione (case insensitive)
    OR     — disgiunzione (case insensitive)
    ( )    — parentesi

L'output di `parse_tag_expr(expr, n_filters)` è la lista di token normalizzata
(uppercase per AND/OR e Fn) — usata poi dal SQL builder per generare il WHERE
clause con EXISTS().

Sicurezza: il tokenizer accetta SOLO la grammatica sopra; qualsiasi altro
carattere/identificatore solleva ValueError. Niente input utente arriva mai
direttamente in SQL: gli EXISTS sono parametrizzati su (tag_key, tag_value).
"""
from __future__ import annotations

import re
from typing import Iterable


_TOKEN_RE = re.compile(r"\(|\)|F\d+|AND|OR", re.IGNORECASE)
_ALLOWED_CHARS_RE = re.compile(r"^[a-zA-Z0-9_() ]*$")


def tokenize(expr: str, n_filters: int) -> list[str]:
    """Splitta `expr` in token validati. Solleva ValueError su token sconosciuti
    o Fn fuori range."""
    if not expr or not expr.strip():
        raise ValueError("Espressione vuota")
    raw = re.sub(r"\s+", " ", expr.strip())
    if not _ALLOWED_CHARS_RE.match(raw):
        raise ValueError(
            "Caratteri non ammessi nell'espressione. Usa solo F1..Fn, AND, OR, parentesi."
        )

    tokens: list[str] = []
    pos = 0
    while pos < len(raw):
        if raw[pos] == " ":
            pos += 1
            continue
        m = _TOKEN_RE.match(raw, pos)
        if not m:
            snippet = raw[pos:pos + 10]
            raise ValueError(f"Token non riconosciuto alla posizione {pos}: '{snippet}…'")
        tok = m.group(0).upper()
        if tok.startswith("F"):
            try:
                idx = int(tok[1:])
            except ValueError:
                raise ValueError(f"Filtro malformato: '{tok}'")
            if idx < 1 or idx > n_filters:
                raise ValueError(
                    f"Filtro {tok} fuori range: hai solo {n_filters} filtro/i (F1..F{n_filters})"
                )
        tokens.append(tok)
        pos = m.end()
    return tokens


def validate(tokens: Iterable[str]) -> None:
    """Verifica la grammatica:
      - parentesi bilanciate
      - operatori non consecutivi
      - filtro/( seguito da AND/OR/) coerente
      - non puo' iniziare con AND/OR/) ne' finire con AND/OR/(
    """
    tokens = list(tokens)
    if not tokens:
        raise ValueError("Espressione vuota")

    # Bilanciamento parentesi
    depth = 0
    for t in tokens:
        if t == "(":
            depth += 1
        elif t == ")":
            depth -= 1
            if depth < 0:
                raise ValueError("Parentesi sbilanciate: ')' senza '(' corrispondente")
    if depth != 0:
        raise ValueError("Parentesi sbilanciate: manca chiusura ')'")

    # Grammatica via state machine
    prev: str | None = None
    for t in tokens:
        is_op = t in ("AND", "OR")
        is_filter = t.startswith("F") and t[1:].isdigit()
        is_open = t == "("
        is_close = t == ")"
        if prev is None:
            if is_op or is_close:
                raise ValueError(f"Espressione non puo' iniziare con '{t}'")
        else:
            p_op = prev in ("AND", "OR")
            p_filter = prev.startswith("F") and prev[1:].isdigit()
            p_open = prev == "("
            p_close = prev == ")"
            if p_op and (is_op or is_close):
                raise ValueError(f"Operatore '{prev}' non puo' essere seguito da '{t}'")
            if p_filter and (is_filter or is_open):
                raise ValueError(f"Manca un operatore fra '{prev}' e '{t}'")
            if p_open and (is_op or is_close):
                raise ValueError(f"'(' non puo' essere seguita da '{t}'")
            if p_close and (is_filter or is_open):
                raise ValueError(f"Manca un operatore fra ')' e '{t}'")
        prev = t
    if prev in ("AND", "OR", "("):
        raise ValueError(f"Espressione non puo' terminare con '{prev}'")


def parse_tag_expr(expr: str, n_filters: int) -> list[str]:
    """Funzione di alto livello: tokenize + validate. Ritorna i token normalizzati
    pronti per il SQL builder."""
    tokens = tokenize(expr, n_filters)
    validate(tokens)
    return tokens


def build_where_clause(
    filters: list[tuple[str, str]],
    mode: str,
    expr: str | None = None,
) -> tuple[str, list]:
    """Costruisce un WHERE-fragment SQL parametrizzato per filtri tag, dato il
    `mode` (and|or|custom) e l'eventuale `expr` (solo se mode=custom).

    Assume che la tabella principale sia aliasata `a` (assets). Ogni filtro
    genera un EXISTS sull'asset_tags.

    Ritorna ("(...)", [params]) o ("", []) se nessun filtro.
    """
    if not filters:
        return "", []

    mode = (mode or "and").strip().lower()
    if mode not in ("and", "or", "custom"):
        raise ValueError(f"mode invalido: {mode!r} (usa and|or|custom)")

    exists_template = (
        "EXISTS (SELECT 1 FROM asset_tags _tf "
        "WHERE _tf.asset_id = a.id AND _tf.tag_key = %s AND _tf.tag_value = %s)"
    )

    if mode in ("and", "or"):
        glue = " AND " if mode == "and" else " OR "
        parts = [exists_template for _ in filters]
        params: list = []
        for k, v in filters:
            params.extend([k, v])
        return "(" + glue.join(parts) + ")", params

    # mode == 'custom'
    if not expr:
        raise ValueError("mode=custom richiede `expr` non vuoto")
    tokens = parse_tag_expr(expr, len(filters))
    sql_parts: list[str] = []
    params = []
    for t in tokens:
        if t.startswith("F") and t[1:].isdigit():
            idx = int(t[1:]) - 1
            k, v = filters[idx]
            sql_parts.append(exists_template)
            params.extend([k, v])
        else:
            # AND, OR, (, )  — uppercase, senza spazi extra
            sql_parts.append(t)
    return "(" + " ".join(sql_parts) + ")", params
