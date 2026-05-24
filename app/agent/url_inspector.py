"""Probe pre-task su una URL: HTTP HEAD + signal extraction.

Obiettivo: PRIMA di creare un task scraping, l'orchestrator chiama
`inspect_url(url)` per sapere se il sito e' accessibile e quale strategia
ha senso (HTTP statico vs browser headed vs skip).

Detection signals:
  - HTTP status: 200 OK / 4xx blocked / 5xx broken
  - Cloudflare: header `server: cloudflare`, cookie `__cf_bm`, `cf-ray`
  - DataDome: cookie `datadome`, header `x-dd-...`
  - Akamai: header `server: AkamaiGHost`, cookie `_abck`
  - Generic anti-bot: `x-amzn-...`, `x-served-by: cache-`, content="Just a moment..."
  - JS-only / SPA: content type HTML ma response body < 5KB con `<noscript>` o `<div id="root"></div>`
  - Login wall: redirect a /login, /signin
  - Geo-block: 451, oppure header `cf-policy-status: blocked`

NOTE su perf: HEAD e' veloce (50-300ms). Se HEAD non e' supportato (alcuni
server ritornano 405), fallback a GET con `stream=True` + range header (no
download body intero).
"""
from __future__ import annotations

import logging
from typing import Any
from urllib.parse import urlparse

import httpx


log = logging.getLogger(__name__)


# Heuristics: stringhe che, se presenti negli header response, segnalano un
# determinato sistema di protezione. Ordine importante: la prima match vince
# (es. Cloudflare e' piu' frequente, va prima di Akamai).
_PROTECTION_SIGNATURES: list[tuple[str, list[tuple[str, str]]]] = [
    ("cloudflare", [
        ("server", "cloudflare"),
        ("cf-ray", ""),       # qualunque cf-ray
        ("set-cookie", "__cf_bm"),
        ("set-cookie", "cf_clearance"),
    ]),
    ("datadome", [
        ("set-cookie", "datadome"),
        ("x-dd-debug", ""),
    ]),
    ("akamai", [
        ("server", "akamaighost"),
        ("set-cookie", "_abck"),
        ("set-cookie", "bm_sz"),
    ]),
    ("perimeterx", [
        ("set-cookie", "_px"),
        ("server", "perimeterx"),
    ]),
    ("imperva_incapsula", [
        ("set-cookie", "visid_incap"),
        ("set-cookie", "incap_ses"),
        ("x-cdn", "incapsula"),
    ]),
    ("aws_waf", [
        ("server", "awselb"),
        ("x-amzn-trace-id", ""),
    ]),
    ("fastly", [
        ("server", "fastly"),
        ("x-served-by", "cache-"),
    ]),
]


def _detect_protection(headers: dict[str, str]) -> str | None:
    """Header-based detection: ritorna il nome del sistema di protezione o
    None se nessuno trovato. Header keys case-insensitive."""
    if not headers:
        return None
    h_lower = {k.lower(): str(v).lower() for k, v in headers.items()}
    for name, sigs in _PROTECTION_SIGNATURES:
        for key, needle in sigs:
            val = h_lower.get(key.lower())
            if val is None:
                continue
            if needle == "":
                return name  # presenza del header basta
            if needle.lower() in val:
                return name
    return None


def _recommend_strategy(
    status_code: int,
    protection: str | None,
    body_short: bool,
) -> dict[str, Any]:
    """Heuristic di strategia consigliata in base ai segnali.

    Ritorna un dict con `strategy` (str) + `reason` (str) + `severity`
    (`ok`|`warning`|`block`)."""
    # Caso 1: HTTP error grave (non recuperabile)
    if status_code in (404, 410):
        return {
            "strategy": "skip",
            "severity": "block",
            "reason": (
                f"HTTP {status_code}: la pagina non esiste o e' stata "
                "rimossa. Verifica l'URL o usa un seed diverso."
            ),
        }
    if 500 <= status_code < 600:
        return {
            "strategy": "skip",
            "severity": "block",
            "reason": (
                f"HTTP {status_code}: server error. Riprova piu' tardi o "
                "verifica che il sito sia online."
            ),
        }

    # Caso 2: anti-bot avanzato (Cloudflare/DataDome/Akamai) — anche se
    # status 200, browser headless ha alta probabilita' di DOM vuoto
    HEAVY_PROTECTIONS = {"cloudflare", "datadome", "akamai", "perimeterx", "imperva_incapsula"}
    if protection in HEAVY_PROTECTIONS:
        # Cloudflare basic spesso si bypassa con curl_cffi TLS impersonation,
        # ma il rischio resta alto. Suggerisci browser_use come fallback.
        if status_code == 200:
            return {
                "strategy": "browser_use_with_caution",
                "severity": "warning",
                "reason": (
                    f"Status 200 ma rilevato {protection}. Il sito risponde "
                    "ora ma potrebbe bloccare il browser headless. "
                    "Consigliato: site_explorer (HTTP+readability, gia' ha "
                    "TLS impersonation Chrome120) per primi tentativi; se "
                    "fallisce, browser_use con stealth. Considera proxy "
                    "residenziali se il sito ha storia di blocchi."
                ),
            }
        return {
            "strategy": "skip_or_proxy",
            "severity": "block",
            "reason": (
                f"HTTP {status_code} + {protection}: sito bloccato. Servono "
                "proxy residenziali + browser headed con stealth maxed. "
                "Non scrapabile dall'infrastruttura attuale di Argos."
            ),
        }

    # Caso 3: 403 / 401 / 429 senza anti-bot riconosciuto
    if status_code in (401, 403):
        return {
            "strategy": "browser_use",
            "severity": "warning",
            "reason": (
                f"HTTP {status_code}: blocco a livello applicativo (User-Agent "
                "blacklist o login wall?). browser_use con Playwright potrebbe "
                "bypassare l'errore se il blocco e' header-based. Se richiede "
                "login non e' scrapabile."
            ),
        }
    if status_code == 429:
        return {
            "strategy": "skip_for_now",
            "severity": "warning",
            "reason": (
                "HTTP 429: rate-limit attivo. Aspetta 1-24h prima di riprovare, "
                "oppure usa un proxy diverso."
            ),
        }

    # Caso 4: 200 OK con body sospettosamente corto → potrebbe essere SPA
    if status_code == 200 and body_short:
        return {
            "strategy": "browser_use",
            "severity": "warning",
            "reason": (
                "Status 200 ma il body iniziale e' molto corto (<3KB). "
                "Potrebbe essere una SPA che renderizza via JS post-load: "
                "browser_use con Playwright e' necessario per vedere i dati."
            ),
        }

    # Caso 5: 200 OK pulito, protezione leggera o assente → HTTP statico OK
    if status_code == 200:
        return {
            "strategy": "bulk_extract_or_site_explorer",
            "severity": "ok",
            "reason": (
                "Sito accessibile via HTTP statico, nessun anti-bot pesante. "
                "Usa bulk_extract se conosci il pattern URL, site_explorer "
                "se serve discovery (paginazione/categorie)."
            ),
        }

    # Caso fallback
    return {
        "strategy": "site_explorer",
        "severity": "warning",
        "reason": f"Risposta inaspettata (HTTP {status_code}). Procedi con cautela.",
    }


async def inspect_url(url: str, *, timeout: float = 10.0) -> dict[str, Any]:
    """Probe HEAD veloce su una URL per detection accessibilita' + protezioni.

    Ritorna un dict standard:
      {
        "url": str,
        "final_url": str,        # dopo redirect
        "status_code": int,
        "accessible": bool,      # True se 2xx-3xx
        "protection": str|None,  # cloudflare|datadome|... |None
        "redirected": bool,
        "redirect_target": str|None,
        "headers_sample": dict,  # solo chiavi rilevanti per audit
        "recommended_strategy": str,  # skip|skip_or_proxy|browser_use|site_explorer|bulk_extract_or_site_explorer
        "severity": str,         # ok|warning|block
        "reason": str,
        "error": str|None,
      }

    Usa curl_cffi se disponibile (TLS impersonation Chrome120 bypassa
    Cloudflare basic), fallback httpx standard.
    """
    parsed = urlparse(url)
    if not parsed.scheme or not parsed.netloc:
        return {
            "url": url, "final_url": url, "status_code": 0,
            "accessible": False, "protection": None,
            "redirected": False, "redirect_target": None,
            "headers_sample": {},
            "recommended_strategy": "skip",
            "severity": "block",
            "reason": "URL malformato (manca scheme o host).",
            "error": "invalid_url",
        }

    # Preferisci curl_cffi (TLS impersonation) se disponibile
    use_curl_cffi = False
    try:
        from curl_cffi import requests as cf_requests
        use_curl_cffi = True
    except ImportError:
        cf_requests = None  # type: ignore

    try:
        if use_curl_cffi:
            # HEAD spesso non e' supportato da siti SPA; usiamo GET con timeout
            # corto + stream per limitare il body letto.
            r = cf_requests.get(
                url, impersonate="chrome120",
                timeout=timeout, allow_redirects=True,
                stream=True,
            )
            status = int(r.status_code)
            headers = dict(r.headers)
            # Leggi solo i primi N byte per detection SPA
            body_chunk = b""
            try:
                for chunk in r.iter_content(chunk_size=4096):
                    body_chunk += chunk
                    if len(body_chunk) >= 4096:
                        break
            except Exception:
                pass
            r.close()
            final_url = str(r.url)
        else:
            async with httpx.AsyncClient(
                timeout=timeout, follow_redirects=True,
                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"},
            ) as client:
                r = await client.get(url)
                status = r.status_code
                headers = dict(r.headers)
                body_chunk = r.content[:4096] if r.content else b""
                final_url = str(r.url)
    except Exception as e:
        return {
            "url": url, "final_url": url, "status_code": 0,
            "accessible": False, "protection": None,
            "redirected": False, "redirect_target": None,
            "headers_sample": {},
            "recommended_strategy": "skip",
            "severity": "block",
            "reason": f"Errore di connessione: {type(e).__name__}: {e}",
            "error": type(e).__name__,
        }

    protection = _detect_protection(headers)
    body_short = len(body_chunk) < 3000

    redirected = final_url != url and final_url != url + "/"
    redirect_target = final_url if redirected else None

    # Esponi solo header rilevanti (no body) per audit
    relevant_headers = {
        k: v for k, v in headers.items()
        if k.lower() in {
            "server", "cf-ray", "x-dd-debug", "x-cdn", "x-served-by",
            "set-cookie", "content-type", "x-frame-options",
            "x-content-type-options", "content-length",
        }
    }
    # Tronca set-cookie a 200 char per safety
    if "set-cookie" in {k.lower(): v for k, v in relevant_headers.items()}:
        for k in list(relevant_headers.keys()):
            if k.lower() == "set-cookie":
                relevant_headers[k] = str(relevant_headers[k])[:200] + "..."

    rec = _recommend_strategy(status, protection, body_short)
    return {
        "url": url,
        "final_url": final_url,
        "status_code": status,
        "accessible": 200 <= status < 400,
        "protection": protection,
        "redirected": redirected,
        "redirect_target": redirect_target,
        "headers_sample": relevant_headers,
        "recommended_strategy": rec["strategy"],
        "severity": rec["severity"],
        "reason": rec["reason"],
        "error": None,
    }
