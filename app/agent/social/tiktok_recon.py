"""TikTok recon — selettori + extract_profile_data per recon_social.

TikTok ha un layout abbastanza stabile per il profilo utente. Usiamo
i `data-e2e` come selettori principali (più stabili del CSS class anonimi).
"""
from __future__ import annotations

import asyncio
import logging
import random
import re
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

if TYPE_CHECKING:
    from playwright.async_api import Page

log = logging.getLogger(__name__)


# === Selettori TikTok ===

USERNAME_LABEL = '[data-e2e="user-title"]'
DISPLAY_NAME = '[data-e2e="user-subtitle"]'
BIO_TEXT = '[data-e2e="user-bio"]'
FOLLOWING_COUNT = '[data-e2e="following-count"]'
FOLLOWER_COUNT = '[data-e2e="followers-count"]'
LIKES_COUNT = '[data-e2e="likes-count"]'

# Video thumbnails (timeline profile)
VIDEO_ITEMS = '[data-e2e="user-post-item"]'

# Private profile / age-gated
PRIVATE_PROFILE = [
    '[data-e2e="user-private"]',
    'p:has-text("This account is private")',
    'p:has-text("Questo account è privato")',
]

# Login wall (non sempre presente, TikTok web è quite permissive per browse)
LOGIN_WALL = [
    'div:has-text("Log in to TikTok")',
    'div:has-text("Accedi a TikTok")',
]


async def search_user_by_name(
    page: "Page", name: str, *, jlog=None
) -> tuple[str | None, str]:
    """Cerca un utente TikTok per nome → ritorna URL profilo.

    TikTok ha `https://www.tiktok.com/@<username>` come URL canonico.
    Strategia (come IG): se è un singolo token, assumi sia l'handle.
    Per full-name search agentico: R2.
    """
    def _log(msg: str) -> None:
        log.info("[tt-search] %s", msg)
        if jlog:
            jlog(msg)

    n = (name or "").strip()
    if not n:
        return None, "empty_name"

    import re
    if re.match(r"^[A-Za-z0-9._]{1,30}$", n) and " " not in n:
        handle = n.lstrip("@")
        url = f"https://www.tiktok.com/@{handle}"
        _log(f"🔍 TikTok: '{n}' sembra uno username → uso {url}")
        return url, f"username_guess: {handle}"

    _log(f"⚠️ TikTok: '{n}' contiene spazi — search by full name non supportato in R1.")
    return None, "full_name_search_not_supported_in_r1"


def _tt_handle_from_url(profile_url: str) -> str | None:
    try:
        p = urlparse(profile_url)
    except Exception:
        return None
    if not p.hostname or "tiktok.com" not in p.hostname:
        return None
    parts = [s for s in (p.path or "").split("/") if s]
    if not parts:
        return None
    first = parts[0]
    if first.startswith("@"):
        h = first[1:]
        if re.match(r"^[A-Za-z0-9._]{1,30}$", h):
            return h
    return None


async def collect_subpage_texts(
    page: "Page",
    safe,
    profile_url: str,
    *,
    jlog=None,
    max_chars_per_page: int = 4000,
) -> dict[str, str]:
    """Visita la tab /playlists del profilo TikTok e scrolla la video grid.

    TikTok: il main feed del profilo è la "video grid" — già visibile sulla
    pagina principale. Aggiungiamo solo /playlists (interessi tematici
    organizzati dall'utente) + uno scroll extra sulla pagina principale per
    catturare più video.
    """
    def _log(msg: str) -> None:
        log.info("[tt-sub] %s", msg)
        if jlog:
            jlog(msg)

    out: dict[str, str] = {}
    handle = _tt_handle_from_url(profile_url)
    if not handle:
        _log(f"      handle non derivabile da {profile_url}")
        return out

    # /playlists: tab "Playlist" del profilo
    pages = [
        ("/playlists", f"https://www.tiktok.com/@{handle}/playlist"),
    ]
    for label, url in pages:
        try:
            await safe.safe_goto(url, label=f"tt_subpage_{label}")
            try:
                await page.wait_for_load_state("networkidle", timeout=8_000)
            except Exception:
                pass
            await asyncio.sleep(random.uniform(2.0, 3.5))
            try:
                await page.mouse.wheel(0, 800)
                await asyncio.sleep(1.5)
            except Exception:
                pass

            try:
                body_text = await page.evaluate("document.body.innerText")
            except Exception:
                body_text = ""
            body_text = (body_text or "")[:max_chars_per_page]

            if len(body_text.strip()) < 150:
                _log(f"      sub-page '{label}': body troppo corto, skip")
                continue
            out[label] = body_text
            _log(f"      sub-page '{label}': {len(body_text)} char raccolti")
        except Exception as e:
            _log(f"      sub-page '{label}' fail: {type(e).__name__}: {e}")
            continue

    return out


async def extract_profile_data(page: "Page") -> dict[str, Any]:
    """Estrae dati strutturati da una pagina profilo TikTok aperta."""
    data: dict[str, Any] = {
        "platform": "tiktok",
        "url": page.url,
        "username": None,
        "display_name": None,
        "bio_text": None,
        "following": None,
        "followers": None,
        "likes_total": None,
        "is_private": False,
        "video_count_visible": 0,
        "title": None,
        "error": None,
    }

    try:
        await page.wait_for_load_state("networkidle", timeout=8_000)
    except Exception:
        pass

    # Private detection
    for sel in PRIVATE_PROFILE:
        try:
            if await page.locator(sel).first.is_visible(timeout=400):
                data["is_private"] = True
                break
        except Exception:
            continue

    try:
        data["title"] = await page.title()
    except Exception:
        pass

    # Username
    try:
        loc = page.locator(USERNAME_LABEL).first
        if await loc.is_visible(timeout=2_000):
            data["username"] = (await loc.text_content() or "").strip().lstrip("@")
    except Exception:
        pass

    # Display name
    try:
        loc = page.locator(DISPLAY_NAME).first
        if await loc.is_visible(timeout=1_500):
            data["display_name"] = (await loc.text_content() or "").strip()
    except Exception:
        pass

    # Bio
    try:
        loc = page.locator(BIO_TEXT).first
        if await loc.is_visible(timeout=1_500):
            data["bio_text"] = (await loc.text_content() or "").strip()[:600]
    except Exception:
        pass

    # Counter (Following / Followers / Likes)
    for key, sel in (
        ("following", FOLLOWING_COUNT),
        ("followers", FOLLOWER_COUNT),
        ("likes_total", LIKES_COUNT),
    ):
        try:
            loc = page.locator(sel).first
            if await loc.is_visible(timeout=800):
                data[key] = (await loc.text_content() or "").strip()
        except Exception:
            pass

    # Conta video visibili (no scroll, primi caricati)
    if not data["is_private"]:
        try:
            items = page.locator(VIDEO_ITEMS)
            data["video_count_visible"] = await items.count()
        except Exception:
            pass

    return data


# === Following list loader ===
# TikTok: /@<username>/following è una scheda nel profilo. Non sempre pubblica
# (l'utente può nasconderla). Strategia: vai a /@<my>/following e scrolla.

async def load_following_list(
    page: "Page",
    safe,
    my_username: str | None,
    *,
    jlog=None,
    max_scrolls: int = 10,
    debug_dir=None,
) -> dict[str, str]:
    """Carica la lista 'following' dell'utente TikTok loggato.

    Ritorna {nome_visualizzato_lowercase: profile_url}. Best-effort.
    Se la lista è privata o non accessibile, ritorna dict vuoto.
    """
    def _log(msg: str) -> None:
        log.info("[tt-following] %s", msg)
        if jlog:
            jlog(msg)

    if not my_username:
        _log("  ❌ my_username non fornito")
        return {}
    handle = my_username.lstrip("@")
    url = f"https://www.tiktok.com/@{handle}/following"

    try:
        await safe.safe_goto(url, label="tt_following_list")
    except Exception as e:
        _log(f"  goto fail: {e}")
        return {}
    try:
        await page.wait_for_load_state("networkidle", timeout=10_000)
    except Exception:
        pass
    await asyncio.sleep(3.0)

    # Scroll progressivo
    for _ in range(max_scrolls):
        try:
            await page.mouse.wheel(0, 1000)
            await asyncio.sleep(random.uniform(1.0, 1.6))
        except Exception:
            break

    # Estrai anchor a profili (URL pattern /@<handle>)
    feed_selectors = [
        'a[href*="/@"]',
    ]
    anchors = None
    for sel in feed_selectors:
        try:
            loc = page.locator(sel)
            if await loc.count() > 5:
                anchors = loc
                break
        except Exception:
            continue
    if not anchors:
        _log("  ⚠️ nessun anchor profilo")
        return {}

    n_total = await anchors.count()
    _log(f"  {n_total} anchor candidati")
    out: dict[str, str] = {}
    debug_rows: list[str] = []
    for i in range(min(n_total, 500)):
        try:
            a = anchors.nth(i)
            href = await a.get_attribute("href") or ""
            if "/@" not in href:
                continue
            # Estrai handle dal primo segmento "@..."
            m = re.search(r"/@([A-Za-z0-9._]{2,30})(?:/|$|\?)", href)
            if not m:
                continue
            h = m.group(1)
            label = (await a.text_content() or "").strip()[:120]
            if not label:
                label = h
            clean = f"https://www.tiktok.com/@{h}"
            debug_rows.append(f"{i:3d} | clean={clean!r:50s} | label={label!r:40s}")
            key = label.lower().strip()
            if key not in out:
                out[key] = clean
            h_key = h.lower()
            if h_key not in out:
                out[h_key] = clean
        except Exception:
            continue

    _log(f"  ✅ following list estratta: {len(out)} entries")

    if debug_dir is not None and debug_rows:
        try:
            from pathlib import Path as _P
            dd = _P(str(debug_dir))
            dd.mkdir(parents=True, exist_ok=True)
            (dd / "tt_following_anchors.txt").write_text(
                "\n".join(debug_rows), encoding="utf-8"
            )
        except Exception:
            pass

    return out


def match_friend(name_query: str, following_list: dict[str, str]) -> tuple[str, str] | None:
    """Cerca `name_query` nella following list TikTok pre-caricata."""
    if not name_query or not following_list:
        return None
    q = name_query.lower().strip().lstrip("@")
    if q in following_list:
        return (q, following_list[q])
    tokens = q.split()
    if not tokens:
        return None
    for friend_name, url in following_list.items():
        if all(t in friend_name for t in tokens):
            return (friend_name, url)
    if len(tokens) > 1:
        cognome = tokens[-1]
        for friend_name, url in following_list.items():
            if cognome in friend_name:
                return (friend_name, url)
    return None
