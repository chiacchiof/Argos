"""Funzioni di umanizzazione del comportamento browser-side.

Tutte cross-piattaforma: Instagram/TikTok/qualsiasi sito Playwright. Si dividono
in due famiglie:

1. **Input umani**: typing con delay random per carattere, click via mouse
   move + click invece di JS click diretto, scroll con velocita' variabile.
2. **Pause naturali**: wait random tra azioni, durata sessione random,
   distribuzione oraria realistica.

Razionale: i sistemi anti-bot moderni (Datadome, Akamai BotManager, Instagram's
"Detect Suspicious Activity") confrontano la *temporal signature* delle azioni
con database di pattern umani noti. Click istantaneo o typing 1000 char/sec =
detection certa.

Niente importa di app.agent (modulo isolato per riuso futuro).
"""
from __future__ import annotations

import asyncio
import math
import random
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from playwright.async_api import Page


# === Pause realistiche ===


async def human_wait(min_s: float = 1.0, max_s: float = 3.0) -> None:
    """Pause random tra azioni. Default 1-3s tipica per 'leggere'."""
    await asyncio.sleep(random.uniform(min_s, max_s))


async def reading_pause(text_length: int = 100) -> None:
    """Simula lettura di un testo: ~200 WPM = ~3.3 chars/sec, +random."""
    chars_per_sec = random.uniform(2.5, 4.5)
    base = max(0.8, text_length / chars_per_sec)
    await asyncio.sleep(base + random.uniform(0, 0.5))


async def idle_session(min_s: float = 30.0, max_s: float = 120.0) -> None:
    """Idle simulando momenti di attenzione altrove (lettura, cambio tab, ecc.)."""
    await asyncio.sleep(random.uniform(min_s, max_s))


def random_session_duration_min() -> float:
    """Durata realistica di una sessione utente social: 5-45 min con bias 10-20."""
    # Distribuzione lognormal-like via mistura di gaussiane
    if random.random() < 0.6:
        return random.gauss(15, 5)
    return random.gauss(30, 10)


# === Mouse + click umani ===


def _bezier_curve_points(
    start: tuple[float, float],
    end: tuple[float, float],
    steps: int = 25,
) -> list[tuple[float, float]]:
    """Genera punti di una curva di Bezier quadratica con punto di controllo random.

    Il movimento mouse umano NON e' una linea retta: ha curvatura naturale, a
    volte overshoot del target. Riproduciamo quello.
    """
    x0, y0 = start
    x1, y1 = end
    # Punto di controllo: random nel rettangolo che contiene start+end, leggermente offset
    cx = (x0 + x1) / 2 + random.uniform(-50, 50)
    cy = (y0 + y1) / 2 + random.uniform(-50, 50)
    pts = []
    for i in range(steps + 1):
        t = i / steps
        x = (1 - t) ** 2 * x0 + 2 * (1 - t) * t * cx + t ** 2 * x1
        y = (1 - t) ** 2 * y0 + 2 * (1 - t) * t * cy + t ** 2 * y1
        pts.append((x, y))
    return pts


async def human_click(page: "Page", selector: str) -> None:
    """Click umano: mouse move con curva di Bezier + small jitter + click + wait."""
    el = page.locator(selector).first
    box = await el.bounding_box()
    if box is None:
        # Fallback: click standard se l'elemento non ha box (es. e' clipped)
        await el.click(delay=random.randint(40, 110))
        return
    # Target con leggero jitter dentro il box (non sempre al centro esatto)
    target_x = box["x"] + box["width"] * random.uniform(0.3, 0.7)
    target_y = box["y"] + box["height"] * random.uniform(0.3, 0.7)
    # Punto di partenza: posizione corrente del mouse o random sulla viewport
    start_x = random.uniform(100, 800)
    start_y = random.uniform(100, 600)
    # Muovi step-by-step
    pts = _bezier_curve_points((start_x, start_y), (target_x, target_y), steps=20)
    for x, y in pts:
        await page.mouse.move(x, y, steps=1)
        await asyncio.sleep(random.uniform(0.005, 0.02))
    # Piccola pausa pre-click
    await asyncio.sleep(random.uniform(0.05, 0.2))
    await page.mouse.click(target_x, target_y, delay=random.randint(40, 110))


async def human_type(page: "Page", selector: str, text: str) -> None:
    """Type con delay random per carattere (60-200ms) + pause su punteggiatura."""
    el = page.locator(selector).first
    await el.click(delay=random.randint(40, 110))
    await asyncio.sleep(random.uniform(0.2, 0.5))
    for ch in text:
        await el.press(ch, delay=random.randint(60, 200))
        # Pause leggermente piu' lunghe dopo punteggiatura
        if ch in ".!?,;:":
            await asyncio.sleep(random.uniform(0.15, 0.4))


async def human_scroll(page: "Page", n: int = 3, direction: str = "down") -> None:
    """Scroll con velocita' variabile + pause."""
    multiplier = 1 if direction == "down" else -1
    for _ in range(n):
        delta = multiplier * random.uniform(300, 700)
        await page.mouse.wheel(0, delta)
        await asyncio.sleep(random.uniform(0.5, 2.0))


async def random_idle_action(page: "Page") -> None:
    """Azione random di 'distrazione': scroll su/giu', hover su elemento random.

    Da chiamare ogni N azioni 'utili' per inserire rumore comportamentale.
    """
    action = random.choice(["scroll_down", "scroll_up", "hover_random", "wait"])
    if action == "scroll_down":
        await human_scroll(page, n=random.randint(1, 3), direction="down")
    elif action == "scroll_up":
        await human_scroll(page, n=random.randint(1, 2), direction="up")
    elif action == "hover_random":
        try:
            x = random.uniform(100, 1100)
            y = random.uniform(100, 700)
            await page.mouse.move(x, y, steps=random.randint(5, 15))
        except Exception:
            pass
    await human_wait(0.5, 2.0)


# === Distribuzione oraria ===


def is_active_hour(now_hour: int | None = None) -> bool:
    """True se l'ora corrente e' una "active hour" plausibile per uso social.

    Default: 9-22 locale. Fuori da questa finestra, NON inviare DM.
    """
    import datetime as _dt
    if now_hour is None:
        now_hour = _dt.datetime.now().hour
    return 9 <= now_hour < 22


def random_gap_between_dms_min() -> float:
    """Gap random tra DM consecutivi della stessa sessione: 8-30 min.

    DEPRECATO per uso diretto: usa `pick_gap_minutes(platform_name, task)`
    che onora override per-task (B-011) e default per platform.
    Restano valori IG/TikTok (anti-ban aggressivo); WA usa range piu' breve.
    """
    return random.uniform(8, 30)


# B-011: default per platform. Una platform "calda" come WhatsApp con account
# reale tollera gap piccoli; IG/TikTok hanno rate-limiting piu' aggressivo.
# Tutto in minuti (float). Override per-task ha priorità (vedi pick_gap_minutes).
DEFAULT_GAP_RANGE_MIN: dict[str, tuple[float, float]] = {
    "whatsapp_browser": (0.15, 0.35),  # 9-21 secondi
    "instagram":        (8.0, 30.0),
    "tiktok":           (8.0, 30.0),
    "facebook":         (8.0, 30.0),
}


def default_gap_range_min(platform_name: str) -> tuple[float, float]:
    """Range (min, max) di default in minuti per `platform_name`.
    Fallback (8, 30) per platform sconosciuta (comportamento legacy)."""
    return DEFAULT_GAP_RANGE_MIN.get(platform_name, (8.0, 30.0))


def pick_gap_minutes(
    platform_name: str,
    *,
    task_min: float | None = None,
    task_max: float | None = None,
) -> float:
    """Sceglie un gap (minuti) per il prossimo DM.

    Precedenza:
      1) Se `task_min` e `task_max` entrambi valorizzati → uniform(min, max).
      2) Se solo uno valorizzato → fix point (no jitter).
      3) Altrimenti → default per platform da `DEFAULT_GAP_RANGE_MIN`.
    Sanitizza min<=max.
    """
    if task_min is not None and task_max is not None:
        lo, hi = float(task_min), float(task_max)
        if lo > hi:
            lo, hi = hi, lo
        return random.uniform(lo, hi)
    if task_min is not None:
        return float(task_min)
    if task_max is not None:
        return float(task_max)
    lo, hi = default_gap_range_min(platform_name)
    return random.uniform(lo, hi)
