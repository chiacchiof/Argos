from __future__ import annotations

import base64
import asyncio
import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator

import httpx
from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse

from .. import db
from ..agent.llm_providers import (
    env_key_status,
    get_provider,
    list_providers,
    resolve_api_key,
    resolve_base_url,
)
from ..agent.ollama import list_models
from ..agent.tools.fetch_http import fetch_http
from ..agent.tools.search import web_search
from ..config import UPLOADS_DIR, settings
from ..orchestrator import (
    AUTONOMY_LEVELS,
    OrchestratorPlan,
    autonomy_meta,
    build_plan,
    execute_plan,
)
from ..templates import templates


router = APIRouter()

CHAT_FILE_MAX_BYTES = 5 * 1024 * 1024
CHAT_FILE_CONTEXT_CHARS = 40_000
CHAT_HISTORY_FILE_CONTEXT_CHARS = 8_000
CHAT_TOOL_MAX_LOOPS = 3
CHAT_MAX_TOKENS = 420
CHAT_ALLOWED_FILE_EXTENSIONS = {
    ".txt",
    ".md",
    ".csv",
    ".json",
    ".jsonl",
    ".ndjson",
    ".html",
    ".htm",
    ".xml",
    ".log",
    ".py",
    ".js",
    ".ts",
    ".css",
    ".sql",
    ".yaml",
    ".yml",
    ".pdf",
}
PDF_EXTENSION = ".pdf"

CHAT_WEB_TOOLS_SPEC = [
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "Cerca sul web e ritorna risultati con titolo, URL e snippet.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Query di ricerca."},
                    "max_results": {
                        "type": "integer",
                        "description": "Numero di risultati, massimo 8.",
                        "default": 5,
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "fetch_url",
            "description": "Scarica una pagina web e ritorna testo principale, titolo e status.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "URL completo http(s)."},
                },
                "required": ["url"],
            },
        },
    },
]

CHAT_TOOL_CAPABLE_PROVIDERS = {"openai", "anthropic", "gemini", "grok", "custom"}
CHAT_TOOL_CAPABLE_OLLAMA_MARKERS = (
    "qwen",
    "llama3.1",
    "llama3.2",
    "llama3.3",
    "llama4",
    "mistral",
    "mixtral",
    "granite",
    "command-r",
    "hermes",
    "phi4",
    "deepseek",
)
CHAT_TEXT_INCAPABLE_MARKERS = (
    "embed",
    "embedding",
    "nomic-embed",
    "bge-",
    "clip",
    "whisper",
    "tts",
    "dall-e",
    "sdxl",
)


def _saved_orchestrator_config() -> dict:
    row = db.get_channel_config("orchestrator") or {}
    cfg = row.get("config") or {}
    return {
        "use_llm": bool(row.get("enabled") or cfg.get("use_llm")),
        "llm_provider": cfg.get("llm_provider") or "ollama",
        "planner_model": cfg.get("planner_model") or "",
        "llm_base_url": cfg.get("llm_base_url") or "",
        "llm_api_key": cfg.get("llm_api_key") or "",
    }


async def _models_for_provider(provider_key: str) -> list[str]:
    if provider_key == "ollama":
        try:
            return await list_models()
        except Exception:
            return [settings.default_model]
    info = get_provider(provider_key)
    return [m["id"] for m in (info.get("suggested_models") or [])]


def _chat_model_capabilities(provider_key: str, model: str) -> dict[str, Any]:
    model_key = (model or "").lower()
    text_capable = not any(marker in model_key for marker in CHAT_TEXT_INCAPABLE_MARKERS)
    if not text_capable:
        return {
            "web": False,
            "files": False,
            "web_reason": "Modello non adatto alla chat testuale.",
            "files_reason": "Modello non adatto a leggere contesto testuale.",
        }

    if provider_key == "ollama":
        web_capable = any(marker in model_key for marker in CHAT_TOOL_CAPABLE_OLLAMA_MARKERS)
        web_reason = (
            "Tool web disponibili per questo modello locale."
            if web_capable
            else "Questo modello locale non e riconosciuto come compatibile con tool calling."
        )
    else:
        web_capable = provider_key in CHAT_TOOL_CAPABLE_PROVIDERS
        web_reason = (
            "Tool web disponibili tramite endpoint OpenAI-compatible."
            if web_capable
            else "Provider non riconosciuto come compatibile con tool calling."
        )

    return {
        "web": web_capable,
        "files": True,
        "web_reason": web_reason,
        "files_reason": "File testuali disponibili: il wrapper li converte in contesto per il modello.",
    }


def _encode_plan(plan: OrchestratorPlan) -> str:
    raw = plan.model_dump_json()
    return base64.b64encode(raw.encode("utf-8")).decode("ascii")


def _decode_plan(payload: str) -> OrchestratorPlan:
    raw = base64.b64decode(payload.encode("ascii")).decode("utf-8")
    return OrchestratorPlan.model_validate_json(raw)


async def _context(
    *,
    request: Request,
    brief: str = "",
    autonomy_level: str | None = None,
    llm_provider: str | None = None,
    planner_model: str | None = None,
    llm_base_url: str | None = None,
    use_llm: bool | None = None,
    plan: OrchestratorPlan | None = None,
    plan_b64: str = "",
    error: str | None = None,
) -> dict:
    saved = _saved_orchestrator_config()
    autonomy_level = autonomy_level or "builder"
    llm_provider = llm_provider or saved["llm_provider"]
    llm_base_url = saved["llm_base_url"] if llm_base_url is None else llm_base_url
    use_llm = saved["use_llm"] if use_llm is None else use_llm
    models = await _models_for_provider(llm_provider)
    effective_model = planner_model or saved["planner_model"] or (models[0] if models else settings.default_model)
    chat_capabilities = _chat_model_capabilities(llm_provider, effective_model)
    return {
        "brief": brief,
        "autonomy_level": autonomy_level,
        "autonomy_levels": AUTONOMY_LEVELS,
        "autonomy_meta": autonomy_meta(autonomy_level),
        "llm_provider": llm_provider,
        "planner_model": effective_model,
        "llm_base_url": llm_base_url,
        "use_llm": use_llm,
        "llm_providers": list_providers(),
        "env_key_status": env_key_status(),
        "models": models,
        "orchestrator_cfg": saved,
        "chat_capabilities": chat_capabilities,
        "plan": plan,
        "plan_b64": plan_b64,
        "chat_messages": db.list_orchestrator_messages(limit=80),
        "error": error,
        "flash": request.query_params.get("flash"),
    }


@router.get("/orchestrator", response_class=HTMLResponse)
async def orchestrator_page(request: Request):
    return templates.TemplateResponse(
        request,
        "orchestrator.html",
        await _context(request=request),
    )


@router.post("/orchestrator/plan", response_class=HTMLResponse)
async def orchestrator_plan(
    request: Request,
    brief: str = Form(""),
    autonomy_level: str = Form("builder"),
    llm_provider: str = Form(""),
    planner_model: str = Form(""),
    llm_base_url: str = Form(""),
    llm_api_key: str = Form(""),
    use_llm: str = Form(""),
):
    brief = brief.strip()
    if not brief:
        return templates.TemplateResponse(
            request,
            "orchestrator.html",
            await _context(
                request=request,
                brief=brief,
                autonomy_level=autonomy_level,
                llm_provider=llm_provider,
                planner_model=planner_model,
                llm_base_url=llm_base_url,
                use_llm=bool(use_llm),
                error="Scrivi un brief prima di generare il piano.",
            ),
            status_code=400,
        )
    if autonomy_level not in AUTONOMY_LEVELS:
        autonomy_level = "builder"
    saved = _saved_orchestrator_config()
    llm_provider = llm_provider.strip() or saved["llm_provider"]
    planner_model = planner_model.strip() or saved["planner_model"]
    llm_base_url = llm_base_url.strip() or saved["llm_base_url"]
    llm_api_key = llm_api_key.strip() or saved["llm_api_key"]
    try:
        plan = await build_plan(
            brief=brief,
            autonomy_level=autonomy_level,  # type: ignore[arg-type]
            provider=llm_provider,
            model=planner_model or None,
            llm_base_url=llm_base_url or None,
            llm_api_key=llm_api_key or None,
            use_llm=bool(use_llm),
        )
        plan_b64 = _encode_plan(plan)
        status_code = 200
        error = None
    except Exception as e:
        plan = None
        plan_b64 = ""
        status_code = 500
        error = f"Planner fallito: {type(e).__name__}: {e}"

    return templates.TemplateResponse(
        request,
        "orchestrator.html",
        await _context(
            request=request,
            brief=brief,
            autonomy_level=autonomy_level,
            llm_provider=llm_provider,
            planner_model=planner_model,
            llm_base_url=llm_base_url,
            use_llm=bool(use_llm),
            plan=plan,
            plan_b64=plan_b64,
            error=error,
        ),
        status_code=status_code,
    )


@router.post("/orchestrator/execute", response_class=HTMLResponse)
async def orchestrator_execute(
    request: Request,
    plan_b64: str = Form(""),
    run_now: str = Form(""),
    confirm_risky: str = Form(""),
):
    try:
        plan = _decode_plan(plan_b64)
        result = execute_plan(
            plan,
            run_now=bool(run_now),
            confirm_risky=bool(confirm_risky),
        )
    except Exception as e:
        plan = None
        try:
            plan = _decode_plan(plan_b64)
        except Exception:
            pass
        return templates.TemplateResponse(
            request,
            "orchestrator.html",
            await _context(
                request=request,
                plan=plan,
                plan_b64=plan_b64,
                autonomy_level=plan.autonomy_level if plan else "builder",
                error=f"Esecuzione piano non riuscita: {type(e).__name__}: {e}",
            ),
            status_code=400,
        )

    sep = "&" if "?" in result.redirect_url else "?"
    return RedirectResponse(
        url=f"{result.redirect_url}{sep}flash={result.message.replace(' ', '+')}",
        status_code=303,
    )


@router.post("/orchestrator/chat")
async def orchestrator_chat(
    message: str = Form(""),
    chat_web_enabled: str = Form(""),
    chat_files_enabled: str = Form(""),
    attachment: UploadFile | None = File(None),
):
    message = (message or "").strip()
    cfg = _saved_orchestrator_config()
    provider_key = cfg["llm_provider"]
    model = cfg["planner_model"]
    if not model:
        models = await _models_for_provider(provider_key)
        model = models[0] if models else settings.default_model
    capabilities = _chat_model_capabilities(provider_key, model)
    has_attachment = bool(attachment and attachment.filename)
    requested_web = bool(chat_web_enabled)
    requested_files = bool(chat_files_enabled) or has_attachment
    allow_web = requested_web and bool(capabilities["web"])
    allow_files = requested_files and bool(capabilities["files"])

    if requested_web and not capabilities["web"]:
        db.add_orchestrator_message("user", message or "[Richiesta con navigazione web]")
        db.add_orchestrator_message(
            "assistant",
            f"Non posso attivare il web con il modello corrente ({provider_key}/{model}): {capabilities['web_reason']}",
            metadata={
                "capability": "web",
                "requested": True,
                "blocked": True,
                "provider": provider_key,
                "model": model,
            },
        )
        return RedirectResponse(url="/orchestrator#orchestrator-chat", status_code=303)
    if requested_files and not capabilities["files"]:
        db.add_orchestrator_message("user", message or "[Richiesta con file]")
        db.add_orchestrator_message(
            "assistant",
            f"Non posso attivare i file con il modello corrente ({provider_key}/{model}): {capabilities['files_reason']}",
            metadata={
                "capability": "files",
                "requested": True,
                "blocked": True,
                "provider": provider_key,
                "model": model,
            },
        )
        return RedirectResponse(url="/orchestrator#orchestrator-chat", status_code=303)

    try:
        file_info = await _save_chat_attachment(
            attachment,
            enabled=allow_files,
        )
    except ValueError as e:
        user_body = message or "[Tentativo di allegare un file]"
        db.add_orchestrator_message("user", user_body)
        db.add_orchestrator_message(
            "assistant",
            f"Non ho caricato l'allegato: {e}",
            metadata={"error": str(e), "capability": "files"},
        )
        return RedirectResponse(url="/orchestrator#orchestrator-chat", status_code=303)

    if not message and not file_info:
        return RedirectResponse(url="/orchestrator#orchestrator-chat", status_code=303)

    user_body = _compose_user_chat_body(message, file_info)
    db.add_orchestrator_message(
        "user",
        user_body,
        metadata={"attachment": _public_file_metadata(file_info)} if file_info else None,
    )
    try:
        reply, metadata = await _generate_chat_reply(
            user_body,
            file_info=file_info,
            chat_options={
                "web_enabled": allow_web,
                "files_enabled": allow_files,
                "capabilities": capabilities,
            },
        )
    except Exception as e:
        reply = (
            "Non riesco a contattare il modello configurato per l'Orchestrator. "
            f"Errore: {type(e).__name__}: {str(e)[:240]}\n\n"
            "Controlla provider, modello, base URL e API key in Settings."
        )
        metadata = {"error": f"{type(e).__name__}: {e}"}
    db.add_orchestrator_message("assistant", reply, metadata=metadata)
    return RedirectResponse(url="/orchestrator#orchestrator-chat", status_code=303)


@router.post("/orchestrator/chat/stream")
async def orchestrator_chat_stream(
    message: str = Form(""),
    chat_web_enabled: str = Form(""),
    chat_files_enabled: str = Form(""),
    attachment: UploadFile | None = File(None),
):
    message = (message or "").strip()
    cfg = _saved_orchestrator_config()
    provider_key = cfg["llm_provider"]
    model = cfg["planner_model"]
    if not model:
        models = await _models_for_provider(provider_key)
        model = models[0] if models else settings.default_model

    capabilities = _chat_model_capabilities(provider_key, model)
    has_attachment = bool(attachment and attachment.filename)
    requested_web = bool(chat_web_enabled)
    requested_files = bool(chat_files_enabled) or has_attachment
    allow_web = requested_web and bool(capabilities["web"])
    allow_files = requested_files and bool(capabilities["files"])

    if requested_web and not capabilities["web"]:
        user_body = message or "[Richiesta con navigazione web]"
        reply = (
            f"Non posso attivare il web con il modello corrente ({provider_key}/{model}): "
            f"{capabilities['web_reason']}"
        )
        db.add_orchestrator_message("user", user_body)
        db.add_orchestrator_message(
            "assistant",
            reply,
            metadata={
                "capability": "web",
                "requested": True,
                "blocked": True,
                "provider": provider_key,
                "model": model,
            },
        )
        return _chat_stream_response(_text_event_stream(reply))

    if requested_files and not capabilities["files"]:
        user_body = message or "[Richiesta con file]"
        reply = (
            f"Non posso attivare i file con il modello corrente ({provider_key}/{model}): "
            f"{capabilities['files_reason']}"
        )
        db.add_orchestrator_message("user", user_body)
        db.add_orchestrator_message(
            "assistant",
            reply,
            metadata={
                "capability": "files",
                "requested": True,
                "blocked": True,
                "provider": provider_key,
                "model": model,
            },
        )
        return _chat_stream_response(_text_event_stream(reply))

    try:
        file_info = await _save_chat_attachment(attachment, enabled=allow_files)
    except ValueError as e:
        user_body = message or "[Tentativo di allegare un file]"
        reply = f"Non ho caricato l'allegato: {e}"
        db.add_orchestrator_message("user", user_body)
        db.add_orchestrator_message(
            "assistant",
            reply,
            metadata={"error": str(e), "capability": "files"},
        )
        return _chat_stream_response(_text_event_stream(reply))

    if not message and not file_info:
        return _chat_stream_response(_done_event_stream())

    user_body = _compose_user_chat_body(message, file_info)
    db.add_orchestrator_message(
        "user",
        user_body,
        metadata={"attachment": _public_file_metadata(file_info)} if file_info else None,
    )

    async def event_stream() -> AsyncIterator[str]:
        full_text = ""
        metadata: dict[str, Any] = {}
        try:
            async for chunk in _stream_chat_reply(
                user_body,
                file_info=file_info,
                chat_options={
                    "web_enabled": allow_web,
                    "files_enabled": allow_files,
                    "capabilities": capabilities,
                },
                metadata_out=metadata,
            ):
                full_text += chunk
                yield _chat_stream_event("token", content=chunk)
            reply = full_text.strip() or "(il modello non ha prodotto risposta)"
        except Exception as e:
            reply = (
                "Non riesco a contattare il modello configurato per l'Orchestrator. "
                f"Errore: {type(e).__name__}: {str(e)[:240]}\n\n"
                "Controlla provider, modello, base URL e API key in Settings."
            )
            metadata = {"error": f"{type(e).__name__}: {e}"}
            async for chunk in _yield_text_chunks(reply):
                yield _chat_stream_event("token", content=chunk)

        db.add_orchestrator_message("assistant", reply, metadata=metadata)
        yield _chat_stream_event("done")

    return _chat_stream_response(event_stream())


@router.post("/orchestrator/chat/clear")
async def orchestrator_chat_clear():
    db.clear_orchestrator_messages()
    return RedirectResponse(url="/orchestrator#orchestrator-chat", status_code=303)


def _chat_stream_response(events: AsyncIterator[str]) -> StreamingResponse:
    return StreamingResponse(
        events,
        media_type="application/x-ndjson",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _chat_stream_event(event_type: str, **payload: Any) -> str:
    return json.dumps({"type": event_type, **payload}, ensure_ascii=False) + "\n"


async def _yield_text_chunks(text: str) -> AsyncIterator[str]:
    for part in re.findall(r"\S+\s*", text):
        yield part
        await asyncio.sleep(0.012)


async def _text_event_stream(text: str) -> AsyncIterator[str]:
    async for chunk in _yield_text_chunks(text):
        yield _chat_stream_event("token", content=chunk)
    yield _chat_stream_event("done")


async def _done_event_stream() -> AsyncIterator[str]:
    yield _chat_stream_event("done")


async def _save_chat_attachment(
    attachment: UploadFile | None,
    *,
    enabled: bool,
) -> dict[str, Any] | None:
    if attachment is None or not attachment.filename:
        return None
    if not enabled:
        raise ValueError("gli allegati file non sono attivi per questa richiesta.")

    original_name = Path(attachment.filename).name
    ext = Path(original_name).suffix.lower()
    content_type = (attachment.content_type or "").lower()
    if ext not in CHAT_ALLOWED_FILE_EXTENSIONS and not content_type.startswith("text/"):
        allowed = ", ".join(sorted(CHAT_ALLOWED_FILE_EXTENSIONS))
        raise ValueError(f"formato non supportato ({ext or content_type or 'sconosciuto'}). Usa file testuali: {allowed}.")

    raw = await attachment.read(CHAT_FILE_MAX_BYTES + 1)
    if len(raw) > CHAT_FILE_MAX_BYTES:
        raise ValueError("file troppo grande: massimo 5 MB.")

    if ext == PDF_EXTENSION or content_type == "application/pdf":
        text = _extract_pdf_text(raw)
    else:
        text = raw.decode("utf-8", errors="replace")
    truncated = len(text) > CHAT_FILE_CONTEXT_CHARS
    context_text = text[:CHAT_FILE_CONTEXT_CHARS]

    day = datetime.now(timezone.utc).strftime("%Y%m%d")
    dest_dir = UPLOADS_DIR / "orchestrator" / day
    dest_dir.mkdir(parents=True, exist_ok=True)
    safe_name = _safe_upload_filename(original_name)
    stamp = datetime.now(timezone.utc).strftime("%H%M%S")
    dest = dest_dir / f"{stamp}_{uuid.uuid4().hex[:8]}_{safe_name}"
    dest.write_bytes(raw)

    return {
        "filename": original_name,
        "stored_path": str(dest),
        "stored_relpath": str(dest.relative_to(UPLOADS_DIR.parent)),
        "content_type": attachment.content_type or "",
        "size_bytes": len(raw),
        "chars": len(text),
        "context_chars": len(context_text),
        "truncated": truncated,
        "context_text": context_text,
    }


def _extract_pdf_text(raw: bytes) -> str:
    try:
        import io

        from pypdf import PdfReader
        from pypdf.errors import PdfReadError
    except ImportError as e:
        raise ValueError(
            "supporto PDF non installato: aggiungi 'pypdf' alle dipendenze."
        ) from e
    try:
        reader = PdfReader(io.BytesIO(raw))
    except PdfReadError as e:
        raise ValueError(f"PDF non leggibile: {e}") from e
    if reader.is_encrypted:
        try:
            reader.decrypt("")
        except Exception as e:
            raise ValueError("PDF cifrato: rimuovi la password e riprova.") from e
    parts: list[str] = []
    for page in reader.pages:
        try:
            chunk = page.extract_text() or ""
        except Exception:
            chunk = ""
        if chunk.strip():
            parts.append(chunk)
    text = "\n\n".join(parts).strip()
    if not text:
        raise ValueError("PDF senza testo estraibile (probabilmente scansione immagine).")
    return text


def _safe_upload_filename(filename: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", filename or "allegato.txt").strip("._")
    return safe or "allegato.txt"


def _public_file_metadata(file_info: dict[str, Any] | None) -> dict[str, Any] | None:
    if not file_info:
        return None
    return {
        "filename": file_info["filename"],
        "stored_path": file_info["stored_path"],
        "stored_relpath": file_info["stored_relpath"],
        "content_type": file_info["content_type"],
        "size_bytes": file_info["size_bytes"],
        "chars": file_info["chars"],
        "context_chars": file_info["context_chars"],
        "truncated": file_info["truncated"],
    }


def _compose_user_chat_body(message: str, file_info: dict[str, Any] | None) -> str:
    if not file_info:
        return message
    note = (
        f"[Allegato: {file_info['filename']} | {file_info['size_bytes']} byte | "
        f"salvato in {file_info['stored_relpath']}]"
    )
    if message:
        return f"{message}\n\n{note}"
    return f"Analizza l'allegato e usalo come contesto per la risposta.\n\n{note}"


def _file_context_message(file_info: dict[str, Any]) -> str:
    truncated_note = (
        "\n\n[Nota: contenuto troncato per limiti di contesto.]"
        if file_info.get("truncated")
        else ""
    )
    return (
        "CONTESTO FILE ALLEGATO\n"
        f"Nome: {file_info['filename']}\n"
        f"Percorso salvato: {file_info['stored_relpath']}\n"
        f"Dimensione: {file_info['size_bytes']} byte\n\n"
        "CONTENUTO:\n"
        f"{file_info['context_text']}"
        f"{truncated_note}"
    )


def _historical_file_context(metadata: dict[str, Any] | None) -> str | None:
    attachment = (metadata or {}).get("attachment") or {}
    stored_path = attachment.get("stored_path")
    if not stored_path:
        return None
    filename = attachment.get("filename") or Path(stored_path).name
    is_pdf = (
        Path(stored_path).suffix.lower() == PDF_EXTENSION
        or (attachment.get("content_type") or "").lower() == "application/pdf"
    )
    try:
        if is_pdf:
            text = _extract_pdf_text(Path(stored_path).read_bytes())
        else:
            text = Path(stored_path).read_text(encoding="utf-8", errors="replace")
    except (OSError, ValueError):
        return (
            "CONTESTO FILE PRECEDENTE NON DISPONIBILE\n"
            f"Nome: {filename}\n"
            f"Percorso salvato: {attachment.get('stored_relpath') or stored_path}"
        )
    truncated = len(text) > CHAT_HISTORY_FILE_CONTEXT_CHARS
    truncated_note = "\n\n[Nota: contenuto precedente troncato.]" if truncated else ""
    return (
        "CONTESTO FILE PRECEDENTE\n"
        f"Nome: {filename}\n"
        f"Percorso salvato: {attachment.get('stored_relpath') or stored_path}\n\n"
        "CONTENUTO:\n"
        f"{text[:CHAT_HISTORY_FILE_CONTEXT_CHARS]}"
        f"{truncated_note}"
    )


async def _generate_chat_reply(
    latest_user_message: str,
    *,
    file_info: dict[str, Any] | None = None,
    chat_options: dict[str, Any] | None = None,
) -> tuple[str, dict]:
    base_url, api_key, payload, metadata, web_enabled = await _build_chat_payload(
        latest_user_message,
        file_info=file_info,
        chat_options=chat_options,
    )
    headers = {"Authorization": f"Bearer {api_key}"}
    async with httpx.AsyncClient(timeout=120) as client:
        try:
            text = await _run_chat_completion_loop(
                client,
                base_url=base_url,
                headers=headers,
                payload=payload,
                metadata=metadata,
            )
        except httpx.HTTPStatusError as e:
            if not web_enabled:
                raise
            metadata["tool_error"] = f"{e.response.status_code}: {e.response.text[:300]}"
            payload.pop("tools", None)
            payload.pop("tool_choice", None)
            payload["messages"].append(
                {
                    "role": "system",
                    "content": (
                        "Il provider non ha accettato i tool web. Rispondi senza navigazione "
                        "e segnala che la capacita web non e disponibile per questa chiamata."
                    ),
                }
            )
            text = await _run_chat_completion_loop(
                client,
                base_url=base_url,
                headers=headers,
                payload=payload,
                metadata=metadata,
            )

    return text or "(il modello non ha prodotto risposta)", metadata


async def _stream_chat_reply(
    latest_user_message: str,
    *,
    file_info: dict[str, Any] | None = None,
    chat_options: dict[str, Any] | None = None,
    metadata_out: dict[str, Any] | None = None,
) -> AsyncIterator[str]:
    base_url, api_key, payload, metadata, web_enabled = await _build_chat_payload(
        latest_user_message,
        file_info=file_info,
        chat_options=chat_options,
    )
    if metadata_out is not None:
        metadata_out.update(metadata)

    headers = {"Authorization": f"Bearer {api_key}"}
    async with httpx.AsyncClient(timeout=120) as client:
        if web_enabled:
            text, final_metadata = await _generate_chat_reply(
                latest_user_message,
                file_info=file_info,
                chat_options=chat_options,
            )
            if metadata_out is not None:
                metadata_out.update(final_metadata)
            async for chunk in _yield_text_chunks(text):
                yield chunk
            return

        try:
            async for chunk in _stream_chat_completion_text(
                client,
                base_url=base_url,
                headers=headers,
                payload=payload,
            ):
                yield chunk
        except httpx.HTTPError as e:
            metadata["stream_fallback"] = f"{type(e).__name__}: {e}"
            if metadata_out is not None:
                metadata_out.update(metadata)
            text = await _run_chat_completion_loop(
                client,
                base_url=base_url,
                headers=headers,
                payload=payload,
                metadata=metadata,
            )
            async for chunk in _yield_text_chunks(text):
                yield chunk


async def _build_chat_payload(
    latest_user_message: str,
    *,
    file_info: dict[str, Any] | None = None,
    chat_options: dict[str, Any] | None = None,
) -> tuple[str, str, dict[str, Any], dict[str, Any], bool]:
    cfg = _saved_orchestrator_config()
    provider_key = cfg["llm_provider"]
    model = cfg["planner_model"]
    if not model:
        models = await _models_for_provider(provider_key)
        model = models[0] if models else settings.default_model

    base_url = resolve_base_url(provider_key, cfg["llm_base_url"])
    api_key = resolve_api_key(provider_key, cfg["llm_api_key"])
    capabilities = (chat_options or {}).get("capabilities") or _chat_model_capabilities(
        provider_key,
        model,
    )
    web_enabled = bool((chat_options or {}).get("web_enabled")) and bool(capabilities["web"])
    files_enabled = bool((chat_options or {}).get("files_enabled")) and bool(capabilities["files"])

    history = db.list_orchestrator_messages(limit=30)
    messages: list[dict[str, Any]] = [
        {
            "role": "system",
            "content": _chat_system_prompt(
                web_enabled=web_enabled,
                files_enabled=files_enabled,
                capabilities=capabilities,
            ),
        }
    ]
    historical_attachments = 0
    for m in history:
        role = "assistant" if m["role"] == "assistant" else "user"
        messages.append({"role": role, "content": (m.get("body") or "")[:5000]})
        if files_enabled and (
            role == "user"
            and m.get("body") != latest_user_message
            and historical_attachments < 2
        ):
            historical_context = _historical_file_context(m.get("metadata"))
            if historical_context:
                messages.append({"role": "user", "content": historical_context})
                historical_attachments += 1
    if not history or history[-1].get("body") != latest_user_message:
        messages.append({"role": "user", "content": latest_user_message})
    if file_info and files_enabled:
        messages.append({"role": "user", "content": _file_context_message(file_info)})

    metadata: dict[str, Any] = {
        "provider": provider_key,
        "model": model,
        "web_enabled": web_enabled,
        "files_enabled": files_enabled,
        "capabilities": capabilities,
        "tool_calls": [],
    }
    payload: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "temperature": 0.25,
        "max_tokens": CHAT_MAX_TOKENS,
    }
    if web_enabled:
        payload["tools"] = CHAT_WEB_TOOLS_SPEC
        payload["tool_choice"] = "auto"

    return base_url, api_key, payload, metadata, web_enabled


async def _stream_chat_completion_text(
    client: httpx.AsyncClient,
    *,
    base_url: str,
    headers: dict[str, str],
    payload: dict[str, Any],
) -> AsyncIterator[str]:
    stream_payload = {**payload, "stream": True}
    async with client.stream(
        "POST",
        f"{base_url.rstrip('/')}/chat/completions",
        json=stream_payload,
        headers=headers,
    ) as r:
        r.raise_for_status()
        async for line in r.aiter_lines():
            line = line.strip()
            if not line:
                continue
            if line.startswith("data:"):
                line = line[5:].strip()
            if line == "[DONE]":
                break
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue
            choice = (data.get("choices") or [{}])[0]
            delta = choice.get("delta") or {}
            content = delta.get("content")
            if content:
                yield content


async def _run_chat_completion_loop(
    client: httpx.AsyncClient,
    *,
    base_url: str,
    headers: dict[str, str],
    payload: dict[str, Any],
    metadata: dict[str, Any],
) -> str:
    url = f"{base_url.rstrip('/')}/chat/completions"
    last_text = ""
    for _ in range(CHAT_TOOL_MAX_LOOPS + 1):
        r = await client.post(url, json=payload, headers=headers)
        r.raise_for_status()
        data = r.json()
        message = data.get("choices", [{}])[0].get("message", {}) or {}
        last_text = (message.get("content") or "").strip()
        tool_calls = message.get("tool_calls") or []
        if not tool_calls:
            return last_text
        normalized_calls: list[dict[str, Any]] = []
        for call in tool_calls:
            normalized = dict(call)
            normalized.setdefault("id", f"call_{uuid.uuid4().hex[:8]}")
            normalized_calls.append(normalized)

        payload["messages"].append(
            {
                "role": "assistant",
                "content": message.get("content") or "",
                "tool_calls": normalized_calls,
            }
        )
        for call in normalized_calls:
            tool_name = (call.get("function") or {}).get("name") or ""
            args = _decode_tool_arguments((call.get("function") or {}).get("arguments"))
            tool_output = await _run_chat_tool(tool_name, args)
            metadata.setdefault("tool_calls", []).append(
                {"name": tool_name, "args": args, "output_chars": len(tool_output)}
            )
            payload["messages"].append(
                {
                    "role": "tool",
                    "tool_call_id": call["id"],
                    "name": tool_name,
                    "content": tool_output[:12_000],
                }
            )
    return last_text or "Ho usato gli strumenti disponibili, ma non ho ricevuto una sintesi finale dal modello."


def _decode_tool_arguments(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if not raw:
        return {}
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


async def _run_chat_tool(name: str, args: dict[str, Any]) -> str:
    try:
        if name == "web_search":
            query = str(args.get("query") or "").strip()
            if not query:
                return "Errore: query mancante."
            max_results = max(1, min(int(args.get("max_results") or 5), 8))
            results = await web_search(query, max_results=max_results)
            return json.dumps(results, ensure_ascii=False, indent=2)
        if name == "fetch_url":
            url = str(args.get("url") or "").strip()
            if not url.startswith(("http://", "https://")):
                return "Errore: URL non valido. Usa http(s)."
            result = await fetch_http(url, max_chars=12_000)
            return json.dumps(result.to_dict(), ensure_ascii=False, indent=2)
        return f"Tool non supportato: {name}"
    except Exception as e:
        return f"Errore tool {name}: {type(e).__name__}: {e}"


def _chat_system_prompt(
    *,
    web_enabled: bool,
    files_enabled: bool,
    capabilities: dict[str, Any],
) -> str:
    snapshot = _orchestrator_snapshot()
    if web_enabled:
        web_line = (
            "Navigazione web abilitata: se servono informazioni aggiornate usa web_search e fetch_url. "
            "Cita gli URL usati nella risposta."
        )
    elif capabilities.get("web"):
        web_line = "Navigazione web disponibile ma non attivata per questa richiesta."
    else:
        web_line = f"Navigazione web non disponibile: {capabilities.get('web_reason')}"

    if files_enabled:
        files_line = (
            "Allegati file abilitati: quando ricevi un blocco CONTESTO FILE ALLEGATO, usalo come fonte primaria."
        )
    elif capabilities.get("files"):
        files_line = "Allegati file disponibili ma non attivati per questa richiesta."
    else:
        files_line = f"Allegati file non disponibili: {capabilities.get('files_reason')}"
    return (
        "Sei l'Orchestrator di AgentScraper. Parli in italiano, in modo operativo e concreto. "
        "Stile hard-coded del progetto: risposte brevi, asciutte, massimo 4-6 righe salvo richiesta esplicita. "
        "Niente introduzioni, niente riepiloghi ovvi, niente liste lunghe. Se servono dettagli, chiedi una sola "
        "domanda o proponi il passo successivo. Aiuti l'utente a capire quali task creare, come collegarli, "
        "che cosa sta succedendo nei job e quali rischi ci sono. Non dici di aver creato/lanciato nulla dalla "
        "chat: per agire, invita l'utente a usare il Brief e Genera piano, oppure a premere i bottoni di "
        "conferma gia presenti. Se l'utente chiede stato, usa lo snapshot qui sotto.\n\n"
        f"CAPACITA CHAT:\n- {web_line}\n- {files_line}\n\n"
        f"SNAPSHOT SISTEMA:\n{snapshot}"
    )


def _orchestrator_snapshot() -> str:
    lines: list[str] = []
    tasks = db.list_tasks()[:10]
    if not tasks:
        return "Nessun task presente."
    for t in tasks:
        latest = db.latest_job(t["id"])
        if latest:
            job_info = f"ultimo job #{latest['id']} status={latest['status']}"
        else:
            job_info = "nessun job"
        lines.append(
            f"- task #{t['id']} {t['name']} mode={t.get('agent_mode')} "
            f"model={t.get('model')} {job_info}"
        )
    return "\n".join(lines)
