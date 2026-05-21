"""Site Profiler — pre-analisi di un sito per scegliere la strategia di scraping.

Per ogni URL della lista del task, fa:
  1. fetch HTTP della home (1 chiamata, ~1-3s)
  2. estrazione di "signals" deterministiche dall'HTML (size, link patterns,
     login forms, JS-heaviness, lingua, ecc.) — niente LLM
  3. una chiamata LLM "capable" (gpt-4o-mini-class) che riceve signals +
     objective utente + schema target e ritorna:
       {strategy, promising, reason, target_hint, expected_yield}

Le strategy sono:
  - "bulk_extract"      → HTML statico + pattern URL chiaro (caso ottimo)
  - "browser_use"       → JS-heavy o login richiesto: serve Playwright
  - "http_llm_guided"   → HTML statico ma URL irregolari: crawler LLM-guided
  - "skip"              → sito non promettente per l'obiettivo (paywall, off-topic)

Il profiler NON estrae dati: classifica e basta. Il dispatcher (in fase
successiva) leggerà l'output e lancerà il runner appropriato.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx
from selectolax.parser import HTMLParser

from .ollama import maybe_add_keep_alive
from .runner_bulk_extract import (
    _group_urls_by_pattern,
    _registrable_domain,
)


log = logging.getLogger(__name__)


_SIGNUP_KEYWORDS = (
    "iscriviti", "registrati", "iscrizione", "registrazione",
    "sign up", "signup", "register", "log in", "login", "accedi",
    "area riservata", "private area", "members only",
    "create account", "join now", "get started",
    "abbonati", "abbonamento", "premium",
)


def _detect_signals(html: str, base_url: str) -> dict[str, Any]:
    """Estrae signals deterministiche dalla home page (zero LLM)."""
    signals: dict[str, Any] = {
        "html_size": len(html),
        "text_size": 0,
        "text_to_html_ratio": 0.0,
        "title": None,
        "meta_description": None,
        "lang": None,
        "n_internal_links": 0,
        "n_external_links": 0,
        "n_forms": 0,
        "has_login_form": False,
        "has_signup_keywords": False,
        "top_link_patterns": [],
        "main_text_snippet": "",
    }
    try:
        tree = HTMLParser(html)
    except Exception:
        return signals

    # title
    t = tree.css_first("title")
    if t:
        signals["title"] = (t.text() or "").strip()[:200]
    # meta description
    md = tree.css_first("meta[name='description']") or tree.css_first("meta[name=description]")
    if md:
        signals["meta_description"] = (md.attributes.get("content") or "").strip()[:300]
    # html lang
    h = tree.css_first("html")
    if h:
        lang = (h.attributes.get("lang") or "").strip().lower()
        signals["lang"] = lang or None

    # text size + ratio
    body = tree.body
    text = (body.text(separator=" ", strip=True) if body else "")[:50000]
    signals["text_size"] = len(text)
    signals["text_to_html_ratio"] = round(
        signals["text_size"] / max(1, signals["html_size"]), 3
    )
    signals["main_text_snippet"] = text[:2000]

    # links classification
    base_host = (urlparse(base_url).hostname or "").lower()
    base_reg = _registrable_domain(base_host)
    internal_links: list[str] = []
    seen_int: set[str] = set()
    n_external = 0
    for a in tree.css("a[href]"):
        href = (a.attributes.get("href") or "").strip()
        if not href or href.startswith(("javascript:", "mailto:", "tel:", "#")):
            continue
        absolute = urljoin(base_url, href).split("#")[0]
        host = (urlparse(absolute).hostname or "").lower()
        if not host:
            continue
        if _registrable_domain(host) == base_reg:
            if absolute not in seen_int:
                seen_int.add(absolute)
                internal_links.append(absolute)
        else:
            n_external += 1
    signals["n_internal_links"] = len(internal_links)
    signals["n_external_links"] = n_external

    if internal_links:
        groups = _group_urls_by_pattern(internal_links[:500])
        signals["top_link_patterns"] = [
            {"pattern": pat, "count": len(urls), "examples": urls[:3]}
            for pat, urls in list(groups.items())[:8]
        ]
        # Segnale derivato: c'e' un pattern ricorrente "tipo target" (con placeholder
        # {slug}/{int}) con almeno 10 URL? Se si', il sito ha link diretti a pagine
        # dettaglio ed e' candidato naturale a bulk_extract anche con text_ratio basso.
        recurring = False
        top_count = 0
        for pat, urls in groups.items():
            if len(urls) > top_count:
                top_count = len(urls)
            if len(urls) >= 10 and ("{slug}" in pat or "{int}" in pat):
                recurring = True
                break
        signals["has_recurring_target_pattern"] = recurring
        signals["top_pattern_count"] = top_count

    # forms
    forms = tree.css("form")
    signals["n_forms"] = len(forms)
    signals["has_login_form"] = any(f.css_first("input[type=password]") for f in forms)

    # signup keywords (case-insensitive su body text)
    body_text_lower = text.lower()
    signals["has_signup_keywords"] = any(kw in body_text_lower for kw in _SIGNUP_KEYWORDS)

    return signals


PROFILER_SYSTEM = """Sei un agente di pre-analisi web. Ricevi la home page di un sito + l'obiettivo \
dell'utente + lo schema dei dati richiesti. Devi scegliere la strategia di scraping migliore.

STRATEGIE DISPONIBILI:
- "bulk_extract": HTML statico, contenuto leggibile direttamente dal raw HTML. È la strategia
  più veloce ed economica. Sceglila quando vedi un pattern ricorrente CHIARO con placeholder
  {slug}/{int} che porta direttamente alle pagine target (≥10 URL del pattern nel sample del seed).
- "site_explorer": agente ReAct intelligente. Apre la home, capisce la struttura, naviga
  step-by-step verso la sezione giusta (es. /vendita/<citta>/), poi estrae i target. Sceglilo
  quando: il sito ha contenuto HTML statico decente MA i target NON sono linkati direttamente
  dalla home (sono dentro listing o sub-categorie), oppure il pattern URL non è ovvio. Costo
  medio (~$0.05-0.20/sito), molto più affidabile di bulk_extract su siti con struttura complessa.
- "browser_use": il sito richiede JavaScript per renderizzare il contenuto, oppure i dati sono dietro
  scroll dinamico, click, login, o richiedono visione del layout. Sceglila quando text_to_html_ratio
  è molto bassa (<0.03), o quando ci sono indizi forti di JS-rendering, login obbligatorio. Lento
  e costoso: usalo come ULTIMA scelta.
- "skip": il sito NON è adatto a fornire i dati richiesti per uno qualsiasi di questi motivi:
  paywall completo, dati dietro registrazione obbligatoria, contenuti irrilevanti rispetto
  all'obiettivo, sito di errore/parking, contenuti illegali o che violano ToS.

CRITERI di scelta (in ordine):
1. Match obiettivo ↔ contenuto: il title/meta/snippet c'entra? Se NO → skip.
2. JS-rendering vero (text_to_html_ratio<0.03 E body quasi vuoto E nessun pattern ricorrente)
   → browser_use.
3. Pattern target CHIARO sulla home (`has_recurring_target_pattern=True` E top_pattern_count≥10
   E URL del pattern includono keyword target tipo /annuncio/, /product/, /profilo/) → bulk_extract.
4. Tutti gli altri casi con HTML statico (text_to_html_ratio ≥0.05) ma struttura "navigabile"
   (sezioni /vendita-case/<citta>/, /categoria/<x>/, sub-domini come slug, ecc.) → **site_explorer**.
5. site_explorer è anche la scelta giusta quando il pattern target non è ovvio dalla home ma il
   sito ha contenuti pubblici accessibili: l'agente naviga e li trova.
6. Slug fissi (about.html, privacy.html, /chi-siamo/) NON sono target; servono solo a navigare.

QUANDO SCEGLIERE site_explorer (default per siti "non banali"):
- text_to_html_ratio decente (≥0.05) ma pattern target non chiaro dalla home.
- Sito con sub-domini come slug per profilo (`<modella>.example.com/`).
- Sito con multi-livello di listing (categoria → sotto-categoria → annuncio).
- Sito grande dove bulk_extract potrebbe perdere il pattern.

QUANDO SCEGLIERE bulk_extract:
- Pattern ricorrente CHIARO sul sample del seed (≥10 URL coerenti con il template).
- Sito flat o catalogo con listing diretto.

QUANDO SCEGLIERE browser_use:
- Solo se text_to_html_ratio<0.03 + body quasi vuoto in raw HTML (vero JS-render).
- È costoso e lento: meglio site_explorer come prima scelta su HTML decente.

ESPLORAZIONE PRELIMINARE (opzionale, max 2 volte):
Se ti serve aprire un URL specifico per capire meglio la struttura prima di decidere
(es. la home suggerisce che la directory vera sta a `/categories/X`, vuoi controllare),
puoi rispondere con `{"action": "explore", "url": "<URL>"}` invece della decisione finale.
Io fetcherò quel URL e ti richiamerò con i suoi signals. Max 2 esplorazioni totali, poi
DEVI rispondere con la decisione finale.

Esempio (esplorazione):
  {"action": "explore", "url": "https://example.com/all-products"}

Esempio (decisione finale):
Rispondi in JSON ESATTO:
{
  "strategy": "bulk_extract" | "site_explorer" | "browser_use" | "skip",
  "promising": "yes" | "maybe" | "no",
  "reason": "<2-3 frasi in italiano: COSA vedi e PERCHÉ scegli quella strategia>",
  "target_hint": "<eventuale pattern host+path con placeholder, oppure stringa vuota>",
  "expected_yield": <int stimato di profili/dati ottenibili, 0 se skip>
}
Solo JSON, niente prosa."""


_MAX_EXPLORE_ITERATIONS = 2


_VALID_STRATEGIES = {"bulk_extract", "site_explorer", "browser_use", "skip"}
_LEGACY_STRATEGY_ALIASES = {"http_llm_guided": "site_explorer"}
_VALID_PROMISING = {"yes", "maybe", "no"}


async def profile_site(
    url: str,
    objective: str,
    schema_text: str,
    llm_base_url: str,
    llm_api_key: str,
    llm_model: str,
    user_agent: str = "Argos/1.0",
    timeout: int = 30,
) -> dict[str, Any]:
    """Classifica un sito e suggerisce la strategia di scraping ottimale.

    Ritorna sempre un dict con almeno {url, strategy, promising, reason, signals}.
    Se l'errore è bloccante (sito down, LLM giù), strategy='skip' con error spiegato.
    """
    # Normalizzazione URL: aggiungi schema se mancante (es. utente ha scritto
    # "example.it" invece di "https://example.it"). Stessa logica di
    # bulk_extract._normalize_url.
    url_normalized = (url or "").strip()
    if url_normalized and not url_normalized.startswith(("http://", "https://")):
        url_normalized = "https://" + url_normalized

    result: dict[str, Any] = {
        "url": url_normalized,
        "final_url": url_normalized,
        "http_status": None,
        "strategy": "skip",
        "promising": "no",
        "reason": "",
        "target_hint": "",
        "expected_yield": 0,
        "signals": {},
        "error": None,
        "llm_raw": None,
    }

    if not url_normalized:
        result["reason"] = "URL vuoto."
        return result

    # 1. Fetch home — usa HttpFetcher con TLS impersonation (bypassa Cloudflare).
    from .http_fetcher import HttpFetcher
    try:
        async with HttpFetcher(
            user_agent=user_agent,
            follow_redirects=True,
            timeout=timeout,
        ) as client:
            r = await client.get(url_normalized)
            result["http_status"] = r.status_code
            result["final_url"] = str(r.url)
            if r.status_code >= 400:
                # 401/403/429 = anti-bot/auth: anche con TLS impersonation non
                # siamo passati. Probabilmente serve JS challenge (Turnstile) o
                # session cookie. Instrada a browser_use con Playwright.
                if r.status_code in (401, 403, 429):
                    result["strategy"] = "browser_use"
                    result["promising"] = "maybe"
                    result["error"] = f"HTTP {r.status_code}"
                    result["reason"] = (
                        f"HTTP {r.status_code} sul profiler anche con TLS impersonation "
                        f"(anti-bot pesante). Tento con browser_use + Playwright."
                    )
                    return result
                result["error"] = f"HTTP {r.status_code}"
                result["reason"] = f"Sito non raggiungibile (HTTP {r.status_code})"
                return result
            html = r.text
    except Exception as e:
        result["error"] = f"{type(e).__name__}: {e}"
        result["reason"] = f"Errore di connessione: {e}"
        return result

    # 2. Detect signals (deterministico)
    signals = _detect_signals(html, url_normalized)
    result["signals"] = signals

    # 3. LLM classification — loop iterativo con esplorazione opzionale
    messages: list[dict[str, str]] = [
        {"role": "system", "content": PROFILER_SYSTEM},
        {"role": "user", "content": _compose_profiler_user_prompt(
            url_normalized, signals, objective, schema_text,
        )},
    ]
    explored_urls: list[str] = []

    async with httpx.AsyncClient(timeout=120) as llm_client:
        from .http_fetcher import HttpFetcher
        async with HttpFetcher(user_agent=user_agent, timeout=timeout) as fetch_client:
            for iteration in range(_MAX_EXPLORE_ITERATIONS + 1):
                payload: dict[str, Any] = {
                    "model": llm_model,
                    "messages": messages,
                    "temperature": 0.0,
                    "max_tokens": 500,
                    "response_format": {"type": "json_object"},
                }
                maybe_add_keep_alive(payload, llm_base_url)
                try:
                    r = await llm_client.post(
                        f"{llm_base_url.rstrip('/')}/chat/completions",
                        json=payload,
                        headers={"Authorization": f"Bearer {llm_api_key}"},
                    )
                    r.raise_for_status()
                    data = r.json()
                    raw = (
                        data.get("choices", [{}])[0]
                        .get("message", {})
                        .get("content", "")
                        or ""
                    ).strip()
                    result["llm_raw"] = raw[:1000]
                except Exception as e:
                    result["error"] = f"LLM error: {type(e).__name__}: {e}"
                    result["reason"] = f"Errore LLM nel profiling: {e}"
                    return result

                obj = _safe_json_parse(raw)
                if not isinstance(obj, dict):
                    result["error"] = "LLM JSON non parseable"
                    result["reason"] = f"Risposta LLM malformata: {raw[:200]!r}"
                    return result

                # Branch: esplorazione richiesta?
                action = (obj.get("action") or "").strip().lower()
                if action == "explore" and iteration < _MAX_EXPLORE_ITERATIONS:
                    explore_url = (obj.get("url") or "").strip()
                    if not explore_url or not explore_url.startswith(("http://", "https://")):
                        # URL invalido — convincilo a decidere
                        messages.append({"role": "assistant", "content": raw})
                        messages.append({
                            "role": "user",
                            "content": "URL per esplorazione non valido. Decidi ORA con i signals che hai.",
                        })
                        continue
                    if explore_url in explored_urls:
                        messages.append({"role": "assistant", "content": raw})
                        messages.append({
                            "role": "user",
                            "content": "Hai gia' esplorato quell'URL. Decidi ORA.",
                        })
                        continue
                    # Fetch URL secondario
                    try:
                        r2 = await fetch_client.get(explore_url)
                        if r2.status_code >= 400:
                            extra_signals = {
                                "url": explore_url,
                                "http_status": r2.status_code,
                                "error": f"HTTP {r2.status_code}",
                            }
                        else:
                            sub_html = r2.text
                            sub_signals = _detect_signals(sub_html, str(r2.url))
                            sub_signals["http_status"] = r2.status_code
                            sub_signals["final_url"] = str(r2.url)
                            extra_signals = sub_signals
                    except Exception as e:
                        extra_signals = {"url": explore_url, "error": f"{type(e).__name__}: {e}"}
                    explored_urls.append(explore_url)
                    messages.append({"role": "assistant", "content": raw})
                    messages.append({
                        "role": "user",
                        "content": (
                            f"Esplorazione di {explore_url} completata. Signals:\n"
                            f"{_format_signals_compact(extra_signals)}\n\n"
                            f"Ora decidi (esplorazioni rimaste: "
                            f"{_MAX_EXPLORE_ITERATIONS - iteration - 1})."
                        ),
                    })
                    continue

                # Decisione finale
                s = obj.get("strategy")
                if s in _LEGACY_STRATEGY_ALIASES:
                    s = _LEGACY_STRATEGY_ALIASES[s]
                p = obj.get("promising")
                result["strategy"] = s if s in _VALID_STRATEGIES else "skip"
                result["promising"] = p if p in _VALID_PROMISING else "no"
                result["reason"] = (obj.get("reason") or "").strip()[:500]
                result["target_hint"] = (obj.get("target_hint") or "").strip()[:200]
                try:
                    result["expected_yield"] = int(obj.get("expected_yield") or 0)
                except (TypeError, ValueError):
                    result["expected_yield"] = 0
                result["explored_urls"] = explored_urls
                return result

    # Fallback se loop esaurito senza decisione (caso patologico)
    result["error"] = "Profiler exhausted iterations without decision"
    result["reason"] = "LLM ha richiesto esplorazioni oltre il cap senza decidere"
    return result


def _compose_profiler_user_prompt(
    url: str, signals: dict, objective: str | None, schema_text: str | None,
) -> str:
    """Compone il primo user prompt del profiler con signals della pagina."""
    parts = [
        f"URL analizzato: {url}",
        f"OBIETTIVO UTENTE (italiano): {(objective or '').strip()[:600] or '(non specificato)'}",
        "",
        f"SCHEMA dei dati richiesti:\n{(schema_text or '').strip()[:1000] or '(nessuno)'}",
        "",
        "SEGNALI OGGETTIVI dalla home:",
        f"  - title: {signals.get('title')!r}",
        f"  - meta_description: {signals.get('meta_description')!r}",
        f"  - lang: {signals.get('lang')!r}",
        f"  - html_size: {signals.get('html_size')} byte",
        f"  - text_size: {signals.get('text_size')} byte",
        f"  - text_to_html_ratio: {signals.get('text_to_html_ratio')}",
        f"  - n_internal_links: {signals.get('n_internal_links')}",
        f"  - n_external_links: {signals.get('n_external_links')}",
        f"  - n_forms: {signals.get('n_forms')}, has_login_form: {signals.get('has_login_form')}",
        f"  - has_signup_keywords: {signals.get('has_signup_keywords')}",
    ]
    if signals.get("top_link_patterns"):
        parts.append("\nTOP PATTERN URL nei link interni:")
        for p in signals["top_link_patterns"]:
            ex = p["examples"][0] if p["examples"] else ""
            parts.append(f"  • {p['pattern']}  ({p['count']} URL, es. {ex})")
    if signals.get("main_text_snippet"):
        parts.append(f"\nESTRATTO TESTO (primi 2000 char):\n{signals['main_text_snippet']}")
    return "\n".join(parts)


def _format_signals_compact(signals: dict) -> str:
    """Versione compatta dei signals per messaggio di follow-up dopo esplorazione."""
    if signals.get("error"):
        return f"  - status: ERROR ({signals['error']})"
    out = [
        f"  - status: {signals.get('http_status')}",
        f"  - title: {signals.get('title')!r}",
        f"  - text_size: {signals.get('text_size')} byte",
        f"  - text_to_html_ratio: {signals.get('text_to_html_ratio')}",
        f"  - n_internal_links: {signals.get('n_internal_links')}",
    ]
    if signals.get("top_link_patterns"):
        out.append("  - top patterns:")
        for p in signals["top_link_patterns"][:4]:
            ex = p["examples"][0] if p["examples"] else ""
            out.append(f"    • {p['pattern']}  ({p['count']} URL, es. {ex})")
    return "\n".join(out)


def _safe_json_parse(raw: str) -> Any:
    """Parse JSON con fallback su greedy {...} match."""
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        m = re.search(r"\{[\s\S]+\}", raw)
        if m:
            try:
                return json.loads(m.group(0))
            except json.JSONDecodeError:
                pass
    return None
