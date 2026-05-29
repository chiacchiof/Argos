"""Retrieval-Augmented chat per i fascicoli.

Vector store: file JSON `.argos/embeddings.json` con lista di chunk
{file, idx, text, embedding}. Per progetti <10k chunk e' veloce.

Pipeline:
1. `index_project(...)`: scan -> extract text -> chunk -> embed -> dump JSON.
2. `query`: embed della domanda -> cosine top-k contro tutti i chunk.
3. `answer`: top-k -> prompt context -> chiamata Ollama /api/chat -> risposta.

Modello chat di default: env `ARGOS_CHAT_MODEL` o `qwen3-coder:30b` (vedi
memorie del progetto sul perche' coder-tuned funzionano meglio).
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Callable

import httpx

from . import db as fdb
from . import embed as femb
from . import fs as ffs
from . import ingest

log = logging.getLogger(__name__)

EMBEDDINGS_FILE = "embeddings.json"
INDEX_SCHEMA_VERSION = 1
DEFAULT_CHAT_MODEL = "qwen3-coder:30b"


def _index_path(project_folder: Path) -> Path:
    return project_folder / ffs.ARGOS_FOLDER / EMBEDDINGS_FILE


def chat_model() -> str:
    return os.environ.get("ARGOS_CHAT_MODEL") or DEFAULT_CHAT_MODEL


def load_index(project_folder: Path) -> dict | None:
    """Legge `.argos/embeddings.json`. None se manca / non parseabile."""
    p = _index_path(project_folder)
    if not p.is_file():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        log.warning("embeddings.json non leggibile per %s", project_folder)
        return None


def index_summary(project_folder: Path | None) -> dict[str, Any]:
    """Ritorna {ready, n_chunks, n_files, model} per la UI del detail."""
    if not project_folder:
        return {"ready": False, "n_chunks": 0, "n_files": 0, "model": None}
    idx = load_index(project_folder)
    if not idx:
        return {"ready": False, "n_chunks": 0, "n_files": 0, "model": None}
    chunks = idx.get("chunks") or []
    files = {c.get("file") for c in chunks if c.get("file")}
    return {
        "ready": bool(chunks),
        "n_chunks": len(chunks),
        "n_files": len(files),
        "model": idx.get("model"),
    }


def index_project(
    project_id: int,
    project_folder: Path,
    *,
    progress: "Callable[..., None] | None" = None,
) -> dict[str, int]:
    """Estrae testo da ogni file supportato, chunking, embedding, salva indice.
    Sovrascrive l'indice precedente.

    `progress` opzionale: callback chiamata a ogni transizione di fase con
    keyword args (phase, current_file, files_total, files_done, chunks_total,
    chunks_done, message). Usata da `app.fascicoli.index_jobs` per la barra
    di avanzamento UI.

    Ritorna summary {files_processed, chunks, skipped_unsupported, skipped_empty,
    skipped_embedding}.
    """
    def _p(**kw):
        if progress is not None:
            try:
                progress(**kw)
            except Exception:
                log.exception("progress callback raised — proseguo")

    chunks_data: list[dict] = []
    files_processed = 0
    skipped_unsupported = 0
    skipped_empty = 0
    skipped_embedding = 0

    # Fase 1: enumera i file e separa supportati da non supportati. Costa 1 stat
    # per file, veloce. Ci serve `files_total` per la barra.
    _p(phase="scanning", message="Scansione cartella…", current_file=None)
    candidates: list[dict] = []
    for info in ffs.iter_project_files(project_folder):
        ext = info["_abs_path"].suffix.lower()
        if ext in ingest.SUPPORTED:
            candidates.append(info)
        else:
            skipped_unsupported += 1

    files_total = len(candidates)
    _p(
        phase="extracting",
        files_total=files_total,
        files_done=0,
        message=f"{files_total} file da elaborare",
    )

    # Fase 2: per ogni file -> extract text -> chunk -> embed con progresso
    # per-chunk (cosi' la barra si muove anche dentro un singolo file lungo).
    for fi, info in enumerate(candidates):
        rel = info["relative_path"]
        abs_path = info["_abs_path"]
        _p(
            phase="extracting",
            current_file=rel,
            files_done=fi,
            message=f"Estrazione testo: {rel}",
        )
        text = ingest.extract_text(abs_path)
        if not text or not text.strip():
            skipped_empty += 1
            continue
        chunks = ingest.chunk_text(text, chunk_size=1000, overlap=200)
        if not chunks:
            skipped_empty += 1
            continue

        _p(
            phase="embedding",
            current_file=rel,
            chunks_total=len(chunks),
            chunks_done=0,
            message=f"Embedding di {len(chunks)} chunk: {rel}",
        )

        # Embedding chunk per chunk, in modo da aggiornare la barra.
        # Performance: nomic-embed-text e' veloce (~30-80 ms per chunk su CPU
        # decente). Per accelerare poi si puo' parallelizzare con asyncio.
        embeddings: list[list[float]] = []
        embed_failed = False
        for ci, text_chunk in enumerate(chunks):
            try:
                vecs = femb.embed_texts([text_chunk])
            except Exception as exc:
                log.warning("embedding failed for %s chunk %d: %s", rel, ci, exc)
                embed_failed = True
                break
            embeddings.append(vecs[0] if vecs else [])
            _p(chunks_done=ci + 1)

        if embed_failed:
            skipped_embedding += 1
            continue

        added = 0
        for i, (text_chunk, vec) in enumerate(zip(chunks, embeddings)):
            if not vec:
                continue
            chunks_data.append({
                "file": rel,
                "idx": i,
                "text": text_chunk,
                "embedding": vec,
            })
            added += 1
        if added:
            fdb.mark_file_indexed(project_id, rel)
            files_processed += 1
        else:
            skipped_embedding += 1

        _p(files_done=fi + 1)

    # Fase 3: dump JSON.
    _p(
        phase="saving",
        current_file=None,
        message="Salvataggio indice…",
    )
    payload = {
        "schema_version": INDEX_SCHEMA_VERSION,
        "model": femb.embed_model(),
        "chunks": chunks_data,
    }
    out = _index_path(project_folder)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    return {
        "files_processed": files_processed,
        "chunks": len(chunks_data),
        "skipped_unsupported": skipped_unsupported,
        "skipped_empty": skipped_empty,
        "skipped_embedding": skipped_embedding,
    }


def remove_file_from_index(project_folder: Path, relative_path: str) -> int:
    """Rimuove dall'indice tutti i chunk relativi a `relative_path`.

    Ritorna il numero di chunk rimossi. Riscrive `.argos/embeddings.json` solo
    se almeno un chunk è stato rimosso. Idempotente: chiamarla due volte
    ritorna 0 al secondo giro.
    """
    idx = load_index(project_folder)
    if not idx:
        return 0
    chunks = idx.get("chunks") or []
    new_chunks = [c for c in chunks if c.get("file") != relative_path]
    removed = len(chunks) - len(new_chunks)
    if removed > 0:
        idx["chunks"] = new_chunks
        _index_path(project_folder).write_text(
            json.dumps(idx, ensure_ascii=False),
            encoding="utf-8",
        )
    return removed


def retrieve(project_folder: Path, query: str, *, k: int = 5) -> list[dict]:
    """Top-k chunk per cosine sim contro l'embedding della query.
    Ritorna lista di {file, idx, text, score}. Lista vuota se indice mancante.
    """
    idx = load_index(project_folder)
    if not idx:
        return []
    chunks = idx.get("chunks") or []
    if not chunks:
        return []
    q_embs = femb.embed_texts([query])
    q_emb = q_embs[0] if q_embs else []
    if not q_emb:
        log.warning("retrieve: embedding query vuoto")
        return []
    scored: list[tuple[float, dict]] = []
    for c in chunks:
        s = femb.cosine(q_emb, c.get("embedding") or [])
        scored.append((s, c))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [
        {"file": c["file"], "idx": c["idx"], "text": c["text"], "score": float(s)}
        for s, c in scored[:k]
    ]


def _build_messages(
    *,
    chunks: list[dict],
    query: str,
    history: list[dict] | None = None,
) -> list[dict[str, str]]:
    """Costruisce la lista `messages` per Ollama /api/chat con context RAG.

    Estratto come helper perche' lo riusano sia `answer` (sync) sia
    `answer_stream` (async generator).
    """
    ctx = "\n\n".join(
        f"[Fonte: {c['file']} | chunk {c['idx']}]\n{c['text']}"
        for c in chunks
    )
    sys_prompt = (
        "Sei l'assistente di Argos Fascicoli. Rispondi in italiano usando "
        "**markdown** quando aiuta a chiarezza (grassetto, liste puntate, "
        "blocchi codice), basandoti SOLO sulle fonti fornite qui sotto. "
        "Le fonti possono essere documenti (PDF/DOCX/...) oppure FOGLI di calcolo "
        "indicati come «Foglio «titolo»»: questi ultimi sono tabelle in formato "
        "TSV con intestazioni di colonna (A, B, C...) e numeri di riga — "
        "interpretane righe e colonne per rispondere su dati, totali, valori. "
        "Se la risposta non e' nelle fonti, dillo chiaramente senza inventare. "
        "Quando usi un'informazione cita la fonte fra parentesi quadre, "
        "es. [contratto-acme.pdf] oppure [Foglio «Budget»]."
    )
    messages: list[dict[str, str]] = [{"role": "system", "content": sys_prompt}]
    if history:
        for m in history[-6:]:
            if m.get("role") in ("user", "assistant"):
                messages.append({"role": m["role"], "content": m["content"]})
    messages.append({
        "role": "user",
        "content": f"FONTI DAL FASCICOLO:\n\n{ctx}\n\nDOMANDA: {query}",
    })
    return messages


async def answer_stream(
    project_folder: Path,
    query: str,
    *,
    top_k: int = 5,
    history: list[dict] | None = None,
    chunks: list[dict] | None = None,
):
    """Async generator: yields chunk di testo (`str`) dalla risposta LLM
    via Ollama streaming (`/api/chat` con `stream=true`).

    Se `chunks` e' None, fa retrieval con `top_k`. Se non trova chunks,
    yields un singolo messaggio fallback e termina.

    Eccezioni httpx vengono propagate al chiamante (l'endpoint le cattura
    e le forwarda come evento SSE 'error').
    """
    if chunks is None:
        chunks = retrieve(project_folder, query, k=top_k)

    if not chunks:
        yield (
            "Non ho trovato informazioni nei documenti del fascicolo. "
            "Hai indicizzato dei file? Premi l'icona 🧠 nella sezione File "
            "per costruire l'indice."
        )
        return

    messages = _build_messages(chunks=chunks, query=query, history=history)
    base = femb.ollama_base()
    model = chat_model()
    url = f"{base}/api/chat"

    async with httpx.AsyncClient(timeout=300.0) as client:
        async with client.stream(
            "POST", url,
            json={"model": model, "messages": messages, "stream": True},
        ) as resp:
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                if not line or not line.strip():
                    continue
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    log.debug("ignored non-json line from ollama stream: %r", line[:120])
                    continue
                delta = (data.get("message") or {}).get("content") or ""
                if delta:
                    yield delta
                if data.get("done"):
                    break


def answer(
    project_folder: Path,
    query: str,
    *,
    top_k: int = 5,
    history: list[dict] | None = None,
) -> dict[str, Any]:
    """Genera la risposta retrieval-augmented.
    Ritorna {answer, citations: [{file, score}], n_chunks_used}.
    """
    chunks = retrieve(project_folder, query, k=top_k)
    if not chunks:
        return {
            "answer": (
                "Non ho trovato informazioni nei documenti del fascicolo. "
                "Hai indicizzato dei file? Premi l'icona 🧠 nella sezione File "
                "per costruire l'indice."
            ),
            "citations": [],
            "n_chunks_used": 0,
        }
    messages = _build_messages(chunks=chunks, query=query, history=history)

    base = femb.ollama_base()
    model = chat_model()
    url = f"{base}/api/chat"
    try:
        with httpx.Client(timeout=180.0) as client:
            r = client.post(url, json={
                "model": model,
                "messages": messages,
                "stream": False,
            })
            r.raise_for_status()
            data = r.json()
            reply = (data.get("message") or {}).get("content") or "(nessuna risposta)"
    except httpx.HTTPError as exc:
        log.exception("rag.answer: chiamata Ollama fallita")
        reply = (
            f"Errore nella chiamata al modello LLM ({model}): {exc}. "
            "Verifica che Ollama sia attivo e che il modello sia installato."
        )
    return {
        "answer": (reply or "").strip(),
        "citations": [
            {"file": c["file"], "score": round(c["score"], 4)} for c in chunks
        ],
        "n_chunks_used": len(chunks),
    }
