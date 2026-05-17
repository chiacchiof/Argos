"""Runner bulk_extract: scraping massivo deterministico (NO loop agentico).

Per cataloghi grandi è 10-30× più veloce ed economico di browser_use:
- Niente decisioni LLM step-by-step, niente click, niente browser
- Per ogni URL: fetch HTTP → readability text → 1 chiamata LLM con schema → JSON
- Concorrenza configurabile + rate limit per host (rispetto del server target)

Fonti URL (cumulative):
1. `seed_queries` del task (URL inseriti manualmente)
2. `input_artifact_path`: file profiles.jsonl/txt/csv da un task upstream

Filtri:
- whitelist `allowed_domains` / blacklist `blocked_domains`
- dedup
- cap a `max_iterations` (riusato come max_urls per safety)

Strategie di estrazione:
- `llm_per_page` (default): chiama LLM con testo della pagina + schema → JSON
- `css_selectors` (futuro): mapping campo→selettore CSS, niente LLM
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx
from selectolax.parser import HTMLParser

from .. import db
from ..config import RESULTS_DIR, settings
from .extraction_templates import get_schema
from .llm_providers import get_provider, resolve_api_key, resolve_base_url
from .ollama import maybe_add_keep_alive
from .tools.fetch_http import fetch_http


log = logging.getLogger(__name__)


# --------------------------------------------------------------------------
# Helpers raccolta URL
# --------------------------------------------------------------------------

def _normalize_url(s: str) -> str | None:
    s = (s or "").strip()
    if not s:
        return None
    if not s.startswith(("http://", "https://")):
        s = "https://" + s
    try:
        p = urlparse(s)
        if not p.hostname:
            return None
    except Exception:
        return None
    return s


def _domain_of(url: str) -> str:
    try:
        return (urlparse(url).hostname or "").lower()
    except Exception:
        return ""


def _domain_allowed(url: str, allowed: list[str], blocked: list[str]) -> bool:
    host = _domain_of(url)
    if not host:
        return False
    if blocked and any(host == d or host.endswith("." + d) for d in blocked):
        return False
    if allowed and not any(host == d or host.endswith("." + d) for d in allowed):
        return False
    return True


def _load_urls_from_artifact(path: str | None) -> list[str]:
    """Legge URL da un file (jsonl con campo 'url' o txt una per riga)."""
    if not path:
        return []
    p = Path(path)
    if not p.exists():
        return []
    urls: list[str] = []
    for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("{"):
            try:
                obj = json.loads(line)
                u = obj.get("url") or obj.get("source_url")
                if isinstance(u, str):
                    urls.append(u)
            except json.JSONDecodeError:
                continue
        else:
            urls.append(line)
    return urls


# --------------------------------------------------------------------------
# Rate limiter per host
# --------------------------------------------------------------------------

class PerHostRateLimiter:
    """Garantisce un intervallo minimo tra le richieste sullo stesso host."""

    def __init__(self, rate_per_sec: float):
        self.interval = 1.0 / max(0.1, rate_per_sec)
        self._last: dict[str, float] = defaultdict(float)
        self._locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    async def wait_for(self, url: str) -> None:
        host = _domain_of(url) or "_"
        async with self._locks[host]:
            now = time.monotonic()
            wait = self._last[host] + self.interval - now
            if wait > 0:
                await asyncio.sleep(wait)
            self._last[host] = time.monotonic()


# --------------------------------------------------------------------------
# Crawler: BFS deterministico con auto-detect del URL pattern
# --------------------------------------------------------------------------

_GENERIC_SUBDOMAINS = {"www", "api", "cdn", "static", "media", "assets", "img", "images"}


def _registrable_domain(host: str) -> str:
    """Ritorna il 'registrable domain' (ultimi 2 livelli) di un host.

    'teverina-hot.mondocamgirls.com' → 'mondocamgirls.com'
    'www.example.com' → 'example.com'
    'example.com' → 'example.com'

    Limite noto: non gestisce public-suffix multi-livello (.co.uk, .com.au).
    Per il 99% dei domini commerciali è sufficiente; per supportare PSL serve
    la libreria `tldextract`.
    """
    if not host:
        return ""
    parts = host.lower().split(".")
    if len(parts) <= 2:
        return host.lower()
    return ".".join(parts[-2:])


def _segment_signature(seg: str) -> str:
    """Riconosce la 'forma' di un segmento di path per raggruppare URL simili.

    Slug = sequenza alfanumerica con `_` o `-`. Accetta sia lowercase che
    maiuscole (es. "Ana_De_Armas" su babepedia, "John-Smith" su LinkedIn).
    """
    if not seg:
        return ""
    if seg.isdigit():
        return "{int}"
    if re.match(r"^\d+\.html?$", seg):
        return "{int}.html"
    if re.match(r"^[A-Za-z0-9_-]+_\d+$", seg):
        return "{slug}_{int}"
    if re.match(r"^[A-Za-z0-9_-]+\.html?$", seg):
        return "{slug}.html"
    if re.match(r"^[A-Za-z0-9_-]+$", seg):
        return "{slug}"
    if "." in seg and re.search(r"\.(html?|php|aspx?)$", seg, re.IGNORECASE):
        return "{file}"
    return seg  # tieni il segmento letterale se non riconosciuto


def _url_match_key(url: str) -> str:
    """Ritorna 'host+path' usato per pattern matching del crawler.

    Includere l'host permette al crawler di distinguere sub-domini come
    profili separati (es. <slug>.example.com) da sezioni della home.
    Niente schema, niente query, niente fragment. Trailing slash rimosso
    per coerenza con `_url_to_pattern` e con i regex generati da
    `_pattern_to_regex` (che ancorano con `$`).
    """
    try:
        p = urlparse(url)
        host = (p.hostname or "").lower()
        path = p.path or "/"
        if len(path) > 1 and path.endswith("/"):
            path = path[:-1]
        return host + path
    except Exception:
        return url


def _url_to_pattern(url: str) -> str:
    """Trasforma un URL in un pattern strutturale 'host+path' per raggruppamento.

    Tokenizza il sub-dominio se è uno slug (es. nome modella):
        'teverina-hot.mondocamgirls.com/en' → '{slug}.mondocamgirls.com/en'
    Lascia intatti i sub-domini "generici" (www, api, cdn, ...) e i domini
    a 2 livelli.
    """
    try:
        p = urlparse(url)
        host = (p.hostname or "").lower()
        path = p.path or ""
    except Exception:
        return url

    host_parts = host.split(".")
    if len(host_parts) >= 3:
        sub = host_parts[0]
        # host già lowercase, ma accetto comunque [A-Za-z] per robustezza + underscore
        if re.match(r"^[A-Za-z0-9_-]+$", sub) and sub not in _GENERIC_SUBDOMAINS:
            host_pattern = "{slug}." + ".".join(host_parts[1:])
        else:
            host_pattern = host
    else:
        host_pattern = host

    if path.endswith("/"):
        path = path[:-1]
    segs = [s for s in path.split("/") if s]
    if not segs:
        return host_pattern + "/"
    pattern_segs = [_segment_signature(s) for s in segs]
    return host_pattern + "/" + "/".join(pattern_segs)


def _group_urls_by_pattern(urls: list[str]) -> dict[str, list[str]]:
    """Raggruppa URL per pattern strutturale 'host+path'."""
    groups: dict[str, list[str]] = defaultdict(list)
    for u in urls:
        groups[_url_to_pattern(u)].append(u)
    return dict(sorted(groups.items(), key=lambda kv: -len(kv[1])))


def _extract_links(html: str, base_url: str, same_origin_only: bool = True) -> list[str]:
    """Estrae tutti i link <a href> assoluti dalla pagina.

    Quando `same_origin_only` è True, accetta link verso lo stesso
    'registrable domain' del seed (cioè anche sub-domini), in modo che siti
    con profili in sub-domini distinti (`<slug>.sito.com`) siano scopribili
    partendo dalla home `www.sito.com`.
    """
    base_host = (urlparse(base_url).hostname or "").lower()
    base_reg = _registrable_domain(base_host)
    out: list[str] = []
    try:
        tree = HTMLParser(html)
    except Exception:
        return out
    for a in tree.css("a[href]"):
        href = (a.attributes.get("href") or "").strip()
        if not href or href.startswith(("javascript:", "mailto:", "tel:", "#")):
            continue
        absolute = urljoin(base_url, href)
        absolute = absolute.split("#")[0]  # strip fragment
        if same_origin_only:
            host = (urlparse(absolute).hostname or "").lower()
            if _registrable_domain(host) != base_reg:
                continue
        if absolute not in out:
            out.append(absolute)
    return out


def _pattern_to_regex(pattern: str) -> str:
    """Converte un pattern 'host+path' (con placeholder {slug}/{int}/...) in regex.

    Es: '{slug}.mondocamgirls.com/'  →  '^[a-z0-9-]+\\.mondocamgirls\\.com/?$'
        'www.example.com/p/{int}'    →  '^www\\.example\\.com/p/[0-9]+$'
        'books.toscrape.com/catalogue/{slug}_{int}/{slug}.html'
            → '^books\\.toscrape\\.com/catalogue/[a-z0-9-]+_[0-9]+/[a-z0-9-]+\\.html$'
    """
    placeholder_map = [
        ("{slug}_{int}", "[a-z0-9-]+_[0-9]+"),
        ("{int}.html", "[0-9]+\\.html"),
        ("{slug}.html", "[a-z0-9-]+\\.html"),
        ("{slug}", "[a-z0-9-]+"),
        ("{int}", "[0-9]+"),
        ("{file}", "[^/]+\\.[a-z0-9]+"),
    ]
    # Sentinella per ogni placeholder, escape del resto, ri-sostituzione.
    sentinels: list[tuple[str, str]] = []
    work = pattern
    for i, (ph, rgx) in enumerate(placeholder_map):
        s = f"\x00{i}\x00"
        if ph in work:
            work = work.replace(ph, s)
            sentinels.append((s, rgx))
    # Escape regex sul resto
    escaped = re.escape(work)
    # Ripristina sentinelle (re.escape non tocca \x00 ma può aver scappato i numeri attorno)
    for s, rgx in sentinels:
        # re.escape lascia \x00 invariato, quindi la sentinella esiste ancora
        escaped = escaped.replace(s, rgx)
    # Permetti slash finale opzionale
    if escaped.endswith("/"):
        return f"^{escaped[:-1]}/?$"
    return f"^{escaped}$"


def _count_pattern_matches(regex_str: str, urls: list[str]) -> int:
    """Quanti URL della lista matchano la regex (su host+path)."""
    try:
        pat = re.compile(regex_str)
    except re.error:
        return 0
    return sum(1 for u in urls if pat.search(_url_match_key(u)))


_LISTING_KEYWORDS = (
    "vendit", "annunci", "annunc", "case", "casa-",
    "categori", "catalog", "prodott", "elenco", "directory",
    "ricerc", "search", "result", "list", "items",
    "all", "comuni", "regioni", "zone", "quartier",
    "marche", "brand", "sezion", "area",
)


def _identify_candidate_listings(
    sample_links: list[str],
    excluded_pattern_str: str | None = None,
    top_n: int = 4,
) -> list[str]:
    """Identifica candidate listing pages tra i link interni del seed.

    Una "listing" e' una pagina che NON e' target ma probabilmente CONTIENE link
    a pagine target (es. /vendita-case/{citta}/ o /annunci/categoria/<x>).

    Score euristico per ciascun URL del sample:
    - +3 se URL contiene keyword listing (vendita, annunci, case, categoria, ...)
    - +2 se il suo pattern ha >= 5 URL nel sample (= pattern ricorrente)
      (+1 se ha 2-4 URL)
    - +1 se il path e' corto (<=3 segmenti).

    `excluded_pattern_str` e' rispettato per evitare di pescare un URL gia'
    classificato come target. NON esclude pattern simili: gli URL `/vendita-case/X/`
    e `/annuncio/X/` possono condividere `{slug}/{slug}` ma essere semanticamente
    diversi — la keyword nello score discrimina.

    Ritorna URL singoli (non pattern), deduplicati, ordinati per score
    discendente. Path piu' corto come tie-breaker.
    """
    if not sample_links:
        return []
    groups = _group_urls_by_pattern(sample_links)
    target_regex: re.Pattern | None = None
    if excluded_pattern_str:
        try:
            target_regex = re.compile(_pattern_to_regex(excluded_pattern_str))
        except re.error:
            target_regex = None
    candidates: list[tuple[int, int, str]] = []  # (score_neg, path_len, url)
    for pat, urls in groups.items():
        pat_count = len(urls)
        # Per ogni pattern strutturale scelgo l'URL "migliore": quello con score
        # piu' alto (privilegiando keyword listing). Senza questa scelta interna,
        # un URL come /account/accedi/ verrebbe scelto come rappresentante del
        # pattern {slug}/{slug} mentre /vendita-case/acireale/ (stesso pattern,
        # ma listing reale) verrebbe ignorato.
        best_url: str | None = None
        best_score = 0
        best_path_len = 9999
        for url in urls:
            if target_regex and target_regex.search(_url_match_key(url)):
                continue
            url_lower = url.lower()
            score = 0
            if any(kw in url_lower for kw in _LISTING_KEYWORDS):
                score += 3
            if pat_count >= 5:
                score += 2
            elif pat_count >= 2:
                score += 1
            path_len = len([s for s in urlparse(url).path.split("/") if s])
            if 1 <= path_len <= 3:
                score += 1
            if score > best_score or (score == best_score and path_len < best_path_len):
                best_url = url
                best_score = score
                best_path_len = path_len
        if best_url and best_score > 0:
            candidates.append((-best_score, best_path_len, best_url))
    candidates.sort()
    out: list[str] = []
    seen: set[str] = set()
    for _s, _pl, url in candidates:
        if url in seen:
            continue
        seen.add(url)
        out.append(url)
        if len(out) >= top_n:
            break
    return out


async def _rerank_listings_via_llm(
    client: httpx.AsyncClient,
    base_url: str,
    api_key: str,
    model: str,
    candidates: list[str],
    user_objective: str,
    schema_text: str,
) -> list[str]:
    """Chiede all'LLM di riordinare i candidate URL secondo la probabilita' che
    siano LISTING che linkano i target dell'obiettivo (es. annunci, prodotti).

    Non hallucina: ritorna SOLO sottoinsiemi della lista candidate input. In caso
    di errore fallback restituisce l'ordine originale.
    """
    if len(candidates) <= 1:
        return list(candidates)
    numbered = "\n".join(f"  {i+1}. {u}" for i, u in enumerate(candidates))
    prompt = (
        "Sei un agente che decide la PRIORITA' di esplorazione di pagine web.\n\n"
        "OBIETTIVO UTENTE:\n"
        f"{user_objective[:1500]}\n\n"
        "SCHEMA DATI TARGET (cosa stiamo cercando di estrarre):\n"
        f"{schema_text[:1500]}\n\n"
        "URL candidate (pagine intermedie del sito):\n"
        f"{numbered}\n\n"
        "Quali di questi URL e' piu' probabile che siano LISTING che linkano "
        "alle pagine-target (annunci, prodotti, articoli, ecc.) coerenti con "
        "l'obiettivo? Considera la coerenza semantica (zona, categoria, tipo) "
        "fra l'obiettivo e l'URL.\n\n"
        "Rispondi SOLO con un JSON di questa forma:\n"
        "{\n"
        '  "ranked": [<numero>, <numero>, ...]\n'
        "}\n"
        "dove i numeri sono gli indici (1-based) della lista, ordinati dal piu' "
        "promettente al meno. Niente prosa."
    )
    payload: dict[str, Any] = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.0,
        "max_tokens": 200,
        "response_format": {"type": "json_object"},
    }
    maybe_add_keep_alive(payload, base_url)
    headers = {"Authorization": f"Bearer {api_key}"}
    try:
        r = await client.post(
            f"{base_url.rstrip('/')}/chat/completions",
            json=payload,
            headers=headers,
            timeout=30,
        )
        r.raise_for_status()
        raw = (r.json().get("choices", [{}])[0].get("message", {}) or {}).get("content") or ""
    except Exception:
        return list(candidates)
    try:
        import json as _json

        obj = _json.loads(raw)
        ranked_idx = obj.get("ranked") or []
    except Exception:
        return list(candidates)
    out: list[str] = []
    seen: set[int] = set()
    for x in ranked_idx:
        try:
            i = int(x) - 1
        except (TypeError, ValueError):
            continue
        if 0 <= i < len(candidates) and i not in seen:
            seen.add(i)
            out.append(candidates[i])
    # eventuale residuo che il modello ha omesso, in coda con ordine originale
    for i, u in enumerate(candidates):
        if i not in seen:
            out.append(u)
    return out


async def _auto_detect_pattern_via_llm(
    client: httpx.AsyncClient,
    base_url: str,
    api_key: str,
    model: str,
    seed_url: str,
    sample_links: list[str],
    schema_text: str,
    user_objective: str = "",
    excluded_patterns: list[str] | None = None,
) -> tuple[str | None, str | None]:
    """L'LLM sceglie quale pattern URL contiene le pagine target.

    Ritorna (pattern_str, reason) — il caller converte pattern_str in regex
    via `_pattern_to_regex`. L'LLM sceglie l'INDICE da una lista numerata
    invece di generare regex (modelli piccoli sbagliano la regex).

    Args:
        user_objective: testo libero dell'obiettivo del task (in italiano);
          è il "perché" dell'utente, va a guidare la scelta dell'LLM. Non
          contiene istruzioni hard-coded.
        excluded_patterns: pattern già provati senza successo, vengono RIMOSSI
          dai candidati così l'LLM non può ripeterli.
    """
    excluded_patterns = excluded_patterns or []
    groups = _group_urls_by_pattern(sample_links)
    candidates: list[tuple[str, list[str]]] = [
        (pat, urls) for pat, urls in groups.items() if pat not in excluded_patterns
    ]
    candidates = candidates[:14]

    if not candidates:
        return None, "nessun pattern candidato disponibile"

    summary_lines = []
    for i, (pat, urls) in enumerate(candidates, start=1):
        examples = urls[:4]
        summary_lines.append(
            f"[{i}] PATTERN: {pat}  ({len(urls)} URL)\n"
            + "\n".join(f"      • {u}" for u in examples)
        )

    objective_block = ""
    if user_objective.strip():
        objective_block = (
            "\nOBIETTIVO DELL'UTENTE (cosa cerca, in italiano):\n"
            f"  {user_objective.strip()[:600]}\n"
        )

    excluded_block = ""
    if excluded_patterns:
        excluded_block = (
            "\nPATTERN GIÀ PROVATI E SCARTATI (non hanno trovato URL target):\n"
            + "\n".join(f"  - {p}" for p in excluded_patterns)
            + "\n"
        )

    user_prompt = (
        f"Ho fatto fetch del seed: {seed_url}\n"
        + objective_block
        + excluded_block
        + "\nPattern di URL trovati nella pagina (formato 'host+path'):\n\n"
        + "\n".join(summary_lines)
        + "\n\n"
        f"Schema dei dati da estrarre:\n{schema_text[:1500]}\n\n"
        "DOMANDA: qual è il NUMERO del pattern che contiene le pagine-DETTAGLIO "
        "da cui posso estrarre i dati dello schema?\n\n"
        "Rispondi in JSON ESATTO:\n"
        '{"index": <numero>, "reason": "<una frase breve in italiano>"}\n'
        "Solo JSON, niente prosa."
    )

    payload = {
        "model": model,
        "messages": [
            {
                "role": "system",
                "content": (
                    "Sei un classificatore di URL. Devi scegliere, da una lista "
                    "numerata di pattern URL osservati in una pagina web, quello "
                    "che corrisponde alle pagine-dettaglio richieste dall'utente. "
                    "Pondera obiettivo dell'utente, schema dei dati, e plausibilità "
                    "del pattern (URL con segmenti variabili tipicamente sono "
                    "pagine-dettaglio; URL con nomi fissi tipicamente sono "
                    "pagine di navigazione)."
                ),
            },
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0.0,
        "max_tokens": 200,
    }
    maybe_add_keep_alive(payload, base_url)
    try:
        r = await client.post(
            f"{base_url.rstrip('/')}/chat/completions",
            json=payload,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=60,
        )
        r.raise_for_status()
        data = r.json()
        raw = (data.get("choices", [{}])[0].get("message", {}).get("content", "") or "").strip()
    except Exception as e:
        log.warning("auto-detect pattern fallito: %s", e)
        return None, f"errore HTTP: {e}"

    try:
        obj = json.loads(raw)
    except json.JSONDecodeError:
        m = re.search(r"\{[\s\S]+?\}", raw)
        if not m:
            return None, "JSON non parseable"
        try:
            obj = json.loads(m.group(0))
        except json.JSONDecodeError:
            return None, "JSON malformato"

    if not isinstance(obj, dict):
        return None, "risposta non-dict"
    idx = obj.get("index")
    reason = (obj.get("reason") or "").strip()
    try:
        idx = int(idx)
    except (TypeError, ValueError):
        return None, f"indice non numerico: {idx!r}"
    if idx < 1 or idx > len(candidates):
        return None, f"indice fuori range: {idx} (candidates: {len(candidates)})"
    chosen_pattern, _ = candidates[idx - 1]
    return chosen_pattern, reason


async def _bfs_crawl(
    client: httpx.AsyncClient,
    seeds: list[str],
    pattern_re: re.Pattern,
    max_depth: int,
    max_urls: int,
    allowed: list[str],
    blocked: list[str],
    rate_limiter: "PerHostRateLimiter",
    jlog,
) -> list[str]:
    """BFS deterministico: parte dai seed, segue i link interni, raccoglie
    quelli che matchano `pattern_re`. Si ferma a max_depth o max_urls.
    """
    visited: set[str] = set()
    discovered: list[str] = []
    discovered_set: set[str] = set()
    frontier: list[str] = [s for s in seeds if s]

    for depth in range(max_depth):
        if not frontier:
            break
        jlog(f"  crawler depth {depth + 1}/{max_depth}: {len(frontier)} URL da esplorare")
        next_frontier: list[str] = []
        for url in frontier:
            if url in visited:
                continue
            visited.add(url)
            if not _domain_allowed(url, allowed, blocked):
                continue
            try:
                await rate_limiter.wait_for(url)
                from .tools.fetch_http import fetch_http
                fr = await fetch_http(url, max_chars=200_000)
                if fr.status >= 400:
                    continue
                # NB: fetch_http ritorna text "principale" (readability),
                # ma per estrarre i link ci serve l'HTML completo.
                # Usiamo httpx direttamente per il crawl (più veloce, niente readability).
                try:
                    raw_resp = await client.get(url, timeout=20, follow_redirects=True)
                    if raw_resp.status_code < 400:
                        html = raw_resp.text
                    else:
                        continue
                except Exception:
                    continue
            except Exception:
                continue

            links = _extract_links(html, url, same_origin_only=True)
            for link in links:
                if not _domain_allowed(link, allowed, blocked):
                    continue
                # Match contro 'host+path' (così sub-domini come profili sono
                # distinguibili da sezioni del sito principale).
                key = _url_match_key(link)
                if pattern_re.search(key):
                    if link not in discovered_set:
                        discovered.append(link)
                        discovered_set.add(link)
                        if len(discovered) >= max_urls:
                            jlog(f"  crawler: cap max_urls raggiunto ({max_urls})")
                            return discovered
                # Anche se non matcha, potrebbe essere una pagina-categoria → BFS
                if link not in visited and link not in next_frontier:
                    next_frontier.append(link)
        # cap sulla frontier per non esplodere
        frontier = next_frontier[:500]
    jlog(f"  crawler ha esplorato {len(visited)} pagine, scoperto {len(discovered)} URL target")
    return discovered


# --------------------------------------------------------------------------
# LLM extraction
# --------------------------------------------------------------------------

EXTRACT_SYSTEM = """Sei un parser deterministico. Ricevi (1) il TESTO PRINCIPALE di una \
pagina web e (2) uno SCHEMA che descrive i campi da estrarre.

Tuo compito: ritornare UN SINGOLO oggetto JSON con i campi richiesti dallo schema.
- Se un campo non è presente nella pagina, metti `null`. NON inventare valori.
- Risposta = SOLO il JSON, niente prose, niente markdown fence.
- Tipi: rispetta number/string/array/null come indicato dallo schema.
- Formati: ISO-8601 per date, codici ISO-639/ISO-3166 per lingua/paese.
"""


def _build_extract_prompt(text: str, url: str, schema: str) -> str:
    return (
        f"URL: {url}\n\n"
        f"TESTO PAGINA (max ~6000 caratteri):\n{text[:6000]}\n\n"
        f"SCHEMA:\n{schema}\n\n"
        "Ritorna ORA il JSON estratto."
    )


async def _llm_extract_json(
    client: httpx.AsyncClient,
    base_url: str,
    api_key: str,
    model: str,
    text: str,
    url: str,
    schema: str,
) -> tuple[dict[str, Any] | None, str]:
    """Ritorna (dict_estratto_o_None, raw_response_per_debug)."""
    payload: dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "system", "content": EXTRACT_SYSTEM},
            {"role": "user", "content": _build_extract_prompt(text, url, schema)},
        ],
        "temperature": 0.0,
        # max_tokens 1500: alcune pagine hanno content lungo (liste prezzi/servizi)
        # e il LLM riempie il campo `estratto` con molto testo. A 800 tokens il
        # JSON veniva troncato a meta', causando parse-fail su tutta la riga.
        # 1500 da' margine; max output gpt-4o-mini è 16k, quindi safe.
        "max_tokens": 1500,
        # response_format funziona su OpenAI (json_object) e su Ollama OpenAI-compat (>=0.4)
        "response_format": {"type": "json_object"},
    }
    # Per Ollama: parametro nativo `format=json` (forza output JSON puro, niente prosa)
    if "11434" in base_url or "/v1" in base_url and "openai.com" not in base_url and "anthropic.com" not in base_url and "x.ai" not in base_url:
        payload["format"] = "json"
    maybe_add_keep_alive(payload, base_url)

    headers = {"Authorization": f"Bearer {api_key}"}
    api_url = f"{base_url.rstrip('/')}/chat/completions"
    raw = ""
    try:
        r = await client.post(api_url, json=payload, headers=headers, timeout=120)
        r.raise_for_status()
        data = r.json()
        raw = (data.get("choices", [{}])[0].get("message", {}).get("content", "") or "").strip()
    except Exception as e:
        log.warning("LLM extract failed for %s: %s", url, e)
        return None, f"<HTTP_ERROR: {type(e).__name__}: {e}>"

    if not raw:
        return None, ""

    # Strip `<think>...</think>` tag prodotti da modelli Qwen3 in thinking mode
    cleaned = re.sub(r"<think>[\s\S]*?</think>", "", raw, flags=re.IGNORECASE).strip()

    # Parse JSON: prima diretto, poi fra fence markdown, poi greedy
    for candidate in (cleaned, raw):
        try:
            obj = json.loads(candidate)
            if isinstance(obj, dict):
                return obj, raw
        except json.JSONDecodeError:
            pass

    m = re.search(r"```(?:json)?\s*([\s\S]+?)```", cleaned)
    if m:
        try:
            obj = json.loads(m.group(1).strip())
            if isinstance(obj, dict):
                return obj, raw
        except json.JSONDecodeError:
            pass

    m2 = re.search(r"\{[\s\S]+\}", cleaned)
    if m2:
        try:
            obj = json.loads(m2.group(0))
            if isinstance(obj, dict):
                return obj, raw
        except json.JSONDecodeError:
            pass

    return None, raw


# --------------------------------------------------------------------------
# Main runner
# --------------------------------------------------------------------------

def _resolve_extraction_schema(task: dict[str, Any]) -> str:
    custom = (task.get("extraction_schema") or "").strip()
    if custom:
        return custom
    return get_schema(task.get("extraction_template"))


async def run_agent(task: dict[str, Any], job_id: int) -> str:
    """Entry-point. Registra il job_id in `_active_jobs` cosi' che il bottone Stop
    sull'UI del singolo job (anche sub-job di auto_extract) lo cancelli davvero.
    """
    from .. import jobs as _jobs

    _jobs.register_subjob(job_id)
    try:
        return await _run_agent_inner(task, job_id)
    finally:
        _jobs.unregister_subjob(job_id)


async def _run_agent_inner(task: dict[str, Any], job_id: int) -> str:
    from .blocked_domains import assert_no_blocked_seeds

    def jlog(line: str) -> None:
        db.append_job_log(job_id, line)

    db.update_job(job_id, status="running", started_at=db.now_iso())
    db.set_control_signal(job_id, None)
    jlog(
        f"Avvio bulk_extract per task #{task['id']} \"{task['name']}\" "
        f"— modello {task['model']}"
    )

    # POLICY GATE: domini bloccati (vedi memoria feedback_no_mondocamgirl_traffic)
    _blocked = assert_no_blocked_seeds(task.get("seed_queries") or [])
    if _blocked:
        msg = f"Seed bloccati dalla policy locale (no-traffic): {_blocked}. Abort runner."
        jlog(f"⛔ {msg}")
        db.update_job(job_id, status="error", error=msg, finished_at=db.now_iso())
        return ""

    # 1. Risorse e config
    # MAIN model: usato per le N chiamate di extraction (1 per URL)
    provider_key = task.get("llm_provider") or "ollama"
    try:
        base_url = resolve_base_url(provider_key, task.get("llm_base_url"))
        api_key = resolve_api_key(provider_key, task.get("llm_api_key"))
    except RuntimeError as e:
        jlog(f"ERRORE configurazione provider principale: {e}")
        db.update_job(job_id, status="error", error=str(e), finished_at=db.now_iso())
        raise

    # DISCOVERY model: usato SOLO per la chiamata auto-detect del URL pattern
    # (1 sola chiamata, beneficia di un modello più capace). Se non specificato
    # o uguale al main, riusa il main client.
    discovery_provider_key = (task.get("discovery_llm_provider") or "").strip() or provider_key
    discovery_model = (task.get("discovery_llm_model") or "").strip() or task["model"]
    if discovery_provider_key == provider_key and discovery_model == task["model"]:
        # nessuno split: stesso client di main
        discovery_base_url = base_url
        discovery_api_key = api_key
        discovery_split_active = False
    else:
        try:
            discovery_base_url = resolve_base_url(discovery_provider_key, None)
            # API key dedicata al discovery (priorità) → env var del provider → errore
            discovery_api_key = resolve_api_key(
                discovery_provider_key,
                task.get("discovery_llm_api_key"),
            )
            discovery_split_active = True
        except RuntimeError as e:
            jlog(
                f"⚠️ provider discovery non utilizzabile ({e}) — "
                f"fallback al provider principale per auto-detect."
            )
            discovery_base_url = base_url
            discovery_api_key = api_key
            discovery_provider_key = provider_key
            discovery_model = task["model"]
            discovery_split_active = False

    if discovery_split_active:
        jlog(
            f"🧠 Mix LLM attivo:\n"
            f"  • Discovery (auto-detect pattern, 1× chiamata): "
            f"{discovery_provider_key}/{discovery_model}\n"
            f"  • Extraction (per URL, N× chiamate): "
            f"{provider_key}/{task['model']}"
        )

    schema_text = _resolve_extraction_schema(task)
    concurrency = int(task.get("bulk_concurrency") or 5)
    rate_per_sec = float(task.get("bulk_rate_limit_per_sec") or 2.0)
    max_urls = int(task.get("max_iterations") or 1000)
    method = task.get("bulk_extraction_method") or "llm_per_page"
    crawler_on = bool(task.get("crawler_enabled"))
    crawler_pattern = task.get("crawler_url_pattern") or None
    crawler_depth = int(task.get("crawler_max_depth") or 3)

    # 2. Run dir + paths
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_dir = RESULTS_DIR / str(task["id"]) / ts
    run_dir.mkdir(parents=True, exist_ok=True)
    profiles_path = run_dir / "profiles.jsonl"
    errors_path = run_dir / "errors.jsonl"

    # 3. Raccolta URL: seed_queries + input_artifact_path, dedup, filtri
    raw_seeds = task.get("seed_queries") or []
    artifact_urls = _load_urls_from_artifact(task.get("input_artifact_path"))
    all_urls = [u for u in (raw_seeds + artifact_urls) if u]
    normalized: list[str] = []
    seen: set[str] = set()
    for u in all_urls:
        nu = _normalize_url(u)
        if not nu:
            continue
        if nu in seen:
            continue
        seen.add(nu)
        normalized.append(nu)

    allowed = task.get("allowed_domains") or []
    blocked = task.get("blocked_domains") or []
    filtered = [u for u in normalized if _domain_allowed(u, allowed, blocked)]
    dropped_filter = len(normalized) - len(filtered)

    jlog(
        f"URL iniziali: {len(raw_seeds)} seed + {len(artifact_urls)} artifact "
        f"= {len(all_urls)} totali → {len(normalized)} validi → {len(filtered)} dopo filtri "
        f"({dropped_filter} fuori scope)"
    )

    # 3b. CRAWLER (opzionale): scopre URL navigando dal seed
    crawler_pattern_id: int | None = None
    crawler_pattern_reused: bool = False
    crawler_pattern_hits: int = 0
    if crawler_on and filtered:
        limiter_for_crawl = PerHostRateLimiter(rate_per_sec)
        async with httpx.AsyncClient(
            headers={"User-Agent": settings.http_user_agent},
            follow_redirects=True,
        ) as crawl_client:
            # Auto-detect pattern se non specificato dall'utente
            effective_pattern = crawler_pattern
            asset_type_for_pattern = task.get("extraction_template") or None
            seed0 = filtered[0]
            seed_domain = _registrable_domain((urlparse(seed0).hostname or "").lower())
            # Memoria DB: se ho gia' un pattern confirmed per questo dominio+template, riusalo.
            if not effective_pattern and seed_domain:
                try:
                    saved_patterns = db.find_site_patterns(
                        seed_domain,
                        asset_type=asset_type_for_pattern,
                        status="confirmed",
                    )
                except Exception as e:
                    jlog(f"  ⚠️ memoria pattern non disponibile: {type(e).__name__}: {e}")
                    saved_patterns = []
                if saved_patterns:
                    sp = saved_patterns[0]
                    effective_pattern = sp.get("regex") or None
                    crawler_pattern_id = int(sp["id"]) if sp.get("id") else None
                    crawler_pattern_reused = True
                    jlog(
                        f"  📌 memoria DB: riuso pattern confermato per '{seed_domain}' "
                        f"(asset_type={asset_type_for_pattern}): {sp.get('pattern')!r} "
                        f"[hits={sp.get('hits')} successes={sp.get('successes')}]"
                    )
            if not effective_pattern:
                # Auto-detect con retry loop: il discovery LLM sceglie un pattern,
                # verifichiamo che matchi almeno un link diretto del seed; se 0
                # match, escludiamo quel pattern e ri-chiamiamo il discovery.
                # Loop fino a `max_discovery_retries + 1` tentativi totali.
                max_retries = int(task.get("max_discovery_retries") or 3)
                user_objective = task.get("objective") or ""
                try:
                    r0 = await crawl_client.get(seed0, timeout=20)
                    sample_links = _extract_links(r0.text, seed0, same_origin_only=True)
                    if not sample_links:
                        jlog("  ⚠️ seed non ha link interni: crawler saltato")
                    else:
                        groups_dbg = _group_urls_by_pattern(sample_links)
                        jlog(
                            f"  crawler: trovati {len(sample_links)} link interni nel seed, "
                            f"{len(groups_dbg)} pattern distinti:"
                        )
                        for pat, urls in list(groups_dbg.items())[:10]:
                            jlog(f"    • {pat}  ({len(urls)} URL, es. {urls[0]})")

                        excluded_patterns: list[str] = []
                        seed_pattern_str: str | None = None
                        seed_n_match: int = 0
                        for attempt in range(max_retries + 1):
                            if attempt == 0:
                                jlog(f"  discovery: tentativo 1 (objective + schema → LLM)...")
                            else:
                                jlog(
                                    f"  discovery: retry #{attempt} "
                                    f"(scartati {len(excluded_patterns)}: {excluded_patterns})"
                                )
                            pattern_str, reason = await _auto_detect_pattern_via_llm(
                                crawl_client, discovery_base_url, discovery_api_key,
                                discovery_model, seed0, sample_links, schema_text,
                                user_objective=user_objective,
                                excluded_patterns=excluded_patterns,
                            )
                            if not pattern_str:
                                jlog(f"  ⚠️ discovery non ha proposto un pattern: {reason}")
                                break
                            regex_str = _pattern_to_regex(pattern_str)
                            n_match = _count_pattern_matches(regex_str, sample_links)
                            jlog(
                                f"    → scelto: {pattern_str!r} → regex {regex_str!r} "
                                f"(reason: {reason})"
                            )
                            jlog(
                                f"    sanity check: matcha {n_match}/{len(sample_links)} "
                                f"link diretti del seed"
                            )
                            if n_match > 0:
                                effective_pattern = regex_str
                                seed_pattern_str = pattern_str
                                seed_n_match = n_match
                                jlog(f"  ✅ pattern accettato dopo {attempt + 1} tentativo/i")
                                # Persisto il pattern come 'candidate' nella memoria DB
                                if seed_domain:
                                    try:
                                        crawler_pattern_id = db.upsert_site_pattern(
                                            registrable_domain=seed_domain,
                                            pattern=pattern_str,
                                            regex=regex_str,
                                            asset_type=asset_type_for_pattern,
                                            source_task_id=task.get("id"),
                                            source_job_id=job_id,
                                        )
                                        jlog(
                                            f"  📌 memoria DB: salvato pattern come 'candidate' "
                                            f"(id={crawler_pattern_id}) per '{seed_domain}'"
                                        )
                                    except Exception as e:
                                        jlog(f"  ⚠️ memoria DB: salvataggio pattern fallito: {type(e).__name__}: {e}")
                                break
                            excluded_patterns.append(pattern_str)

                        # DRILL-DOWN: se il pattern dal seed e' debole (<3 link target diretti),
                        # cerca pagine listing intermediarie (es. /vendita-case/{citta}/) che
                        # potrebbero contenere link target piu' specifici, e ri-fai discovery li'.
                        # Questo gestisce il caso in cui la home non linka direttamente le
                        # pagine target ma ha solo un menu di sezioni.
                        if effective_pattern and seed_n_match < 3:
                            # Non escludiamo il pattern del seed: dato che e' debole
                            # (n_match<3), un URL che lo matcha potrebbe in realta'
                            # essere una listing utile, non un target affidabile.
                            candidate_listings = _identify_candidate_listings(
                                sample_links,
                                excluded_pattern_str=None,
                                top_n=6,  # piu' alto: il rerank LLM filtrera' meglio
                            )
                            if candidate_listings:
                                # Rerank LLM: chiede al modello "quali sono LISTING
                                # plausibili dato l'obiettivo?". Se il rerank fallisce
                                # silenziosamente, ricade sull'ordine euristico keyword.
                                try:
                                    candidate_listings = await _rerank_listings_via_llm(
                                        crawl_client,
                                        discovery_base_url,
                                        discovery_api_key,
                                        discovery_model,
                                        candidate_listings,
                                        user_objective=user_objective,
                                        schema_text=schema_text,
                                    )
                                except Exception as e:
                                    jlog(
                                        f"  ⚠️ rerank LLM fallito ({type(e).__name__}: {e}). "
                                        f"Uso ordine euristico."
                                    )
                                # Limito a 4 dopo il rerank per non visitare troppe pagine
                                candidate_listings = candidate_listings[:4]
                                jlog(
                                    f"  🔍 pattern dal seed debole ({seed_n_match} match). "
                                    f"Esploro {len(candidate_listings)} candidate listing "
                                    f"(LLM-ranked) per cercare un pattern piu' ricco."
                                )
                                best_pattern_str = seed_pattern_str
                                best_regex = effective_pattern
                                best_n = seed_n_match
                                best_listing_url: str | None = None
                                for listing_url in candidate_listings:
                                    jlog(f"    listing candidate: {listing_url}")
                                    try:
                                        rl = await crawl_client.get(listing_url, timeout=20)
                                        listing_links = _extract_links(
                                            rl.text, listing_url, same_origin_only=True
                                        )
                                    except Exception as e:
                                        jlog(f"      errore fetch ({type(e).__name__}: {e})")
                                        continue
                                    if not listing_links:
                                        jlog("      no link interni nella listing")
                                        continue
                                    try:
                                        new_pattern, new_reason = await _auto_detect_pattern_via_llm(
                                            crawl_client, discovery_base_url, discovery_api_key,
                                            discovery_model, listing_url, listing_links, schema_text,
                                            user_objective=user_objective,
                                            excluded_patterns=[],
                                        )
                                    except Exception as e:
                                        jlog(f"      errore discovery LLM ({type(e).__name__}: {e})")
                                        continue
                                    if not new_pattern:
                                        jlog(f"      LLM non ha proposto pattern ({new_reason})")
                                        continue
                                    new_regex = _pattern_to_regex(new_pattern)
                                    new_n = _count_pattern_matches(new_regex, listing_links)
                                    jlog(
                                        f"      → pattern {new_pattern!r}: matcha "
                                        f"{new_n}/{len(listing_links)} link della listing"
                                    )
                                    if new_n > best_n:
                                        best_pattern_str = new_pattern
                                        best_regex = new_regex
                                        best_n = new_n
                                        best_listing_url = listing_url
                                        if best_n >= 5:
                                            break  # pattern fortemente confermato
                                if best_listing_url and best_pattern_str != seed_pattern_str:
                                    jlog(
                                        f"  ✅ pattern migliorato dalla listing {best_listing_url}: "
                                        f"{best_pattern_str!r} ({best_n} match)"
                                    )
                                    effective_pattern = best_regex
                                    if best_listing_url not in seen:
                                        seen.add(best_listing_url)
                                        filtered.append(best_listing_url)
                                        jlog(
                                            f"  ➕ listing aggiunta come seed: {best_listing_url}"
                                        )
                                    if seed_domain:
                                        try:
                                            crawler_pattern_id = db.upsert_site_pattern(
                                                registrable_domain=seed_domain,
                                                pattern=best_pattern_str,
                                                regex=best_regex,
                                                asset_type=asset_type_for_pattern,
                                                source_task_id=task.get("id"),
                                                source_job_id=job_id,
                                                notes=f"Migliorato via drill-down su {best_listing_url}",
                                            )
                                            jlog(
                                                f"  📌 memoria DB: pattern aggiornato (id={crawler_pattern_id}) "
                                                f"per '{seed_domain}' dopo drill-down"
                                            )
                                        except Exception as e:
                                            jlog(f"  ⚠️ memoria DB drill-down save fallito: {e}")
                                else:
                                    jlog(
                                        f"  ↩ drill-down: nessun candidate ha prodotto un pattern "
                                        f"migliore. Tengo quello del seed."
                                    )

                        if not effective_pattern:
                            jlog(
                                f"  ⚠️ discovery ha esaurito i tentativi "
                                f"({max_retries + 1}/{max_retries + 1}): nessun pattern matcha. "
                                f"Crawler saltato."
                            )
                except Exception as e:
                    jlog(f"  ⚠️ errore durante discovery: {e}")
            else:
                jlog(f"  crawler: pattern manuale: {effective_pattern!r}")

            if effective_pattern:
                try:
                    pattern_re = re.compile(effective_pattern)
                except re.error as e:
                    jlog(f"  ⚠️ pattern regex non valido ({e}): crawler saltato")
                    pattern_re = None

                if pattern_re is not None:
                    discovered = await _bfs_crawl(
                        crawl_client,
                        seeds=filtered,
                        pattern_re=pattern_re,
                        max_depth=crawler_depth,
                        max_urls=max_urls,
                        allowed=allowed,
                        blocked=blocked,
                        rate_limiter=limiter_for_crawl,
                        jlog=jlog,
                    )
                    # Aggiungi i discovered alla lista (dedup)
                    for u in discovered:
                        if u not in seen:
                            seen.add(u)
                            filtered.append(u)
                    crawler_pattern_hits = len(discovered)
                    jlog(
                        f"  crawler: {len(discovered)} URL nuovi aggiunti alla lista "
                        f"(totale ora: {len(filtered)})"
                    )

    # 3c. Cap finale
    if len(filtered) > max_urls:
        jlog(
            f"Lista URL troncata: {len(filtered)} → {max_urls} "
            f"(adegua 'Max URL' se vuoi processarne di più)"
        )
        urls_to_process = filtered[:max_urls]
    else:
        urls_to_process = filtered

    jlog(f"URL finali da processare con LLM extraction: {len(urls_to_process)}")
    if not urls_to_process:
        jlog("⚠️ Nessuna URL da processare. Aborto.")
        msg = "Nessuna URL valida (controlla seed_queries, input_artifact_path, whitelist domini)"
        db.update_job(job_id, status="error", error=msg, finished_at=db.now_iso())
        return ""

    # 4. Pool concorrente + rate limiter
    sem = asyncio.Semaphore(concurrency)
    limiter = PerHostRateLimiter(rate_per_sec)

    n_ok = 0
    n_failed = 0
    n_done = 0
    stopped = False
    write_lock = asyncio.Lock()

    profiles_f = profiles_path.open("a", encoding="utf-8")
    errors_f = errors_path.open("a", encoding="utf-8")

    refresh_policy_days = int(task.get("refresh_policy_days") if task.get("refresh_policy_days") is not None else 7)
    extraction_template_for_skip = task.get("extraction_template") or None
    n_skipped_recent = 0

    async def process_one(client: httpx.AsyncClient, url: str) -> None:
        nonlocal n_ok, n_failed, n_done, stopped, n_skipped_recent
        # Stop check cooperativo
        if db.get_control_signal(job_id) == "stop":
            stopped = True
            return
        # Fix 3: skip se asset esiste in DB ed e' fresco (per re-run incrementali).
        if extraction_template_for_skip and db.has_recent_asset(
            url, extraction_template_for_skip, refresh_policy_days
        ):
            n_skipped_recent += 1
            n_done += 1
            return
        async with sem:
            await limiter.wait_for(url)
            try:
                fr = await fetch_http(url)
                if fr.status >= 400 or not fr.text or len(fr.text.strip()) < 50:
                    raise RuntimeError(f"fetch ko (status={fr.status}, len={len(fr.text or '')})")

                if method == "llm_per_page":
                    obj, raw_resp = await _llm_extract_json(
                        client, base_url, api_key, task["model"],
                        fr.text, url, schema_text,
                    )
                else:
                    raise RuntimeError(f"strategy '{method}' non ancora implementata")

                if not obj:
                    # Log del raw response per debug (troncato a 500 char per non esplodere)
                    async with write_lock:
                        errors_f.write(
                            json.dumps(
                                {
                                    "url": url,
                                    "error": "LLM ha ritornato JSON vuoto/non-parsabile",
                                    "raw_response": (raw_resp or "")[:500],
                                },
                                ensure_ascii=False,
                            )
                            + "\n"
                        )
                        errors_f.flush()
                    n_failed += 1
                    return  # esce senza marcare ok

                # Iniezione metadati standard
                obj.setdefault("source_url", url)
                obj.setdefault("source_domain", _domain_of(url))
                obj.setdefault("crawled_at", db.now_iso())

                async with write_lock:
                    profiles_f.write(json.dumps(obj, ensure_ascii=False) + "\n")
                    profiles_f.flush()
                n_ok += 1
            except Exception as e:
                n_failed += 1
                async with write_lock:
                    errors_f.write(
                        json.dumps(
                            {"url": url, "error": f"{type(e).__name__}: {e}"},
                            ensure_ascii=False,
                        )
                        + "\n"
                    )
                    errors_f.flush()
            finally:
                n_done += 1
                if n_done % 10 == 0 or n_done == len(urls_to_process):
                    jlog(
                        f"  progress: {n_done}/{len(urls_to_process)} "
                        f"({n_ok} ok, {n_failed} failed)"
                    )

    try:
        async with httpx.AsyncClient(
            headers={"User-Agent": settings.http_user_agent}
        ) as client:
            tasks_running = [
                asyncio.create_task(process_one(client, u)) for u in urls_to_process
            ]
            try:
                await asyncio.gather(*tasks_running, return_exceptions=True)
            except asyncio.CancelledError:
                stopped = True
                for t in tasks_running:
                    t.cancel()
                raise
    finally:
        profiles_f.close()
        errors_f.close()

    # 5. Ingest in DB (riusa lo stesso meccanismo del browser_use runner)
    # IMPORTANTE: facciamo l'ingest PRIMA delle stats del pattern, cosi' possiamo
    # contare i veri "successes" (asset validi post-validation) invece dei semplici
    # "extraction LLM ok" — un pattern che produce 200 JSON parseable ma tutti vuoti
    # NON e' un successo, e non deve essere promosso a 'confirmed' nella memoria.
    from .runner_browseruse import _ingest_to_assets, _ingest_to_contacts
    n_ingested = _ingest_to_contacts(
        profiles_path,
        task["id"],
        job_id,
        jlog,
        extraction_template=task.get("extraction_template"),
    )
    n_assets = _ingest_to_assets(
        profiles_path,
        task["id"],
        job_id,
        jlog,
        extraction_template=task.get("extraction_template"),
    )

    # 5b. Memoria pattern: aggiorna stats POST-VALIDATION e prova promozione.
    # `successes` = asset realmente validi (post-filtro completezza), non
    # extraction LLM "ok". Cosi' un pattern che produce 200 JSON parseable
    # ma tutti vuoti diventa 200 failures invece che 200 successes.
    if crawler_pattern_id is not None:
        try:
            real_successes = int(n_assets)
            real_failures = max(0, int(n_done) - real_successes)
            db.record_pattern_run(
                crawler_pattern_id,
                hits=int(crawler_pattern_hits),
                successes=real_successes,
                failures=real_failures,
            )
            new_status = db.maybe_promote_pattern(crawler_pattern_id)
            if new_status:
                jlog(
                    f"  📌 memoria DB: pattern id={crawler_pattern_id} -> "
                    f"status='{new_status}' (post-validation: "
                    f"{real_successes} successes / {real_failures} failures)"
                )
        except Exception as e:
            jlog(f"  ⚠️ memoria DB stats fallito: {type(e).__name__}: {e}")

    # 6. Report finale
    fmt = task.get("output_format") or "md"
    report_ext = "md" if fmt in ("md", "both") else "txt"
    status_word = "INTERROTTA dall'utente" if stopped else "completata"
    report = (
        f"# Riepilogo bulk_extract {ts} ({status_word})\n\n"
        f"- **URL processate**: {n_done}/{len(urls_to_process)}\n"
        f"- **Estrazioni OK**: {n_ok}\n"
        f"- **Fallite**: {n_failed} (vedi `errors.jsonl`)\n"
        f"- **Contatti ingestiti in DB**: {n_ingested}\n"
        f"- **Asset ingestiti in DB**: {n_assets}\n"
        f"- **Strategia**: `{method}`, concorrenza={concurrency}, rate={rate_per_sec}/s/host\n"
        f"- **Provider/Modello**: {provider_key} / {task['model']}\n\n"
        f"Vedi `profiles.jsonl` per i dati strutturati.\n"
    )
    report_path = run_dir / f"report.{report_ext}"
    report_path.write_text(report, encoding="utf-8")

    final_status = "cancelled" if stopped else "done"
    jlog(
        f"Run {status_word}: {n_ok} estratti, {n_failed} falliti, "
        f"{n_ingested} ingest. Report: {report_path}"
    )
    db.update_job(
        job_id, status=final_status, finished_at=db.now_iso(),
        result_path=str(report_path),
    )
    db.set_control_signal(job_id, None)
    return str(report_path)
