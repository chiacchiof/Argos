"""Advisor deterministico per scegliere `agent_mode` in base a evidenze del sito.

Obiettivo: rimuovere il giudizio LLM dalla scelta di agent_mode. L'orchestrator
chiama `recommend_agent_mode(url)` PRIMA di create_task e ottiene una
raccomandazione basata su segnali concreti (paginazione statica, pattern URL,
markers infinite-scroll/SPA, anti-bot), non sul "secondo me" del modello.

Decision tree (in ordine di priorità):
  1. inspect_url verdict 'block' (404, anti-bot pesante con 4xx)        → skip
  2. Anti-bot pesante + 200 OK                                          → site_explorer (TLS impersonation Chrome120)
  3. SPA markers (React/Next/Angular root, body corto)                  → browser_use
  4. Infinite-scroll JS markers + no `?page=N`                          → site_explorer (target_cap_per_site=0)
  5. Paginazione classica + pattern URL dettaglio prevedibile (≥30%)   → bulk_extract + crawler
  6. Paginazione classica ma pattern URL ambiguo                        → site_explorer
  7. Fallback generico                                                  → site_explorer (low confidence)

Le motivazioni di ogni scelta sono restituite in `reasons[]` per essere
mostrate all'utente. I `suggested_params` sono pronti per essere passati a
create_task.
"""
from __future__ import annotations

import logging
import re
from dataclasses import asdict, dataclass, field
from typing import Any
from urllib.parse import urlparse

import httpx

from .runner_bulk_extract import (
    _extract_links,
    _group_urls_by_pattern,
    _pattern_to_regex,
    _registrable_domain,
)
from .url_inspector import inspect_url


log = logging.getLogger(__name__)


# Pattern di paginazione classica
_QUERY_PAGINATION_RE = re.compile(r"[?&](?:page|p|pg)=\d+", re.IGNORECASE)
_PATH_PAGINATION_RE = re.compile(r"/(?:page|p|pg)/\d+(?:/|$|\?)", re.IGNORECASE)
_REL_NEXT_RE = re.compile(r'rel\s*=\s*["\']next["\']', re.IGNORECASE)

# Markers di infinite-scroll lato JS
_INFINITE_SCROLL_MARKERS = (
    "infinite-scroll",
    "infinitescroll",
    "data-infinite",
    "intersection-observer",
    "loadmoreitems",
    "loadmore",
    "data-load-more",
)

# Markers di SPA (React/Next/Vue/Angular) — se presenti, il body iniziale è
# spesso un guscio vuoto e il contenuto reale viene renderizzato via JS post-load.
_SPA_MARKERS = (
    '<div id="root"></div>',
    '<div id="app"></div>',
    'window.__NEXT_DATA__',
    'data-reactroot',
    "ng-version=",
    'id="__next"',
    "window.__NUXT__",
)

# Body considerato "corto" (sospetto SPA / wall di login). Soglia in byte di body raw.
_BODY_SHORT_THRESHOLD = 5_000

# Quanti byte massimo scaricare per l'analisi. PG e simili directory hanno
# body ~200-400KB con il menu globale che occupa i primi 50-100KB; per
# vedere il blocco listing e i link `?p=N` di paginazione (spesso in fondo)
# serve un sample generoso. 256KB è un buon compromesso.
_FETCH_SAMPLE_BYTES = 256 * 1024

# Soglia: un pattern URL è "predicible" se copre almeno questa frazione del
# totale link interni della pagina. PG: ~29/110 ≈ 26% — mettiamo cap a 15% per
# essere generosi sui siti che hanno il menu globale che inquina.
_PREDICTABLE_PATTERN_MIN_RATIO = 0.15
# E almeno N link assoluti devono matchare (sotto i 5 è rumore statistico).
_PREDICTABLE_PATTERN_MIN_COUNT = 5
# Placeholder che indicano un pattern "stabile da dettaglio" (slug+id, slug.html, ecc.)
_DETAIL_PLACEHOLDERS = ("{slug}_{int}", "{slug}.html", "{int}.html", "{slug}", "{int}")


@dataclass
class AgentModeAdvice:
    """Risultato dell'advisor: agent_mode raccomandato + motivazioni + parametri suggeriti."""
    url: str
    agent_mode: str  # bulk_extract|site_explorer|browser_use|skip
    confidence: str  # high|medium|low
    reasons: list[str] = field(default_factory=list)
    suggested_params: dict[str, Any] = field(default_factory=dict)
    inspect_url_result: dict[str, Any] = field(default_factory=dict)
    signals: dict[str, Any] = field(default_factory=dict)  # per debug/audit
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# agent_mode equivalenti: se l'utente/orchestrator chiede X e l'advisor consiglia Y,
# alcune combinazioni sono "ragionevoli" (non blocchiamo, solo warning).
_AGENT_MODE_COMPATIBLE: dict[str, set[str]] = {
    # bulk_extract può sostituire site_explorer se l'utente passa crawler_url_pattern
    "bulk_extract": {"site_explorer"},
    "site_explorer": {"bulk_extract"},
    # auto_extract è un meta-dispatcher, sempre compatibile
    "auto_extract": {"bulk_extract", "site_explorer", "browser_use"},
}


def is_compatible_with_advice(requested_mode: str, advised_mode: str) -> bool:
    """True se l'agent_mode richiesto è considerato 'abbastanza vicino' al consigliato
    da non scatenare un reject hard. Match esatto sempre OK; combinazioni adiacenti
    in `_AGENT_MODE_COMPATIBLE` accettate. Altri casi = mismatch."""
    if not requested_mode or not advised_mode:
        return True  # mancanza di info → tolleranza
    if requested_mode == advised_mode:
        return True
    if requested_mode == "auto_extract":
        return True  # auto_extract può gestire qualunque cosa
    return advised_mode in _AGENT_MODE_COMPATIBLE.get(requested_mode, set())


async def _fetch_body_sample(url: str, *, timeout: float = 10.0) -> tuple[str, int]:
    """Scarica i primi `_FETCH_SAMPLE_BYTES` byte del body, decodifica come UTF-8.

    Preferisce curl_cffi (TLS impersonation Chrome120) per bypassare Cloudflare basic;
    fallback httpx. Ritorna (body_text, body_bytes_len). body_text può essere
    parzialmente troncato all'ultimo byte UTF-8 valido.
    """
    try:
        from curl_cffi import requests as cf_requests
        try:
            r = cf_requests.get(
                url, impersonate="chrome120",
                timeout=timeout, allow_redirects=True,
                stream=True,
            )
            buf = b""
            try:
                for chunk in r.iter_content(chunk_size=4096):
                    buf += chunk
                    if len(buf) >= _FETCH_SAMPLE_BYTES:
                        break
            except Exception:
                pass
            r.close()
            return buf.decode("utf-8", errors="replace"), len(buf)
        except Exception as e:
            log.debug("curl_cffi failed for %s: %s — falling back to httpx", url, e)
    except ImportError:
        pass

    async with httpx.AsyncClient(
        timeout=timeout, follow_redirects=True,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            ),
        },
    ) as client:
        r = await client.get(url)
        body = r.content[:_FETCH_SAMPLE_BYTES] if r.content else b""
        return body.decode("utf-8", errors="replace"), len(body)


def _analyze_pagination(body: str) -> dict[str, bool]:
    """Cerca segnali di paginazione classica vs infinite-scroll vs SPA."""
    if not body:
        return {
            "has_query_pagination": False,
            "has_path_pagination": False,
            "has_rel_next": False,
            "infinite_scroll": False,
            "spa_markers": False,
        }
    body_lc = body.lower()
    return {
        "has_query_pagination": bool(_QUERY_PAGINATION_RE.search(body)),
        "has_path_pagination": bool(_PATH_PAGINATION_RE.search(body)),
        "has_rel_next": bool(_REL_NEXT_RE.search(body)),
        "infinite_scroll": any(m in body_lc for m in _INFINITE_SCROLL_MARKERS),
        "spa_markers": any(m.lower() in body_lc for m in _SPA_MARKERS),
    }


def _analyze_url_patterns(body: str, base_url: str) -> dict[str, Any]:
    """Estrae link interni dal body e raggruppa per pattern URL.

    Filtra:
    - link cross-registrable-domain (riusa `_extract_links` same_origin_only)
    - pattern che sono il seed stesso o sub-path del seed (es. paginazione)
    - pattern troppo generici (es. solo "host" senza path)

    Ritorna {
        "total_internal_links": int,
        "top_pattern": str|None,             # pattern stringa con placeholder
        "top_pattern_count": int,
        "top_pattern_ratio": float,
        "top_pattern_regex": str|None,       # regex pronta per crawler_url_pattern
        "is_detail_pattern": bool,           # True se il pattern ha placeholder dettaglio (slug_int, slug.html)
        "predictable": bool,                 # True se sopra le soglie
        "all_patterns": list[(pattern, count)],
    }
    """
    links = _extract_links(body, base_url, same_origin_only=True)
    total = len(links)
    if total == 0:
        return {
            "total_internal_links": 0, "top_pattern": None,
            "top_pattern_count": 0, "top_pattern_ratio": 0.0,
            "top_pattern_regex": None, "is_detail_pattern": False,
            "predictable": False, "all_patterns": [],
        }

    # Escludi link che coincidono col seed o sono solo il dominio root
    base_path = urlparse(base_url).path.rstrip("/")
    seed_host = (urlparse(base_url).hostname or "").lower()
    filtered = []
    for u in links:
        p = urlparse(u)
        path = p.path.rstrip("/")
        # Escludi root del dominio e seed esatto
        if not path or path == base_path:
            continue
        filtered.append(u)

    groups = _group_urls_by_pattern(filtered)
    if not groups:
        return {
            "total_internal_links": total, "top_pattern": None,
            "top_pattern_count": 0, "top_pattern_ratio": 0.0,
            "top_pattern_regex": None, "is_detail_pattern": False,
            "predictable": False, "all_patterns": [],
        }

    # Itera in ordine di count e prendi il primo pattern che (a) ha placeholder
    # di tipo "dettaglio" e (b) ha path non vuoto. Salta pattern "{slug}" puro
    # senza ulteriori segmenti (sono pagine-categoria, non dettaglio).
    sorted_groups = sorted(groups.items(), key=lambda kv: -len(kv[1]))
    top_pattern, top_urls = sorted_groups[0]
    top_count = len(top_urls)

    # Un pattern di "dettaglio" è quello con almeno 2 segmenti di path E almeno
    # un placeholder distintivo ({slug}_{int}, {slug}.html, ecc.).
    is_detail = False
    for ph in _DETAIL_PLACEHOLDERS:
        if ph in top_pattern:
            # Aggiungi check: deve avere almeno 1 "/" nel path-portion del pattern
            # (cioè non solo "host/{slug}", che potrebbe essere una sezione).
            path_part = top_pattern.split("/", 1)[1] if "/" in top_pattern else ""
            slashes_in_path = path_part.count("/")
            # PG: host=paginegialle.it path=catania-ct/abbigliamento/calzedonia_T...
            # → slashes_in_path = 2, OK detail
            # Sito semplice: host=example.com path={slug}
            # → slashes_in_path = 0, NO detail
            if slashes_in_path >= 1:
                is_detail = True
                break
            # Caso "{slug}_{int}" con 1 solo segmento è comunque dettaglio
            # (pattern tipo /<id>_<slug>): accetta anche zero slash
            if ph == "{slug}_{int}":
                is_detail = True
                break

    ratio = top_count / max(total, 1)
    predictable = (
        is_detail
        and top_count >= _PREDICTABLE_PATTERN_MIN_COUNT
        and ratio >= _PREDICTABLE_PATTERN_MIN_RATIO
    )

    return {
        "total_internal_links": total,
        "top_pattern": top_pattern,
        "top_pattern_count": top_count,
        "top_pattern_ratio": round(ratio, 3),
        "top_pattern_regex": _pattern_to_regex(top_pattern) if predictable else None,
        "is_detail_pattern": is_detail,
        "predictable": predictable,
        "all_patterns": [(p, len(u)) for p, u in sorted_groups[:5]],
    }


async def recommend_agent_mode(url: str, *, timeout: float = 10.0) -> AgentModeAdvice:
    """Decide deterministicamente quale agent_mode usare per una URL data.

    Pipeline:
      1. inspect_url() → HTTP status, protezioni anti-bot.
      2. Se severity='block' → return skip.
      3. Se anti-bot pesante + 200 → return site_explorer (TLS impersonation).
      4. Fetch body sample (64KB) per pattern analysis.
      5. Se SPA markers → browser_use.
      6. Se infinite-scroll markers ma no `?page=N` → site_explorer cap=0.
      7. Se paginazione classica + pattern URL dettaglio predictible → bulk_extract.
      8. Se paginazione classica + pattern ambiguo → site_explorer.
      9. Fallback → site_explorer low confidence.
    """
    advice = AgentModeAdvice(url=url, agent_mode="", confidence="")

    parsed = urlparse(url)
    if not parsed.scheme or not parsed.netloc:
        advice.agent_mode = "skip"
        advice.confidence = "high"
        advice.error = "invalid_url"
        advice.reasons = [f"URL malformato (manca scheme o host): {url!r}"]
        return advice

    # 1. inspect_url
    try:
        insp = await inspect_url(url, timeout=timeout)
    except Exception as e:
        advice.agent_mode = "skip"
        advice.confidence = "high"
        advice.error = f"inspect_url_failed: {type(e).__name__}: {e}"
        advice.reasons = [f"Impossibile ispezionare l'URL: {e}"]
        return advice
    advice.inspect_url_result = insp

    severity = insp.get("severity")
    protection = insp.get("protection")
    status_code = insp.get("status_code") or 0

    # 2. Block grave
    if severity == "block":
        advice.agent_mode = "skip"
        advice.confidence = "high"
        advice.reasons = [
            f"inspect_url verdict 'block': {insp.get('reason')}",
            f"HTTP {status_code}, protection={protection}",
        ]
        return advice

    # 3. Anti-bot pesante + 200 → site_explorer (ha TLS impersonation)
    HEAVY_PROTECTIONS = {"cloudflare", "datadome", "akamai", "perimeterx", "imperva_incapsula"}
    if protection in HEAVY_PROTECTIONS:
        advice.agent_mode = "site_explorer"
        advice.confidence = "medium"
        advice.reasons = [
            f"Anti-bot pesante rilevato ({protection})",
            "site_explorer ha TLS impersonation Chrome120 via HTTP fetcher → bypassa CF basic",
            "Fallback su browser_use se site_explorer fallisce",
        ]
        advice.suggested_params = {"target_cap_per_site": 30}
        advice.signals = {"protection": protection, "status_code": status_code}
        return advice

    # 4. Fetch body sample per pattern analysis
    try:
        body, body_bytes = await _fetch_body_sample(url, timeout=timeout)
    except Exception as e:
        log.debug("body fetch failed for %s: %s — proceedo con fallback", url, e)
        body, body_bytes = "", 0

    pagination = _analyze_pagination(body)
    patterns = _analyze_url_patterns(body, url) if body else {
        "total_internal_links": 0, "predictable": False,
        "top_pattern": None, "top_pattern_count": 0,
    }
    advice.signals = {
        "body_bytes": body_bytes,
        "pagination": pagination,
        "patterns": {k: v for k, v in patterns.items() if k != "all_patterns"},
        "all_patterns_top5": patterns.get("all_patterns") or [],
    }

    # 5. SPA markers + body corto → browser_use
    if pagination["spa_markers"] and body_bytes < _BODY_SHORT_THRESHOLD:
        advice.agent_mode = "browser_use"
        advice.confidence = "high"
        advice.reasons = [
            "Markers SPA rilevati (React/Next/Vue/Angular)",
            f"Body iniziale corto ({body_bytes} byte): contenuto reso via JS post-load",
            "browser_use con Playwright vede il DOM dopo render",
        ]
        advice.suggested_params = {"target_cap_per_site": 30}
        return advice

    # 6. Infinite-scroll JS + no paginazione classica → site_explorer cap=0
    if pagination["infinite_scroll"] and not (
        pagination["has_query_pagination"] or pagination["has_path_pagination"]
    ):
        advice.agent_mode = "site_explorer"
        advice.confidence = "high"
        advice.reasons = [
            "Markers infinite-scroll JS rilevati",
            "Nessuna paginazione classica (?page=N o /page/N)",
            "site_explorer con target_cap_per_site=0 → attiva auto-discovery via browser headless",
        ]
        advice.suggested_params = {"target_cap_per_site": 0}
        return advice

    # 7. Paginazione classica + pattern URL dettaglio prevedibile → bulk_extract
    has_pagination = (
        pagination["has_query_pagination"]
        or pagination["has_path_pagination"]
        or pagination["has_rel_next"]
    )
    if has_pagination and patterns.get("predictable"):
        advice.agent_mode = "bulk_extract"
        advice.confidence = "high"
        pag_type = (
            "?page=N" if pagination["has_query_pagination"]
            else "/page/N" if pagination["has_path_pagination"]
            else "rel=next"
        )
        advice.reasons = [
            f"Paginazione classica rilevata ({pag_type})",
            (
                f"Pattern URL dettaglio prevedibile: {patterns['top_pattern']} "
                f"({patterns['top_pattern_count']} link su "
                f"{patterns['total_internal_links']}, "
                f"{int(patterns['top_pattern_ratio'] * 100)}%)"
            ),
            "bulk_extract + crawler con pattern URL chiuso è più affidabile di site_explorer",
        ]
        advice.suggested_params = {
            "crawler_enabled": True,
            "crawler_url_pattern": patterns["top_pattern_regex"],
            "crawler_max_depth": 5,
            "bulk_extraction_method": "llm_per_page",
        }
        return advice

    # 8. Pattern URL detail predictible SENZA paginazione visibile → bulk_extract medium.
    # Logica: 5+ URL stesso pattern detail nella stessa pagina = è un listing,
    # anche se la paginazione `?page=N` non è nel sample (spesso è in fondo
    # o caricata via JS dopo il render iniziale).
    if patterns.get("predictable"):
        advice.agent_mode = "bulk_extract"
        advice.confidence = "medium"
        advice.reasons = [
            (
                f"Pattern URL dettaglio prevedibile: {patterns['top_pattern']} "
                f"({patterns['top_pattern_count']} link su "
                f"{patterns['total_internal_links']}, "
                f"{int(patterns['top_pattern_ratio'] * 100)}%)"
            ),
            "Paginazione esplicita non rilevata nel sample, ma il pattern detail è chiaro",
            "bulk_extract + crawler con pattern URL chiuso è più affidabile di site_explorer",
        ]
        advice.suggested_params = {
            "crawler_enabled": True,
            "crawler_url_pattern": patterns["top_pattern_regex"],
            "crawler_max_depth": 5,
            "bulk_extraction_method": "llm_per_page",
        }
        return advice

    # 9. Paginazione classica ma pattern ambiguo → site_explorer
    if has_pagination:
        advice.agent_mode = "site_explorer"
        advice.confidence = "medium"
        advice.reasons = [
            "Paginazione classica rilevata ma pattern URL dettaglio non chiaro",
            "site_explorer farà mapping LLM delle listing",
        ]
        return advice

    # 10. Fallback: nessun segnale chiaro → site_explorer low confidence
    advice.agent_mode = "site_explorer"
    advice.confidence = "low"
    advice.reasons = [
        "Nessun pattern paginazione o markers SPA/infinite-scroll chiari",
        "site_explorer come fallback prudente (mapping LLM determinerà la struttura)",
    ]
    return advice
