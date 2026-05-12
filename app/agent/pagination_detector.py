"""Pagination detector — estrae info di paginazione da HTML di directory page.

Due segnali ortogonali:

1. **Testo descrittivo** ("Listing 32690 profiles, page 1 of 1363", "Page 1/50",
   "1-25 di 1234 risultati", ecc.). Regex multi-lingua. Restituisce
   `total_items` e `total_pages` quando trovati.

2. **Link di paginazione** (`?page=N`, `/page/N`, `?p=N`, `?offset=N`).
   Restituisce il `max_page_number` visto nelle URL.

Il chiamante puo' poi usare `generate_paginated_urls(base_url, max_page, param='page')`
per produrre la lista completa di URL da estrarre, saltando ogni discovery LLM.

Tutto deterministico, zero chiamate esterne, ~50 righe attive.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import urlparse, urlencode, parse_qsl, urlunparse


# Regex per pattern testuali di paginazione.
# Ordine: dal piu' specifico al piu' generico. Tutti hanno almeno 2 numeri:
# o (items, pages) o (current_page, total_pages).
_TEXT_PATTERNS: tuple[tuple[str, str], ...] = (
    # "Listing 32690 profiles, page 1 of 1363"
    (r"listing\s+(\d{1,7})\s+(?:profile|profili|babe|prodott|annunc|model|item|result)s?,?\s+page\s+\d+\s+of\s+(\d{1,5})", "items_pages"),
    # "32690 profili in 1363 pagine"
    (r"(\d{1,7})\s+(?:profile|profili|babe|prodott|annunc|model|item|result)s?\s+in\s+(\d{1,5})\s+pagin", "items_pages"),
    # "Page 1 of 1363" / "Pagina 1 di 1363"
    (r"page\s+\d+\s+of\s+(\d{1,5})", "pages_only"),
    (r"pagin[ae]\s+\d+\s+di\s+(\d{1,5})", "pages_only"),
    # "1-25 of 1234 results" / "1-25 di 1234 risultati"
    (r"\d+\s*[-–]\s*\d+\s+(?:of|di)\s+(\d{1,7})\s+(?:result|risultat|profile|item)", "items_only"),
    # "1234 results" generico
    (r"(\d{2,7})\s+(?:profile|profili|babe|prodott|annunc|model|risultat|result)s?\b", "items_only_loose"),
    # "Page 1/50"
    (r"page\s+\d+\s*/\s*(\d{1,5})", "pages_only"),
)

# Regex per anchor di paginazione: catturiamo il numero di pagina nell'URL.
_URL_PAGE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"[?&](?:page|p|pagina)=(\d{1,5})\b", re.IGNORECASE),
    re.compile(r"/page/(\d{1,5})\b", re.IGNORECASE),
    re.compile(r"/p/(\d{1,5})\b", re.IGNORECASE),
)


@dataclass
class PaginationInfo:
    total_items: int | None = None
    total_pages: int | None = None
    max_page_in_urls: int | None = None
    detected_param: str | None = None  # nome del parametro: "page", "p", "pagina"
    detected_style: str | None = None  # "query" (?page=N) o "path" (/page/N)
    evidence: list[str] = field(default_factory=list)

    @property
    def has_pagination(self) -> bool:
        return (
            (self.total_pages is not None and self.total_pages > 1)
            or (self.max_page_in_urls is not None and self.max_page_in_urls > 1)
            or (self.total_items is not None and self.total_items > 50)
        )

    @property
    def estimated_pages(self) -> int:
        """Stima totale pagine. Usa total_pages > max_in_urls > total_items/25."""
        if self.total_pages:
            return self.total_pages
        if self.max_page_in_urls:
            return self.max_page_in_urls
        if self.total_items:
            # assume ~25 item per pagina (varianti tipiche)
            return max(1, (self.total_items + 24) // 25)
        return 1


def _strip_tags_for_match(html: str) -> str:
    """Rimuove tag HTML mantenendo il testo, in modo che le regex di paginazione
    possano matchare frasi come 'listing <strong>32711</strong> profiles' come
    'listing 32711 profiles'."""
    # Rimuovi <script>/<style> con contenuto
    html = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html, flags=re.DOTALL | re.IGNORECASE)
    # Rimuovi tutti i tag rimanenti
    html = re.sub(r"<[^>]+>", " ", html)
    # Decode minimo HTML entities (i piu' comuni)
    html = html.replace("&nbsp;", " ").replace("&amp;", "&").replace("&#39;", "'")
    # Collassa whitespace multiplo
    html = re.sub(r"\s+", " ", html)
    return html


def detect_pagination(html: str, base_url: str = "") -> PaginationInfo:
    """Analizza HTML per signali di paginazione (testo + URL).

    Conservativo: ritorna PaginationInfo vuoto se nessun segnale chiaro.
    """
    info = PaginationInfo()
    if not html:
        return info

    # 1. Pattern testuali su HTML strippato (tag tolti) per matchare frasi
    #    spezzate da <strong>, <span>, ecc.
    text_lower = _strip_tags_for_match(html).lower()
    for pattern, kind in _TEXT_PATTERNS:
        m = re.search(pattern, text_lower)
        if not m:
            continue
        groups = m.groups()
        if kind == "items_pages":
            info.total_items = int(groups[0])
            info.total_pages = int(groups[1])
            info.evidence.append(f"text '{m.group(0)[:80]}' → items={info.total_items}, pages={info.total_pages}")
            break
        elif kind == "pages_only":
            info.total_pages = int(groups[0])
            info.evidence.append(f"text '{m.group(0)[:80]}' → pages={info.total_pages}")
            break
        elif kind == "items_only":
            info.total_items = int(groups[0])
            info.evidence.append(f"text '{m.group(0)[:80]}' → items={info.total_items}")
            break
        elif kind == "items_only_loose":
            # accetto solo se >= 50 (sotto e' probabilmente count parziale)
            v = int(groups[0])
            if v >= 50:
                info.total_items = v
                info.evidence.append(f"text loose '{m.group(0)[:80]}' → items={v}")
                break

    # 2. Pattern URL (cerco anchor con ?page=N o /page/N e prendo il max).
    #    Qui uso l'HTML grezzo (gli href sono dentro i tag).
    for pat in _URL_PAGE_PATTERNS:
        nums = pat.findall(html)
        if not nums:
            continue
        max_n = max(int(n) for n in nums)
        if info.max_page_in_urls is None or max_n > info.max_page_in_urls:
            info.max_page_in_urls = max_n
            # Inferisci style + param
            sample = pat.search(html)
            if sample:
                src = sample.group(0)
                if "/page/" in src.lower():
                    info.detected_style = "path"
                    info.detected_param = "page"
                elif "/p/" in src.lower():
                    info.detected_style = "path"
                    info.detected_param = "p"
                else:
                    info.detected_style = "query"
                    # estraggo il nome del param
                    pm = re.search(r"[?&](page|p|pagina)=", src, re.IGNORECASE)
                    info.detected_param = pm.group(1).lower() if pm else "page"
            info.evidence.append(
                f"urls: trovati anchor di paginazione fino a page={max_n} "
                f"(style={info.detected_style}, param={info.detected_param})"
            )

    return info


def generate_paginated_urls(
    base_url: str,
    max_page: int,
    *,
    start_page: int = 1,
    cap: int = 2500,
    param: str = "page",
    style: str = "query",
) -> list[str]:
    """Genera lista di URL paginati a partire da una base URL.

    style="query"  → `base?param=N`
    style="path"   → `base/{param}/N` (es. `/page/N`)
    """
    if max_page < start_page:
        return []
    end = min(max_page, start_page + cap - 1)
    out: list[str] = []
    parsed = urlparse(base_url)
    if style == "path":
        prefix = base_url.rstrip("/") + f"/{param}/"
        for n in range(start_page, end + 1):
            out.append(f"{prefix}{n}")
        return out
    # query style: dedup con eventuali param esistenti
    existing = [(k, v) for k, v in parse_qsl(parsed.query) if k.lower() != param.lower()]
    for n in range(start_page, end + 1):
        new_q = urlencode(existing + [(param, str(n))])
        out.append(urlunparse((
            parsed.scheme, parsed.netloc, parsed.path, "", new_q, ""
        )))
    return out


# === CLI debug ===

def _main_cli() -> None:
    import argparse
    import asyncio
    import json as _json
    import httpx

    parser = argparse.ArgumentParser(description="Pagination detector CLI")
    parser.add_argument("url")
    args = parser.parse_args()

    async def _run() -> None:
        ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:120.0) Gecko/20100101 Firefox/120.0"
        async with httpx.AsyncClient(headers={"User-Agent": ua}, follow_redirects=True, timeout=15) as client:
            r = await client.get(args.url)
        info = detect_pagination(r.text, args.url)
        print(f"status={r.status_code} html_size={len(r.text)}")
        print(_json.dumps({
            "total_items": info.total_items,
            "total_pages": info.total_pages,
            "max_page_in_urls": info.max_page_in_urls,
            "detected_param": info.detected_param,
            "detected_style": info.detected_style,
            "has_pagination": info.has_pagination,
            "estimated_pages": info.estimated_pages,
            "evidence": info.evidence,
        }, indent=2))
        if info.has_pagination:
            sample = generate_paginated_urls(
                args.url, min(info.estimated_pages, 5),
                param=info.detected_param or "page",
                style=info.detected_style or "query",
            )
            print(f"\nSample generated URLs (first 5):")
            for u in sample:
                print(f"  {u}")

    asyncio.run(_run())


if __name__ == "__main__":
    _main_cli()
