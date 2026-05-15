"""Instagram recon — selettori + extract_profile_data per recon_social.

IG ha 2 layout: webapp standard (con login) e public-profile (no login).
Recon target è la webapp standard (account loggato).
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


# === Selettori IG ===

PROFILE_HEADER = 'header section'
USERNAME_TITLE = 'header h2'
DISPLAY_NAME = 'header section span > span'
BIO_TEXT = 'header section h1 + div'  # bio sotto il display name (fragile)
BIO_FALLBACK = 'header section div span'

# Counter (post / followers / following)
COUNTERS_LIST = 'header section ul li'

# Post grid (con thumbnails)
POST_THUMBS = 'article a[href*="/p/"]'

# Indicatore profilo privato
PRIVATE_PROFILE = [
    'h2:has-text("This Account is Private")',
    'h2:has-text("Questo account è privato")',
]

# Login wall
LOGIN_WALL = [
    'div:has-text("Accedi")',
    'div:has-text("Log in")',
    'a:has-text("Iscriviti")',
]


async def search_user_by_name(
    page: "Page", name: str, *, jlog=None
) -> tuple[str | None, str]:
    """Cerca un utente IG per nome → ritorna URL profilo.

    IG ha un search dialog interattivo difficile da automatizzare in modo
    affidabile. Strategia pragmatica: se il `name` sembra già uno username
    (no spazi, alfanumerico), assumiamo che sia direttamente l'handle e
    costruisci `https://www.instagram.com/<handle>/`.

    Per nomi multi-token ("Mario Rossi"): non supportato in R1, ritorna None
    con reason chiara. R2 avrà tool dedicato `search_users` agentico.
    """
    def _log(msg: str) -> None:
        log.info("[ig-search] %s", msg)
        if jlog:
            jlog(msg)

    n = (name or "").strip()
    if not n:
        return None, "empty_name"

    # Se è un singolo token alfanumerico → probabilmente è già lo username
    import re
    if re.match(r"^[A-Za-z0-9._]{1,30}$", n) and " " not in n:
        handle = n.lstrip("@")
        url = f"https://www.instagram.com/{handle}/"
        _log(f"🔍 IG: '{n}' sembra uno username → uso {url}")
        return url, f"username_guess: {handle}"

    _log(f"⚠️ IG: '{n}' contiene spazi/caratteri speciali — search by full name "
         "non supportato in R1. Inserisci direttamente l'handle (es. 'mario.rossi') "
         "o un URL completo (https://instagram.com/mario.rossi).")
    return None, "full_name_search_not_supported_in_r1"


def _ig_username_from_url(profile_url: str) -> str | None:
    try:
        p = urlparse(profile_url)
    except Exception:
        return None
    if not p.hostname or "instagram.com" not in p.hostname:
        return None
    parts = [s for s in (p.path or "").split("/") if s]
    if not parts:
        return None
    handle = parts[0].lstrip("@")
    if not re.match(r"^[A-Za-z0-9._]{1,30}$", handle):
        return None
    return handle


async def collect_subpage_texts(
    page: "Page",
    safe,
    profile_url: str,
    *,
    jlog=None,
    max_chars_per_page: int = 4000,
) -> dict[str, str]:
    """Visita /reels e /tagged del profilo IG. Ritorna {label: body_text}.

    /reels: griglia dei reel del soggetto (interessi attivi: cosa pubblica)
    /tagged: foto/video in cui è taggato da altri (cosa fa nella vita)
    """
    def _log(msg: str) -> None:
        log.info("[ig-sub] %s", msg)
        if jlog:
            jlog(msg)

    out: dict[str, str] = {}
    handle = _ig_username_from_url(profile_url)
    if not handle:
        _log(f"      handle non derivabile da {profile_url}")
        return out

    pages = [
        ("/reels", f"https://www.instagram.com/{handle}/reels/"),
        ("/tagged", f"https://www.instagram.com/{handle}/tagged/"),
    ]
    for label, url in pages:
        try:
            await safe.safe_goto(url, label=f"ig_subpage_{label}")
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

            if len(body_text.strip()) < 150:
                _log(f"      sub-page '{label}': body troppo corto, skip")
                continue
            low = body_text.lower()
            if ("log in" in low or "accedi" in low) and len(body_text) < 600:
                _log(f"      sub-page '{label}': login wall, skip")
                continue
            # IG mostra "Reels" o "Tagged" come heading; conferma minima
            out[label] = body_text
            _log(f"      sub-page '{label}': {len(body_text)} char raccolti")
        except Exception as e:
            _log(f"      sub-page '{label}' fail: {type(e).__name__}: {e}")
            continue

    return out


async def extract_profile_data(page: "Page") -> dict[str, Any]:
    """Estrae dati strutturati da una pagina profilo IG aperta."""
    data: dict[str, Any] = {
        "platform": "instagram",
        "url": page.url,
        "username": None,
        "display_name": None,
        "bio_text": None,
        "posts_count": None,
        "followers": None,
        "following": None,
        "is_private": False,
        "recent_post_urls": [],
        "title": None,
        "error": None,
    }

    try:
        await page.wait_for_load_state("networkidle", timeout=8_000)
    except Exception:
        pass

    # Detection privato
    for sel in PRIVATE_PROFILE:
        try:
            if await page.locator(sel).first.is_visible(timeout=400):
                data["is_private"] = True
                break
        except Exception:
            continue

    # Detection login wall (= non loggato)
    for sel in LOGIN_WALL:
        try:
            if await page.locator(sel).first.is_visible(timeout=400):
                data["error"] = "login_wall"
                return data
        except Exception:
            continue

    try:
        data["title"] = await page.title()
    except Exception:
        pass

    # Username (in header h2)
    try:
        loc = page.locator(USERNAME_TITLE).first
        if await loc.is_visible(timeout=2_000):
            data["username"] = (await loc.text_content() or "").strip()
    except Exception:
        pass

    # Display name — prova multipli selettori (layout IG cambia), poi validazione
    display_name_selectors = [
        'header section h1',                # layout 2026
        'header section span > span',       # fallback layout vecchio
    ]
    for _dn_sel in display_name_selectors:
        try:
            loc = page.locator(_dn_sel).first
            if await loc.is_visible(timeout=1_500):
                val = (await loc.text_content() or "").strip()
                if val:
                    data["display_name"] = val
                    break
        except Exception:
            continue

    # Validazione display_name: se è puramente numerico → ha pescato un counter
    # follower/following/posts. Fallback su og:title / page title / username URL.
    _dn = (data.get("display_name") or "").strip()
    if not _dn or _dn.isdigit() or (len(_dn) <= 3 and any(ch.isdigit() for ch in _dn)):
        data["display_name"] = None
        # 1. og:title (es. "Carmen Faro (@_carmen_faro_) • Instagram photos and videos")
        try:
            og_loc = page.locator('meta[property="og:title"]').first
            og = await og_loc.get_attribute("content", timeout=1_500)
            if og:
                m = re.match(r"^(.+?)\s*\(@", og)
                if m:
                    cand = m.group(1).strip()
                    if cand and not cand.isdigit():
                        data["display_name"] = cand
        except Exception:
            pass
        # 2. page title parsing (stesso pattern)
        if not data.get("display_name"):
            try:
                t = data.get("title") or await page.title()
                if t:
                    m = re.match(r"^(.+?)\s*\(@", t)
                    if m:
                        cand = m.group(1).strip()
                        if cand and not cand.isdigit():
                            data["display_name"] = cand
            except Exception:
                pass
        # 3. fallback finale: username dall'URL (sempre meglio di un numero)
        if not data.get("display_name"):
            handle = _ig_username_from_url(page.url)
            if handle:
                data["display_name"] = handle

    # Bio
    try:
        loc = page.locator(BIO_TEXT).first
        if await loc.is_visible(timeout=1_500):
            data["bio_text"] = (await loc.text_content() or "").strip()[:800]
        else:
            loc2 = page.locator(BIO_FALLBACK).first
            if await loc2.is_visible(timeout=800):
                data["bio_text"] = (await loc2.text_content() or "").strip()[:800]
    except Exception:
        pass

    # Counters (post / followers / following)
    try:
        counters = page.locator(COUNTERS_LIST)
        n = await counters.count()
        if n >= 3:
            for i, key in enumerate(("posts_count", "followers", "following")):
                if i < n:
                    txt = (await counters.nth(i).text_content() or "").strip()
                    data[key] = txt
    except Exception:
        pass

    # Post URLs (max 12)
    posts: list[str] = []
    if not data["is_private"]:
        try:
            links = page.locator(POST_THUMBS)
            n = await links.count()
            for i in range(min(n, 12)):
                href = await links.nth(i).get_attribute("href")
                if href:
                    if not href.startswith("http"):
                        href = "https://www.instagram.com" + href
                    posts.append(href)
        except Exception:
            pass
    data["recent_post_urls"] = posts

    return data


# === Following list loader (analog di friend list FB, asimmetrico) ===
# IG: la lista "following" si apre in modale su /<username>/following/.
# Modale è interno alla pagina con role="dialog", scroll incrementale.

async def load_following_list(
    page: "Page",
    safe,
    my_username: str | None,
    *,
    jlog=None,
    max_scrolls: int = 12,
    debug_dir=None,
) -> dict[str, str]:
    """Carica la lista 'following' dell'utente IG loggato.

    Ritorna {nome_visualizzato_lowercase: profile_url}. Best-effort.

    NB: IG following è una MODALE — non puoi navigare direttamente all'URL,
    devi aprire il profilo dell'utente loggato e cliccare il count "following".
    Comportamento alternative: vai a /<username>/ , aspetta, e clicca su "following".
    Strategia: navighiamo a `instagram.com/<my_username>/following/` e IG di
    solito apre la modale automaticamente.
    """
    def _log(msg: str) -> None:
        log.info("[ig-following] %s", msg)
        if jlog:
            jlog(msg)

    if not my_username:
        _log("  ❌ my_username non fornito (richiede l'username del tuo account IG)")
        return {}

    url = f"https://www.instagram.com/{my_username}/following/"
    try:
        await safe.safe_goto(url, label="ig_following_list")
    except Exception as e:
        _log(f"  goto fail: {e}")
        return {}
    try:
        await page.wait_for_load_state("networkidle", timeout=10_000)
    except Exception:
        pass
    await asyncio.sleep(3.0)

    # Trova la modale del following (oppure il pannello full-page se IG lo
    # serve direttamente). Scroll dentro la modale per lazy-load.
    modal_selectors = [
        'div[role="dialog"]',
        '[aria-label*="ollowing"]',
    ]
    container = None
    for sel in modal_selectors:
        try:
            loc = page.locator(sel).first
            if await loc.is_visible(timeout=1_000):
                container = loc
                break
        except Exception:
            continue
    if container is None:
        # Fallback: scroll su body
        container = page.locator("body").first

    for _ in range(max_scrolls):
        try:
            # Scroll dentro il container (se è la modale)
            await container.evaluate("el => el.scrollBy(0, 800)")
        except Exception:
            try:
                await page.mouse.wheel(0, 800)
            except Exception:
                break
        await asyncio.sleep(random.uniform(1.0, 1.8))

    # Estrai i link a profili
    feed_selectors = [
        'div[role="dialog"] a[href^="/"]',
        'div[role="main"] a[href^="/"]',
        'a[role="link"][href^="/"]',
    ]
    anchors = None
    for sel in feed_selectors:
        try:
            loc = page.locator(sel)
            n = await loc.count()
            if n > 5:
                anchors = loc
                break
        except Exception:
            continue
    if not anchors:
        _log("  ⚠️ nessun anchor profilo trovato")
        return {}

    n_total = await anchors.count()
    _log(f"  {n_total} anchor candidati nella modale following")
    out: dict[str, str] = {}
    debug_rows: list[str] = []
    for i in range(min(n_total, 500)):
        try:
            a = anchors.nth(i)
            href = await a.get_attribute("href")
            if not href or "/p/" in href or "/reel/" in href or "/explore/" in href:
                continue  # skip post/reel/explore links
            handle = href.strip("/").split("/")[0].lstrip("@")
            if not re.match(r"^[A-Za-z0-9._]{2,30}$", handle):
                continue
            # Su IG la modale following mostra display_name + handle: prendi
            # come label l'innerText dell'ancora (di solito il handle)
            label = (await a.text_content() or "").strip()[:120]
            if not label:
                label = handle
            clean = f"https://www.instagram.com/{handle}/"
            debug_rows.append(f"{i:3d} | clean={clean!r:55s} | label={label!r:40s}")
            key = label.lower().strip()
            if key not in out:
                out[key] = clean
            # Aggiungi anche per handle come key separata (utile per match handle-only)
            h_key = handle.lower()
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
            (dd / "ig_following_anchors.txt").write_text(
                "\n".join(debug_rows), encoding="utf-8"
            )
        except Exception:
            pass

    return out


_IG_NAV_BLACKLIST = {
    "reels", "explore", "direct", "stories", "accounts", "p", "about",
    "press", "api", "developer", "jobs", "privacy", "terms", "locations",
    "hashtag", "tv", "challenge", "legal", "blog", "help", "ads",
    "your_activity", "saved", "settings", "graphql",
}


async def enumerate_followers_of_target(
    page: "Page",
    safe,
    target_handle: str,
    *,
    cap: int = 100,
    jlog=None,
    debug_dir=None,
) -> list[dict[str, str]]:
    """Enumera i FOLLOWER di un account IG target.

    Diverso da `load_following_list` (che è "chi io seguo"). Apre il profilo
    target, **clicca sul link 'follower'** (NON via URL diretto — IG 2026
    cambia layout e la URL `/followers/` può non aprire la modale), aspetta
    la modale `div[role="dialog"]`, poi scrolla finché cap raggiunto o
    fine lista.

    Strict: se la modale non si apre, ABORT (no fallback body) per evitare
    di pescare i link della NAV IG (Home/Reels/Direct/...) come falsi positivi.

    Ritorna list di {handle, display_name, profile_url}. Best-effort.
    """
    def _log(msg: str) -> None:
        log.info("[ig-followers] %s", msg)
        if jlog:
            jlog(msg)

    target_handle = (target_handle or "").strip().lstrip("@")
    if not target_handle or not re.match(r"^[A-Za-z0-9._]{1,30}$", target_handle):
        _log(f"  ❌ target_handle non valido: {target_handle!r}")
        return []

    profile_url = f"https://www.instagram.com/{target_handle}/"
    try:
        await safe.safe_goto(profile_url, label=f"ig_target_{target_handle}")
        try:
            await page.wait_for_load_state("networkidle", timeout=10_000)
        except Exception:
            pass
        await asyncio.sleep(random.uniform(2.5, 4.0))
    except Exception as e:
        _log(f"  goto target fail: {e}")
        return []

    # Click sul link "follower" del profilo. Strategia: trovare l'anchor con
    # href contenente "/followers" (o anche aria-label/testo con 'follower'/'seguaci')
    # e cliccarlo. Apre la modale IG con role=dialog.
    follower_link_selectors = [
        f'a[href="/{target_handle}/followers/"]',
        f'a[href*="/{target_handle}/followers"]',
        'a[href$="/followers/"]',
        'a[href*="/followers"]:not([href*="/web"])',
        'a:has-text("follower")',
        'a:has-text("Follower")',
        'a:has-text("seguaci")',
        'a:has-text("Seguaci")',
    ]
    clicked = False
    for sel in follower_link_selectors:
        try:
            loc = page.locator(sel).first
            if await loc.is_visible(timeout=1_500):
                await loc.click(timeout=5_000)
                _log(f"  click su link follower: {sel!r}")
                clicked = True
                break
        except Exception:
            continue
    if not clicked:
        _log(f"  ⚠️ link 'follower' non trovato nel profilo @{target_handle}. "
             f"Profilo privato o layout sconosciuto.")
        if debug_dir is not None:
            try:
                from pathlib import Path as _P
                dd = _P(str(debug_dir))
                dd.mkdir(parents=True, exist_ok=True)
                safe_n = re.sub(r"[^A-Za-z0-9_]+", "_", target_handle)[:60]
                await page.screenshot(path=str(dd / f"ig_no_followlink_{safe_n}.png"))
            except Exception:
                pass
        return []

    # Aspetta modale role=dialog visibile
    try:
        await page.wait_for_load_state("networkidle", timeout=6_000)
    except Exception:
        pass
    await asyncio.sleep(random.uniform(1.5, 2.5))

    modal_selectors = [
        'div[role="dialog"][aria-label*="ollower"]',
        'div[role="dialog"][aria-label*="eguaci"]',
        'div[role="dialog"]',
    ]
    container = None
    used_sel = None
    for sel in modal_selectors:
        try:
            loc = page.locator(sel).first
            if await loc.is_visible(timeout=3_000):
                container = loc
                used_sel = sel
                break
        except Exception:
            continue
    if container is None:
        _log(f"  ❌ modale follower non aperta dopo click. Abort.")
        if debug_dir is not None:
            try:
                from pathlib import Path as _P
                dd = _P(str(debug_dir))
                dd.mkdir(parents=True, exist_ok=True)
                safe_n = re.sub(r"[^A-Za-z0-9_]+", "_", target_handle)[:60]
                await page.screenshot(path=str(dd / f"ig_no_modal_{safe_n}.png"))
            except Exception:
                pass
        return []
    _log(f"  ✅ modale follower aperta ({used_sel})")

    # Scroll incrementale DENTRO la modale finché cap o fine. Anchor pescati
    # SOLO da dentro la modale (selettore con ancestor role=dialog).
    out: dict[str, dict[str, str]] = {}
    consecutive_no_growth = 0
    max_total_scrolls = max(30, cap // 2)
    _log(f"  inizio enumeration, cap={cap}, max_scrolls={max_total_scrolls}")

    # OPTIMIZATION: trova il container scrollable UNA SOLA VOLTA (era O(N) ad
    # ogni scroll). Marca con __ig_sc_pinned__ così le misure successive sono O(1).
    # Bug fix di "scroll esponenzialmente più lento": prima si chiamava
    # querySelectorAll('*') + getComputedStyle su ogni node ad ogni scroll,
    # con modal che cresceva → reflow costoso che scalava O(n_followers²).
    try:
        pin_info = await container.evaluate("""
            (root) => {
                if (root.__ig_sc_pinned__) {
                    const sc = root.__ig_sc_pinned__;
                    const r = sc.getBoundingClientRect();
                    return { rect_x: r.left, rect_y: r.top, rect_w: r.width, rect_h: r.height,
                             tag: sc.tagName, pinned: true };
                }
                const all = root.querySelectorAll('*');
                for (const child of all) {
                    const cs = getComputedStyle(child);
                    const ov = cs.overflowY;
                    if ((ov === 'auto' || ov === 'scroll') && child.scrollHeight > child.clientHeight + 5) {
                        root.__ig_sc_pinned__ = child;
                        const r = child.getBoundingClientRect();
                        return { rect_x: r.left, rect_y: r.top, rect_w: r.width, rect_h: r.height,
                                 tag: child.tagName + '.' + (child.className || '').split(' ').slice(0, 2).join('.'),
                                 pinned: false };
                    }
                }
                root.__ig_sc_pinned__ = root;
                const r = root.getBoundingClientRect();
                return { rect_x: r.left, rect_y: r.top, rect_w: r.width, rect_h: r.height,
                         tag: 'root.fallback', pinned: false };
            }
        """)
        cx = pin_info.get("rect_x", 0) + pin_info.get("rect_w", 400) / 2
        cy = pin_info.get("rect_y", 0) + pin_info.get("rect_h", 400) / 2 + 40
        _log(f"  📌 scrollable container pinned: <{pin_info.get('tag')}> @({cx:.0f},{cy:.0f})")
    except Exception as e:
        log.warning("pin scrollable fail, fallback to keyboard End: %s", e)
        cx, cy = 632, 446  # fallback coords

    for scroll_i in range(max_total_scrolls):
        before_n = len(out)
        try:
            # OPTIMIZATION: estrai TUTTI gli anchor in UNA SOLA evaluate JS.
            # Prima: 2N IPC Playwright per scroll (get_attribute + text_content
            # per ogni anchor); a scroll #30 = 800 IPC = ~30s di solo overhead.
            # Ora: 1 IPC che ritorna lista [{href, text}, ...] → ~50ms.
            anchor_data = await page.evaluate("""
                () => {
                    const anchors = document.querySelectorAll(
                        'div[role="dialog"] a[role="link"][href^="/"]'
                    );
                    const out = [];
                    for (const a of anchors) {
                        out.push({
                            href: a.getAttribute('href') || '',
                            text: (a.textContent || '').trim().slice(0, 120),
                        });
                        if (out.length >= 800) break;
                    }
                    return out;
                }
            """)
            for ad in anchor_data:
                try:
                    href = ad.get("href") or ""
                    if not href:
                        continue
                    # Filter: solo URL profilo (/handle/), skip /p/, /reel/, /explore/, /direct/, ecc.
                    if any(bad in href for bad in ("/p/", "/reel/", "/explore/", "/direct/", "/stories/")):
                        continue
                    handle = href.strip("/").split("/")[0].lstrip("@")
                    if not handle:
                        continue
                    if handle.lower() in _IG_NAV_BLACKLIST:
                        continue
                    if not re.match(r"^[A-Za-z0-9._]{2,30}$", handle):
                        continue
                    if handle == target_handle:
                        continue  # skip il target stesso
                    if handle in out:
                        continue
                    label = (ad.get("text") or "")[:120]
                    display_name = label or handle
                    out[handle] = {
                        "handle": handle,
                        "display_name": display_name,
                        "profile_url": f"https://www.instagram.com/{handle}/",
                    }
                    if len(out) >= cap:
                        break
                except Exception:
                    continue
        except Exception as e:
            _log(f"  scroll {scroll_i} extract fail: {e}")

        if len(out) >= cap:
            _log(f"  ✅ cap {cap} raggiunto allo scroll #{scroll_i}")
            break

        after_n = len(out)
        if after_n == before_n:
            consecutive_no_growth += 1
            if consecutive_no_growth >= 4:
                _log(f"  ⏹ nessun nuovo follower dopo 4 scroll: stop a {after_n}")
                break
        else:
            consecutive_no_growth = 0

        # Scroll dentro la modale. IG ha un guard anti-bot che rifiuta scroll
        # PROGRAMMATICI (element.scrollBy / scrollTop=N): non triggera il
        # lazy-load di nuovi follower. Soluzione: WHEEL umano via mouse.wheel.
        # Misuriamo top/height SOLO sul container pinned (O(1), non O(n²)).
        try:
            # Pre-measure leggera (no findScrollable)
            before_info = await container.evaluate("""
                (root) => {
                    const sc = root.__ig_sc_pinned__ || root;
                    return { top: sc.scrollTop, height: sc.scrollHeight };
                }
            """)
            before_top = before_info.get("top", 0)
            before_height = before_info.get("height", 0)

            # Humanize wheel: varianza maggiore → meno pattern-matching antibot.
            # - 2-5 wheel per burst (era fisso 3)
            # - step 180-520px per wheel (era fisso 320)
            # - pause 50-900ms tra wheel (era 150-400ms)
            # - micro mouse-move tra wheel (±15px x, ±8px y)
            # - ogni ~10 scroll, scroll-up accidentale -120/-260px poi continua
            try:
                # Mouse move iniziale con leggera jitter
                jx = cx + random.uniform(-12, 12)
                jy = cy + random.uniform(-8, 8)
                await page.mouse.move(jx, jy)

                # Scroll-up accidentale ogni 8-12 scroll (umano "rilegge" una riga sopra)
                if scroll_i > 4 and scroll_i % random.randint(8, 12) == 0:
                    up_amount = random.randint(120, 260)
                    await page.mouse.wheel(0, -up_amount)
                    await asyncio.sleep(random.uniform(0.4, 1.1))

                # Burst principale
                n_wheels = random.randint(2, 5)
                for _w in range(n_wheels):
                    step = random.randint(180, 520)
                    await page.mouse.wheel(0, step)
                    # Micro mouse-move tra wheel (umano sposta leggermente il mouse)
                    if random.random() < 0.35:
                        await page.mouse.move(
                            cx + random.uniform(-18, 18),
                            cy + random.uniform(-10, 10),
                        )
                    # Pause inter-wheel: distribuzione a coda lunga (occasionale 0.8-1.2s)
                    pause = random.uniform(0.05, 0.45)
                    if random.random() < 0.18:
                        pause = random.uniform(0.6, 1.4)
                    await asyncio.sleep(pause)
            except Exception as we:
                log.debug("mouse.wheel fail: %s", we)
                # Fallback: scroll programmatico via container pinned
                await container.evaluate(
                    "(root) => { const sc = root.__ig_sc_pinned__ || root; sc.scrollBy(0, 1200); }"
                )

            # Post-measure leggera (no findScrollable)
            after_info = await container.evaluate("""
                (root) => {
                    const sc = root.__ig_sc_pinned__ || root;
                    return { top: sc.scrollTop, height: sc.scrollHeight };
                }
            """)
            after_top = after_info.get("top", 0)
            after_height = after_info.get("height", 0)

            if scroll_i % 5 == 0:
                _log(
                    f"  scroll #{scroll_i}: scrollTop {before_top:.0f}→{after_top:.0f}, "
                    f"height {before_height}→{after_height}, "
                    f"followers_seen={len(out)}"
                )
            # Se scrollTop non avanza E height non cresce → niente più follower
            if after_top == before_top and after_height == before_height:
                consecutive_no_growth = max(consecutive_no_growth, 3)
        except Exception as e:
            log.debug("scroll evaluate fail: %s", e)
            try:
                await page.keyboard.press("End")
            except Exception:
                break
        # Pause finale tra burst: più varianza (era 1.5-2.8s)
        await asyncio.sleep(random.uniform(0.6, 2.2))

    result = list(out.values())[:cap]
    _log(f"  ✅ enumerati {len(result)} follower di @{target_handle}")

    if debug_dir is not None and result:
        try:
            from pathlib import Path as _P
            dd = _P(str(debug_dir))
            dd.mkdir(parents=True, exist_ok=True)
            safe_name = re.sub(r"[^A-Za-z0-9_]+", "_", target_handle)[:60]
            lines = [f"{f['handle']}\t{f['display_name']}\t{f['profile_url']}" for f in result]
            (dd / f"ig_followers_{safe_name}.txt").write_text(
                "\n".join(lines), encoding="utf-8"
            )
        except Exception:
            pass

    return result


def match_friend(name_query: str, following_list: dict[str, str]) -> tuple[str, str] | None:
    """Cerca `name_query` nella following list IG pre-caricata.

    Strategia: stesso pattern di facebook_recon.match_friend.
    """
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
