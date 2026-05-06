"""Route per i Task (era 'projects'). Un task è un'attività autonoma."""
from __future__ import annotations

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import ValidationError

from .. import db, jobs
from ..agent.extraction_templates import TEMPLATES, get_schema, list_templates
from ..agent.llm_providers import (
    DEFAULT_PROVIDER,
    env_key_status,
    get_provider,
    list_providers,
)
from ..agent.ollama import list_models
from ..config import settings
from ..models import TaskIn
from ..templates import templates

router = APIRouter()


@router.get("/", response_class=HTMLResponse)
async def index(request: Request):
    tasks = db.list_tasks()
    return templates.TemplateResponse(
        request, "tasks_list.html", {"tasks": tasks}
    )


@router.get("/tasks/new", response_class=HTMLResponse)
async def new_task_form(request: Request):
    try:
        models = await list_models()
    except Exception:
        models = [settings.default_model]
    return templates.TemplateResponse(
        request,
        "task_form.html",
        {
            "task": None,
            "models": models,
            "default_model": settings.default_model,
            "errors": None,
            **_form_extra_context(),
        },
    )


@router.get("/tasks/{task_id}/edit", response_class=HTMLResponse)
async def edit_task_form(request: Request, task_id: int):
    task = db.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="task non trovato")
    try:
        models = await list_models()
    except Exception:
        models = [settings.default_model]
    return templates.TemplateResponse(
        request,
        "task_form.html",
        {
            "task": task,
            "models": models,
            "default_model": settings.default_model,
            "errors": None,
            **_form_extra_context(),
        },
    )


def _form_to_dict(
    name: str,
    description: str | None,
    objective: str,
    seed_queries: str,
    allowed_domains: str,
    blocked_domains: str,
    max_iterations: int,
    model: str,
    output_format: str,
    cron: str,
    agent_mode: str,
    extraction_template: str,
    extraction_schema: str,
    llm_provider: str,
    llm_base_url: str,
    llm_api_key: str,
    input_artifact_path: str = "",
    message_template: str = "",
    message_subject: str = "",
    message_channels: str = "",
    responder_system_prompt: str = "",
) -> dict:
    return {
        "name": name.strip(),
        "description": (description or "").strip() or None,
        "objective": objective.strip(),
        "seed_queries": seed_queries,
        "allowed_domains": allowed_domains,
        "blocked_domains": blocked_domains,
        "max_iterations": max_iterations,
        "model": model.strip() or settings.default_model,
        "output_format": output_format,
        "cron": cron,
        "agent_mode": agent_mode or "react",
        "extraction_template": (extraction_template or "").strip() or None,
        "extraction_schema": (extraction_schema or "").strip() or None,
        "llm_provider": (llm_provider or DEFAULT_PROVIDER).strip(),
        "llm_base_url": (llm_base_url or "").strip() or None,
        "llm_api_key": (llm_api_key or "").strip() or None,
        "input_artifact_path": (input_artifact_path or "").strip() or None,
        "message_template": (message_template or "").strip() or None,
        "message_subject": (message_subject or "").strip() or None,
        "message_channels": message_channels,  # validator del Pydantic lo splitta
        "responder_system_prompt": (responder_system_prompt or "").strip() or None,
    }


def _form_extra_context() -> dict:
    return {
        "extraction_templates": list_templates(),
        "default_schema": get_schema(None),
        "llm_providers": list_providers(),
        "env_key_status": env_key_status(),
    }


@router.post("/tasks")
async def create_task(
    request: Request,
    name: str = Form(""),
    description: str = Form(""),
    objective: str = Form(""),
    seed_queries: str = Form(""),
    allowed_domains: str = Form(""),
    blocked_domains: str = Form(""),
    max_iterations: int = Form(10),
    model: str = Form(""),
    output_format: str = Form("txt"),
    cron: str = Form(""),
    agent_mode: str = Form("react"),
    extraction_template: str = Form(""),
    extraction_schema: str = Form(""),
    llm_provider: str = Form("ollama"),
    llm_base_url: str = Form(""),
    llm_api_key: str = Form(""),
    input_artifact_path: str = Form(""),
    message_template: str = Form(""),
    message_subject: str = Form(""),
    message_channels: str = Form(""),
    responder_system_prompt: str = Form(""),
):
    payload = _form_to_dict(
        name, description, objective, seed_queries, allowed_domains, blocked_domains,
        max_iterations, model, output_format, cron, agent_mode,
        extraction_template, extraction_schema, llm_provider, llm_base_url, llm_api_key,
        input_artifact_path, message_template, message_subject, message_channels,
        responder_system_prompt,
    )
    try:
        validated = TaskIn(**payload)
    except ValidationError as e:
        try:
            models = await list_models()
        except Exception:
            models = [settings.default_model]
        return templates.TemplateResponse(
            request,
            "task_form.html",
            {
                "task": payload,
                "models": models,
                "default_model": settings.default_model,
                "errors": e.errors(),
                **_form_extra_context(),
            },
            status_code=400,
        )
    task_id = db.create_task(validated.model_dump())
    jobs.reload_schedules()
    return RedirectResponse(url=f"/tasks/{task_id}", status_code=303)


@router.post("/tasks/{task_id}")
async def update_task(
    task_id: int,
    request: Request,
    name: str = Form(""),
    description: str = Form(""),
    objective: str = Form(""),
    seed_queries: str = Form(""),
    allowed_domains: str = Form(""),
    blocked_domains: str = Form(""),
    max_iterations: int = Form(10),
    model: str = Form(""),
    output_format: str = Form("txt"),
    cron: str = Form(""),
    agent_mode: str = Form("react"),
    extraction_template: str = Form(""),
    extraction_schema: str = Form(""),
    llm_provider: str = Form("ollama"),
    llm_base_url: str = Form(""),
    llm_api_key: str = Form(""),
    input_artifact_path: str = Form(""),
    message_template: str = Form(""),
    message_subject: str = Form(""),
    message_channels: str = Form(""),
    responder_system_prompt: str = Form(""),
):
    if not db.get_task(task_id):
        raise HTTPException(status_code=404, detail="task non trovato")
    payload = _form_to_dict(
        name, description, objective, seed_queries, allowed_domains, blocked_domains,
        max_iterations, model, output_format, cron, agent_mode,
        extraction_template, extraction_schema, llm_provider, llm_base_url, llm_api_key,
        input_artifact_path, message_template, message_subject, message_channels,
        responder_system_prompt,
    )
    try:
        validated = TaskIn(**payload)
    except ValidationError as e:
        try:
            models = await list_models()
        except Exception:
            models = [settings.default_model]
        existing = db.get_task(task_id) or {}
        existing.update(payload)
        existing["id"] = task_id
        return templates.TemplateResponse(
            request,
            "task_form.html",
            {
                "task": existing,
                "models": models,
                "default_model": settings.default_model,
                "errors": e.errors(),
                **_form_extra_context(),
            },
            status_code=400,
        )
    db.update_task(task_id, validated.model_dump())
    jobs.reload_schedules()
    return RedirectResponse(url=f"/tasks/{task_id}", status_code=303)


@router.post("/tasks/{task_id}/delete")
async def delete_task(task_id: int):
    db.delete_task(task_id)
    jobs.reload_schedules()
    return RedirectResponse(url="/", status_code=303)


@router.get("/providers/model-field", response_class=HTMLResponse)
async def provider_model_field(
    request: Request,
    llm_provider: str = "ollama",
):
    """Endpoint HTMX: ritorna il blocco con dropdown modelli + base URL + API key per il provider."""
    info = get_provider(llm_provider)
    suggested = list(info.get("suggested_models") or [])
    if llm_provider == "ollama":
        try:
            ollama_models = await list_models()
        except Exception:
            ollama_models = []
        suggested = [
            {"id": m, "desc": "(installato in locale)"} for m in ollama_models
        ] + suggested
    default_model = suggested[0]["id"] if suggested else ""
    return templates.TemplateResponse(
        request,
        "partials/provider_model_field.html",
        {
            "provider_key": llm_provider,
            "provider": info,
            "suggested_models": suggested,
            "default_model": default_model,
            "current_model": "",
            "current_base_url": "",
            "current_api_key": "",
            "env_key_set": env_key_status().get(llm_provider, False),
        },
    )


@router.get("/templates/extraction", response_class=HTMLResponse)
async def template_schema_partial(request: Request, key: str = ""):
    """Endpoint HTMX: ritorna la textarea con lo schema del template scelto."""
    schema_text = get_schema(key) if key in TEMPLATES else ""
    return templates.TemplateResponse(
        request,
        "partials/extraction_schema_field.html",
        {"schema_text": schema_text, "selected_key": key},
    )


@router.get("/tasks/{task_id}", response_class=HTMLResponse)
async def task_detail(request: Request, task_id: int):
    task = db.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="task non trovato")
    task_jobs = db.list_jobs(task_id)
    latest = db.latest_job(task_id)
    from ..dashboard import compute_dashboard
    latest_dashboard = compute_dashboard(latest["id"]) if latest else None
    # Workflow in cui appare questo task
    edges_with_task = db.list_edges(from_task_id=task_id) + db.list_edges(to_task_id=task_id)
    workflow_ids = sorted({e["workflow_id"] for e in edges_with_task if e.get("workflow_id")})
    related_workflows = [db.get_workflow(wid) for wid in workflow_ids if db.get_workflow(wid)]
    return templates.TemplateResponse(
        request,
        "task_detail.html",
        {
            "task": task,
            "jobs": task_jobs,
            "latest_dashboard": latest_dashboard,
            "related_workflows": related_workflows,
        },
    )


@router.get("/tasks/{task_id}/jobs", response_class=HTMLResponse)
async def task_jobs_partial(request: Request, task_id: int):
    task = db.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="task non trovato")
    task_jobs = db.list_jobs(task_id)
    has_active = any(j["status"] in ("queued", "running") for j in task_jobs)
    return templates.TemplateResponse(
        request,
        "partials/jobs_history_wrapper.html",
        {"task": task, "jobs": task_jobs, "has_active": has_active},
    )
