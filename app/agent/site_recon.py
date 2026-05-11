"""Site Reconnaissance — scoperta della directory canonica di un sito target.

Stage preliminare al profiler. Prima di scegliere una strategia di scraping,
proviamo a sostituire il seed URL (spesso la home, una pagina marketing curata)
con una directory page paginata, che e' il vero entry-point per cataloghi grandi.

Tre tecniche complementari, eseguite in parallelo:

1. **Probe URL candidati**: lista canonica di path per `target_type` (es.
   `/escorts`, `/profiles`, `/products`, `/listings`). HEAD/GET veloci, scelgo
   quello che risponde 200 con piu' link al pattern target.

2. **Sitemap**: parsing di `/robots.txt` (`Sitemap:` directive) e `/sitemap.xml`.
   Espande indici sitemap nidificati. Da' URL gia' pronti per estrazione,
   saltando del tutto la discovery.

3. **Nav/footer**: estraggo i link da `<nav>`, `<footer>`, `<menu>` della home.
   Sono le "directory promosse dal sito stesso".

Output: `RecceResult` con `best_seed_url` (potenzialmente diverso da input) e
`prepopulated_urls` (lista di URL gia' pronti per estrazione, ridotti via
`canonical_url` e filtri service-path).

Tutto agnostico al dominio: nessun match hardcoded su siti specifici.
"""
from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from typing import Iterable
from urllib.parse import urljoin, urlparse
from xml.etree import ElementTree as ET

import httpx
from selectolax.parser import HTMLParser

from .pagination_detector import detect_pagination, generate_paginated_urls, PaginationInfo
from .url_canonical import canonical_url, looks_like_service_path

log = logging.getLogger(__name__)


# === Candidate directory paths per target_type ===
# Ordinati per popolarita' nel web (i piu' comuni prima). Per ogni target_type,
# 8-15 candidati: si esegue HEAD in parallelo, costo trascurabile.
CANDIDATE_PATHS: dict[str, tuple[str, ...]] = {
    "profile_contacts": (
        "/escorts", "/profiles", "/models", "/girls", "/performers",
        "/talents", "/members", "/users", "/directory", "/browse",
        "/listings", "/profili", "/modelle", "/ragazze", "/annunci",
        "/people", "/creators", "/cams", "/find",
    ),
    "ecommerce_products": (
        "/products", "/catalog", "/shop", "/collections", "/store",
        "/products/all", "/all-products", "/catalogo", "/prodotti",
        "/categorie", "/categories", "/c", "/p",
    ),
    "real_estate": (
        "/immobili", "/annunci", "/vendita", "/affitto", "/properties",
        "/listings", "/case", "/appartamenti", "/properties-for-sale",
        "/for-sale", "/for-rent", "/in-vendita", "/in-affitto", "/cerca",
    ),
    "events": (
        "/events", "/eventi", "/concerts", "/concerti", "/agenda",
        "/calendar", "/calendario", "/programma", "/whats-on", "/cosa-fare",
    ),
    "news_articles": (
        "/articles", "/posts", "/news", "/blog", "/archive",
        "/articoli", "/notizie", "/cronaca", "/latest", "/feed",
    ),
    "job_listings": (
        "/jobs", "/careers", "/lavoro", "/positions", "/listings",
        "/annunci", "/offerte-lavoro", "/posizioni", "/cerca-lavoro",
        "/job-listings", "/openings",
    ),
}

# Marker da cercare nel path/URL di un sitemap per dedurre se contiene target
# (vs. pagine di sistema come privacy/sitemap-blog/sitemap-faq)
SITEMAP_TARGET_HINTS: dict[str, tuple[str, ...]] = {
    "profile_contacts": (
        "profile", "profil", "escort", "model", "performer",
        "girl", "user", "member", "talent", "annunc",
        "babe", "creator", "performer", "cam", "ragazz",
    ),
    "ecommerce_products": ("product", "prodott", "shop", "catalog", "item"),
    "real_estate": ("immob", "propert", "annunc", "listing", "vendita", "affitto"),
    "events": ("event", "concert", "agenda"),
    "news_articles": ("article", "post", "news", "blog", "stor"),
    "job_listings": ("job", "career", "annunc", "position", "lavor"),
}

# Marker generici di "directory-like" path (validi cross-target). Usati per
# filtrare i link estratti da nav/footer della home: se il path contiene uno
# di questi token e' verosimilmente una directory o un indice.
DIRECTORY_HINT_TOKENS: tuple[str, ...] = (
    "index", "top", "list", "archive", "newest", "latest", "all",
    "browse", "directory", "categories", "category", "elenco", "catalogo",
    "everyone", "everybody", "atoz", "a-z",
)

DEFAULT_TIMEOUT = 15
MAX_SITEMAP_URLS = 5000  # cap difensivo per non saturare memoria
PROBE_CONCURRENCY = 4    # max richieste in parallelo (alcuni siti rate-limitano)

# UA browser-like: molti siti (cloudflare, vercel, app server) rate-limitano o
# bloccano UA "bot-like" come "AgentScraper/1.0". Usiamo Mozilla/Firefox default.
DEFAULT_BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:120.0) "
    "Gecko/20100101 Firefox/120.0"
)


@dataclass
class RecceResult:
    original_seed: str
    best_seed_url: str
    seed_changed: bool
    prepopulated_urls: list[str] = field(default_factory=list)
    sitemap_urls_total: int = 0
    candidates_probed: list[dict] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "original_seed": self.original_seed,
            "best_seed_url": self.best_seed_url,
            "seed_changed": self.seed_changed,
            "prepopulated_urls": self.prepopulated_urls[:50],  # log-friendly
            "prepopulated_count": len(self.prepopulated_urls),
            "sitemap_urls_total": self.sitemap_urls_total,
            "candidates_probed": self.candidates_probed,
            "evidence": self.evidence,
        }


def _origin(url: str) -> str:
    """Ritorna scheme://host (senza path) dell'URL."""
    p = urlparse(url)
    return f"{p.scheme}://{p.netloc}" if p.scheme and p.netloc else url


def _path_matches_target(url: str, hints: Iterable[str]) -> bool:
    """True se l'URL contiene almeno uno dei marker del target nel path."""
    if not hints:
        return False
    path_lower = (urlparse(url).path or "").lower()
    return any(h in path_lower for h in hints)


def _extract_anchor_urls(html: str, base_url: str) -> list[str]:
    """Estrae tutti gli URL assoluti dalla pagina (anchor + nav/footer)."""
    try:
        tree = HTMLParser(html)
    except Exception:
        return []
    out: list[str] = []
    seen: set[str] = set()
    base_host = (urlparse(base_url).hostname or "").lower()
    for a in tree.css("a[href]"):
        href = (a.attributes.get("href") or "").strip()
        if not href or href.startswith(("javascript:", "mailto:", "tel:", "#")):
            continue
        absolute = urljoin(base_url, href).split("#")[0]
        host = (urlparse(absolute).hostname or "").lower()
        if not host or host != base_host:
            continue
        if absolute not in seen:
            seen.add(absolute)
            out.append(absolute)
    return out


def _extract_nav_footer_urls(html: str, base_url: str) -> list[str]:
    """Estrae URL solo da <nav>, <footer>, <menu> — le directory promosse dal sito."""
    try:
        tree = HTMLParser(html)
    except Exception:
        return []
    out: list[str] = []
    seen: set[str] = set()
    base_host = (urlparse(base_url).hostname or "").lower()
    for selector in ("nav a[href]", "footer a[href]", "menu a[href]", "[role=navigation] a[href]"):
        for a in tree.css(selector):
            href = (a.attributes.get("href") or "").strip()
            if not href or href.startswith(("javascript:", "mailto:", "tel:", "#")):
                continue
            absolute = urljoin(base_url, href).split("#")[0]
            host = (urlparse(absolute).hostname or "").lower()
            if not host or host != base_host:
                continue
            if absolute not in seen:
                seen.add(absolute)
                out.append(absolute)
    return out


async def _probe_url(
    client: httpx.AsyncClient,
    url: str,
) -> dict:
    """GET veloce + analisi: status, content-length, num link, num link target.

    Uso GET (non HEAD) perche' molti siti rispondono 405/200-vuoto a HEAD ma
    200 con corpo a GET. Cap a 2 MB per gestire siti con <head> giganti
    (es. tryst.link ha 362 KB di CSS inline prima del body).
    """
    info: dict = {"url": url, "status": None, "html_size": 0, "n_links": 0, "error": None}
    try:
        r = await client.get(url, follow_redirects=True)
        info["status"] = r.status_code
        info["final_url"] = str(r.url)
        if r.status_code >= 400:
            return info
        ct = (r.headers.get("content-type") or "").lower()
        if "html" not in ct:
            info["error"] = f"content-type non HTML: {ct}"
            return info
        html = r.text[:2_000_000]
        info["html_size"] = len(html)
        links = _extract_anchor_urls(html, str(r.url))
        info["n_links"] = len(links)
        info["sample_links"] = links[:20]
        # Detect paginazione (testo + URL): "page 1 of N", anchor ?page=N
        pag = detect_pagination(html, str(r.url))
        info["pagination"] = pag
        info["has_pagination"] = pag.has_pagination
        info["estimated_pages"] = pag.estimated_pages
    except Exception as e:
        info["error"] = f"{type(e).__name__}: {e}"
    return info


async def _probe_candidates(
    seed_origin: str,
    target_type: str,
    client: httpx.AsyncClient,
    extra_urls: Iterable[str] = (),
) -> list[dict]:
    """Testa candidati canonici + extra (es. nav/footer links), in parallelo.

    Alcuni siti (es. tryst.link) rate-limitano a 429 se ricevono 15+ richieste
    burst, quindi limitiamo la concorrenza con un semaforo.
    """
    paths = CANDIDATE_PATHS.get(target_type) or CANDIDATE_PATHS["profile_contacts"]
    canonical_candidates = [seed_origin.rstrip("/") + p for p in paths]
    all_candidates = list(canonical_candidates)
    seen = {canonical_url(u) for u in all_candidates}
    for u in extra_urls:
        cu = canonical_url(u)
        if cu not in seen:
            seen.add(cu)
            all_candidates.append(u)
    sem = asyncio.Semaphore(PROBE_CONCURRENCY)

    async def _guarded(u: str) -> dict:
        async with sem:
            return await _probe_url(client, u)

    results = await asyncio.gather(
        *[_guarded(u) for u in all_candidates],
        return_exceptions=False,
    )
    return list(results)


def _select_directory_candidates_from_nav(
    nav_urls: Iterable[str],
    target_hints: Iterable[str],
    seed_url: str,
    *,
    max_candidates: int = 12,
) -> list[str]:
    """Filtra i link estratti da nav/footer della home per quelli che sembrano
    directory page candidate.

    Criterio (OR):
      - path contiene un target_hint (es. /babes, /escorts, /models)
      - path contiene un directory_hint_token (es. /top100, /index/A, /newest)

    Esclude service-paths (privacy, faq, login, ecc.) e il seed stesso.
    """
    seed_canon = canonical_url(seed_url)
    hints_t = tuple(target_hints) if target_hints else ()
    chosen: list[str] = []
    seen: set[str] = set()
    for u in nav_urls:
        cu = canonical_url(u)
        if cu == seed_canon or cu in seen:
            continue
        if looks_like_service_path(u):
            continue
        path_lower = (urlparse(u).path or "").lower()
        if not path_lower or path_lower == "/":
            continue
        matches_target = any(h in path_lower for h in hints_t) if hints_t else False
        matches_directory = any(t in path_lower for t in DIRECTORY_HINT_TOKENS)
        if matches_target or matches_directory:
            seen.add(cu)
            chosen.append(u)
            if len(chosen) >= max_candidates:
                break
    return chosen


def _pick_best_candidate(
    probed: list[dict],
    target_hints: Iterable[str],
    home_n_links: int,
) -> dict | None:
    """Sceglie il candidato migliore.

    Criterio composito (in ordine di priorita'):
      1. has_pagination=True → directory paginata, copre molti piu' profili
         rispetto a una sub-directory specifica con solo 1 pagina visibile.
      2. target_count (link che matchano target_hint nel path) nel sample.
      3. n_links totali.

    Una directory paginata con 25 target-link nel sample + page 1 of 1363 batte
    facilmente una sub-directory geografica con 88 link senza paginazione.
    """
    valid = [c for c in probed if c.get("status") == 200 and c.get("html_size", 0) > 0]
    if not valid:
        return None
    hints_t = tuple(target_hints) if target_hints else ()
    scored = []
    for c in valid:
        sample = c.get("sample_links", []) or []
        target_count = sum(1 for u in sample if _path_matches_target(u, hints_t)) if hints_t else 0
        n_links = c.get("n_links", 0)
        has_pag = 1 if c.get("has_pagination") else 0
        est_pages = c.get("estimated_pages", 1) or 1
        # Sorting:
        # 1. has_pagination=True batte False (directory paginata > pagina statica)
        # 2. Tra paginati: est_pages desc (la directory piu' ampia = piu' copertura totale)
        # 3. target_count desc (qualita' del sample)
        # 4. n_links desc
        scored.append((has_pag, est_pages, target_count, n_links, c))
    scored.sort(key=lambda x: (x[0], x[1], x[2], x[3]), reverse=True)
    best = scored[0][-1]
    best_target_count = scored[0][2]
    best_n_links = scored[0][3]
    # Soglia di promozione:
    # - se ha paginazione visibile → promuovi (vale comunque)
    # - se ha target hint match >= 1 → promuovi
    # - altrimenti richiede 1.5x i link della home + >=10 link
    if best["has_pagination"]:
        return best
    if hints_t and best_target_count >= 1:
        return best
    if not hints_t and best_n_links > home_n_links * 1.5 and best_n_links >= 10:
        return best
    return None


async def _fetch_sitemap_urls_for_seed(
    seed_origin: str,
    target_hints: Iterable[str],
    client: httpx.AsyncClient,
) -> list[str]:
    """Legge robots.txt + sitemap.xml (e indici) per estrarre URL del target."""
    hints_t = tuple(target_hints) if target_hints else ()
    sitemap_urls: list[str] = []

    # 1. Trova URL dei sitemap (robots.txt + fallback)
    sitemap_locations: list[str] = []
    try:
        r = await client.get(seed_origin.rstrip("/") + "/robots.txt", follow_redirects=True)
        if r.status_code == 200:
            for ln in r.text.splitlines():
                ln = ln.strip()
                if ln.lower().startswith("sitemap:"):
                    sm = ln.split(":", 1)[1].strip()
                    if sm:
                        sitemap_locations.append(sm)
    except Exception:
        pass
    if not sitemap_locations:
        sitemap_locations.append(seed_origin.rstrip("/") + "/sitemap.xml")

    # 2. Scarica + parsea ogni sitemap (espandi indici)
    seen_sitemaps: set[str] = set()
    queue = list(sitemap_locations)
    while queue and len(sitemap_urls) < MAX_SITEMAP_URLS:
        sm_url = queue.pop(0)
        if sm_url in seen_sitemaps:
            continue
        seen_sitemaps.add(sm_url)
        try:
            r = await client.get(sm_url, follow_redirects=True)
            if r.status_code != 200:
                continue
            body = r.text
        except Exception:
            continue
        # Filtra sitemap che chiaramente non riguardano il target (best-effort)
        if hints_t and not _path_matches_target(sm_url, hints_t):
            # ma se e' un sitemap index, espandilo comunque (potrebbe contenere
            # un sub-sitemap che invece e' rilevante)
            is_index = "<sitemapindex" in body[:500].lower()
            if not is_index:
                # se non e' un index e non matcha hint, skip
                # (es. sitemap-blog, sitemap-pages, sitemap-faq)
                continue
        try:
            root = ET.fromstring(body)
        except ET.ParseError:
            continue
        tag = root.tag.lower()
        if "sitemapindex" in tag:
            for child in root:
                loc_el = next((c for c in child if c.tag.lower().endswith("loc")), None)
                if loc_el is not None and loc_el.text:
                    queue.append(loc_el.text.strip())
        elif "urlset" in tag:
            for url_el in root:
                loc_el = next((c for c in url_el if c.tag.lower().endswith("loc")), None)
                if loc_el is None or not loc_el.text:
                    continue
                u = loc_el.text.strip()
                if looks_like_service_path(u):
                    continue
                if hints_t and not _path_matches_target(u, hints_t):
                    continue
                sitemap_urls.append(u)
                if len(sitemap_urls) >= MAX_SITEMAP_URLS:
                    break

    # Dedupe via canonical
    out: list[str] = []
    seen_canon: set[str] = set()
    for u in sitemap_urls:
        c = canonical_url(u)
        if c not in seen_canon:
            seen_canon.add(c)
            out.append(u)
    return out


async def recon_site(
    seed_url: str,
    target_type: str = "profile_contacts",
    *,
    user_agent: str = DEFAULT_BROWSER_UA,
    timeout: int = DEFAULT_TIMEOUT,
) -> RecceResult:
    """Esegue reconnaissance e ritorna il miglior seed da usare + URL pre-popolati.

    Logica:
      1. GET home → conta link/pattern
      2. In parallelo: probe candidati + sitemap.xml
      3. Se sitemap contiene >= 20 URL target → mantieni seed home, ritorna URL pre-popolati
         (saltiamo del tutto la discovery)
      4. Se candidato batte la home → cambia seed
      5. Altrimenti → mantieni seed originale
    """
    seed_url = (seed_url or "").strip()
    if seed_url and not seed_url.startswith(("http://", "https://")):
        seed_url = "https://" + seed_url
    result = RecceResult(
        original_seed=seed_url,
        best_seed_url=seed_url,
        seed_changed=False,
    )
    if not seed_url:
        result.evidence.append("seed URL vuoto, recon skip")
        return result

    target_hints = SITEMAP_TARGET_HINTS.get(target_type, ())
    seed_orig = _origin(seed_url)

    async with httpx.AsyncClient(
        headers={"User-Agent": user_agent},
        timeout=timeout,
        follow_redirects=True,
    ) as client:
        # Step 1: fetch home + sitemap in parallelo (la home serve sia per
        # confronto sia per estrarre nav/footer links come candidati dinamici)
        home_info, sitemap_urls = await asyncio.gather(
            _probe_url(client, seed_url),
            _fetch_sitemap_urls_for_seed(seed_orig, target_hints, client),
        )

        # Step 2: estraggo URL nav/footer dalla home per arricchire i candidati
        # con quelli "promossi dal sito stesso" (es. babepedia /top100, /index/A)
        nav_candidates: list[str] = []
        if home_info.get("status") == 200 and home_info.get("html_size", 0) > 0:
            # re-fetch della home per leggere l'HTML (probe lo aveva troncato a
            # 2MB ma non lo aveva ritornato). Riusiamo la stessa connessione.
            try:
                r_home = await client.get(seed_url)
                if r_home.status_code == 200:
                    home_html = r_home.text[:2_000_000]
                    nav_urls = _extract_nav_footer_urls(home_html, seed_url)
                    nav_candidates = _select_directory_candidates_from_nav(
                        nav_urls, target_hints, seed_url
                    )
            except Exception as e:
                log.debug("nav extraction failed for %s: %s", seed_url, e)

        # Step 3: probe candidati canonici + nav/footer in parallelo
        probed = await _probe_candidates(
            seed_orig, target_type, client, extra_urls=nav_candidates,
        )

    result.candidates_probed = [
        {k: v for k, v in c.items() if k not in ("sample_links", "pagination")}
        for c in probed
    ]
    result.sitemap_urls_total = len(sitemap_urls)

    # Sitemap-first: se abbiamo abbastanza URL dalla sitemap, e' la strada migliore
    if len(sitemap_urls) >= 20:
        result.prepopulated_urls = sitemap_urls
        result.evidence.append(
            f"sitemap: {len(sitemap_urls)} URL del target trovati (target_hints={list(target_hints)}). "
            f"Saltiamo discovery, passiamo URL pre-popolati al runner."
        )
        # Anche se la sitemap copre tutto, scegliamo comunque il miglior seed
        # come fallback (utile se runner vuole scrollare per dedup/refresh).
        best = _pick_best_candidate(probed, target_hints, home_info.get("n_links", 0))
        if best:
            result.best_seed_url = best.get("final_url") or best["url"]
            result.seed_changed = (canonical_url(result.best_seed_url) != canonical_url(seed_url))
            if result.seed_changed:
                result.evidence.append(
                    f"seed promosso: {seed_url} → {result.best_seed_url} "
                    f"(candidato con {best.get('n_links')} link, sample target_count alto)"
                )
        return result

    # No sitemap utile: prova promozione seed via candidati
    best = _pick_best_candidate(probed, target_hints, home_info.get("n_links", 0))
    if best:
        result.best_seed_url = best.get("final_url") or best["url"]
        result.seed_changed = (canonical_url(result.best_seed_url) != canonical_url(seed_url))
        if result.seed_changed:
            result.evidence.append(
                f"seed promosso: {seed_url} → {result.best_seed_url}. "
                f"Motivo: {best.get('n_links')} link nel candidato vs "
                f"{home_info.get('n_links')} nella home."
                + (f" [paginazione: {best.get('estimated_pages')} pagine]" if best.get("has_pagination") else "")
            )
        # Pagination expansion: se il candidato ha paginazione visibile, genero
        # gli URL paginati come prepopulated → runner downstream salta auto-discovery
        # e itera direttamente le pagine.
        pag = best.get("pagination")
        if pag and isinstance(pag, PaginationInfo) and pag.has_pagination:
            n_pages = min(pag.estimated_pages, 200)  # cap difensivo
            paged_urls = generate_paginated_urls(
                result.best_seed_url,
                n_pages,
                start_page=1,
                param=pag.detected_param or "page",
                style=pag.detected_style or "query",
            )
            if paged_urls:
                result.prepopulated_urls = paged_urls
                result.evidence.append(
                    f"pagination expansion: generati {len(paged_urls)} URL paginati "
                    f"({pag.detected_style or 'query'} style, param={pag.detected_param or 'page'}) "
                    f"da page 1 a {n_pages}. {pag.evidence[0] if pag.evidence else ''}"
                )
    else:
        result.evidence.append(
            f"nessun candidato batte la home ({home_info.get('n_links')} link). Mantengo seed."
        )

    return result


# === CLI di debug (per test offline) ===

def _main_cli() -> None:
    import argparse
    import json as _json

    parser = argparse.ArgumentParser(description="Site recon CLI (debug)")
    parser.add_argument("url")
    parser.add_argument("--target", default="profile_contacts")
    args = parser.parse_args()

    res = asyncio.run(recon_site(args.url, args.target))
    print(_json.dumps(res.as_dict(), indent=2, ensure_ascii=False))


if __name__ == "__main__":
    _main_cli()
