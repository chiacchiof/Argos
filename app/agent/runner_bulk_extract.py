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

def _segment_signature(seg: str) -> str:
    """Riconosce la 'forma' di un segmento di path per raggruppare URL simili."""
    if not seg:
        return ""
    if seg.isdigit():
        return "{int}"
    if re.match(r"^\d+\.html?$", seg):
        return "{int}.html"
    if re.match(r"^[a-z0-9-]+_\d+$", seg):
        return "{slug}_{int}"
    if re.match(r"^[a-z0-9-]+\.html?$", seg):
        return "{slug}.html"
    if re.match(r"^[a-z0-9-]+$", seg):
        return "{slug}"
    if "." in seg and re.search(r"\.(html?|php|aspx?)$", seg, re.IGNORECASE):
        return "{file}"
    return seg  # tieni il segmento letterale se non riconosciuto


def _url_to_pattern(url: str) -> str:
    """Trasforma un URL in un pattern strutturale (per raggruppamento)."""
    try:
        path = urlparse(url).path
    except Exception:
        return url
    if path.endswith("/"):
        path = path[:-1]
    segs = [s for s in path.split("/") if s]
    if not segs:
        return "/"
    pattern_segs = [_segment_signature(s) for s in segs]
    return "/" + "/".join(pattern_segs)


def _group_urls_by_pattern(urls: list[str]) -> dict[str, list[str]]:
    """Raggruppa URL per pattern strutturale di path, ritorna {pattern: [urls]}."""
    groups: dict[str, list[str]] = defaultdict(list)
    for u in urls:
        groups[_url_to_pattern(u)].append(u)
    # ordina per dimensione del gruppo decrescente (i pattern più frequenti prima)
    return dict(sorted(groups.items(), key=lambda kv: -len(kv[1])))


def _extract_links(html: str, base_url: str, same_origin_only: bool = True) -> list[str]:
    """Estrae tutti i link <a href> assoluti dalla pagina."""
    base_host = urlparse(base_url).hostname or ""
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
            host = urlparse(absolute).hostname or ""
            if host != base_host and not host.endswith("." + base_host):
                continue
        if absolute not in out:
            out.append(absolute)
    return out


async def _auto_detect_pattern_via_llm(
    client: httpx.AsyncClient,
    base_url: str,
    api_key: str,
    model: str,
    seed_url: str,
    sample_links: list[str],
    schema_text: str,
) -> str | None:
    """Chiede all'LLM quale pattern di URL contiene le pagine target."""
    groups = _group_urls_by_pattern(sample_links)
    # Mostriamo i primi 12 pattern (con ≥2 URL ciascuno) e qualche esempio per gruppo
    summary_lines = []
    shown = 0
    for pat, urls in groups.items():
        if len(urls) < 2 and shown >= 6:
            continue
        examples = urls[:2]
        summary_lines.append(f'  "{pat}"  → {len(urls)} URL  (es. {", ".join(examples)})')
        shown += 1
        if shown >= 12:
            break

    user_prompt = (
        f"Ho fatto fetch di un seed URL: {seed_url}\n\n"
        f"Trovati questi pattern di URL nella pagina:\n"
        + "\n".join(summary_lines)
        + "\n\n"
        f"Devo estrarre dati che corrispondono a questo schema:\n{schema_text[:1500]}\n\n"
        "QUALE pattern URL contiene le pagine-DETTAGLIO che corrispondono allo schema "
        "(le pagine da cui estrarre i dati richiesti)?\n\n"
        "Rispondi in JSON con questa forma ESATTA:\n"
        '{"regex": "<regex Python che matcha il path delle pagine target>", '
        '"reason": "<una frase breve>"}\n\n'
        'La regex deve matchare il PATH (es. "^/catalogue/[^/]+/index\\\\.html$"), '
        "non l'URL completo. Solo JSON, niente prosa."
    )

    payload = {
        "model": model,
        "messages": [
            {
                "role": "system",
                "content": (
                    "Sei un classificatore di URL. Identifichi quale pattern di "
                    "path contiene le pagine-dettaglio in un sito web."
                ),
            },
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0.0,
        "max_tokens": 200,
    }
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
        return None

    # parse JSON o fallback regex extraction
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError:
        m = re.search(r"\{[\s\S]+?\}", raw)
        if not m:
            return None
        try:
            obj = json.loads(m.group(0))
        except json.JSONDecodeError:
            return None

    regex_str = obj.get("regex") if isinstance(obj, dict) else None
    if not regex_str:
        return None
    # validate regex compila
    try:
        re.compile(regex_str)
    except re.error:
        log.warning("auto-detected regex non compila: %r", regex_str)
        return None
    return regex_str


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
                # Match contro il path
                path = urlparse(link).path or "/"
                if pattern_re.search(path):
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
        "max_tokens": 800,
        # response_format funziona su OpenAI (json_object) e su Ollama OpenAI-compat (>=0.4)
        "response_format": {"type": "json_object"},
    }
    # Per Ollama: parametro nativo `format=json` (forza output JSON puro, niente prosa)
    if "11434" in base_url or "/v1" in base_url and "openai.com" not in base_url and "anthropic.com" not in base_url and "x.ai" not in base_url:
        payload["format"] = "json"

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
    def jlog(line: str) -> None:
        db.append_job_log(job_id, line)

    db.update_job(job_id, status="running", started_at=db.now_iso())
    db.set_control_signal(job_id, None)
    jlog(
        f"Avvio bulk_extract per task #{task['id']} \"{task['name']}\" "
        f"— modello {task['model']}"
    )

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
    if crawler_on and filtered:
        limiter_for_crawl = PerHostRateLimiter(rate_per_sec)
        async with httpx.AsyncClient(
            headers={"User-Agent": settings.http_user_agent},
            follow_redirects=True,
        ) as crawl_client:
            # Auto-detect pattern se non specificato dall'utente
            effective_pattern = crawler_pattern
            if not effective_pattern:
                jlog("  crawler: auto-detect pattern via LLM (1 chiamata)...")
                # Fai un fetch del primo seed per ottenere link sample
                try:
                    seed0 = filtered[0]
                    r0 = await crawl_client.get(seed0, timeout=20)
                    sample_links = _extract_links(r0.text, seed0, same_origin_only=True)
                    if sample_links:
                        effective_pattern = await _auto_detect_pattern_via_llm(
                            crawl_client, discovery_base_url, discovery_api_key,
                            discovery_model, seed0, sample_links, schema_text,
                        )
                        if effective_pattern:
                            jlog(f"  ✅ pattern auto-detected: {effective_pattern!r}")
                        else:
                            jlog("  ⚠️ auto-detect fallito: il crawler verrà saltato")
                    else:
                        jlog("  ⚠️ seed non ha link interni: crawler saltato")
                except Exception as e:
                    jlog(f"  ⚠️ errore fetching seed per auto-detect: {e}")
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

    async def process_one(client: httpx.AsyncClient, url: str) -> None:
        nonlocal n_ok, n_failed, n_done, stopped
        # Stop check cooperativo
        if db.get_control_signal(job_id) == "stop":
            stopped = True
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
    from .runner_browseruse import _ingest_to_contacts
    n_ingested = _ingest_to_contacts(profiles_path, task["id"], job_id, jlog)

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
