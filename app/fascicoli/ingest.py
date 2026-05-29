"""Estrazione testo + chunking dai file dei fascicoli.

Formati supportati in v1:
- PDF  (via pypdf, gia' in dependencies)
- TXT  (lettura diretta UTF-8 con errors=replace)
- MD / MARKDOWN (idem)
- EML  (parsing via email std lib; estrae text/plain piu' Subject/From)

DOCX, XLSX, PPTX, P7M (PEC) restano fuori dalla v1: richiedono librerie
aggiuntive (python-docx, openpyxl, asn1crypto) che decideremo se aggiungere
quando avremo feedback reale da fornitori italiani.
"""
from __future__ import annotations

import email
import logging
import re
from pathlib import Path

log = logging.getLogger(__name__)


# Estensioni gestibili dal pipeline RAG. Il sync registra TUTTI i file in
# `project_files`; questo set decide quali finiscono nell'indice embedding.
SUPPORTED = {".pdf", ".txt", ".md", ".markdown", ".eml"}


def extract_text(path: Path) -> str | None:
    """Restituisce il testo estratto da `path`, o None se formato non supportato
    / errore di lettura."""
    ext = path.suffix.lower()
    if ext in {".txt", ".md", ".markdown"}:
        try:
            return path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            log.warning("read failed for %s: %s", path, exc)
            return None
    if ext == ".pdf":
        return _extract_pdf(path)
    if ext == ".eml":
        return _extract_eml(path)
    return None


# Sotto questa soglia di caratteri "utili" (non whitespace) consideriamo
# l'estrazione fallita e provo il fallback successivo.
_PDF_TEXT_MIN_USEFUL = 200


def _useful_chars(text: str) -> int:
    """Conta caratteri non-whitespace. Utile per decidere se l'estrazione e'
    riuscita (alcuni PDF tornano migliaia di '\\n' e zero contenuto)."""
    return sum(1 for c in (text or "") if not c.isspace())


def _extract_pdf_pypdf(path: Path) -> str:
    """Estrazione layer testuale standard via pypdf. Funziona per PDF generati
    da Word/InDesign/LaTeX. Fallisce silenziosamente su PDF scansione/immagine."""
    try:
        from pypdf import PdfReader
    except ImportError:
        log.error("pypdf non disponibile")
        return ""
    try:
        reader = PdfReader(str(path))
    except Exception as exc:
        log.warning("pypdf open failed for %s: %s", path, exc)
        return ""
    pages: list[str] = []
    for i, page in enumerate(reader.pages):
        try:
            t = page.extract_text() or ""
            pages.append(t)
        except Exception as exc:
            log.debug("pypdf page %d extract failed (%s): %s", i, path.name, exc)
            pages.append("")
    return "\n\n".join(pages)


def _extract_pdf_pdfplumber(path: Path) -> str:
    """Fallback: pdfplumber spesso recupera testo dove pypdf fallisce, in
    particolare su PDF con encoding strani prodotti da gestionali italiani
    (Fatture in Cloud / Aruba / Danea). Rallenta di ~2-3x rispetto a pypdf
    ma per PDF medi resta sotto il secondo per pagina."""
    try:
        import pdfplumber
    except ImportError:
        log.error("pdfplumber non disponibile (cfr. pyproject.toml)")
        return ""
    try:
        pages: list[str] = []
        with pdfplumber.open(str(path)) as pdf:
            for i, page in enumerate(pdf.pages):
                try:
                    t = page.extract_text() or ""
                    pages.append(t)
                except Exception as exc:
                    log.debug("pdfplumber page %d extract failed (%s): %s", i, path.name, exc)
                    pages.append("")
        return "\n\n".join(pages)
    except Exception as exc:
        log.warning("pdfplumber open failed for %s: %s", path, exc)
        return ""


def _extract_pdf(path: Path) -> str | None:
    """Pipeline a tre livelli con fallback automatico:

      1. pypdf (veloce, copre PDF testuali)
      2. pdfplumber (recupera dove pypdf fallisce per encoding strani)
      3. Vision LLM OCR (se ARGOS_PDF_VISION_MODEL e' settato) per scansioni

    Ritorna il primo testo "utile" trovato (≥ `_PDF_TEXT_MIN_USEFUL` chars
    non-whitespace). Se anche la vision fallisce, ritorna il meglio tra i
    primi due livelli (anche se sotto soglia) per non perdere segnale.
    """
    # Livello 1: pypdf
    t1 = _extract_pdf_pypdf(path)
    n1 = _useful_chars(t1)
    if n1 >= _PDF_TEXT_MIN_USEFUL:
        log.info("pdf %s: pypdf ok (%d chars utili)", path.name, n1)
        return t1

    # Livello 2: pdfplumber
    log.info("pdf %s: pypdf scarso (%d chars), provo pdfplumber", path.name, n1)
    t2 = _extract_pdf_pdfplumber(path)
    n2 = _useful_chars(t2)
    if n2 >= _PDF_TEXT_MIN_USEFUL:
        log.info("pdf %s: pdfplumber ok (%d chars utili)", path.name, n2)
        return t2

    # Livello 3: Vision LLM OCR (se configurato)
    from . import vision
    if vision.is_enabled():
        log.info(
            "pdf %s: pdfplumber scarso (%d chars), provo OCR vision (%s)",
            path.name, n2, vision.vision_model(),
        )
        t3 = vision.ocr_pdf(path)
        if t3:
            n3 = _useful_chars(t3)
            log.info("pdf %s: vision OCR ok (%d chars utili)", path.name, n3)
            return t3
        log.warning("pdf %s: vision OCR non ha prodotto testo", path.name)
    else:
        log.info("pdf %s: vision OCR disabilitato (ARGOS_PDF_VISION_MODEL non settato)", path.name)

    # Niente di utile. Ritorna il migliore tra i due fallback testuali (per
    # log/debug; il chiamante poi triggera "nessun testo" nell'UI).
    return t2 if n2 > n1 else t1


def _extract_eml(path: Path) -> str | None:
    try:
        msg = email.message_from_bytes(path.read_bytes())
    except OSError as exc:
        log.warning("EML open failed for %s: %s", path, exc)
        return None
    parts: list[str] = []
    subj = msg.get("Subject", "") or ""
    sender = msg.get("From", "") or ""
    to = msg.get("To", "") or ""
    date = msg.get("Date", "") or ""
    parts.append(f"Subject: {subj}\nFrom: {sender}\nTo: {to}\nDate: {date}\n")
    if msg.is_multipart():
        for p in msg.walk():
            if p.get_content_type() == "text/plain":
                payload = p.get_payload(decode=True)
                if payload:
                    charset = p.get_content_charset() or "utf-8"
                    try:
                        parts.append(payload.decode(charset, errors="replace"))
                    except (LookupError, UnicodeDecodeError):
                        parts.append(payload.decode("utf-8", errors="replace"))
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            charset = msg.get_content_charset() or "utf-8"
            try:
                parts.append(payload.decode(charset, errors="replace"))
            except (LookupError, UnicodeDecodeError):
                parts.append(payload.decode("utf-8", errors="replace"))
    return "\n".join(parts).strip() or None


def chunk_text(text: str, *, chunk_size: int = 1000, overlap: int = 200) -> list[str]:
    """Recursive-style character chunker.

    1. Trim + skip vuoto.
    2. Split per double-newline (paragrafi) -> accumula finche' sotto soglia.
    3. Paragrafo singolo > soglia -> sliding window con overlap.

    Mantiene un overlap per non perdere context al confine tra chunks.
    """
    text = (text or "").strip()
    if not text:
        return []
    if len(text) <= chunk_size:
        return [text]

    paras = re.split(r"\n\s*\n", text)
    chunks: list[str] = []
    cur = ""
    for para in paras:
        para = para.strip()
        if not para:
            continue
        if len(cur) + len(para) + 2 <= chunk_size:
            cur = (cur + "\n\n" + para) if cur else para
            continue
        if cur:
            chunks.append(cur)
            cur = ""
        if len(para) > chunk_size:
            step = max(1, chunk_size - overlap)
            for i in range(0, len(para), step):
                chunks.append(para[i:i + chunk_size])
        else:
            cur = para
    if cur:
        chunks.append(cur)
    return chunks
