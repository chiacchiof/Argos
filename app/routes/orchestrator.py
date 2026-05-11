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

from .. import db, jobs
from ..agent.extraction_templates import get_schema, list_templates
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
    PlannedTask,
    RISKY_AGENT_MODES,
    _task_to_db_payload,
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

CHAT_DOMAIN_READ_TOOLS_SPEC: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "list_tasks",
            "description": "Lista i task del progetto. Ritorna id, name, agent_mode, model, status_tag.",
            "parameters": {
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "description": "Massimo task da ritornare (default 20)"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_task",
            "description": "Ritorna la configurazione completa di un task dato il suo id.",
            "parameters": {
                "type": "object",
                "properties": {"task_id": {"type": "integer"}},
                "required": ["task_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_workflows",
            "description": "Lista i workflow del progetto (id, name, description).",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_jobs",
            "description": "Lista i job di un task ordinati dal piu recente.",
            "parameters": {
                "type": "object",
                "properties": {
                    "task_id": {"type": "integer"},
                    "limit": {"type": "integer", "description": "Default 20"},
                },
                "required": ["task_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_job_status",
            "description": "Stato di un job (status, started_at, finished_at, result_path, error) e tail dei log (~80 righe).",
            "parameters": {
                "type": "object",
                "properties": {"job_id": {"type": "integer"}},
                "required": ["job_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_extraction_templates",
            "description": "Lista i template di estrazione (key, name, description) usabili nei task scraping.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_chat_models",
            "description": "Lista i modelli disponibili per un provider (default: provider corrente della chat).",
            "parameters": {
                "type": "object",
                "properties": {"provider": {"type": "string"}},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_guide_topics",
            "description": (
                "Lista tutte le sezioni della guida ufficiale del progetto (GUIDA.md). "
                "Usalo PRIMA di rispondere a domande tipo 'come configuro X', 'qual e' la "
                "differenza fra Y e Z', 'quale modello usare per W' — la guida contiene "
                "best practice e dettagli operativi specifici di AgentScraper. "
                "Ritorna [{id, level, title}] di tutte le sezioni."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_guide_section",
            "description": (
                "Legge una sezione della guida ufficiale del progetto (GUIDA.md). "
                "Query per id esatto (es. '3-4-1-site-explorer-agente-react') oppure "
                "per parole chiave nel titolo (es. 'site_explorer', 'workflow', 'qualifier'). "
                "Se la query e' ambigua ritorna lista candidati. "
                "Usalo per leggere la documentazione vera prima di consigliare/configurare un task."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "id sezione o keyword (es. 'site_explorer', 'auto_extract')",
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_assets",
            "description": (
                "Lista asset (annunci/prodotti/profili/...) estratti dai task. "
                "Filtra per asset_type, status e tag. Tag come array di 'key:value'."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "asset_type": {"type": "string"},
                    "status": {"type": "string"},
                    "source_task_id": {"type": "integer"},
                    "tags": {"type": "array", "items": {"type": "string"}, "description": "es. ['tipo:vendita','citta:Acireale']"},
                    "limit": {"type": "integer"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_asset",
            "description": "Dettaglio completo di un asset (incluso raw_json e tag).",
            "parameters": {
                "type": "object",
                "properties": {"asset_id": {"type": "integer"}},
                "required": ["asset_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_site_patterns",
            "description": (
                "Lista i pattern URL appresi per dominio (memoria pattern). "
                "Filtri: registrable_domain (es. 'yescasa.it'), status ('candidate'|'confirmed'|'rejected')."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "registrable_domain": {"type": "string"},
                    "status": {"type": "string"},
                    "limit": {"type": "integer"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_site_playbooks",
            "description": (
                "Lista i playbook persistiti — istruzioni operative scritte da un agente "
                "potente (browser_use) per insegnare a uno debole (site_explorer) come "
                "estrarre dati da un dominio. Stage 2 del knowledge transfer cross-runner. "
                "Filtri: registrable_domain, status ('active'|'stale'|'archived')."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "registrable_domain": {"type": "string"},
                    "status": {"type": "string"},
                    "limit": {"type": "integer"},
                },
            },
        },
    },
]

CHAT_DOMAIN_WRITE_TOOLS_SPEC: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "propose_plan",
            "description": (
                "Costruisce un OrchestratorPlan dal brief usando il planner (heuristic + LLM). "
                "Non committa nulla: ritorna il plan da mostrare all'utente prima di execute_plan."
            ),
            "parameters": {
                "type": "object",
                "properties": {"brief": {"type": "string"}},
                "required": ["brief"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "execute_plan",
            "description": (
                "Committa un OrchestratorPlan: crea task e workflow, opzionalmente avvia. "
                "Per outreach/responder serve confirm_risky=true."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "plan": {"type": "object", "description": "OrchestratorPlan come dict (title, summary, tasks[], edges[], ...)"},
                    "run_now": {"type": "boolean"},
                    "confirm_risky": {"type": "boolean"},
                },
                "required": ["plan"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_task",
            "description": (
                "Crea un singolo task. Per piani multi-step preferisci propose_plan+execute_plan. "
                "Campi minimi: name, agent_mode, objective. "
                "Per site_explorer su sito infinite-scroll/unbounded: passa target_cap_per_site=0 E un objective "
                "con keyword-trigger ('tutti i profili', 'infinite scroll', 'centinaia', ecc.)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "agent_mode": {"type": "string"},
                    "objective": {"type": "string"},
                    "model": {"type": "string"},
                    "seed_queries": {"type": "array", "items": {"type": "string"}},
                    "allowed_domains": {"type": "array", "items": {"type": "string"}},
                    "max_iterations": {"type": "integer"},
                    "extraction_template": {"type": "string"},
                    "input_artifact_path": {"type": "string"},
                    "message_subject": {"type": "string"},
                    "message_template": {"type": "string"},
                    "message_channels": {"type": "array", "items": {"type": "string"}},
                    "responder_system_prompt": {"type": "string"},
                    "target_cap_per_site": {
                        "type": "integer",
                        "description": (
                            "site_explorer: cap target estratti per sito. 0 = unbounded (cap interno di "
                            "sicurezza 5000). Default 30."
                        ),
                    },
                    "refresh_policy_days": {
                        "type": "integer",
                        "description": (
                            "Re-run incrementali: 0=mai re-extract se asset esiste in DB; N>0=re-extract se "
                            "asset più vecchio di N giorni (default 7); -1=sempre re-extract."
                        ),
                    },
                    "crawler_enabled": {
                        "type": "boolean",
                        "description": "bulk_extract: abilita BFS crawler dal seed con auto-detect pattern URL.",
                    },
                    "crawler_max_depth": {
                        "type": "integer",
                        "description": "bulk_extract crawler: hop massimi dal seed (default 3).",
                    },
                },
                "required": ["name", "agent_mode", "objective"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_workflow",
            "description": "Crea un workflow vuoto (poi servono add_edge per collegare i task).",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "description": {"type": "string"},
                },
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "add_edge",
            "description": "Aggiunge un edge fra due task in un workflow (pass_artifact tipico: 'profiles.jsonl' o 'qualified.jsonl').",
            "parameters": {
                "type": "object",
                "properties": {
                    "workflow_id": {"type": "integer"},
                    "from_task_id": {"type": "integer"},
                    "to_task_id": {"type": "integer"},
                    "pass_artifact": {"type": "string"},
                },
                "required": ["workflow_id", "from_task_id", "to_task_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "start_job",
            "description": "Avvia un job per un task. Per task outreach/responder serve confirm_risky=true.",
            "parameters": {
                "type": "object",
                "properties": {
                    "task_id": {"type": "integer"},
                    "confirm_risky": {"type": "boolean"},
                },
                "required": ["task_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "start_workflow",
            "description": "Avvia un workflow (lancia il primo task). Per workflow con outreach/responder serve confirm_risky=true.",
            "parameters": {
                "type": "object",
                "properties": {
                    "workflow_id": {"type": "integer"},
                    "confirm_risky": {"type": "boolean"},
                },
                "required": ["workflow_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_asset_status",
            "description": "Aggiorna stato di un asset (new|qualified|rejected|archived) con note opzionali.",
            "parameters": {
                "type": "object",
                "properties": {
                    "asset_id": {"type": "integer"},
                    "status": {"type": "string"},
                    "notes": {"type": "string"},
                },
                "required": ["asset_id", "status"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_site_pattern_status",
            "description": (
                "Imposta lo status di un pattern in memoria DB ('candidate'|'confirmed'|'rejected'). "
                "Usa 'rejected' per scartare definitivamente un pattern sbagliato."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern_id": {"type": "integer"},
                    "status": {"type": "string"},
                    "notes": {"type": "string"},
                },
                "required": ["pattern_id", "status"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_site_playbook",
            "description": (
                "Cancella un playbook (force-refresh: il prossimo browser_use sul dominio "
                "lo rigenera). Usa quando il sito e' cambiato e il playbook non funziona piu'."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "playbook_id": {"type": "integer"},
                },
                "required": ["playbook_id"],
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
            "actions": False,
            "web_reason": "Modello non adatto alla chat testuale.",
            "files_reason": "Modello non adatto a leggere contesto testuale.",
            "actions_reason": "Modello non adatto a chiamare tool.",
        }

    if provider_key == "ollama":
        tool_capable = any(marker in model_key for marker in CHAT_TOOL_CAPABLE_OLLAMA_MARKERS)
        web_reason = (
            "Tool web disponibili per questo modello locale."
            if tool_capable
            else "Questo modello locale non e riconosciuto come compatibile con tool calling."
        )
        actions_reason = (
            "Azioni disponibili: il modello puo usare tool di lettura e (se autorizzato) di scrittura."
            if tool_capable
            else "Modello locale senza tool calling: la chat resta read-only."
        )
    else:
        tool_capable = provider_key in CHAT_TOOL_CAPABLE_PROVIDERS
        web_reason = (
            "Tool web disponibili tramite endpoint OpenAI-compatible."
            if tool_capable
            else "Provider non riconosciuto come compatibile con tool calling."
        )
        actions_reason = (
            "Azioni disponibili tramite tool calling sul provider corrente."
            if tool_capable
            else "Provider senza tool calling: la chat resta read-only."
        )

    return {
        "web": tool_capable,
        "files": True,
        "actions": tool_capable,
        "web_reason": web_reason,
        "files_reason": "File testuali disponibili: il wrapper li converte in contesto per il modello.",
        "actions_reason": actions_reason,
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
    if use_llm is None:
        use_llm = True
    models = await _models_for_provider(llm_provider)
    effective_model = planner_model or saved["planner_model"] or (models[0] if models else settings.default_model)
    chat_capabilities = _chat_model_capabilities(llm_provider, effective_model)
    planner_model_warning = _planner_model_warning(effective_model)
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
        "planner_model_warning": planner_model_warning,
        "plan": plan,
        "plan_b64": plan_b64,
        "chat_messages": db.list_orchestrator_messages(limit=80),
        "error": error,
        "flash": request.query_params.get("flash"),
    }


def _planner_model_warning(model: str) -> str | None:
    m = (model or "").lower()
    if not m:
        return None
    if any(marker in m for marker in ("embed", "embedding", "clip", "whisper", "tts", "dall-e", "sdxl", "vision")):
        return (
            f"Modello '{model}' non adatto a planning testuale (embedding/vision/audio). "
            "Cambialo in Settings con un modello chat."
        )
    if "coder" in m or "-code" in m or m.endswith("code") or m.startswith("code"):
        return (
            f"Modello '{model}' è code-tuned: poco adatto a pianificare workflow in linguaggio naturale. "
            "Per piani migliori usa un modello chat (es. qwen3.5:latest, llama3.1, mistral)."
        )
    return None


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
    chat_actions_enabled: str = Form(""),
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
    requested_actions = bool(chat_actions_enabled)
    allow_web = requested_web and bool(capabilities["web"])
    allow_actions = requested_actions and bool(capabilities["actions"])

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
    if requested_actions and not capabilities["actions"]:
        db.add_orchestrator_message("user", message or "[Richiesta con azioni]")
        db.add_orchestrator_message(
            "assistant",
            f"Non posso attivare le azioni con il modello corrente ({provider_key}/{model}): {capabilities['actions_reason']}",
            metadata={
                "capability": "actions",
                "requested": True,
                "blocked": True,
                "provider": provider_key,
                "model": model,
            },
        )
        return RedirectResponse(url="/orchestrator#orchestrator-chat", status_code=303)
    if has_attachment and not capabilities["files"]:
        db.add_orchestrator_message("user", message or "[Tentativo di allegare un file]")
        db.add_orchestrator_message(
            "assistant",
            f"Non posso usare allegati con il modello corrente ({provider_key}/{model}): {capabilities['files_reason']}",
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
            enabled=bool(capabilities["files"]),
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
                "files_enabled": bool(capabilities["files"]),
                "actions_enabled": allow_actions,
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
    chat_actions_enabled: str = Form(""),
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
    requested_actions = bool(chat_actions_enabled)
    allow_web = requested_web and bool(capabilities["web"])
    allow_actions = requested_actions and bool(capabilities["actions"])

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

    if requested_actions and not capabilities["actions"]:
        user_body = message or "[Richiesta con azioni]"
        reply = (
            f"Non posso attivare le azioni con il modello corrente ({provider_key}/{model}): "
            f"{capabilities['actions_reason']}"
        )
        db.add_orchestrator_message("user", user_body)
        db.add_orchestrator_message(
            "assistant",
            reply,
            metadata={
                "capability": "actions",
                "requested": True,
                "blocked": True,
                "provider": provider_key,
                "model": model,
            },
        )
        return _chat_stream_response(_text_event_stream(reply))

    if has_attachment and not capabilities["files"]:
        user_body = message or "[Tentativo di allegare un file]"
        reply = (
            f"Non posso usare allegati con il modello corrente ({provider_key}/{model}): "
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
        file_info = await _save_chat_attachment(attachment, enabled=bool(capabilities["files"]))
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
                    "files_enabled": bool(capabilities["files"]),
                    "actions_enabled": allow_actions,
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
    base_url, api_key, payload, metadata, tools_active = await _build_chat_payload(
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
            if not tools_active:
                raise
            metadata["tool_error"] = f"{e.response.status_code}: {e.response.text[:300]}"
            payload.pop("tools", None)
            payload.pop("tool_choice", None)
            payload["messages"].append(
                {
                    "role": "system",
                    "content": (
                        "Il provider non ha accettato i tool. Rispondi senza tool calling "
                        "e segnala che le capacita tool non sono disponibili per questa chiamata."
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
    base_url, api_key, payload, metadata, tools_active = await _build_chat_payload(
        latest_user_message,
        file_info=file_info,
        chat_options=chat_options,
    )
    if metadata_out is not None:
        metadata_out.update(metadata)

    headers = {"Authorization": f"Bearer {api_key}"}
    async with httpx.AsyncClient(timeout=120) as client:
        if tools_active:
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
    actions_enabled = bool((chat_options or {}).get("actions_enabled")) and bool(capabilities["actions"])
    tools_capable = bool(capabilities["actions"])

    history = db.list_orchestrator_messages(limit=30)
    messages: list[dict[str, Any]] = [
        {
            "role": "system",
            "content": _chat_system_prompt(
                web_enabled=web_enabled,
                files_enabled=files_enabled,
                actions_enabled=actions_enabled,
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
        "actions_enabled": actions_enabled,
        "capabilities": capabilities,
        "tool_calls": [],
    }
    payload: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "temperature": 0.25,
        "max_tokens": CHAT_MAX_TOKENS,
    }
    tools: list[dict[str, Any]] = []
    if web_enabled:
        tools.extend(CHAT_WEB_TOOLS_SPEC)
    if tools_capable:
        tools.extend(CHAT_DOMAIN_READ_TOOLS_SPEC)
        if actions_enabled:
            tools.extend(CHAT_DOMAIN_WRITE_TOOLS_SPEC)
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"

    tools_active = bool(tools)
    return base_url, api_key, payload, metadata, tools_active


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
        if name == "list_tasks":
            return _tool_list_tasks(args)
        if name == "get_task":
            return _tool_get_task(args)
        if name == "list_workflows":
            return _tool_list_workflows(args)
        if name == "list_jobs":
            return _tool_list_jobs(args)
        if name == "get_job_status":
            return _tool_get_job_status(args)
        if name == "list_extraction_templates":
            return _tool_list_extraction_templates()
        if name == "list_chat_models":
            return await _tool_list_chat_models(args)
        if name == "list_guide_topics":
            return _tool_list_guide_topics()
        if name == "read_guide_section":
            return _tool_read_guide_section(args)
        if name == "propose_plan":
            return await _tool_propose_plan(args)
        if name == "execute_plan":
            return _tool_execute_plan(args)
        if name == "create_task":
            return _tool_create_task(args)
        if name == "create_workflow":
            return _tool_create_workflow(args)
        if name == "add_edge":
            return _tool_add_edge(args)
        if name == "start_job":
            return _tool_start_job(args)
        if name == "start_workflow":
            return _tool_start_workflow(args)
        if name == "list_assets":
            return _tool_list_assets(args)
        if name == "get_asset":
            return _tool_get_asset(args)
        if name == "update_asset_status":
            return _tool_update_asset_status(args)
        if name == "list_site_patterns":
            return _tool_list_site_patterns(args)
        if name == "set_site_pattern_status":
            return _tool_set_site_pattern_status(args)
        if name == "list_site_playbooks":
            return _tool_list_site_playbooks(args)
        if name == "delete_site_playbook":
            return _tool_delete_site_playbook(args)
        return f"Tool non supportato: {name}"
    except Exception as e:
        return f"Errore tool {name}: {type(e).__name__}: {e}"


def _tool_list_tasks(args: dict[str, Any]) -> str:
    limit = max(1, min(int(args.get("limit") or 20), 100))
    tasks = db.list_tasks()[:limit]
    slim = [
        {
            "id": t.get("id"),
            "name": t.get("name"),
            "agent_mode": t.get("agent_mode"),
            "model": t.get("model"),
            "status_tag": t.get("status_tag"),
        }
        for t in tasks
    ]
    return json.dumps({"ok": True, "tasks": slim}, ensure_ascii=False, indent=2)


def _tool_get_task(args: dict[str, Any]) -> str:
    task_id = int(args.get("task_id") or 0)
    if task_id <= 0:
        return json.dumps({"ok": False, "reason": "task_id mancante o non valido"})
    t = db.get_task(task_id)
    if not t:
        return json.dumps({"ok": False, "reason": f"task #{task_id} non trovato"})
    return json.dumps({"ok": True, "task": t}, ensure_ascii=False, indent=2, default=str)


def _tool_list_workflows(args: dict[str, Any]) -> str:
    workflows = db.list_workflows()
    slim = [
        {"id": w.get("id"), "name": w.get("name"), "description": w.get("description")}
        for w in workflows
    ]
    return json.dumps({"ok": True, "workflows": slim}, ensure_ascii=False, indent=2)


def _tool_list_jobs(args: dict[str, Any]) -> str:
    task_id = int(args.get("task_id") or 0)
    if task_id <= 0:
        return json.dumps({"ok": False, "reason": "task_id mancante"})
    limit = max(1, min(int(args.get("limit") or 20), 100))
    rows = db.list_jobs(task_id)[:limit]
    slim = [
        {
            "id": j.get("id"),
            "status": j.get("status"),
            "started_at": j.get("started_at"),
            "finished_at": j.get("finished_at"),
            "result_path": j.get("result_path"),
            "error": (j.get("error") or "")[:200] if j.get("error") else None,
        }
        for j in rows
    ]
    return json.dumps({"ok": True, "task_id": task_id, "jobs": slim}, ensure_ascii=False, indent=2, default=str)


def _tool_get_job_status(args: dict[str, Any]) -> str:
    job_id = int(args.get("job_id") or 0)
    if job_id <= 0:
        return json.dumps({"ok": False, "reason": "job_id mancante"})
    j = db.get_job(job_id)
    if not j:
        return json.dumps({"ok": False, "reason": f"job #{job_id} non trovato"})
    log_lines = (j.get("log") or "").splitlines()
    log_tail = "\n".join(log_lines[-80:]) if len(log_lines) > 80 else (j.get("log") or "")
    out = {
        "ok": True,
        "job_id": job_id,
        "task_id": j.get("task_id"),
        "status": j.get("status"),
        "started_at": j.get("started_at"),
        "finished_at": j.get("finished_at"),
        "result_path": j.get("result_path"),
        "error": j.get("error"),
        "log_tail": log_tail,
    }
    return json.dumps(out, ensure_ascii=False, indent=2, default=str)


def _tool_list_extraction_templates() -> str:
    return json.dumps({"ok": True, "templates": list_templates()}, ensure_ascii=False, indent=2)


async def _tool_list_chat_models(args: dict[str, Any]) -> str:
    cfg = _saved_orchestrator_config()
    target = (str(args.get("provider") or "").strip()) or cfg["llm_provider"]
    if target == "ollama":
        try:
            models = await list_models()
        except Exception:
            models = [settings.default_model]
    else:
        info = get_provider(target) or {}
        models = [m["id"] for m in (info.get("suggested_models") or [])]
    return json.dumps({"ok": True, "provider": target, "models": models}, ensure_ascii=False)


# ---------- Guide (GUIDA.md) parsing + tool helpers ---------------------------

_GUIDE_PATH = Path(__file__).resolve().parent.parent.parent / "GUIDA.md"
_GUIDE_CACHE: tuple[list[dict[str, Any]], float] | None = None


def _slugify(text: str) -> str:
    s = text.lower()
    s = re.sub(r"[^\w\s-]", "", s, flags=re.UNICODE)
    s = re.sub(r"[\s_-]+", "-", s).strip("-")
    return s[:90]


def _parse_guide_sections(md_text: str) -> list[dict[str, Any]]:
    """Parsa GUIDA.md in sezioni piatte. Ogni header (## / ### / ####) inizia una
    nuova sezione che termina al prossimo header di QUALSIASI livello.
    """
    out: list[dict[str, Any]] = []
    cur: dict[str, Any] | None = None
    in_code_fence = False
    for line in md_text.split("\n"):
        if line.startswith("```"):
            in_code_fence = not in_code_fence
        is_header = (not in_code_fence) and bool(re.match(r"^(#{2,4})\s+\S", line))
        if is_header:
            if cur:
                cur["content"] = cur["_buf"].strip()
                del cur["_buf"]
                out.append(cur)
            m = re.match(r"^(#{2,4})\s+(.+?)\s*$", line)
            assert m  # non dovrebbe fallire grazie al check is_header
            level = len(m.group(1))
            title = m.group(2).strip()
            cur = {
                "id": _slugify(title),
                "level": level,
                "title": title,
                "_buf": "",
            }
        else:
            if cur is not None:
                cur["_buf"] += line + "\n"
    if cur is not None:
        cur["content"] = cur["_buf"].strip()
        del cur["_buf"]
        out.append(cur)
    # Dedup ID identici (capita su sezioni con titolo simile, es. "3.5" doppio)
    seen: dict[str, int] = {}
    for s in out:
        base = s["id"]
        if base in seen:
            seen[base] += 1
            s["id"] = f"{base}-{seen[base]}"
        else:
            seen[base] = 1
    return out


def _guide_sections() -> list[dict[str, Any]]:
    global _GUIDE_CACHE
    if not _GUIDE_PATH.exists():
        return []
    try:
        mtime = _GUIDE_PATH.stat().st_mtime
    except OSError:
        return []
    if _GUIDE_CACHE is None or _GUIDE_CACHE[1] != mtime:
        try:
            text = _GUIDE_PATH.read_text(encoding="utf-8")
        except OSError:
            return []
        _GUIDE_CACHE = (_parse_guide_sections(text), mtime)
    return _GUIDE_CACHE[0]


def _tool_list_guide_topics() -> str:
    secs = _guide_sections()
    if not secs:
        return json.dumps(
            {"ok": False, "reason": f"GUIDA.md non trovata o vuota ({_GUIDE_PATH})"}
        )
    listing = [
        {"id": s["id"], "level": s["level"], "title": s["title"]}
        for s in secs
    ]
    return json.dumps({"ok": True, "count": len(listing), "topics": listing}, ensure_ascii=False, indent=2)


def _tool_read_guide_section(args: dict[str, Any]) -> str:
    query = (str(args.get("query") or "").strip()).lower()
    if not query:
        return json.dumps({"ok": False, "reason": "query mancante"})
    secs = _guide_sections()
    if not secs:
        return json.dumps({"ok": False, "reason": "GUIDA.md non trovata"})

    # 1. Match per id esatto
    exact = [s for s in secs if s["id"] == query]
    if len(exact) == 1:
        s = exact[0]
        return json.dumps(
            {"ok": True, "id": s["id"], "title": s["title"], "level": s["level"], "content": s["content"][:6000]},
            ensure_ascii=False,
        )

    # 2. Match per substring nel titolo (case-insensitive)
    title_matches = [s for s in secs if query in s["title"].lower()]
    if len(title_matches) == 1:
        s = title_matches[0]
        return json.dumps(
            {"ok": True, "id": s["id"], "title": s["title"], "level": s["level"], "content": s["content"][:6000]},
            ensure_ascii=False,
        )
    if len(title_matches) > 1:
        return json.dumps(
            {
                "ok": False,
                "reason": f"piu' sezioni hanno '{query}' nel titolo. Specifica con un id esatto.",
                "candidates": [{"id": s["id"], "title": s["title"]} for s in title_matches[:10]],
            },
            ensure_ascii=False,
        )

    # 3. Match per substring nel contenuto (fallback)
    body_matches = [s for s in secs if query in s["content"].lower()]
    if len(body_matches) == 1:
        s = body_matches[0]
        return json.dumps(
            {"ok": True, "id": s["id"], "title": s["title"], "level": s["level"], "content": s["content"][:6000]},
            ensure_ascii=False,
        )
    if len(body_matches) > 1:
        return json.dumps(
            {
                "ok": False,
                "reason": f"piu' sezioni contengono '{query}' nel testo. Specifica con un id esatto o un titolo piu' preciso.",
                "candidates": [{"id": s["id"], "title": s["title"]} for s in body_matches[:10]],
            },
            ensure_ascii=False,
        )

    return json.dumps(
        {
            "ok": False,
            "reason": f"nessuna sezione matcha '{query}'. Usa list_guide_topics() per vedere i titoli disponibili.",
        }
    )


async def _tool_propose_plan(args: dict[str, Any]) -> str:
    brief = str(args.get("brief") or "").strip()
    if not brief:
        return json.dumps({"ok": False, "reason": "brief mancante"})
    cfg = _saved_orchestrator_config()
    try:
        plan = await build_plan(
            brief=brief,
            autonomy_level="supervised",  # type: ignore[arg-type]
            provider=cfg["llm_provider"],
            model=cfg["planner_model"] or None,
            llm_base_url=cfg["llm_base_url"] or None,
            llm_api_key=cfg["llm_api_key"] or None,
            use_llm=bool(cfg["use_llm"]),
        )
    except Exception as e:
        return json.dumps({"ok": False, "reason": f"{type(e).__name__}: {e}"})
    return json.dumps(
        {"ok": True, "plan": plan.model_dump()},
        ensure_ascii=False,
        indent=2,
        default=str,
    )


def _tool_execute_plan(args: dict[str, Any]) -> str:
    plan_dict = args.get("plan")
    if not isinstance(plan_dict, dict):
        return json.dumps({"ok": False, "reason": "campo 'plan' deve essere un oggetto"})
    plan_dict = dict(plan_dict)
    plan_dict.setdefault("autonomy_level", "supervised")
    try:
        plan = OrchestratorPlan.model_validate(plan_dict)
    except Exception as e:
        return json.dumps({"ok": False, "reason": f"plan non valido: {type(e).__name__}: {e}"})
    try:
        result = execute_plan(
            plan,
            run_now=bool(args.get("run_now")),
            confirm_risky=bool(args.get("confirm_risky")),
        )
    except ValueError as e:
        return json.dumps({"ok": False, "reason": str(e)})
    return json.dumps(
        {"ok": True, "result": result.model_dump()},
        ensure_ascii=False,
        default=str,
    )


def _tool_create_task(args: dict[str, Any]) -> str:
    name = str(args.get("name") or "").strip()
    agent_mode = str(args.get("agent_mode") or "").strip()
    objective = str(args.get("objective") or "").strip()
    if not name or not agent_mode or not objective:
        return json.dumps({"ok": False, "reason": "name, agent_mode e objective sono obbligatori"})
    base_key = re.sub(r"[^a-z0-9_]+", "_", name.lower()).strip("_") or "task"
    extraction_template = args.get("extraction_template")
    extraction_schema = get_schema(extraction_template) if extraction_template else None
    try:
        planned_kwargs: dict[str, Any] = dict(
            key=base_key[:60],
            name=name[:200],
            agent_mode=agent_mode,  # type: ignore[arg-type]
            objective=objective,
            seed_queries=[str(s) for s in (args.get("seed_queries") or [])],
            allowed_domains=[str(s).lower() for s in (args.get("allowed_domains") or [])],
            max_iterations=int(args.get("max_iterations") or 10),
            model=str(args.get("model") or settings.default_model),
            extraction_template=extraction_template,
            extraction_schema=extraction_schema,
            input_artifact_path=args.get("input_artifact_path"),
            message_subject=args.get("message_subject"),
            message_template=args.get("message_template"),
            message_channels=[str(s) for s in (args.get("message_channels") or [])],
            responder_system_prompt=args.get("responder_system_prompt"),
            notes="Creato da chat Orchestrator.",
            status_tag="tuning",
        )
        if args.get("target_cap_per_site") is not None:
            planned_kwargs["target_cap_per_site"] = max(0, int(args.get("target_cap_per_site") or 0))
        if args.get("crawler_enabled") is not None:
            planned_kwargs["crawler_enabled"] = bool(args.get("crawler_enabled"))
        if args.get("crawler_max_depth") is not None:
            planned_kwargs["crawler_max_depth"] = max(1, int(args.get("crawler_max_depth") or 3))
        planned = PlannedTask(**planned_kwargs)
    except Exception as e:
        return json.dumps({"ok": False, "reason": f"campi task non validi: {type(e).__name__}: {e}"})
    payload = _task_to_db_payload(planned)
    # refresh_policy_days non esiste su PlannedTask: applicalo direttamente al payload DB.
    if args.get("refresh_policy_days") is not None:
        try:
            payload["refresh_policy_days"] = int(args.get("refresh_policy_days"))
        except Exception:
            pass
    task_id = db.create_task(payload)
    return json.dumps(
        {"ok": True, "task_id": task_id, "name": planned.name, "agent_mode": planned.agent_mode}
    )


def _tool_create_workflow(args: dict[str, Any]) -> str:
    name = str(args.get("name") or "").strip()
    if not name:
        return json.dumps({"ok": False, "reason": "nome workflow mancante"})
    description = str(args.get("description") or "").strip() or None
    workflow_id = db.create_workflow(name, description)
    return json.dumps({"ok": True, "workflow_id": workflow_id, "name": name})


def _tool_add_edge(args: dict[str, Any]) -> str:
    try:
        workflow_id = int(args.get("workflow_id") or 0)
        from_id = int(args.get("from_task_id") or 0)
        to_id = int(args.get("to_task_id") or 0)
    except (TypeError, ValueError):
        return json.dumps({"ok": False, "reason": "id non validi"})
    if not workflow_id or not from_id or not to_id:
        return json.dumps({"ok": False, "reason": "workflow_id, from_task_id, to_task_id obbligatori"})
    pass_artifact = args.get("pass_artifact") or None
    try:
        db.create_edge(
            from_task_id=from_id,
            to_task_id=to_id,
            workflow_id=workflow_id,
            pass_artifact=str(pass_artifact) if pass_artifact else None,
            enabled=True,
        )
    except ValueError as e:
        return json.dumps({"ok": False, "reason": str(e)})
    return json.dumps(
        {
            "ok": True,
            "workflow_id": workflow_id,
            "from_task_id": from_id,
            "to_task_id": to_id,
            "pass_artifact": pass_artifact,
        }
    )


def _tool_start_job(args: dict[str, Any]) -> str:
    task_id = int(args.get("task_id") or 0)
    if task_id <= 0:
        return json.dumps({"ok": False, "reason": "task_id mancante"})
    confirm_risky = bool(args.get("confirm_risky"))
    task = db.get_task(task_id)
    if not task:
        return json.dumps({"ok": False, "reason": f"task #{task_id} non trovato"})
    if task.get("agent_mode") in RISKY_AGENT_MODES and not confirm_risky:
        return json.dumps(
            {
                "ok": False,
                "reason": (
                    f"task #{task_id} ha agent_mode={task.get('agent_mode')} (rischioso). "
                    "Chiedi consenso esplicito all'utente in chat, poi richiama con confirm_risky=true."
                ),
            }
        )
    try:
        job_id = jobs.start_job(task_id)
        jobs.reload_schedules()
    except Exception as e:
        return json.dumps({"ok": False, "reason": f"{type(e).__name__}: {e}"})
    return json.dumps({"ok": True, "job_id": job_id, "task_id": task_id})


def _tool_start_workflow(args: dict[str, Any]) -> str:
    workflow_id = int(args.get("workflow_id") or 0)
    if workflow_id <= 0:
        return json.dumps({"ok": False, "reason": "workflow_id mancante"})
    confirm_risky = bool(args.get("confirm_risky"))
    edges = db.list_edges(workflow_id=workflow_id)
    risky = False
    seen_task_ids: set[int] = set()
    for e in edges:
        for tid_key in ("from_task_id", "to_task_id"):
            tid = e.get(tid_key)
            if not tid or tid in seen_task_ids:
                continue
            seen_task_ids.add(int(tid))
            t = db.get_task(int(tid))
            if t and t.get("agent_mode") in RISKY_AGENT_MODES:
                risky = True
                break
        if risky:
            break
    if risky and not confirm_risky:
        return json.dumps(
            {
                "ok": False,
                "reason": (
                    f"workflow #{workflow_id} contiene task rischiosi (outreach/responder). "
                    "Chiedi consenso esplicito all'utente, poi richiama con confirm_risky=true."
                ),
            }
        )
    try:
        result = jobs.start_workflow(workflow_id)
        jobs.reload_schedules()
    except Exception as e:
        return json.dumps({"ok": False, "reason": f"{type(e).__name__}: {e}"})
    return json.dumps({"ok": True, "workflow_id": workflow_id, "result": result}, default=str)


def _parse_tag_pairs(raw: Any) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    if not raw:
        return out
    items: list[Any] = list(raw) if isinstance(raw, (list, tuple)) else [raw]
    for item in items:
        if isinstance(item, dict):
            for k, v in item.items():
                if k and v:
                    out.append((str(k).lower(), str(v)))
            continue
        if not isinstance(item, str):
            continue
        if ":" not in item:
            continue
        k, _, v = item.partition(":")
        k = k.strip().lower()
        v = v.strip()
        if k and v:
            out.append((k, v))
    return out


def _tool_list_assets(args: dict[str, Any]) -> str:
    asset_type = (str(args.get("asset_type") or "").strip()) or None
    status = (str(args.get("status") or "").strip()) or None
    source_task_id = args.get("source_task_id")
    if source_task_id is not None:
        try:
            source_task_id = int(source_task_id)
        except (TypeError, ValueError):
            source_task_id = None
    tag_filters = _parse_tag_pairs(args.get("tags"))
    limit = max(1, min(int(args.get("limit") or 30), 200))
    rows = db.list_assets(
        asset_type=asset_type,
        status=status,
        source_task_id=source_task_id,
        tag_filters=tag_filters or None,
        limit=limit,
    )
    slim = [
        {
            "id": r.get("id"),
            "asset_type": r.get("asset_type"),
            "title": r.get("title"),
            "source_url": r.get("source_url"),
            "source_domain": r.get("source_domain"),
            "status": r.get("status"),
            "tags": r.get("tags") or {},
        }
        for r in rows
    ]
    return json.dumps(
        {"ok": True, "count": len(slim), "assets": slim, "filters": {
            "asset_type": asset_type, "status": status, "tags": tag_filters,
        }},
        ensure_ascii=False,
        indent=2,
        default=str,
    )


def _tool_get_asset(args: dict[str, Any]) -> str:
    asset_id = int(args.get("asset_id") or 0)
    if asset_id <= 0:
        return json.dumps({"ok": False, "reason": "asset_id mancante"})
    a = db.get_asset(asset_id)
    if not a:
        return json.dumps({"ok": False, "reason": f"asset #{asset_id} non trovato"})
    return json.dumps({"ok": True, "asset": a}, ensure_ascii=False, indent=2, default=str)


def _tool_update_asset_status(args: dict[str, Any]) -> str:
    asset_id = int(args.get("asset_id") or 0)
    status = str(args.get("status") or "").strip()
    notes = args.get("notes")
    if asset_id <= 0:
        return json.dumps({"ok": False, "reason": "asset_id mancante"})
    if status not in {"new", "qualified", "rejected", "archived"}:
        return json.dumps({"ok": False, "reason": "status deve essere new|qualified|rejected|archived"})
    if not db.get_asset(asset_id):
        return json.dumps({"ok": False, "reason": f"asset #{asset_id} non trovato"})
    db.update_asset_status(asset_id, status, notes=notes)
    return json.dumps({"ok": True, "asset_id": asset_id, "status": status})


def _tool_list_site_patterns(args: dict[str, Any]) -> str:
    domain = (str(args.get("registrable_domain") or "").strip()) or None
    status = (str(args.get("status") or "").strip()) or None
    limit = max(1, min(int(args.get("limit") or 50), 200))
    rows = db.list_site_patterns(registrable_domain=domain, status=status, limit=limit)
    slim = [
        {
            "id": r.get("id"),
            "registrable_domain": r.get("registrable_domain"),
            "pattern": r.get("pattern"),
            "regex": r.get("regex"),
            "asset_type": r.get("asset_type"),
            "status": r.get("status"),
            "hits": r.get("hits"),
            "successes": r.get("successes"),
            "failures": r.get("failures"),
        }
        for r in rows
    ]
    return json.dumps(
        {"ok": True, "count": len(slim), "patterns": slim},
        ensure_ascii=False,
        indent=2,
        default=str,
    )


def _tool_set_site_pattern_status(args: dict[str, Any]) -> str:
    pattern_id = int(args.get("pattern_id") or 0)
    status = str(args.get("status") or "").strip()
    notes = args.get("notes")
    if pattern_id <= 0:
        return json.dumps({"ok": False, "reason": "pattern_id mancante"})
    if status not in {"candidate", "confirmed", "rejected"}:
        return json.dumps({"ok": False, "reason": "status deve essere candidate|confirmed|rejected"})
    db.set_site_pattern_status(pattern_id, status, notes=notes)
    return json.dumps({"ok": True, "pattern_id": pattern_id, "status": status})


def _tool_list_site_playbooks(args: dict[str, Any]) -> str:
    """Lista i playbook persistiti. Cross-runner knowledge transfer (Stage 2)."""
    domain = (str(args.get("registrable_domain") or "").strip()) or None
    status = (str(args.get("status") or "").strip()) or None
    limit = max(1, min(int(args.get("limit") or 50), 200))
    rows = db.list_site_playbooks(registrable_domain=domain, status=status, limit=limit)
    slim = []
    for r in rows:
        # Il campo playbook e' JSON serializzato: esponilo parsato per leggibilita'
        pb_text = ""
        pb_blockers: list[Any] = []
        try:
            pb_obj = json.loads(r.get("playbook") or "{}")
            pb_text = (pb_obj.get("text") or "")[:400]
            pb_blockers = pb_obj.get("blockers") or []
        except Exception:
            pb_text = (r.get("playbook") or "")[:400]
        slim.append({
            "id": r.get("id"),
            "registrable_domain": r.get("registrable_domain"),
            "asset_type": r.get("asset_type"),
            "source_runner": r.get("source_runner"),
            "transferable": bool(r.get("transferable")),
            "status": r.get("status"),
            "hits": r.get("hits"),
            "successes": r.get("successes"),
            "failures": r.get("failures"),
            "playbook_preview": pb_text,
            "blockers": pb_blockers,
            "updated_at": r.get("updated_at"),
        })
    return json.dumps(
        {"ok": True, "count": len(slim), "playbooks": slim},
        ensure_ascii=False, indent=2, default=str,
    )


def _tool_delete_site_playbook(args: dict[str, Any]) -> str:
    """Cancella un playbook (force-refresh: il prossimo browser_use lo rigenera)."""
    pid = int(args.get("playbook_id") or 0)
    if pid <= 0:
        return json.dumps({"ok": False, "reason": "playbook_id mancante"})
    db.delete_site_playbook(pid)
    return json.dumps({"ok": True, "playbook_id": pid, "action": "deleted"})


def _chat_system_prompt(
    *,
    web_enabled: bool,
    files_enabled: bool,
    actions_enabled: bool,
    capabilities: dict[str, Any],
) -> str:
    snapshot = _orchestrator_snapshot()
    if web_enabled:
        web_line = (
            "Web abilitato: usa web_search e fetch_url per info aggiornate. Cita gli URL usati."
        )
    elif capabilities.get("web"):
        web_line = "Web disponibile ma non attivato per questa richiesta."
    else:
        web_line = f"Web non disponibile: {capabilities.get('web_reason')}"

    if files_enabled:
        files_line = (
            "Allegati abilitati: usa il blocco CONTESTO FILE ALLEGATO come fonte primaria quando presente."
        )
    elif capabilities.get("files"):
        files_line = "Allegati disponibili ma non in uso per questa richiesta."
    else:
        files_line = f"Allegati non disponibili: {capabilities.get('files_reason')}"

    if not capabilities.get("actions"):
        actions_line = (
            f"Azioni non disponibili: {capabilities.get('actions_reason')}. "
            "Non puoi creare/lanciare task: descrivi e proponi soltanto."
        )
    elif actions_enabled:
        actions_line = (
            "AZIONI ABILITATE per questo turno. Puoi usare i tool di scrittura "
            "(propose_plan, execute_plan, create_task, create_workflow, add_edge, start_job, start_workflow, "
            "update_asset_status, set_site_pattern_status). "
            "Per outreach/responder serve sempre confirm_risky=true E consenso esplicito dell'utente in chat."
        )
    else:
        actions_line = (
            "Azioni disabilitate per questo turno: hai solo i tool di lettura "
            "(list_tasks, get_task, list_workflows, list_jobs, get_job_status, list_extraction_templates, "
            "list_chat_models, list_assets, get_asset, list_site_patterns, "
            "list_guide_topics, read_guide_section). "
            "Per agire l'utente deve abilitare il toggle 'Azioni'."
        )

    return (
        "Sei l'Orchestrator di AgentScraper, il meta-agente che progetta, costruisce, lancia e monitora "
        "altri agenti per conto dell'utente. Italiano, operativo, asciutto: max 4-6 righe salvo richiesta esplicita. "
        "Niente introduzioni, niente riepiloghi ovvi, niente liste lunghe.\n\n"
        "MODALITA AGENTE DISPONIBILI (8 in totale, per pianificare task):\n"
        "Scraping (5):\n"
        "- react: ricerca web leggera con DDG+HTTP+readability, output report .md/.txt.\n"
        "- bulk_extract: HTTP+readability per URL noti su sito statico, output profiles.jsonl. Supporta crawler BFS dal seed con auto-detect pattern URL.\n"
        "- browser_use: Chromium reale via browser-use, JS/login/scroll/anti-bot, output profiles.jsonl. Lento e costoso (~$5-10 per sito).\n"
        "- auto_extract: profiler + dispatch automatico (bulk_extract / site_explorer / browser_use / skip) per liste eterogenee. Fallback bidirezionale.\n"
        "- site_explorer: Mapping LLM (3-5 step) + Extraction runner-driven deterministico. Tool LLM: fetch_page, enqueue_listings, discover_via_browser, start_extraction. Per siti listing→dettaglio, multi-livello, infinite-scroll.\n"
        "Pipeline downstream (3):\n"
        "- qualifier: legge profiles.jsonl, scora 0-10 via LLM, produce qualified.jsonl + DB contacts.\n"
        "- outreach: invia email/telegram ai qualified. RISCHIOSO.\n"
        "- responder: auto-reply inbound (con opt-out detection). RISCHIOSO.\n"
        "Convenzione artifact: extract*->qualifier passa profiles.jsonl; qualifier->outreach/responder passa qualified.jsonl.\n\n"
        "STRATEGIE SCRAPING (decision tree operativo — sintetizzato dalla GUIDA §3.0.3):\n"
        "A. Sito statico con pattern URL chiaro (cataloghi, e-commerce piccolo, immobili, directory) → bulk_extract con crawler ON (auto-detect pattern via 1 LLM call discovery, poi BFS deterministico).\n"
        "B. Sito multi-livello (categorie + sotto-categorie + paginazioni) → site_explorer con target_cap_per_site esplicito (30-100). Il LLM mappa le listing, il runner estrae.\n"
        "C. Sito INFINITE-SCROLL (social feed, camgirl, lazy-load, news feed) o 'voglio TUTTI i target del sito' → site_explorer con target_cap_per_site=0 (♾️ unbounded) E objective contenente keyword-trigger: 'tutti i profili', 'tutti i target', 'tutti gli annunci', 'tutti i prodotti', 'tutto il sito', 'centinaia', 'migliaia', 'infinite scroll', 'tutti i contatti', 'tutta la lista'. Il runner attiva auto-discovery FORZATA via Chromium headless (discover_via_browser, gratis come token, ~10-30s) PRIMA del turno LLM → raccoglie centinaia/migliaia di URL via scroll → li accoda al direct_target_queue. Senza il trigger il LLM potrebbe ignorare il sito infinite-scroll e vedere solo il first-paint statico.\n"
        "D. Sito con anti-bot / Cloudflare / login / JS-render puro → browser_use esplicito (riserva: lento e costoso).\n"
        "E. Lista mista di N siti diversi (B2B lead-gen, audit) → auto_extract (profiler decide per ogni sito).\n"
        "REFRESH POLICY (re-run incrementali): campo task refresh_policy_days. 0='mai re-extract se in DB' (risparmio max), N>0='re-extract se asset più vecchio di N giorni' (default 7), -1='sempre re-extract'. Il check usa source_url_canonical → dedup cross-lingua (`/it/x/` ≡ `/en/x/`) e cross-paginazione (`?p=0` ≡ no-query). I re-run dello stesso task saltano automaticamente gli URL già in DB freschi: niente fetch HTTP, niente LLM extractor, costo ~0.\n"
        "SITE PLAYBOOKS: a fine job riuscito, site_explorer e browser_use salvano un playbook nella tabella site_playbooks; al run successivo sullo stesso dominio, il LLM in fase MAPPING legge il playbook e salta 2-3 step di esplorazione.\n"
        "TOOL LLM di site_explorer in fase MAPPING (info utile per consigliare l'objective): fetch_page(url) ispeziona; enqueue_listings(urls, reason) accoda listing/categorie/paginazioni; discover_via_browser(url, scrolls, target_pattern_hint) è il tool browser headless deterministico (zero token, gratis) per siti infinite-scroll; start_extraction(summary) cede al runner. extract_target è chiamato dal runner, non dal LLM.\n\n"
        "FONTE PRIMARIA DI VERITA' — GUIDA.md:\n"
        "Hai accesso alla guida completa del progetto via i tool `list_guide_topics()` e "
        "`read_guide_section(query)`. La guida contiene best practice, configurazioni "
        "consigliate, regole d'oro, quale modello usare per quale task, casi d'uso "
        "completi. **PRIMA di consigliare un workflow o configurare un task complesso, "
        "leggi la sezione pertinente della guida** invece di tirare a indovinare.\n"
        "Esempi di flusso corretto:\n"
        "  • Utente chiede 'come configuro un site_explorer per un sito immobiliare':\n"
        "    1) read_guide_section('site_explorer') → leggi la sez. 3.4.1\n"
        "    2) sintetizzi i parametri consigliati (target_cap_per_site, refresh_policy_days, modello)\n"
        "    3) se Azioni ON, costruisci e proponi il task con propose_plan/create_task.\n"
        "  • Utente chiede 'come scrappo un sito infinite-scroll tipo Instagram':\n"
        "    1) read_guide_section('infinite scroll') o read_guide_section('3.0.3')\n"
        "    2) raccomandi site_explorer + target_cap_per_site=0 + objective con keyword-trigger\n"
        "       ('estrai tutti i profili pubblici del sito, è un sito infinite scroll').\n"
        "  • Utente chiede 'quale modello usare per qualifier':\n"
        "    1) read_guide_section('qualifier') o read_guide_section('provider LLM')\n"
        "    2) sintesi 1-2 righe.\n"
        "  • Utente chiede 'come faccio re-run incrementali senza ri-spendere':\n"
        "    1) read_guide_section('refresh_policy') o read_guide_section('3.0.3')\n"
        "    2) spieghi refresh_policy_days=0 ('mai') o 7 (default) + dedup via source_url_canonical.\n"
        "Non inventare configurazioni: se la guida lo dice, citala.\n\n"
        "OPERATIVITA:\n"
        "- Per costellazioni multi-task suggerisci di compilare il Brief e premere 'Genera piano' (canale canonico). "
        "In alternativa, con Azioni ON, usa propose_plan + execute_plan.\n"
        "- Per modifiche puntuali (lancia job 12, crea questo task, mostra stato workflow 4): usa direttamente i tool.\n"
        "- Per stato dei job/agenti usa list_jobs / get_job_status / list_tasks invece di descrivere a vuoto.\n"
        "- Per outreach/responder: chiedi consenso esplicito all'utente in chat PRIMA di passare confirm_risky=true.\n"
        "- Quando crei un task site_explorer per un sito infinite-scroll, passa SEMPRE target_cap_per_site=0 nel create_task E componi un objective che contenga una delle keyword-trigger sopra. Senza trigger, l'auto-discovery FORZATA non scatta e perderai i target lazy-loaded.\n\n"
        f"CAPACITA CHAT:\n- {web_line}\n- {files_line}\n- {actions_line}\n\n"
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
