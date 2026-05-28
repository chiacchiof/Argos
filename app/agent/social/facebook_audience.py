"""Facebook audience discovery — primitive FB-loggate per esplorazione audience.

Estende `facebook_recon.py` con tool primitive che il runner
`audience_discovery` chiama nel suo loop ReAct quando deve scoprire profili
matching un brief NL (topic + demografia). A differenza di `facebook_recon`
che parte da URL profili specifici, qui partiamo da keyword libere e/o gruppi
tematici e/o friends-of-friends.

Funzioni esposte:
- `search_people_by_keyword(page, query, limit)` — search FB persone by topic
  (NON nome). Ritorna lista [{'url', 'name'}].
- `search_groups(page, query, limit)` — cerca gruppi tematici.
  Ritorna lista [{'url', 'name', 'description'}].
- `open_group_and_collect_members(page, group_url, scrolls, limit)` — apre
  un gruppo, scrolla N volte sul feed, raccoglie URL degli autori dei post
  più recenti come "candidati audience" (sono persone attive sul topic del
  gruppo). Ritorna lista [{'url', 'name', 'snippet'}].
- `friends_of_profile(page, profile_url, limit)` — apre /friends del profilo
  se pubblica e ritorna [{'url', 'name'}]. Funziona solo se la friend list
  del profilo è visibile.

Riusa selettori + helpers da `facebook_recon.py`. Selettori FB cambiano
spesso: aggiornare al bisogno.
"""
from __future__ import annotations

import asyncio
import logging
import re
from typing import TYPE_CHECKING, Any
from urllib.parse import quote, urlparse

from . import facebook_recon

if TYPE_CHECKING:
    from playwright.async_api import Page

log = logging.getLogger(__name__)


# === URL templates ===

SEARCH_PEOPLE_URL = "https://www.facebook.com/search/people/?q={q}"
SEARCH_GROUPS_URL = "https://www.facebook.com/search/groups/?q={q}"


# === Selettori ===

# Risultati people (riusa pattern di facebook_recon.search_user_by_name)
PEOPLE_RESULT_SELECTORS = [
    'div[role="feed"] a[role="link"][href*="facebook.com"]',
    'div[role="feed"] a[role="link"]',
    '[data-pagelet*="SearchResults"] a[role="link"]',
    'div[role="main"] a[role="link"][href*="facebook.com"]',
    'a[role="link"][href*="facebook.com"]',
]

# Risultati gruppi: <a href="/groups/<slug-or-id>/...">
GROUP_RESULT_SELECTORS = [
    'div[role="feed"] a[href*="/groups/"]',
    'div[role="main"] a[href*="/groups/"]',
    'a[role="link"][href*="/groups/"]',
]

# Post containers nel feed di un gruppo (autori da raccogliere)
GROUP_POST_CONTAINERS = [
    'div[role="feed"] div[role="article"]',
    'div[role="article"]',
]

# Friend link su pagina /friends di un profilo
FRIENDS_LINKS_SELECTORS = [
    'div[role="main"] a[role="link"][href*="facebook.com"]',
    'a[role="link"][href^="https://www.facebook.com/"]',
]


# === Helpers privati ===

_GROUP_URL_RE = re.compile(r"^/groups/([A-Za-z0-9.\-_]+)(?:/|$|\?)")
_GROUP_USER_RE = re.compile(r"^/groups/[^/]+/user/(\d+)/?")


def _normalize_author_url(href: str) -> str | None:
    """Versione "tollerante" di facebook_recon._normalize_fb_profile_url:
    accetta anche il formato `/groups/<gid>/user/<uid>/` tipico dei post
    nei gruppi (FB linka l'autore al "member-of-group" invece che al
    profilo globale). Ritorna sempre l'URL profilo globale
    `https://www.facebook.com/profile.php?id=<uid>` quando può estrarre
    l'uid; altrimenti delega al normalizer standard.
    """
    if not href:
        return None
    try:
        p = urlparse(href)
    except Exception:
        return None
    path = p.path or "/"
    m = _GROUP_USER_RE.match(path)
    if m:
        uid = m.group(1)
        return f"https://www.facebook.com/profile.php?id={uid}"
    return facebook_recon._normalize_fb_profile_url(href)


def _normalize_group_url(href: str) -> str | None:
    """Pulisce un href a gruppo FB: ritorna `https://www.facebook.com/groups/<slug>`
    oppure None se non è un URL gruppo plausibile."""
    if not href:
        return None
    try:
        p = urlparse(href)
    except Exception:
        return None
    # Path può essere assoluto (https://...) o relativo (/groups/...)
    path = p.path or "/"
    if not path.startswith("/groups/"):
        return None
    m = _GROUP_URL_RE.match(path)
    if not m:
        return None
    slug = m.group(1)
    # Esclude segmenti tecnici tipo /groups/feed, /groups/discover, /groups/joins
    if slug in {"feed", "discover", "joins", "create", "browse"}:
        return None
    return f"https://www.facebook.com/groups/{slug}"


async def _lazy_load_scroll(page: "Page", times: int = 3, delay: float = 2.0) -> None:
    """Scrolla N volte per triggerare lazy-load dei risultati."""
    for _ in range(times):
        try:
            await page.mouse.wheel(0, 800)
            await asyncio.sleep(delay)
        except Exception:
            pass


# === Primitive 1: search persone by keyword ===

async def search_people_by_keyword(
    page: "Page",
    keyword: str,
    *,
    limit: int = 20,
    jlog=None,
) -> list[dict[str, str]]:
    """Cerca persone su FB tramite la barra ricerca per keyword/topic.

    A differenza di `facebook_recon.search_user_by_name`, qui NON filtriamo
    per cognome (keyword può essere un topic, es. "vintage anni 80"). Tutti
    i risultati plausibili vengono raccolti. Il filtraggio audience è
    delegato al runner ReAct downstream (via score LLM).

    Args:
        page: Playwright Page già su un browser FB loggato.
        keyword: testo libero da cercare.
        limit: cap risultati (default 20).
        jlog: log callback opzionale.

    Returns:
        Lista di dict {'url': str, 'name': str}. Lista vuota se nessun
        risultato o errore.
    """
    def _log(msg: str) -> None:
        log.info("[fb-audience] search_people: %s", msg)
        if jlog:
            jlog(f"search_people: {msg}")

    if not keyword or not keyword.strip():
        return []

    q = quote(keyword.strip())
    url = SEARCH_PEOPLE_URL.format(q=q)
    _log(f"goto {url}")

    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
    except Exception as e:
        _log(f"goto error: {e}")
        return []

    try:
        await page.wait_for_load_state("networkidle", timeout=12_000)
    except Exception:
        pass
    await asyncio.sleep(3.0)
    await _lazy_load_scroll(page, times=3)

    results: list[dict[str, str]] = []
    seen_urls: set[str] = set()

    for sel in PEOPLE_RESULT_SELECTORS:
        try:
            anchors = page.locator(sel)
            n = await anchors.count()
            if n == 0:
                continue
            _log(f"selettore '{sel}' → {n} ancore")
            for i in range(min(n, 200)):
                try:
                    a = anchors.nth(i)
                    href = await a.get_attribute("href") or ""
                    txt = (await a.text_content() or "").strip()
                    if not href:
                        continue
                    norm = facebook_recon._normalize_fb_profile_url(href)
                    if not norm or norm in seen_urls:
                        continue
                    if not facebook_recon._is_plausible_name(txt):
                        continue
                    seen_urls.add(norm)
                    results.append({"url": norm, "name": txt})
                    if len(results) >= limit:
                        break
                except Exception:
                    continue
            if results:
                break  # primo selettore che ha trovato risultati: stop
        except Exception:
            continue

    _log(f"→ {len(results)} profili")
    return results


# === Primitive 2: search gruppi by keyword ===

async def search_groups(
    page: "Page",
    keyword: str,
    *,
    limit: int = 10,
    jlog=None,
) -> list[dict[str, Any]]:
    """Cerca gruppi pubblici/privati su FB per keyword/topic.

    Args:
        page: Playwright Page su browser FB loggato.
        keyword: keyword tematica (es. "vintage anni 80").
        limit: cap risultati.
        jlog: log callback.

    Returns:
        Lista di dict {'url': str, 'name': str, 'description': str}. La
        description può essere vuota (FB non sempre la mostra in search).
    """
    def _log(msg: str) -> None:
        log.info("[fb-audience] search_groups: %s", msg)
        if jlog:
            jlog(f"search_groups: {msg}")

    if not keyword or not keyword.strip():
        return []

    q = quote(keyword.strip())
    url = SEARCH_GROUPS_URL.format(q=q)
    _log(f"goto {url}")

    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
    except Exception as e:
        _log(f"goto error: {e}")
        return []

    try:
        await page.wait_for_load_state("networkidle", timeout=12_000)
    except Exception:
        pass
    await asyncio.sleep(3.0)
    await _lazy_load_scroll(page, times=3)

    results: list[dict[str, Any]] = []
    seen_urls: set[str] = set()

    for sel in GROUP_RESULT_SELECTORS:
        try:
            anchors = page.locator(sel)
            n = await anchors.count()
            if n == 0:
                continue
            _log(f"selettore '{sel}' → {n} ancore")
            for i in range(min(n, 200)):
                try:
                    a = anchors.nth(i)
                    href = await a.get_attribute("href") or ""
                    txt = (await a.text_content() or "").strip()
                    if not href:
                        continue
                    norm = _normalize_group_url(href)
                    if not norm or norm in seen_urls:
                        continue
                    # Il name del gruppo è il testo dell'ancora, ma a volte
                    # è solo l'icona/data → fallback su slug
                    name = txt or norm.rsplit("/", 1)[-1].replace(".", " ").title()
                    if len(name) > 200:
                        name = name[:200]
                    seen_urls.add(norm)
                    results.append({
                        "url": norm,
                        "name": name,
                        "description": "",
                    })
                    if len(results) >= limit:
                        break
                except Exception:
                    continue
            if results:
                break
        except Exception:
            continue

    _log(f"→ {len(results)} gruppi")
    return results


# === Primitive 3: open gruppo + collect autori post ===

async def open_group_and_collect_members(
    page: "Page",
    group_url: str,
    *,
    scrolls: int = 10,
    limit: int = 30,
    jlog=None,
) -> list[dict[str, str]]:
    """Apre un gruppo FB, scrolla sul feed, raccoglie URL degli autori dei
    post visibili. Sono "candidati audience" perché stanno attivamente
    postando sul topic del gruppo.

    Note: non apre la lista membri completa del gruppo (spesso non visibile
    o cap-pata a poche centinaia); preferiamo gli autori attivi nel feed.

    Args:
        page: Playwright Page su browser FB loggato.
        group_url: URL del gruppo (es. https://www.facebook.com/groups/vintage80).
        scrolls: numero di scroll sul feed per caricare più post.
        limit: cap autori unici da ritornare.
        jlog: log callback.

    Returns:
        Lista di dict {'url': str, 'name': str, 'snippet': str} dove snippet
        è un estratto del testo del post (max 200 char).
    """
    def _log(msg: str) -> None:
        log.info("[fb-audience] open_group: %s", msg)
        if jlog:
            jlog(f"open_group: {msg}")

    if not group_url:
        return []

    _log(f"goto {group_url}")
    try:
        await page.goto(group_url, wait_until="domcontentloaded", timeout=30_000)
    except Exception as e:
        _log(f"goto error: {e}")
        return []

    try:
        await page.wait_for_load_state("networkidle", timeout=12_000)
    except Exception:
        pass
    await asyncio.sleep(3.0)

    # Scroll progressivo per caricare più post
    for i in range(scrolls):
        try:
            await page.mouse.wheel(0, 900)
            await asyncio.sleep(2.0)
        except Exception:
            pass

    results: list[dict[str, str]] = []
    seen_urls: set[str] = set()

    # Per ogni post nel feed, raccogliamo:
    # - URL autore (cercato prima nei heading h2/h3/strong, poi fallback generico)
    # - Testo del post (per snippet)
    # In un post di gruppo l'autore appare in formato vario: profilo globale
    # (/vanity o /profile.php?id=N) oppure "member-of-group"
    # (/groups/<gid>/user/<uid>/) — _normalize_author_url gestisce entrambi.
    AUTHOR_LINK_SELECTORS_IN_POST = [
        'h2 a[role="link"]',
        'h3 a[role="link"]',
        'h4 a[role="link"]',
        'strong a[role="link"]',
        # Fallback: tutti i link role=link che puntano a FB o relativi
        'a[role="link"][href*="facebook.com"], a[role="link"][href^="/"]',
    ]
    for sel in GROUP_POST_CONTAINERS:
        try:
            posts = page.locator(sel)
            n_posts = await posts.count()
            if n_posts == 0:
                continue
            _log(f"selettore '{sel}' → {n_posts} post")
            for i in range(min(n_posts, 100)):
                try:
                    post = posts.nth(i)
                    author_url = None
                    author_name = None
                    # Prova selettori in ordine di specificità
                    for link_sel in AUTHOR_LINK_SELECTORS_IN_POST:
                        if author_url:
                            break
                        try:
                            author_links = post.locator(link_sel)
                            n_links = await author_links.count()
                        except Exception:
                            continue
                        for j in range(min(n_links, 8)):
                            try:
                                a = author_links.nth(j)
                                href = await a.get_attribute("href") or ""
                                txt = (await a.text_content() or "").strip()
                                if not href:
                                    continue
                                if href.startswith("/"):
                                    href = "https://www.facebook.com" + href
                                # _normalize_author_url accetta anche
                                # /groups/<gid>/user/<uid>/ (=member-of-group)
                                norm = _normalize_author_url(href)
                                if not norm:
                                    continue
                                if not facebook_recon._is_plausible_name(txt):
                                    continue
                                author_url = norm
                                author_name = txt
                                break
                            except Exception:
                                continue
                    if not author_url or author_url in seen_urls:
                        continue

                    # Snippet: testo del post (primo div data-ad-comet-preview="message")
                    snippet = ""
                    try:
                        msg = post.locator('div[data-ad-comet-preview="message"]').first
                        if await msg.count() > 0:
                            snippet = (await msg.text_content() or "").strip()[:200]
                    except Exception:
                        pass

                    seen_urls.add(author_url)
                    results.append({
                        "url": author_url,
                        "name": author_name or "",
                        "snippet": snippet,
                    })
                    if len(results) >= limit:
                        break
                except Exception:
                    continue
            if results:
                break
        except Exception:
            continue

    _log(f"→ {len(results)} autori unici")
    return results


# === Primitive 4: friends-of-profile (se pubblica) ===

async def friends_of_profile(
    page: "Page",
    profile_url: str,
    *,
    limit: int = 50,
    jlog=None,
) -> list[dict[str, str]]:
    """Apre la pagina /friends di un profilo FB (se pubblica) e raccoglie
    gli amici visibili. Funziona solo se l'utente target ha la friend list
    pubblica (Public) o visibile al loggato (es. amico di amico).

    Args:
        page: Playwright Page su browser FB loggato.
        profile_url: URL profilo (NON sezione friends — viene composta).
        limit: cap amici da raccogliere.
        jlog: log callback.

    Returns:
        Lista di dict {'url': str, 'name': str}. Vuota se la friend list non
        è visibile.
    """
    def _log(msg: str) -> None:
        log.info("[fb-audience] friends_of: %s", msg)
        if jlog:
            jlog(f"friends_of: {msg}")

    if not profile_url:
        return []

    # Componi URL /friends. Se è già /friends, usalo. Altrimenti aggiungi /friends.
    if "/friends" not in profile_url:
        friends_url = profile_url.rstrip("/") + "/friends"
    else:
        friends_url = profile_url

    _log(f"goto {friends_url}")
    try:
        await page.goto(friends_url, wait_until="domcontentloaded", timeout=30_000)
    except Exception as e:
        _log(f"goto error: {e}")
        return []

    try:
        await page.wait_for_load_state("networkidle", timeout=12_000)
    except Exception:
        pass
    await asyncio.sleep(3.0)
    await _lazy_load_scroll(page, times=5, delay=1.5)

    # Heuristica per detectare "lista non visibile":
    # se il body contiene "Questa lista è vuota" o "Nessun amico da mostrare",
    # esci subito.
    try:
        body = (await page.evaluate("document.body.innerText") or "")[:5000].lower()
        if any(marker in body for marker in (
            "questa lista è vuota",
            "nessun amico da mostrare",
            "no friends to show",
            "this list is empty",
        )):
            _log("friend list non visibile o vuota")
            return []
    except Exception:
        pass

    results: list[dict[str, str]] = []
    seen_urls: set[str] = set()

    # Self-URL: lo escludiamo dai risultati (l'utente target sarebbe sempre
    # primo nella lista come "header" della pagina).
    self_norm = facebook_recon._normalize_fb_profile_url(profile_url)

    for sel in FRIENDS_LINKS_SELECTORS:
        try:
            anchors = page.locator(sel)
            n = await anchors.count()
            if n == 0:
                continue
            _log(f"selettore '{sel}' → {n} ancore")
            for i in range(min(n, 300)):
                try:
                    a = anchors.nth(i)
                    href = await a.get_attribute("href") or ""
                    txt = (await a.text_content() or "").strip()
                    if not href:
                        continue
                    if href.startswith("/"):
                        href = "https://www.facebook.com" + href
                    norm = facebook_recon._normalize_fb_profile_url(href)
                    if not norm or norm in seen_urls:
                        continue
                    if norm == self_norm:
                        continue  # esclude il target stesso
                    if not facebook_recon._is_plausible_name(txt):
                        continue
                    seen_urls.add(norm)
                    results.append({"url": norm, "name": txt})
                    if len(results) >= limit:
                        break
                except Exception:
                    continue
            if results:
                break
        except Exception:
            continue

    _log(f"→ {len(results)} amici visibili")
    return results
