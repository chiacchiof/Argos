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
- "bulk_extract": HTML statico, pattern URL chiaro nei link interni, contenuto leggibile direttamente
  dal raw HTML. È la strategia più veloce ed economica. Sceglila quando vedi pattern URL ricorrenti
  con segmenti variabili ({slug}/{int}) e text_to_html_ratio decente (≥0.05).
- "http_llm_guided": HTML statico ma URL senza pattern chiaro (slug random, hash). Serve un crawler
  che ad ogni hop chiede a un LLM "questo link è target?". Costo medio.
- "browser_use": il sito richiede JavaScript per renderizzare il contenuto, oppure i dati sono dietro
  scroll dinamico, click, login, o richiedono visione del layout. Sceglila quando text_to_html_ratio
  è molto bassa (<0.03), o quando ci sono indizi forti di JS-rendering, login obbligatorio, ecc.
- "skip": il sito NON è adatto a fornire i dati richiesti per uno qualsiasi di questi motivi:
  paywall completo, dati dietro registrazione obbligatoria, contenuti irrilevanti rispetto
  all'obiettivo, sito di errore/parking, contenuti illegali o che violano ToS. Non vale la pena
  spendere tempo/$. Indica chiaramente il motivo.

CRITERI:
1. Match obiettivo ↔ contenuto: il title/meta/snippet del sito c'entra con quello che cerca l'utente?
   Se NO → skip.
2. Disponibilità dei dati richiesti: i campi dello schema (es. email, telegram, prezzo, ecc.) sembrano
   pubblicamente visibili nelle pagine di dettaglio? Se sembrano dietro login/registrazione → skip
   o browser_use con caveat esplicito.
3. Tipo di rendering: HTML statico vs JS-heavy. Bassissimo text_to_html_ratio + tanti forms = JS o login.
4. Qualità del pattern URL: presenza di pattern ricorrenti con placeholder {slug}/{int} = bulk_extract.
   Slug fissi (about.html, privacy.html) NON sono target. Slug variabili nei sub-domini sono profili.

Rispondi in JSON ESATTO:
{
  "strategy": "bulk_extract" | "browser_use" | "http_llm_guided" | "skip",
  "promising": "yes" | "maybe" | "no",
  "reason": "<2-3 frasi in italiano: COSA vedi e PERCHÉ scegli quella strategia>",
  "target_hint": "<eventuale pattern host+path con placeholder, oppure stringa vuota>",
  "expected_yield": <int stimato di profili/dati ottenibili, 0 se skip>
}
Solo JSON, niente prosa."""


_VALID_STRATEGIES = {"bulk_extract", "browser_use", "http_llm_guided", "skip"}
_VALID_PROMISING = {"yes", "maybe", "no"}


async def profile_site(
    url: str,
    objective: str,
    schema_text: str,
    llm_base_url: str,
    llm_api_key: str,
    llm_model: str,
    user_agent: str = "AgentScraper/1.0",
    timeout: int = 30,
) -> dict[str, Any]:
    """Classifica un sito e suggerisce la strategia di scraping ottimale.

    Ritorna sempre un dict con almeno {url, strategy, promising, reason, signals}.
    Se l'errore è bloccante (sito down, LLM giù), strategy='skip' con error spiegato.
    """
    result: dict[str, Any] = {
        "url": url,
        "final_url": url,
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

    # 1. Fetch home
    try:
        async with httpx.AsyncClient(
            headers={"User-Agent": user_agent},
            follow_redirects=True,
            timeout=timeout,
        ) as client:
            r = await client.get(url)
            result["http_status"] = r.status_code
            result["final_url"] = str(r.url)
            if r.status_code >= 400:
                result["error"] = f"HTTP {r.status_code}"
                result["reason"] = f"Sito non raggiungibile (HTTP {r.status_code})"
                return result
            html = r.text
    except Exception as e:
        result["error"] = f"{type(e).__name__}: {e}"
        result["reason"] = f"Errore di connessione: {e}"
        return result

    # 2. Detect signals (deterministico)
    signals = _detect_signals(html, url)
    result["signals"] = signals

    # 3. LLM classification
    user_prompt_parts = [
        f"URL analizzato: {url}",
        f"OBIETTIVO UTENTE (italiano): {(objective or '').strip()[:600] or '(non specificato)'}",
        "",
        f"SCHEMA dei dati richiesti:\n{(schema_text or '').strip()[:1000] or '(nessuno)'}",
        "",
        "SEGNALI OGGETTIVI dalla home:",
        f"  - title: {signals.get('title')!r}",
        f"  - meta_description: {signals.get('meta_description')!r}",
        f"  - lang: {signals.get('lang')!r}",
        f"  - html_size: {signals['html_size']} byte",
        f"  - text_size: {signals['text_size']} byte (testo dopo strip HTML)",
        f"  - text_to_html_ratio: {signals['text_to_html_ratio']}  "
        "(0=JS-heavy/template, 0.05+=HTML con contenuto, 0.3+=ricco di testo)",
        f"  - n_internal_links: {signals['n_internal_links']}",
        f"  - n_external_links: {signals['n_external_links']}",
        f"  - n_forms: {signals['n_forms']}, has_login_form: {signals['has_login_form']}",
        f"  - has_signup_keywords: {signals['has_signup_keywords']}",
    ]
    if signals["top_link_patterns"]:
        user_prompt_parts.append("\nTOP PATTERN URL nei link interni (host+path con placeholder):")
        for p in signals["top_link_patterns"]:
            ex = p["examples"][0] if p["examples"] else ""
            user_prompt_parts.append(f"  • {p['pattern']}  ({p['count']} URL, es. {ex})")
    if signals["main_text_snippet"]:
        user_prompt_parts.append(
            f"\nESTRATTO TESTO HOME (primi 2000 char):\n{signals['main_text_snippet']}"
        )

    user_prompt = "\n".join(user_prompt_parts)

    payload = {
        "model": llm_model,
        "messages": [
            {"role": "system", "content": PROFILER_SYSTEM},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0.0,
        "max_tokens": 500,
        "response_format": {"type": "json_object"},
    }

    try:
        async with httpx.AsyncClient(timeout=120) as client:
            r = await client.post(
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

    # parse JSON (con fallback su greedy {...})
    obj: Any = None
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError:
        m = re.search(r"\{[\s\S]+\}", raw)
        if m:
            try:
                obj = json.loads(m.group(0))
            except json.JSONDecodeError:
                obj = None

    if not isinstance(obj, dict):
        result["error"] = "LLM JSON non parseable"
        result["reason"] = f"Risposta LLM malformata: {raw[:200]!r}"
        return result

    s = obj.get("strategy")
    p = obj.get("promising")
    result["strategy"] = s if s in _VALID_STRATEGIES else "skip"
    result["promising"] = p if p in _VALID_PROMISING else "no"
    result["reason"] = (obj.get("reason") or "").strip()[:500]
    result["target_hint"] = (obj.get("target_hint") or "").strip()[:200]
    try:
        result["expected_yield"] = int(obj.get("expected_yield") or 0)
    except (TypeError, ValueError):
        result["expected_yield"] = 0

    return result
