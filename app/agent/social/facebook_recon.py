"""Facebook recon — selettori + extract_profile_data + search per recon_social.

Selettori basati su DOM FB 2026-Q2. FB li cambia spesso → tenere lista qui
centralizzata, aggiornare al bisogno.

Funzioni esposte:
- `extract_profile_data(page)`: estrae bio + ultimi post + meta visibili
- `search_user_by_name(page, name)`: search FB by nome → ritorna URL profilo
  del primo match plausibile (cognome obbligatorio).
- `collect_subpage_texts(page, safe, profile_url, jlog)`: visita /about e
  /about_contact_and_basic_info e ritorna {label: body_text} per arricchire
  il prompt LLM.
- `load_friend_list(page, safe, jlog)`: naviga la friend list dell'utente
  loggato e ritorna {nome_lowercase: profile_url} (zero ambiguità).
"""
from __future__ import annotations

import asyncio
import logging
import random
import re
from typing import TYPE_CHECKING, Any
from urllib.parse import quote, urlparse, urlunparse

if TYPE_CHECKING:
    from playwright.async_api import Page

log = logging.getLogger(__name__)


# === Selettori FB ===

PROFILE_NAME = 'h1[dir="auto"]'  # nome utente in alto profilo
BIO_INTRO_SECTION = 'div[data-pagelet="ProfileTilesFeed_0"]'  # sezione "Intro"
ABOUT_LINK = 'a[href*="/about"]'

# Post nella timeline
POST_CONTAINERS = [
    'div[role="article"]',
    'div[data-pagelet^="FeedUnit_"]',
    'div[data-ad-comet-preview="message"]',
]

# Testo del post (selectolax o playwright)
POST_TEXT_INNER = 'div[data-ad-comet-preview="message"]'

# "About" page sections
ABOUT_OVERVIEW = '[data-pagelet="ProfileAppSection_0"]'

# Friend list / followers count
FRIENDS_LINK = 'a[href*="/friends"]'

# Modal/dialog di richiesta login (non loggato)
LOGIN_REQUIRED_PROMPT = [
    'div:has-text("Devi accedere a Facebook")',
    'div:has-text("You must log in")',
    'a:has-text("Accedi")',
]


SEARCH_PEOPLE_URL = "https://www.facebook.com/search/people/?q={q}"

# Link a profili nei risultati di ricerca FB.
# Pattern: <a href="https://www.facebook.com/<username>"> oppure
# <a href="https://www.facebook.com/profile.php?id=12345">
PROFILE_LINK_PATTERNS = [
    re.compile(r"^https?://(?:www\.|m\.)?facebook\.com/profile\.php\?id=\d+"),
    re.compile(r"^https?://(?:www\.|m\.)?facebook\.com/[A-Za-z0-9.\-_]{3,80}/?(?:\?|$)"),
]

# Path FB non-profilo (gruppi, marketplace, watch, ecc.) — filtrati dal normalizer.
NON_PROFILE_FIRST_SEGMENTS = {
    "groups", "marketplace", "watch", "pages", "search",
    "help", "policies", "login", "reg", "photo", "photos",
    "events", "notifications", "messages", "settings", "feed",
    "home.php", "stories", "memories", "saved", "ads",
    "business", "gaming", "live", "media", "share",
    "permalink.php", "story.php", "video.php",
}

# Testi di sistema FB che non sono nomi di persona (notifiche, timestamp).
SYSTEM_TEXT_BLACKLIST = [
    "non letta", "non lette", "unread",
    "hai approvato", "hai accettato", "hai aggiunto",
    "approvato un accesso",
    "ricevuto una richiesta",
    "compleanno", "ti ha taggato",
    "vedi tutto", "see all",
]

_TIMESTAMP_RE = re.compile(r"\b\d+\s*(?:h|m|min|d|sec|ora|ore|giorni|secondi)\b", re.IGNORECASE)


def _is_plausible_name(label: str) -> bool:
    """Heuristic: label deve sembrare un nome di persona, non testo di sistema."""
    if not label:
        return False
    l = label.lower()
    for bad in SYSTEM_TEXT_BLACKLIST:
        if bad in l:
            return False
    if _TIMESTAMP_RE.search(l):
        return False
    if not (2 <= len(label) <= 60):
        return False
    return any(c.isupper() for c in label)


def _normalize_fb_profile_url(url: str) -> str | None:
    """Pulisce un URL FB: rimuove tracking params, mantiene solo path principale.

    Esempi:
      https://www.facebook.com/carlotta.castoro?__cft__=ABC → https://www.facebook.com/carlotta.castoro
      https://www.facebook.com/profile.php?id=123&__cft__=X → https://www.facebook.com/profile.php?id=123
    """
    try:
        p = urlparse(url)
    except Exception:
        return None
    if not p.hostname or "facebook.com" not in p.hostname:
        return None
    # Tieni solo path; per profile.php tieni anche ?id=<num>
    if p.path.startswith("/profile.php"):
        m = re.search(r"id=(\d+)", p.query or "")
        if not m:
            return None
        return f"https://www.facebook.com/profile.php?id={m.group(1)}"
    # Profilo a username: path = /<username>/...
    path = (p.path or "/").rstrip("/")
    if path == "":
        return None
    parts_check = [s for s in path.split("/") if s]
    if parts_check and parts_check[0] in NON_PROFILE_FIRST_SEGMENTS:
        return None
    # Solo primo segmento (username): /<username>
    parts = [s for s in path.split("/") if s]
    if not parts:
        return None
    username = parts[0]
    # Esclude nomi "tecnici" comuni
    if username in {"home.php", "messages", "settings", "feed", "watch"}:
        return None
    if not re.match(r"^[A-Za-z0-9.\-_]{3,80}$", username):
        return None
    return f"https://www.facebook.com/{username}"


def _cognome_match(query_name: str, candidate_label: str) -> bool:
    """Verifica che il cognome (ultima parola della query) sia presente nel
    label candidato (nome visualizzato del risultato).

    Es: query="carlotta castoro", candidate="Carlotta Castoro" → True
        query="carlotta castoro", candidate="Carla Rossi" → False
        query="paolo", candidate="Paolo Maugeri" → True (un solo token, niente match cognome richiesto)
    """
    q = (query_name or "").strip().lower()
    cand = (candidate_label or "").strip().lower()
    if not q or not cand:
        return False
    tokens = q.split()
    if len(tokens) == 1:
        return tokens[0] in cand
    # Cognome = ultimo token
    cognome = tokens[-1]
    return cognome in cand


def _slug_match(query_name: str, profile_url: str) -> bool:
    """Verifica che TUTTI i token del nome compaiano nello slug del URL FB.

    Es: query="fabio famoso", url="facebook.com/famoso.fabio" → True
        query="fabio famoso", url="facebook.com/fabio.famoso.79" → True
        query="fabio famoso", url="facebook.com/profile.php?id=123" → False
        query="fabio famoso", url="facebook.com/frabio" → False
    """
    if not query_name or not profile_url:
        return False
    try:
        p = urlparse(profile_url)
    except Exception:
        return False
    path = (p.path or "/").strip("/")
    if not path or path.startswith("profile.php"):
        return False  # niente nome nello slug numerico
    slug = path.split("/")[0].lower()
    slug_norm = re.sub(r"[._\-]+", " ", slug)
    tokens = [t for t in query_name.lower().split() if t]
    if not tokens:
        return False
    return all(t in slug_norm for t in tokens)


async def search_user_by_name(
    page: "Page",
    name: str,
    *,
    jlog=None,
    debug_dir=None,
) -> tuple[str | None, str]:
    """Cerca un utente FB per nome, ritorna (url_profilo, reason).

    Logica robusta (rev. 3):
      1. Apri /search/people/?q=<name>
      2. Aspetta rendering progressivo (FB hidrata async + lazy load)
      3. Scrolla per triggerare il lazy-load dei risultati people
      4. Se `debug_dir` è valorizzato, salva HTML + screenshot + dump di tutti
         gli anchor (href + text) per analisi offline post-mortem.
      5. Filtra:
          - solo link che hanno l'aria di profili (href matching profile pattern)
          - escludi link a notifiche/feed/account approval
          - label deve essere "nome plausibile"
      6. Per ognuno verifica il cognome del query nel display_name del candidato
      7. Ritorna il primo che matcha. Se nessuno → (None, reason).
    """
    def _log(msg: str) -> None:
        log.info("[fb-search] %s", msg)
        if jlog:
            jlog(msg)

    if not name or not name.strip():
        return None, "empty_name"

    q_encoded = quote(name.strip())
    url = SEARCH_PEOPLE_URL.format(q=q_encoded)
    _log(f"🔍 FB search: '{name}' → {url}")
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
    except Exception as e:
        return None, f"goto_error: {e}"

    # Wait progressivo (FB rende il pannello results in modo lazy)
    try:
        await page.wait_for_load_state("networkidle", timeout=12_000)
    except Exception:
        pass
    await asyncio.sleep(3.0)
    # Scroll medio per triggerare lazy-load dei risultati people
    try:
        await page.mouse.wheel(0, 600)
        await asyncio.sleep(2.0)
        await page.mouse.wheel(0, 600)
        await asyncio.sleep(2.0)
    except Exception:
        pass

    # === DUMP DIAGNOSTICO se debug_dir presente ===
    debug_prefix = None
    if debug_dir is not None:
        try:
            from pathlib import Path as _P
            dd = _P(str(debug_dir))
            dd.mkdir(parents=True, exist_ok=True)
            safe_name = re.sub(r"[^A-Za-z0-9_]+", "_", name.strip())[:60]
            debug_prefix = dd / f"fb_search_{safe_name}"
            # Screenshot full page
            try:
                await page.screenshot(
                    path=str(debug_prefix) + ".png", full_page=True,
                )
                _log(f"  📸 screenshot: {debug_prefix}.png")
            except Exception as e:
                _log(f"  screenshot fail: {e}")
            # Body innerText
            try:
                body_txt = await page.evaluate("document.body.innerText")
                (debug_prefix.parent / f"{debug_prefix.name}_body.txt").write_text(
                    (body_txt or "")[:30_000], encoding="utf-8"
                )
                _log(f"  📄 body text salvato: {debug_prefix.name}_body.txt ({len(body_txt or '')} char)")
            except Exception as e:
                _log(f"  body dump fail: {e}")
            # HTML completo
            try:
                html = await page.content()
                (debug_prefix.parent / f"{debug_prefix.name}.html").write_text(
                    (html or "")[:500_000], encoding="utf-8"
                )
                _log(f"  📄 HTML salvato ({len(html or '')} char)")
            except Exception as e:
                _log(f"  HTML dump fail: {e}")
        except Exception as e:
            _log(f"  debug_dir setup fail: {e}")

    # Strategia migliorata: cerchiamo specificamente il pannello "feed di
    # ricerca" (role=feed o div con results) e da lì estraiamo i link a profili.
    # Selettori multipli, in ordine di specificità.
    feed_selectors = [
        'div[role="feed"] a[role="link"][href*="facebook.com"]',
        'div[role="feed"] a[role="link"]',
        '[data-pagelet*="SearchResults"] a[role="link"]',
        'div[role="main"] a[role="link"][href*="facebook.com"]',
        'a[role="link"][href*="facebook.com"]',  # ultimo fallback
    ]
    anchors = None
    used_selector = None
    for sel in feed_selectors:
        try:
            test = page.locator(sel)
            n = await test.count()
            if n > 0:
                anchors = test
                used_selector = sel
                break
        except Exception:
            continue
    if not anchors:
        # Dump ancore globali per debug anche se selettori specifici falliscono
        if debug_prefix:
            try:
                all_anchors = page.locator("a")
                n_all = await all_anchors.count()
                rows = []
                for i in range(min(n_all, 80)):
                    try:
                        a = all_anchors.nth(i)
                        href = await a.get_attribute("href") or ""
                        txt = (await a.text_content() or "").strip()[:120]
                        rows.append(f"{i:3d} | {href[:100]!r:100s} | {txt!r}")
                    except Exception:
                        continue
                (debug_prefix.parent / f"{debug_prefix.name}_anchors.txt").write_text(
                    "\n".join(rows), encoding="utf-8"
                )
                _log(f"  📄 dump {len(rows)} ancore (fallback)")
            except Exception:
                pass
        return None, "no_anchors_found_in_results"

    _log(f"  selettore: '{used_selector}' → {await anchors.count()} link nel pannello")

    # FB rende ogni card-risultato con due anchor allo stesso URL profilo:
    # uno avvolge l'avatar (text vuoto) + uno è il bottone CTA "Visualizza
    # profilo". Il NOME del profilo è in uno span sibling, non nel testo
    # dell'anchor. Quindi non filtriamo per label-vuota: prendiamo TUTTI gli
    # URL profilo unici e poi matchiamo via slug-URL (forte) + label/aria
    # quando disponibili.
    candidates: list[tuple[str, str, str]] = []  # (clean_url, label, aria_label)
    seen_urls: set[str] = set()
    n_total = await anchors.count()
    n_rejected_url = 0
    debug_rows: list[str] = []
    for i in range(min(n_total, 80)):
        try:
            a = anchors.nth(i)
            href = await a.get_attribute("href")
            if not href:
                continue
            label = (await a.text_content() or "").strip()[:150]
            try:
                aria = (await a.get_attribute("aria-label") or "").strip()[:150]
            except Exception:
                aria = ""
            clean = _normalize_fb_profile_url(href)
            reject_reason = ""
            if not clean:
                n_rejected_url += 1
                reject_reason = "url_not_profile"
            elif clean in seen_urls:
                reject_reason = "duplicate"

            debug_rows.append(
                f"{i:3d} | href={href[:80]!r:80s} | clean={clean!r:50s} | "
                f"label={label!r:40s} | aria={aria!r:30s} | reject={reject_reason!r}"
            )

            if reject_reason:
                continue

            seen_urls.add(clean)
            candidates.append((clean, label, aria))
            if len(candidates) >= 30:
                break
        except Exception as e:
            debug_rows.append(f"{i:3d} | EXC: {e}")
            continue

    if debug_prefix and debug_rows:
        try:
            (debug_prefix.parent / f"{debug_prefix.name}_anchors.txt").write_text(
                "\n".join(debug_rows), encoding="utf-8"
            )
            _log(f"  📄 dump {len(debug_rows)} ancore in {debug_prefix.name}_anchors.txt")
        except Exception:
            pass

    _log(
        f"  scansionati {n_total} link, rifiutati per URL: {n_rejected_url} "
        f"→ {len(candidates)} candidati unici"
    )

    if not candidates:
        return None, "no_candidates_after_filtering"

    # === MATCHING STRATEGY ===
    # Priorità (dal più forte al più debole):
    #   1. slug-URL match: TUTTI i token del nome appaiono nello slug → vince
    #      il primo (FB ordina i risultati per affinità per l'utente loggato)
    #   2. cognome match su label visibile o aria-label
    #   3. cognome match nel testo del card parent (innerText container)
    # FB normalmente piazza il match più rilevante nei primi 3 risultati per
    # gli utenti loggati (amici diretti → amici di amici → altri).
    for url_clean, label, aria in candidates:
        if _slug_match(name, url_clean):
            _log(f"  ✅ match (slug URL): '{name}' ⊂ '{url_clean}'")
            return url_clean, f"match_slug: {url_clean}"

    for url_clean, label, aria in candidates:
        if label and _cognome_match(name, label):
            _log(f"  ✅ match (label): '{label}' → {url_clean}")
            return url_clean, f"match_label: '{label}'"
        if aria and _cognome_match(name, aria):
            _log(f"  ✅ match (aria-label): '{aria}' → {url_clean}")
            return url_clean, f"match_aria: '{aria}'"

    # Nessun match certo: ritorna None ma indica il top
    first_url, first_label, _ = candidates[0]
    _log(f"  ⚠️ nessun match certo per '{name}'. "
         f"Top candidato URL: {first_url} (label='{first_label}') — SCARTATO.")
    return None, f"no_match (top_url: '{first_url}')"


def _fb_subpage_urls(profile_url: str) -> list[tuple[str, str]]:
    """Costruisce gli URL delle sotto-pagine FB ad alto valore informativo.

    FB 2026: la vecchia `/likes_pages` non esiste più (404 "Pagina non
    disponibile"). I "mi piace" / interessi del soggetto sono ora integrati
    nelle sotto-sezioni di `/about`. Le sezioni più informative oggi sono:

      - /about → overview generale (lavoro, studi, città)
      - /about_contact_and_basic_info → contatti pubblici + lingue + religione
      - /about_work_and_education → dettaglio lavori/studi cronologici

    Gestisce entrambi i pattern di URL profilo FB:
      - /<username> → /<username>/about, /<username>/about_contact_and_basic_info
      - /profile.php?id=N → /profile.php?id=N&sk=about, &sk=about_contact_and_basic_info
    """
    try:
        p = urlparse(profile_url)
    except Exception:
        return []
    if not p.hostname or "facebook.com" not in p.hostname:
        return []
    if p.path.startswith("/profile.php"):
        m = re.search(r"id=(\d+)", p.query or "")
        if not m:
            return []
        base = f"https://www.facebook.com/profile.php?id={m.group(1)}"
        return [
            ("/about", f"{base}&sk=about"),
            ("/about_contact_and_basic_info", f"{base}&sk=about_contact_and_basic_info"),
        ]
    parts = [s for s in (p.path or "").split("/") if s]
    if not parts:
        return []
    username = parts[0]
    return [
        ("/about", f"https://www.facebook.com/{username}/about"),
        ("/about_contact_and_basic_info",
         f"https://www.facebook.com/{username}/about_contact_and_basic_info"),
    ]


# Pattern testuali che indicano la pagina di errore FB ("Pagina non disponibile",
# "Page isn't available", ecc.). Usati da `collect_subpage_texts` per skip.
FB_ERROR_PAGE_MARKERS = [
    "questa pagina non è disponibile",
    "questa pagina non e' disponibile",
    "this page isn't available",
    "this page isnt available",
    "this content isn't available",
    "il link potrebbe essere non funzionante",
    "torna indietro",  # solo se compare con accedi al feed
]


def _is_fb_error_page(body_text: str) -> bool:
    """Heuristic: rileva la pagina di errore FB (404 link rotto, contenuto
    rimosso, login wall). Richiede match marker + lunghezza compatibile."""
    if not body_text:
        return False
    low = body_text.lower()
    # FB error page tipicamente ha 200-500 char totali
    if len(body_text) > 1200:
        return False
    for marker in FB_ERROR_PAGE_MARKERS:
        if marker in low:
            return True
    return False


async def collect_subpage_texts(
    page: "Page",
    safe,
    profile_url: str,
    *,
    jlog=None,
    max_chars_per_page: int = 4000,
) -> dict[str, str]:
    """Visita le sotto-pagine ad alto valore (/about + /likes_pages) e ritorna
    {label: body_text}. Best-effort: pagine private/redirect → skip silenzioso.

    Usa `safe.safe_goto` (SafeBrowser) per audit + blacklist.
    """
    def _log(msg: str) -> None:
        log.info("[fb-sub] %s", msg)
        if jlog:
            jlog(msg)

    out: dict[str, str] = {}
    urls = _fb_subpage_urls(profile_url)
    if not urls:
        _log(f"      no sub-page URL derivabili da {profile_url}")
        return out

    for label, url in urls:
        try:
            await safe.safe_goto(url, label=f"fb_subpage_{label}")
            # Wait per hydration (FB SSRs uno scheletro poi popola)
            try:
                await page.wait_for_load_state("networkidle", timeout=8_000)
            except Exception:
                pass
            await asyncio.sleep(random.uniform(2.0, 3.5))
            try:
                await page.mouse.wheel(0, 800)
                await asyncio.sleep(1.5)
                await page.mouse.wheel(0, 800)
                await asyncio.sleep(1.0)
            except Exception:
                pass

            try:
                body_text = await page.evaluate("document.body.innerText")
            except Exception:
                body_text = ""
            body_text = (body_text or "")[:max_chars_per_page]

            # Heuristic: se il body è quasi vuoto (<150 char) o contiene solo
            # boilerplate ("Devi accedere") consideriamo la pagina inaccessibile.
            if len(body_text.strip()) < 150:
                _log(f"      sub-page '{label}': body troppo corto ({len(body_text)}), skip")
                continue
            low = body_text.lower()
            if "devi accedere" in low and "facebook" in low and len(body_text) < 600:
                _log(f"      sub-page '{label}': login wall rilevato, skip")
                continue
            if _is_fb_error_page(body_text):
                _log(f"      sub-page '{label}': pagina di errore FB ('Pagina non disponibile'), skip")
                continue

            out[label] = body_text
            _log(f"      sub-page '{label}': {len(body_text)} char raccolti")
        except Exception as e:
            _log(f"      sub-page '{label}' fail: {type(e).__name__}: {e}")
            continue

    return out


async def extract_profile_data(page: "Page") -> dict[str, Any]:
    """Estrae dati strutturati da una pagina profilo FB aperta.

    Best-effort: se selettori non matchano (FB cambia DOM o profilo privato),
    ritorna i campi disponibili con None per quelli mancanti.
    """
    data: dict[str, Any] = {
        "platform": "facebook",
        "url": page.url,
        "display_name": None,
        "bio_text": None,
        "recent_posts": [],
        "intro_box": None,
        "title": None,
        "error": None,
    }

    # Wait soft per lazy load (FB ha sempre placeholder iniziali)
    try:
        await page.wait_for_load_state("networkidle", timeout=8_000)
    except Exception:
        pass

    # Detection "non loggato / non accessibile"
    for sel in LOGIN_REQUIRED_PROMPT:
        try:
            loc = page.locator(sel).first
            if await loc.is_visible(timeout=300):
                data["error"] = "login_required_or_private"
                return data
        except Exception:
            continue

    # Title
    try:
        data["title"] = await page.title()
    except Exception:
        pass

    # Display name
    try:
        loc = page.locator(PROFILE_NAME).first
        if await loc.is_visible(timeout=2_000):
            data["display_name"] = (await loc.text_content() or "").strip()
    except Exception:
        pass

    # Bio / intro box
    try:
        intro = page.locator(BIO_INTRO_SECTION).first
        if await intro.is_visible(timeout=2_000):
            txt = await intro.text_content()
            data["intro_box"] = (txt or "").strip()[:1500]
    except Exception:
        pass

    # Post recenti (primi N)
    posts_out: list[dict[str, Any]] = []
    for sel in POST_CONTAINERS:
        try:
            posts = page.locator(sel)
            count = await posts.count()
            if count == 0:
                continue
            for i in range(min(count, 10)):
                try:
                    txt = await posts.nth(i).text_content() or ""
                    txt = txt.strip()[:600]
                    if txt:
                        posts_out.append({"text": txt})
                except Exception:
                    continue
            if posts_out:
                break  # primo selettore che matcha vince
        except Exception:
            continue
    data["recent_posts"] = posts_out[:10]

    return data


# === Friend list loader ===
# FB ha più URL per la friend list dell'utente loggato. Tentiamo in ordine:
# 1. /me/friends_all (più completo, FB risolve /me/ → tuo profile)
# 2. /friends/list (suggerimenti + richieste, ma anche la lista completa via tab)
# 3. /me/friends (variante alternativa)
FRIEND_LIST_URLS = [
    "https://www.facebook.com/me/friends_all",
    "https://www.facebook.com/me/friends",
    "https://www.facebook.com/friends/list",
]


async def search_friend_via_friendlist(
    page: "Page",
    safe,
    name: str,
    *,
    jlog=None,
    debug_dir=None,
) -> tuple[str, str] | None:
    """Usa il search box dentro /friends/list per trovare un amico per nome.

    Vantaggi rispetto a `load_friend_list` (bulk):
      - Veloce (~5-10s per nome vs 5-10 min per bulk load di 500+ amici)
      - Accurato (FB filtra serverside contro la friend list reale)
      - Robusto (no scroll, no lazy-load, no dedup)
      - Non legge mai URL "estranei" (la search dentro /friends/list è ristretta agli amici)

    Strategia:
      1. Naviga a /friends/list (solo se non già lì) — usa la persistenza della pagina
         se chiamato N volte di seguito per N amici (1 sola navigazione).
      2. Trova il search input "Cerca amici"
      3. Clear + type del nome (con delay umano)
      4. Wait ~2s per FB filter
      5. Prendi il primo profilo nei risultati filtrati

    Ritorna (display_name, profile_url) o None se nessun match.
    """
    def _log(msg: str) -> None:
        log.info("[fb-friend-search] %s", msg)
        if jlog:
            jlog(msg)

    if not name or not name.strip():
        return None
    name = name.strip()

    # 1. Naviga se non già su /friends/list (sticky tra chiamate consecutive)
    cur_url = page.url or ""
    if "facebook.com/friends/list" not in cur_url:
        try:
            await safe.safe_goto(
                "https://www.facebook.com/friends/list",
                label="fb_friend_list_search",
            )
            try:
                await page.wait_for_load_state("networkidle", timeout=10_000)
            except Exception:
                pass
            await asyncio.sleep(random.uniform(2.0, 3.0))
        except Exception as e:
            _log(f"  goto /friends/list fail: {e}")
            return None

    # 2. Trova il search input "Cerca amici"
    search_selectors = [
        'input[placeholder="Cerca amici"]',
        'input[placeholder*="Cerca amici"]',
        'input[placeholder="Search friends"]',
        'input[placeholder*="Search friends"]',
        'input[aria-label*="Cerca amici"]',
        'input[aria-label*="Search friends"]',
        'input[type="search"]',
    ]
    search_input = None
    used_sel = None
    for sel in search_selectors:
        try:
            loc = page.locator(sel).first
            if await loc.is_visible(timeout=1_500):
                search_input = loc
                used_sel = sel
                break
        except Exception:
            continue
    if not search_input:
        _log("  ❌ search input 'Cerca amici' non trovato in /friends/list")
        if debug_dir is not None:
            try:
                from pathlib import Path as _P
                dd = _P(str(debug_dir))
                dd.mkdir(parents=True, exist_ok=True)
                await page.screenshot(path=str(dd / "fb_friend_search_no_input.png"))
            except Exception:
                pass
        return None
    _log(f"  search input trovato ({used_sel})")

    # 3. Pulisci input + type
    try:
        await search_input.click()
        await asyncio.sleep(0.3)
        await page.keyboard.press("Control+A")
        await page.keyboard.press("Delete")
        await asyncio.sleep(0.2)
        await search_input.type(name, delay=70)
        _log(f"  typed '{name}' nel search box")
    except Exception as e:
        _log(f"  type fail: {e}")
        return None

    # 4. Wait per debounce FB + filter rendering
    await asyncio.sleep(random.uniform(1.8, 2.8))

    # 5. Pesca il primo profilo dai risultati filtrati.
    # FB renderizza il pannello sidebar con i risultati: cerchiamo anchor a
    # profili che NON siano la pagina corrente (/friends/), non avatar topbar,
    # e con label/nome plausibile.
    result_selectors = [
        'div[role="navigation"] a[role="link"][href*="facebook.com/"]',
        'div[role="main"] a[role="link"][href*="facebook.com/"]',
        'a[role="link"][href*="facebook.com/"]',
    ]
    for sel in result_selectors:
        try:
            anchors = page.locator(sel)
            n = await anchors.count()
            for i in range(min(n, 30)):
                try:
                    a = anchors.nth(i)
                    href = await a.get_attribute("href")
                    if not href:
                        continue
                    clean = _normalize_fb_profile_url(href)
                    if not clean:
                        continue
                    # Escludi la pagina corrente
                    if clean.endswith("/friends") or clean.endswith("/friends_all"):
                        continue
                    label = (await a.text_content() or "").strip()[:120]
                    # Risultato valido: prima ancora che ha label plausibile OPPURE
                    # è un avatar (label vuota) ma URL profilo nel pannello risultati
                    if label and _is_plausible_name(label):
                        _log(f"  ✅ match: '{label}' → {clean}")
                        return (label, clean)
                    # Avatar (label vuota): prova a estrarre il nome dal parent
                    if not label:
                        try:
                            parent_txt = await a.evaluate(
                                "el => {"
                                "  let p = el.closest('div[role=\"listitem\"]') || el.closest('div'); "
                                "  return p %s p.innerText : '';"
                                "}"
                            )
                            parent_label = (parent_txt or "").strip().split("\n")[0][:120]
                            if parent_label and _is_plausible_name(parent_label):
                                _log(f"  ✅ match (parent text): '{parent_label}' → {clean}")
                                return (parent_label, clean)
                        except Exception:
                            pass
                except Exception:
                    continue
            if n > 0:
                # Almeno un selettore ha matchato anchors; se nessuno era valido
                # passa al prossimo selettore (fallback)
                pass
        except Exception:
            continue

    _log(f"  ⚠️ nessun amico trovato per '{name}' (search box vuoto o no match)")
    if debug_dir is not None:
        try:
            from pathlib import Path as _P
            dd = _P(str(debug_dir))
            dd.mkdir(parents=True, exist_ok=True)
            safe_n = re.sub(r"[^A-Za-z0-9_]+", "_", name)[:60]
            await page.screenshot(path=str(dd / f"fb_friend_search_{safe_n}_nomatch.png"))
        except Exception:
            pass
    return None


async def load_friend_list(
    page: "Page",
    safe,
    *,
    jlog=None,
    max_scrolls: int = 8,
    debug_dir=None,
) -> dict[str, str]:
    """Carica la friend list dell'utente FB loggato.

    Ritorna: dict {nome_visualizzato_lowercase: profile_url_canonico}.
    Best-effort: se la lista non è accessibile (privacy, errore, login wall),
    ritorna dict vuoto.

    Strategia:
      1. Naviga a /me/friends_all (fallback /me/friends, /friends/list)
      2. Aspetta hydration + scroll per lazy-load (FB carica friend chunks di 20-40)
      3. Per ogni link `[href*=facebook.com]` nel feed: estrai nome (label) + URL
      4. Normalizza URL, dedup per nome+URL

    NB: max_scrolls limita il numero di scroll = per ~500 amici servono 10-15
    scroll. Default 8 (~300 amici); aumenta se la friend list è più grande.
    """
    def _log(msg: str) -> None:
        log.info("[fb-friends] %s", msg)
        if jlog:
            jlog(msg)

    # 1. Naviga al primo URL che non dà errore
    loaded_url = None
    for url in FRIEND_LIST_URLS:
        try:
            await safe.safe_goto(url, label="fb_friend_list")
            try:
                await page.wait_for_load_state("networkidle", timeout=10_000)
            except Exception:
                pass
            await asyncio.sleep(2.0)
            # Verifica che non sia una error page
            body_check = ""
            try:
                body_check = await page.evaluate("document.body.innerText")
            except Exception:
                pass
            if _is_fb_error_page(body_check or ""):
                _log(f"  '{url}' → error page, provo prossimo")
                continue
            loaded_url = url
            break
        except Exception as e:
            _log(f"  '{url}' goto fail: {e}")
            continue
    if not loaded_url:
        _log("  ❌ nessuna URL friend list accessibile")
        return {}
    _log(f"  ✅ friend list caricata: {loaded_url}")

    # 2. Scroll progressivo per triggerare il lazy-load
    for i in range(max_scrolls):
        try:
            await page.mouse.wheel(0, 1200)
            await asyncio.sleep(random.uniform(1.2, 2.2))
        except Exception:
            break

    # 3. Estrai i link a profili dal feed
    feed_selectors = [
        'div[role="main"] a[role="link"][href*="facebook.com"]',
        'div[role="feed"] a[role="link"]',
        'a[role="link"][href*="facebook.com"]',
    ]
    anchors = None
    for sel in feed_selectors:
        try:
            loc = page.locator(sel)
            n = await loc.count()
            if n > 5:  # almeno qualche risultato
                anchors = loc
                break
        except Exception:
            continue
    if not anchors:
        _log("  ⚠️ nessun anchor profilo trovato nella pagina friend list")
        return {}

    n_total = await anchors.count()
    _log(f"  {n_total} anchor candidati nel pannello friend list")

    # 4. Per ogni anchor: href + label visibile (per friend list FB rende il
    #    NOME dell'amico come text content dell'ancora, non vuoto come search).
    out: dict[str, str] = {}
    debug_rows: list[str] = []
    for i in range(min(n_total, 500)):  # cap di sicurezza
        try:
            a = anchors.nth(i)
            href = await a.get_attribute("href")
            if not href:
                continue
            label = (await a.text_content() or "").strip()[:120]
            clean = _normalize_fb_profile_url(href)

            debug_rows.append(
                f"{i:3d} | clean={clean!r:55s} | label={label!r:60s}"
            )

            if not clean:
                continue
            if not label or len(label) < 2:
                continue
            # Heuristic: nome plausibile (mai testo system)
            if not _is_plausible_name(label):
                continue
            key = label.lower().strip()
            # Se già visto questo nome con un URL diverso, ignora (FB potrebbe
            # avere duplicati a causa di hidratazione progressiva)
            if key not in out:
                out[key] = clean
        except Exception:
            continue

    _log(f"  ✅ friend list estratta: {len(out)} amici unici")

    if debug_dir is not None and debug_rows:
        try:
            from pathlib import Path as _P
            dd = _P(str(debug_dir))
            dd.mkdir(parents=True, exist_ok=True)
            (dd / "fb_friend_list_anchors.txt").write_text(
                "\n".join(debug_rows), encoding="utf-8"
            )
            _log(f"  📄 dump {len(debug_rows)} anchor in friend_list_anchors.txt")
        except Exception:
            pass

    return out


def match_friend(name_query: str, friend_list: dict[str, str]) -> tuple[str, str] | None:
    """Cerca `name_query` (case-insensitive) nella friend list pre-caricata.

    Strategia:
      1. Match esatto sul nome (lowercase)
      2. Match per tutti i token: ogni token del query è in qualche key
      3. Match per cognome (ultimo token) presente in qualche key

    Ritorna (nome_amico_canonico, profile_url) o None se nessun match.
    """
    if not name_query or not friend_list:
        return None
    q = name_query.lower().strip()
    if q in friend_list:
        return (q, friend_list[q])
    tokens = q.split()
    if not tokens:
        return None
    # Match completo (tutti i token presenti nel nome friend, in qualsiasi ordine)
    for friend_name, url in friend_list.items():
        if all(t in friend_name for t in tokens):
            return (friend_name, url)
    # Fallback: solo cognome (ultimo token) match
    if len(tokens) > 1:
        cognome = tokens[-1]
        for friend_name, url in friend_list.items():
            if cognome in friend_name:
                return (friend_name, url)
    return None


# ---------------------------------------------------------------------------
# FOLLOWER ENUMERATION (per recon_mode="follower_scrape")
# ---------------------------------------------------------------------------

def _fb_handle_from_url(profile_url: str) -> str | None:
    """Estrae l'identifier FB da un profile URL.

    Per profili con username: `carlotta.castoro` da
    https://www.facebook.com/carlotta.castoro.
    Per profili numerici: ritorna l'id come stringa (es. `100012345678`)
    da https://www.facebook.com/profile.php?id=100012345678.

    Ritorna None se l'URL non e' un profilo FB valido.
    """
    norm = _normalize_fb_profile_url(profile_url)
    if not norm:
        return None
    try:
        p = urlparse(norm)
    except Exception:
        return None
    if p.path.startswith("/profile.php"):
        m = re.search(r"id=(\d+)", p.query or "")
        return m.group(1) if m else None
    parts = [s for s in (p.path or "/").split("/") if s]
    return parts[0] if parts else None


def _fb_followers_url(target_identifier: str) -> str:
    """Costruisce l'URL della pagina follower per un target FB.

    `target_identifier` puo' essere:
    - username (es. 'carlotta.castoro') → /carlotta.castoro/followers
    - id numerico (es. '100012345678') → /profile.php?id=X&sk=followers
    """
    ident = (target_identifier or "").strip().lstrip("@")
    if ident.isdigit():
        return f"https://www.facebook.com/profile.php?id={ident}&sk=followers"
    return f"https://www.facebook.com/{ident}/followers"


async def enumerate_followers_of_target(
    page: "Page",
    safe,
    target_handle: str,
    *,
    cap: int = 100,
    jlog=None,
    debug_dir=None,
) -> list[dict[str, str]]:
    """Enumera i FOLLOWER pubblici di un profilo FB target.

    BETA — implementazione basata sul layout FB 2025. Selettori CSS multipli
    come fallback; FB cambia DOM spesso, quindi alcuni potrebbero fallire.

    Flow:
    1. Naviga a /<username>/followers (o profile.php?id=X&sk=followers).
    2. Aspetta che la lista carichi (presenza di anchor link a profili).
    3. Scroll infinito fino a `cap` follower estratti o fine lista.
    4. Estrae anchor con href ai profili e display_name dallo span.

    Limiti noti:
    - Se il target ha privacy "amici" sulla lista follower, ritorna [].
    - Account FB con bloccol/2FA → page goto fail (gestito).
    - 'People you may know' ha selettori simili a follower: filtriamo
      controllando heading/contenuto del container.

    Ritorna list di {handle, display_name, profile_url}. Best-effort.
    """
    def _log(msg: str) -> None:
        log.info("[fb-followers] %s", msg)
        if jlog:
            jlog(msg)

    target = (target_handle or "").strip().lstrip("@")
    if not target:
        _log("  ❌ target_handle vuoto")
        return []

    url = _fb_followers_url(target)
    _log(f"goto {url}")
    try:
        await safe.safe_goto(url, label=f"fb_followers_{target}")
        try:
            await page.wait_for_load_state("networkidle", timeout=12_000)
        except Exception:
            pass
        await asyncio.sleep(random.uniform(2.5, 4.0))
    except Exception as e:
        _log(f"  goto fail: {e}")
        return []

    # Debug screenshot iniziale
    if debug_dir is not None:
        try:
            from pathlib import Path as _P
            dd = _P(str(debug_dir))
            dd.mkdir(parents=True, exist_ok=True)
            safe_n = re.sub(r"[^A-Za-z0-9_]+", "_", target)[:60]
            await page.screenshot(path=str(dd / f"fb_followers_initial_{safe_n}.png"))
        except Exception:
            pass

    # Check accesso negato / privacy: pattern noti FB
    try:
        body_text = (await page.locator("body").text_content(timeout=2_000)) or ""
    except Exception:
        body_text = ""
    if _is_fb_error_page(body_text):
        _log(f"  ❌ pagina inaccessibile per @{target} (privacy / inesistente / login required)")
        return []

    # Selettori candidate per il container della lista follower. FB usa
    # role="main" con div annidati; cerchiamo il container che contiene
    # almeno N anchor a profili. Strategia robusta.
    main_locator = page.locator('div[role="main"]').first
    try:
        await main_locator.wait_for(state="visible", timeout=8_000)
    except Exception:
        _log("  ⚠️ main container non trovato — fallback su body")

    followers: list[dict[str, str]] = []
    seen_urls: set[str] = set()

    # Selettori per gli ITEM follower. FB renderizza ogni item come anchor
    # con href al profilo + span con display name. Cerchiamo anchor che
    # puntano a profili (non a foto/eventi/post).
    item_selectors = [
        'div[role="main"] a[role="link"][href]:not([href*="/photo"]):not([href*="/posts"]):not([href*="?comment"])',
        'div[role="main"] a[href^="/"]:not([href*="/photo"]):not([href*="/posts"])',
        'div[aria-label*="ollower"] a[href]',
        'div[aria-label*="eguaci"] a[href]',
    ]

    max_scrolls = max(8, cap // 10)  # +/- 10 follower per scroll
    last_count = 0
    stagnant_rounds = 0

    for scroll_idx in range(max_scrolls):
        # Estrai gli anchor visibili
        anchors_found: list[tuple[str, str]] = []  # (href, display_name)
        for sel in item_selectors:
            try:
                els = await page.locator(sel).all()
            except Exception:
                continue
            if not els:
                continue
            for el in els[:cap * 3]:  # raccogliamo extra per filtraggio
                try:
                    href = await el.get_attribute("href", timeout=500)
                except Exception:
                    href = None
                if not href:
                    continue
                # Filtro: solo profili (URL FB normalizzabile)
                full_url = href if href.startswith("http") else f"https://www.facebook.com{href}"
                normalized = _normalize_fb_profile_url(full_url)
                if not normalized:
                    continue
                # Display name: testo interno dell'anchor
                try:
                    name = (await el.text_content(timeout=500)) or ""
                    name = name.strip()
                except Exception:
                    name = ""
                if not _is_plausible_name(name):
                    # nome vuoto o "Vedi tutto" / "Mostra altro" — skip
                    continue
                anchors_found.append((normalized, name))
            if anchors_found:
                break  # primo selettore che pesca qualcosa wins

        # Dedup + aggiungi a `followers`
        for normalized_url, name in anchors_found:
            if normalized_url in seen_urls:
                continue
            # Filtro: skip il profilo target stesso
            if _fb_handle_from_url(normalized_url) == target:
                continue
            seen_urls.add(normalized_url)
            handle = _fb_handle_from_url(normalized_url) or ""
            followers.append({
                "handle": handle,
                "display_name": name,
                "profile_url": normalized_url,
            })
            if len(followers) >= cap:
                break

        current_count = len(followers)
        _log(f"  scroll {scroll_idx + 1}/{max_scrolls}: {current_count} follower (cap={cap})")

        if current_count >= cap:
            break

        # Detect stagnation: se 3 scroll consecutivi senza nuovi follower → stop
        if current_count == last_count:
            stagnant_rounds += 1
            if stagnant_rounds >= 3:
                _log("  ⏹ stop: 3 scroll senza nuovi follower")
                break
        else:
            stagnant_rounds = 0
        last_count = current_count

        # Scroll giù
        try:
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        except Exception:
            pass
        await asyncio.sleep(random.uniform(1.5, 2.8))

    _log(f"  ✓ enumerati {len(followers)} follower di @{target}")
    if debug_dir is not None:
        try:
            from pathlib import Path as _P
            dd = _P(str(debug_dir))
            safe_n = re.sub(r"[^A-Za-z0-9_]+", "_", target)[:60]
            await page.screenshot(path=str(dd / f"fb_followers_final_{safe_n}.png"))
        except Exception:
            pass
    return followers
