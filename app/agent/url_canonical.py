"""URL canonicalization + filtri agnostici al dominio.

Tre primitive riusabili da TUTTI i runner (bulk_extract, site_explorer,
browser_use) e dal DB layer per dedup:

- `canonical_url(url)`: forma canonica per detection duplicati cross-lingua/paginazione
- `looks_like_service_path(url)`: True se URL e' una pagina di sistema (privacy, faq, ecc.)
- costanti pubbliche: SERVICE_PATH_TOKENS, LANGUAGE_CODES, LANG_QUERY_PARAMS

Tutto agnostico al sito: nessun match hardcoded su domini specifici.
"""
from __future__ import annotations

import re
from urllib.parse import urlparse, urlencode, parse_qsl, urlunparse


# === F1 — Service-path markers ===
# Keyword nel path che indicano pagine di sistema. Match come token (segmenti
# completi del path), non sub-string permissiva.
SERVICE_PATH_TOKENS: frozenset[str] = frozenset({
    # Legali / compliance
    "privacy", "privacy-policy", "terms", "tos", "termini", "condizioni",
    "gdpr", "cookie", "cookies", "cookie-policy", "legal", "disclaimer",
    "licenze", "license", "2257",
    # Customer service
    "assistenza", "support", "help", "faq", "faqs", "faqutenti",
    "contattaci", "contact", "contact-us", "contatti",
    # Aziendali
    "chi-siamo", "about", "about-us", "aboutus", "team", "careers",
    "lavora-con-noi", "work-with-us", "press", "stampa", "media",
    # Tecniche
    "sitemap", "accessibility", "accessibilita", "robots",
    # Account / auth (varianti con underscore + trattino)
    "account", "myaccount", "my-account", "my_account",
    "login", "log-in", "log_in", "signin", "sign-in", "sign_in",
    "register", "signup", "sign-up", "sign_up", "logout", "log_out", "log-out",
    "password", "password-reset", "password_reset", "reset-password", "reset_password",
    "forgot-password", "forgot_password",
    "area-cliente", "areacliente", "area-personale", "area-utente",
    "area-camgirl", "areacamgirl", "diventaunacamgirl", "diventa-",
    # E-commerce service
    "checkout", "cart", "carrello", "wishlist", "lista-desideri",
    "primoacquisto", "ordine", "ordini", "fatturazione",
})

# === F2 — URL canonicalization helpers ===

# Codici ISO 639-1 di lingua (per dedup di /it/, /en/, ecc.)
LANGUAGE_CODES: frozenset[str] = frozenset({
    "it", "en", "es", "fr", "de", "pt", "ja", "zh", "ru", "ar", "ko",
    "nl", "pl", "tr", "sv", "da", "no", "fi", "cs", "sk", "hu", "ro",
    "bg", "el", "he", "vi", "th", "id", "ms", "hi", "uk", "ca", "ga",
    "et", "lv", "lt", "sl", "hr", "sr", "mk", "is", "fa", "ur", "bn",
})

# Query parameters di lingua/locale strip-pati per dedup
LANG_QUERY_PARAMS: frozenset[str] = frozenset({
    "setlang", "lang", "language", "locale", "hl", "l",
})

# Query parameters di paginazione "pagina 1" strip-pati per dedup
PAGE_ZERO_QUERY: frozenset[str] = frozenset({
    "p", "page", "offset", "start", "from",
})

# Tracking/analytics params: stessa risorsa, parametri puramente di attribuzione.
# IG: igsh (deep-link share). Meta: fbclid. Google: gclid, gbraid, wbraid.
# Mailchimp: mc_cid, mc_eid. Adobe: cid, icid. UTM family.
TRACKING_QUERY_PREFIXES: tuple[str, ...] = ("utm_", "mc_")
TRACKING_QUERY_KEYS: frozenset[str] = frozenset({
    "fbclid", "gclid", "gbraid", "wbraid", "igsh", "igshid",
    "cid", "icid", "ref", "ref_src", "ref_url", "_ga",
})


def looks_like_service_path(url: str) -> bool:
    """True se l'URL ha un segmento di path che matcha un service-token noto.

    Match: per ogni segmento del path (rimossa estensione `.html`/`.php`/`.aspx`/...),
    check se il segmento e' nei SERVICE_PATH_TOKENS o se inizia con un token + `-`
    (es. "diventa-camgirl" matcha "diventa-").

    Generalista: applicabile a qualunque sito web.
    """
    if not url:
        return False
    try:
        path = urlparse(url).path or ""
    except Exception:
        return False
    if not path:
        return False
    segments = [s for s in path.split("/") if s]
    for seg in segments:
        bare = re.sub(r"\.(html?|php|aspx?|jsp|do)$", "", seg, flags=re.IGNORECASE).lower()
        if not bare:
            continue
        if bare in SERVICE_PATH_TOKENS:
            return True
        for tok in SERVICE_PATH_TOKENS:
            if tok.endswith("-") and bare.startswith(tok):
                return True
    return False


def canonical_url(url: str) -> str:
    """Forma canonica di un URL per detection duplicati cross-lingua/paginazione-zero.

    Trasformazioni applicate:
      1. Strip locale prefix (`/it/`, `/en/`, ...) se primo segmento e' codice ISO 639
      2. Strip query params di lingua (setlang, lang, locale, hl, l)
      3. Strip query params di paginazione "pagina 1" (?p=0, ?p=1, ?page=0, ...)
      4. Strip trailing slash + fragment, lowercase host

    Esempi:
      `https://x.com/it/profilo/123/?setlang=it`  -> `https://x.com/profilo/123`
      `https://x.com/en/profilo/123/`             -> `https://x.com/profilo/123`
      `https://x.com/profilo/123/?p=0`            -> `https://x.com/profilo/123`
      `https://x.com/profilo/123/?lang=fr&p=2`    -> `https://x.com/profilo/123?p=2`
    """
    if not url:
        return url
    try:
        parsed = urlparse(url)
    except Exception:
        return url

    # 1. Strip locale prefix dal path
    path = parsed.path or "/"
    segments = path.split("/", 2)  # ['', 'it', 'rest...']
    if len(segments) >= 3 and segments[1].lower() in LANGUAGE_CODES:
        path = "/" + segments[2]
    elif len(segments) == 2 and segments[1].lower() in LANGUAGE_CODES:
        path = "/"

    # 2. Strip query params di lingua + paginazione-zero + tracking/analytics
    new_query_pairs = []
    for k, v in parse_qsl(parsed.query, keep_blank_values=False):
        k_l = k.lower()
        if k_l in LANG_QUERY_PARAMS:
            continue
        if k_l in TRACKING_QUERY_KEYS:
            continue
        if any(k_l.startswith(pref) for pref in TRACKING_QUERY_PREFIXES):
            continue
        if k_l in PAGE_ZERO_QUERY:
            try:
                if int(v) <= 1:
                    continue
            except (ValueError, TypeError):
                pass
        new_query_pairs.append((k, v))
    new_query = urlencode(new_query_pairs)

    # 3. Strip trailing slash inconsistencies
    if path != "/" and path.endswith("/"):
        path = path.rstrip("/")
    elif path == "":
        path = "/"

    return urlunparse((
        parsed.scheme.lower(),
        parsed.netloc.lower(),
        path,
        "",
        new_query,
        "",
    ))
