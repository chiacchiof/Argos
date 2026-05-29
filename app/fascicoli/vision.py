"""OCR via Vision LLM su Ollama.

Pipeline:
1. Render pagina PDF -> PNG (via pypdfium2, niente Poppler native install).
2. Encode PNG in base64.
3. Chiamata Ollama `/api/generate` con `images: [<b64>]` + prompt OCR italiano.
4. Concatena le risposte per-pagina.

Modello di default: `llama3.2-vision` (Meta, buon trade-off OCR/latency).
Override con env `ARGOS_PDF_VISION_MODEL`. Se vuoto, OCR vision disabilitato.

Performance: render a ~150 DPI (scale 2.0) e' sufficiente per OCR di scansioni
da camera/scanner. Vision LLM su CPU e' lento: ~30-90 sec per pagina su CPU
decente, ~5-15 sec su GPU. Cap pagine via `ARGOS_PDF_VISION_MAX_PAGES`.
"""
from __future__ import annotations

import base64
import io
import logging
import os
from pathlib import Path

import httpx

log = logging.getLogger(__name__)


DEFAULT_VISION_MODEL_ENV = "ARGOS_PDF_VISION_MODEL"
DEFAULT_VISION_DPI_ENV = "ARGOS_PDF_VISION_DPI"
DEFAULT_VISION_MAX_PAGES_ENV = "ARGOS_PDF_VISION_MAX_PAGES"

DEFAULT_PROMPT = (
    "Estrai TUTTO il testo presente in questa pagina di un documento italiano. "
    "Mantieni la struttura (titoli, paragrafi, liste, tabelle). "
    "Riporta solo il testo estratto, senza commenti aggiuntivi, senza "
    "introduzioni del tipo 'Ecco il testo:'. Se la pagina e' vuota o non "
    "contiene testo leggibile, rispondi con una stringa vuota."
)


def vision_model() -> str | None:
    """Modello configurato per OCR via Vision LLM. None = OCR disabilitato."""
    v = (os.environ.get(DEFAULT_VISION_MODEL_ENV) or "").strip()
    return v or None


def is_enabled() -> bool:
    return vision_model() is not None


def _dpi() -> int:
    raw = (os.environ.get(DEFAULT_VISION_DPI_ENV) or "").strip()
    try:
        v = int(raw)
        return max(72, min(v, 300))
    except (ValueError, TypeError):
        return 150


def _max_pages() -> int:
    raw = (os.environ.get(DEFAULT_VISION_MAX_PAGES_ENV) or "").strip()
    try:
        v = int(raw)
        return max(1, min(v, 500))
    except (ValueError, TypeError):
        return 50


def _ollama_base() -> str:
    return (os.environ.get("OLLAMA_BASE_URL") or "http://localhost:11434").rstrip("/")


def _render_page_to_png_b64(page, dpi: int) -> str:
    """Render una pagina pypdfium2 a PNG base64."""
    # scale = dpi / 72 (default PDF e' 72 dpi)
    scale = dpi / 72.0
    bitmap = page.render(scale=scale)
    pil_img = bitmap.to_pil()
    buf = io.BytesIO()
    pil_img.save(buf, format="PNG", optimize=True)
    return base64.b64encode(buf.getvalue()).decode("ascii")


def ocr_pdf(pdf_path: Path, *, model: str | None = None, prompt: str | None = None) -> str | None:
    """Esegue OCR su un PDF intero usando un Vision LLM Ollama.

    Ritorna il testo aggregato (pagine separate da "\\n\\n---\\n\\n") o None se:
    - OCR disabilitato (nessun modello configurato)
    - pypdfium2 non riesce ad aprire il PDF
    - tutte le chiamate vision falliscono
    """
    chosen_model = model or vision_model()
    if not chosen_model:
        log.info("vision OCR skip: nessun modello configurato (set %s)", DEFAULT_VISION_MODEL_ENV)
        return None

    try:
        import pypdfium2 as pdfium
    except ImportError:
        log.error("pypdfium2 non installato — OCR via vision disabilitato")
        return None

    try:
        doc = pdfium.PdfDocument(str(pdf_path))
    except Exception as exc:
        log.warning("pdf open failed for %s: %s", pdf_path, exc)
        return None

    dpi = _dpi()
    max_pages = _max_pages()
    n_pages = len(doc)
    if n_pages > max_pages:
        log.info(
            "vision OCR: pdf ha %d pagine, processo solo le prime %d (cap %s)",
            n_pages, max_pages, DEFAULT_VISION_MAX_PAGES_ENV,
        )
        n_pages = max_pages

    base_url = _ollama_base()
    prompt_text = prompt or DEFAULT_PROMPT
    page_texts: list[str] = []
    ok_pages = 0

    # Client con timeout alto: vision LLM possono richiedere minuti per pagina.
    with httpx.Client(timeout=300.0) as client:
        for i in range(n_pages):
            try:
                page = doc[i]
                b64 = _render_page_to_png_b64(page, dpi=dpi)
            except Exception as exc:
                log.warning("render page %d failed: %s", i, exc)
                page_texts.append("")
                continue

            try:
                resp = client.post(
                    f"{base_url}/api/generate",
                    json={
                        "model": chosen_model,
                        "prompt": prompt_text,
                        "images": [b64],
                        "stream": False,
                    },
                )
                resp.raise_for_status()
                data = resp.json()
                txt = (data.get("response") or "").strip()
                page_texts.append(txt)
                if txt:
                    ok_pages += 1
            except httpx.HTTPError as exc:
                log.warning("ollama vision call failed page %d: %s", i, exc)
                page_texts.append("")

    doc.close()

    if ok_pages == 0:
        log.warning("vision OCR su %s: 0 pagine con testo", pdf_path.name)
        return None

    return "\n\n---\n\n".join(p for p in page_texts if p)
