"""Route per i Task (era 'projects'). Un task è un'attività autonoma."""
from __future__ import annotations

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
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


@router.get("/api/models", response_class=JSONResponse)
async def api_models_for_provider(provider: str = ""):
    """Ritorna la lista dei modelli compatibili per un provider.

    Usato dal form task per ripopolare i dropdown del modello quando l'utente
    cambia il provider (evita mismatch provider+model salvati incongruenti).

    Per `ollama`: query live a `list_models()` (cache lato Ollama).
    Per provider cloud: lista `suggested_models` dalla config del provider.
    """
    provider_key = (provider or "").strip().lower()
    if not provider_key:
        return {"provider": "", "models": []}
    if provider_key == "ollama":
        try:
            models = await list_models()
        except Exception:
            models = [settings.default_model]
        return {"provider": provider_key, "models": models}
    try:
        info = get_provider(provider_key)
    except Exception:
        return {"provider": provider_key, "models": []}
    suggested = info.get("suggested_models") or []
    return {
        "provider": provider_key,
        "models": [m["id"] if isinstance(m, dict) else str(m) for m in suggested],
    }


# Mapping agent_mode -> macro tipo (tab in /tasks). Gli agent_mode non
# elencati ricadono in 'other' (es. 'react' o futuri custom).
_TASK_TYPE_BY_MODE: dict[str, str] = {
    "browser_use":       "scraping",
    "bulk_extract":      "scraping",
    "auto_extract":      "scraping",
    "site_explorer":     "scraping",
    "recon_social":      "scraping",
    "qualifier":         "qualifier",
    "outreach":          "outreach",
    "outreach_social":   "outreach",
    "outreach_whatsapp": "outreach",
    "responder":         "responder",
}

# Tab definiti nella UI (ordine + label + icona). 'all' e' speciale (mostra tutto).
TASK_TYPE_TABS: list[tuple[str, str]] = [
    ("all",       "Tutti"),
    ("scraping",  "🕸️ Scraping / Recon"),
    ("qualifier", "✅ Qualifier"),
    ("outreach",  "📤 Outreach"),
    ("responder", "💬 Responder"),
    ("other",     "🤖 Altri"),
]


def _task_type_of(agent_mode: str | None) -> str:
    return _TASK_TYPE_BY_MODE.get((agent_mode or "").strip(), "other")


@router.get("/", response_class=HTMLResponse)
async def index(request: Request, status_tag: str = "", type: str = ""):
    all_tasks = db.list_tasks()
    # Conteggi per tab (sempre calcolati su all_tasks, prima di filtrare per tab,
    # ma DOPO l'eventuale filtro status_tag — cosi' i numeri sui tab riflettono
    # cio' che vedrebbe l'utente cliccandoli con lo status_tag attivo).
    if status_tag:
        if status_tag == "_unset":
            base = [t for t in all_tasks if not t.get("status_tag")]
        else:
            base = [t for t in all_tasks if (t.get("status_tag") or "") == status_tag]
    else:
        base = all_tasks

    counts_by_type: dict[str, int] = {"all": len(base)}
    for t in base:
        k = _task_type_of(t.get("agent_mode"))
        counts_by_type[k] = counts_by_type.get(k, 0) + 1

    active_type = (type or "").strip() or "all"
    if active_type != "all":
        tasks = [t for t in base if _task_type_of(t.get("agent_mode")) == active_type]
    else:
        tasks = base

    from ..dashboard import compute_task_health
    health_by_task = {t["id"]: compute_task_health(t["id"]) for t in tasks}
    return templates.TemplateResponse(
        request,
        "tasks_list.html",
        {
            "tasks": tasks,
            "health_by_task": health_by_task,
            "active_status_tag": status_tag,
            "active_type": active_type,
            "task_type_tabs": TASK_TYPE_TABS,
            "counts_by_type": counts_by_type,
        },
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
    bulk_concurrency: int = 5,
    target_cap_per_site: int = 30,
    refresh_policy_days: int = 7,
    bulk_rate_limit_per_sec: float = 2.0,
    bulk_extraction_method: str = "llm_per_page",
    bulk_css_selectors: str = "",
    crawler_enabled: str = "",
    crawler_url_pattern: str = "",
    crawler_max_depth: int = 3,
    discovery_llm_provider: str = "",
    discovery_llm_model: str = "",
    discovery_llm_api_key: str = "",
    max_discovery_retries: int = 3,
    browser_llm_provider: str = "",
    browser_llm_model: str = "",
    browser_llm_api_key: str = "",
    rating: str = "",
    notes: str = "",
    status_tag: str = "",
    # Campi outreach_social (aggiunti 2026-05-12)
    social_platform: str = "",
    outreach_intent: str = "",
    message_template_variants: str = "",
    max_dms_per_run: int = 30,
    max_dms_per_session: int = 5,
    headed: str = "",
    target_contact_ids: list[str] | None = None,
    # Campi outreach_whatsapp (aggiunti 2026-05-13)
    whatsapp_engine_preference: str = "auto",
    whatsapp_dry_run: str = "",
    whatsapp_account_id: str = "",
    whatsapp_api_config_id: str = "",
    # Campi recon_social (R1)
    recon_mode: str = "",
    recon_social_account_id: str = "",
    recon_hypothesis: str = "",
    recon_max_targets_per_day: int = 50,
    recon_score_threshold: int = 6,
    seed_queries_friends: str = "",
    input_asset_filter_type: str = "",
    output_asset_type: str = "",
    speed_profile: str = "safe",
    outreach_filter_source_task_id: str = "",
    outreach_filter_source_follower_of: str = "",
    outreach_filter_tags: list | None = None,
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
        "message_channels": message_channels,
        "responder_system_prompt": (responder_system_prompt or "").strip() or None,
        "bulk_concurrency": bulk_concurrency,
        "target_cap_per_site": target_cap_per_site,
        "refresh_policy_days": refresh_policy_days,
        "bulk_rate_limit_per_sec": bulk_rate_limit_per_sec,
        "bulk_extraction_method": (bulk_extraction_method or "llm_per_page").strip(),
        "bulk_css_selectors": (bulk_css_selectors or "").strip() or None,
        "crawler_enabled": bool(crawler_enabled),
        "crawler_url_pattern": (crawler_url_pattern or "").strip() or None,
        "crawler_max_depth": crawler_max_depth,
        "discovery_llm_provider": (discovery_llm_provider or "").strip() or None,
        "discovery_llm_model": (discovery_llm_model or "").strip() or None,
        "discovery_llm_api_key": (discovery_llm_api_key or "").strip() or None,
        "max_discovery_retries": max_discovery_retries,
        "browser_llm_provider": (browser_llm_provider or "").strip() or None,
        "browser_llm_model": (browser_llm_model or "").strip() or None,
        "browser_llm_api_key": (browser_llm_api_key or "").strip() or None,
        "rating": (rating or "").strip() or None,
        "notes": (notes or "").strip() or None,
        "status_tag": (status_tag or "").strip().lower() or None,
        # outreach_social
        "social_platform": (social_platform or "").strip().lower() or None,
        "outreach_intent": (outreach_intent or "").strip() or None,
        "message_template_variants": (message_template_variants or "").strip() or None,
        "max_dms_per_run": int(max_dms_per_run or 30),
        "max_dms_per_session": int(max_dms_per_session or 5),
        "headed": 1 if (str(headed).strip() in ("1", "on", "true", "yes")) else 0,
        "target_contact_ids": list(target_contact_ids or []),
        # outreach_whatsapp
        "whatsapp_engine_preference": (whatsapp_engine_preference or "auto").strip(),
        "whatsapp_dry_run": 1 if (str(whatsapp_dry_run).strip() in ("1", "on", "true", "yes")) else 0,
        "whatsapp_account_id": int(whatsapp_account_id) if str(whatsapp_account_id).strip().isdigit() else None,
        "whatsapp_api_config_id": int(whatsapp_api_config_id) if str(whatsapp_api_config_id).strip().isdigit() else None,
        # recon_social
        "recon_mode": (recon_mode or "").strip() or None,
        "recon_social_account_id": int(recon_social_account_id) if str(recon_social_account_id).strip().isdigit() else None,
        "recon_hypothesis": (recon_hypothesis or "").strip() or None,
        "recon_max_targets_per_day": int(recon_max_targets_per_day or 50),
        "recon_score_threshold": int(recon_score_threshold or 6),
        "seed_queries_friends": seed_queries_friends,
        "input_asset_filter": (
            {"asset_type": input_asset_filter_type.strip().lower()}
            if (input_asset_filter_type or "").strip()
            else None
        ),
        "output_asset_type": (output_asset_type or "").strip().lower() or None,
        "speed_profile": (speed_profile or "safe").strip().lower(),
        "outreach_filter_source_task_id": (
            int(outreach_filter_source_task_id) if str(outreach_filter_source_task_id).strip().isdigit() else None
        ),
        "outreach_filter_source_follower_of": (outreach_filter_source_follower_of or "").strip() or None,
        "outreach_filter_tags": list(outreach_filter_tags or []),
    }


def _form_extra_context() -> dict:
    # Helper: per ogni contatto con asset_id linkato, restituisce
    # (asset_type, [(tag_key, tag_value), ...] top 2) per arricchire la UI di
    # selezione (filtri e visualizzazione). Cache batch per ridurre query.
    def _enrich_contacts_with_asset_info(rows: list[dict]) -> list[dict]:
        if not rows:
            return []
        asset_ids = sorted({int(c["asset_id"]) for c in rows if c.get("asset_id")})
        assets_meta: dict[int, dict] = {}
        if asset_ids:
            with db.connect() as con:
                placeholders = ",".join(["%s"] * len(asset_ids))
                # asset_type per ogni asset_id
                for r in con.execute(
                    f"SELECT id, asset_type FROM assets WHERE id IN ({placeholders})",
                    asset_ids,
                ):
                    assets_meta[int(r["id"])] = {"asset_type": r["asset_type"], "tags": []}
                # tag dei top-2 per ogni asset, escludendo le keys "tecniche"
                # che non aiutano a filtrare contatti
                blacklist = {"platform", "source_domain", "source_url", "language", "source_follower_of"}
                for r in con.execute(
                    f"SELECT asset_id, tag_key, tag_value FROM asset_tags "
                    f"WHERE asset_id IN ({placeholders}) ORDER BY asset_id, tag_key",
                    asset_ids,
                ):
                    aid = int(r["asset_id"])
                    if aid not in assets_meta:
                        continue
                    if r["tag_key"] in blacklist:
                        continue
                    if len(assets_meta[aid]["tags"]) >= 2:
                        continue
                    assets_meta[aid]["tags"].append((r["tag_key"], r["tag_value"]))
        out: list[dict] = []
        for c in rows:
            aid = c.get("asset_id")
            meta = assets_meta.get(int(aid)) if aid else None
            out.append({
                "id": c["id"],
                "display_name": (c.get("display_name") or "").strip() or None,
                "source_domain": c.get("source_domain"),
                "status": c.get("status"),
                "url": c.get("_platform_url"),
                "email": c.get("email"),
                "asset_id": aid,
                "asset_type": (meta or {}).get("asset_type") or "(senza asset)",
                "top_tags": (meta or {}).get("tags") or [],
            })
        return out

    # Contacts disponibili per outreach_social + recon_social, raggruppati per
    # platform. Limit 1000/platform (era 500 — alzato per accomodare DB grandi).
    contacts_by_platform: dict[str, list[dict]] = {}
    for plat in ("instagram", "tiktok", "facebook"):
        rows = db.list_contacts_with_social_platform(plat, limit=1000)
        contacts_by_platform[plat] = _enrich_contacts_with_asset_info(rows)
    # Contacts disponibili per outreach_whatsapp (con whatsapp != null, no optedout)
    wa_rows = db.list_contacts_with_whatsapp(limit=500)
    contacts_with_whatsapp = [
        {
            "id": c["id"],
            "display_name": (c.get("display_name") or "").strip() or None,
            "source_domain": c.get("source_domain"),
            "source_url": c.get("source_url"),
            "status": c.get("status"),
            "whatsapp": c.get("whatsapp"),
            "whatsapp_consent": c.get("whatsapp_consent") or "cold",
            "email": c.get("email"),
        }
        for c in wa_rows
    ]
    # Sender disponibili per outreach_whatsapp (Motore A account + Motore B config)
    wa_accounts_rows = db.list_social_accounts(platform="whatsapp_browser")
    wa_accounts = [
        {
            "id": a["id"],
            "username": a.get("username"),
            "phone_number": a.get("phone_number"),
            "status": a.get("status"),
            "daily_dm_cap": a.get("daily_dm_cap"),
        }
        for a in wa_accounts_rows
    ]
    wa_api_configs_rows = db.list_whatsapp_api_config()
    wa_api_configs = [
        {
            "id": c["id"],
            "label": c.get("label"),
            "phone_number_id": c.get("phone_number_id"),
            "status": c.get("status"),
        }
        for c in wa_api_configs_rows
    ]
    # Account social loggati per recon_social (esclude whatsapp_browser =
    # è solo per messaggi WA; recon_social usa fb/ig/tiktok).
    recon_accounts_rows = db.list_social_accounts()
    recon_accounts = [
        {
            "id": a["id"],
            "platform": a.get("platform"),
            "username": a.get("username"),
            "status": a.get("status"),
        }
        for a in recon_accounts_rows
        if (a.get("platform") or "") in ("facebook", "instagram", "tiktok")
    ]
    # asset_types disponibili con count, per popolare il filtro "Asset DB"
    # nel form qualifier/outreach (alternativa a input_artifact_path).
    try:
        asset_types_in_use = db.list_asset_types_in_use()
    except Exception:
        asset_types_in_use = []

    # Per outreach_filter dropdown: task generatori di contacts + source_follower_of distinct
    try:
        outreach_source_tasks = db.list_distinct_contact_source_tasks()
    except Exception:
        outreach_source_tasks = []
    try:
        outreach_source_followers = db.list_distinct_source_follower_of()
    except Exception:
        outreach_source_followers = []
    # Tag keys disponibili sui contatti (multi-tag filter outreach)
    try:
        outreach_tag_keys = db.list_distinct_tag_keys_for_contacts()
    except Exception:
        outreach_tag_keys = []

    return {
        "extraction_templates": list_templates(),
        "default_schema": get_schema(None),
        "llm_providers": list_providers(),
        "env_key_status": env_key_status(),
        "contacts_by_platform": contacts_by_platform,
        "contacts_with_whatsapp": contacts_with_whatsapp,
        "wa_accounts": wa_accounts,
        "wa_api_configs": wa_api_configs,
        "recon_accounts": recon_accounts,
        "asset_types_in_use": asset_types_in_use,
        "outreach_source_tasks": outreach_source_tasks,
        "outreach_source_followers": outreach_source_followers,
        "outreach_tag_keys": outreach_tag_keys,
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
    bulk_concurrency: int = Form(5),
    target_cap_per_site: int = Form(30),
    refresh_policy_days: int = Form(7),
    bulk_rate_limit_per_sec: float = Form(2.0),
    bulk_extraction_method: str = Form("llm_per_page"),
    bulk_css_selectors: str = Form(""),
    crawler_enabled: str = Form(""),
    crawler_url_pattern: str = Form(""),
    crawler_max_depth: int = Form(3),
    discovery_llm_provider: str = Form(""),
    discovery_llm_model: str = Form(""),
    discovery_llm_api_key: str = Form(""),
    max_discovery_retries: int = Form(3),
    browser_llm_provider: str = Form(""),
    browser_llm_model: str = Form(""),
    browser_llm_api_key: str = Form(""),
    rating: str = Form(""),
    notes: str = Form(""),
    status_tag: str = Form(""),
    social_platform: str = Form(""),
    outreach_intent: str = Form(""),
    message_template_variants: str = Form(""),
    max_dms_per_run: int = Form(30),
    max_dms_per_session: int = Form(5),
    headed: str = Form(""),
    whatsapp_engine_preference: str = Form("auto"),
    whatsapp_dry_run: str = Form(""),
    whatsapp_account_id: str = Form(""),
    whatsapp_api_config_id: str = Form(""),
    recon_mode: str = Form(""),
    recon_social_account_id: str = Form(""),
    recon_hypothesis: str = Form(""),
    recon_max_targets_per_day: int = Form(50),
    recon_score_threshold: int = Form(6),
    seed_queries_friends: str = Form(""),
    input_asset_filter_type: str = Form(""),
    output_asset_type: str = Form(""),
    speed_profile: str = Form("safe"),
    outreach_filter_source_task_id: str = Form(""),
    outreach_filter_source_follower_of: str = Form(""),
):
    form = await request.form()
    target_contact_ids_raw = (
        form.getlist("target_contact_ids") if hasattr(form, "getlist") else []
    )
    # Parsing outreach_filter_tags da campi indicizzati tag_key__N/tag_value__N
    # (UI multi-row). Vince ordine di insert.
    outreach_filter_tags_raw: list[dict] = []
    for _i_form in range(20):
        _k = (form.get(f"outreach_filter_tag_key__{_i_form}") or "").strip().lower()
        _v = (form.get(f"outreach_filter_tag_value__{_i_form}") or "").strip()
        if _k and _v:
            outreach_filter_tags_raw.append({"key": _k, "value": _v})
    payload = _form_to_dict(
        name, description, objective, seed_queries, allowed_domains, blocked_domains,
        max_iterations, model, output_format, cron, agent_mode,
        extraction_template, extraction_schema, llm_provider, llm_base_url, llm_api_key,
        input_artifact_path, message_template, message_subject, message_channels,
        responder_system_prompt,
        bulk_concurrency, target_cap_per_site, refresh_policy_days, bulk_rate_limit_per_sec, bulk_extraction_method, bulk_css_selectors,
        crawler_enabled, crawler_url_pattern, crawler_max_depth,
        discovery_llm_provider, discovery_llm_model, discovery_llm_api_key,
        max_discovery_retries,
        browser_llm_provider, browser_llm_model, browser_llm_api_key,
        rating, notes, status_tag,
        social_platform=social_platform,
        outreach_intent=outreach_intent,
        message_template_variants=message_template_variants,
        max_dms_per_run=max_dms_per_run,
        max_dms_per_session=max_dms_per_session,
        headed=headed,
        target_contact_ids=target_contact_ids_raw,
        whatsapp_engine_preference=whatsapp_engine_preference,
        whatsapp_dry_run=whatsapp_dry_run,
        whatsapp_account_id=whatsapp_account_id,
        whatsapp_api_config_id=whatsapp_api_config_id,
        recon_mode=recon_mode,
        recon_social_account_id=recon_social_account_id,
        recon_hypothesis=recon_hypothesis,
        recon_max_targets_per_day=recon_max_targets_per_day,
        recon_score_threshold=recon_score_threshold,
        seed_queries_friends=seed_queries_friends,
        input_asset_filter_type=input_asset_filter_type,
        output_asset_type=output_asset_type,
        speed_profile=speed_profile,
        outreach_filter_source_task_id=outreach_filter_source_task_id,
        outreach_filter_source_follower_of=outreach_filter_source_follower_of,
        outreach_filter_tags=outreach_filter_tags_raw,
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
    bulk_concurrency: int = Form(5),
    target_cap_per_site: int = Form(30),
    refresh_policy_days: int = Form(7),
    bulk_rate_limit_per_sec: float = Form(2.0),
    bulk_extraction_method: str = Form("llm_per_page"),
    bulk_css_selectors: str = Form(""),
    crawler_enabled: str = Form(""),
    crawler_url_pattern: str = Form(""),
    crawler_max_depth: int = Form(3),
    discovery_llm_provider: str = Form(""),
    discovery_llm_model: str = Form(""),
    discovery_llm_api_key: str = Form(""),
    max_discovery_retries: int = Form(3),
    browser_llm_provider: str = Form(""),
    browser_llm_model: str = Form(""),
    browser_llm_api_key: str = Form(""),
    rating: str = Form(""),
    notes: str = Form(""),
    status_tag: str = Form(""),
    social_platform: str = Form(""),
    outreach_intent: str = Form(""),
    message_template_variants: str = Form(""),
    max_dms_per_run: int = Form(30),
    max_dms_per_session: int = Form(5),
    headed: str = Form(""),
    whatsapp_engine_preference: str = Form("auto"),
    whatsapp_dry_run: str = Form(""),
    whatsapp_account_id: str = Form(""),
    whatsapp_api_config_id: str = Form(""),
    recon_mode: str = Form(""),
    recon_social_account_id: str = Form(""),
    recon_hypothesis: str = Form(""),
    recon_max_targets_per_day: int = Form(50),
    recon_score_threshold: int = Form(6),
    seed_queries_friends: str = Form(""),
    input_asset_filter_type: str = Form(""),
    output_asset_type: str = Form(""),
    speed_profile: str = Form("safe"),
    outreach_filter_source_task_id: str = Form(""),
    outreach_filter_source_follower_of: str = Form(""),
):
    form = await request.form()
    target_contact_ids_raw = (
        form.getlist("target_contact_ids") if hasattr(form, "getlist") else []
    )
    # Parsing outreach_filter_tags da campi indicizzati tag_key__N/tag_value__N
    # (UI multi-row). Vince ordine di insert.
    outreach_filter_tags_raw: list[dict] = []
    for _i_form in range(20):
        _k = (form.get(f"outreach_filter_tag_key__{_i_form}") or "").strip().lower()
        _v = (form.get(f"outreach_filter_tag_value__{_i_form}") or "").strip()
        if _k and _v:
            outreach_filter_tags_raw.append({"key": _k, "value": _v})
    existing = db.get_task(task_id)
    if not existing:
        raise HTTPException(status_code=404, detail="task non trovato")

    # PRESERVE-ON-EMPTY per i campi sensibili (password): se il form li manda
    # vuoti significa "non cambiare", non "azzera". I browser non ri-popolano
    # i campi password per sicurezza, quindi un utente che modifica solo altri
    # campi non perde le chiavi salvate.
    # Per ESPLICITAMENTE cancellare una chiave salvata, l'utente scrive "CLEAR"
    # nel campo (un sentinel pubblicizzato nella UI).
    def _password_field_action(submitted: str, existing_value: str | None) -> str:
        s = (submitted or "").strip()
        if s.upper() == "CLEAR":
            return ""  # azzeramento esplicito
        if not s and existing_value:
            return existing_value  # preserve
        return s

    llm_api_key = _password_field_action(llm_api_key, existing.get("llm_api_key"))
    discovery_llm_api_key = _password_field_action(
        discovery_llm_api_key, existing.get("discovery_llm_api_key")
    )
    browser_llm_api_key = _password_field_action(
        browser_llm_api_key, existing.get("browser_llm_api_key")
    )

    payload = _form_to_dict(
        name, description, objective, seed_queries, allowed_domains, blocked_domains,
        max_iterations, model, output_format, cron, agent_mode,
        extraction_template, extraction_schema, llm_provider, llm_base_url, llm_api_key,
        input_artifact_path, message_template, message_subject, message_channels,
        responder_system_prompt,
        bulk_concurrency, target_cap_per_site, refresh_policy_days, bulk_rate_limit_per_sec, bulk_extraction_method, bulk_css_selectors,
        crawler_enabled, crawler_url_pattern, crawler_max_depth,
        discovery_llm_provider, discovery_llm_model, discovery_llm_api_key,
        max_discovery_retries,
        browser_llm_provider, browser_llm_model, browser_llm_api_key,
        rating, notes, status_tag,
        social_platform=social_platform,
        outreach_intent=outreach_intent,
        message_template_variants=message_template_variants,
        max_dms_per_run=max_dms_per_run,
        max_dms_per_session=max_dms_per_session,
        headed=headed,
        target_contact_ids=target_contact_ids_raw,
        whatsapp_engine_preference=whatsapp_engine_preference,
        whatsapp_dry_run=whatsapp_dry_run,
        whatsapp_account_id=whatsapp_account_id,
        whatsapp_api_config_id=whatsapp_api_config_id,
        recon_mode=recon_mode,
        recon_social_account_id=recon_social_account_id,
        recon_hypothesis=recon_hypothesis,
        recon_max_targets_per_day=recon_max_targets_per_day,
        recon_score_threshold=recon_score_threshold,
        seed_queries_friends=seed_queries_friends,
        input_asset_filter_type=input_asset_filter_type,
        output_asset_type=output_asset_type,
        speed_profile=speed_profile,
        outreach_filter_source_task_id=outreach_filter_source_task_id,
        outreach_filter_source_follower_of=outreach_filter_source_follower_of,
        outreach_filter_tags=outreach_filter_tags_raw,
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


@router.post("/tasks/{task_id}/clone")
async def clone_task(task_id: int):
    """Crea una copia esatta del task con suffisso '(copy)' e cron=null.
    Tutti gli altri campi (model, agent_mode, target_contact_ids,
    output_asset_type, ecc.) sono preservati. Redirect alla pagina edit
    del nuovo task per permettere modifica immediata.

    Pattern d'uso: 1 task base "Donald_Scrap_Template" → clona N volte →
    per ognuno cambi target_contact_ids + cron sfalsato in giorni diversi.
    Permette scaling per agente target senza ricompilare il form.
    """
    src = db.get_task(task_id)
    if not src:
        raise HTTPException(status_code=404, detail="task non trovato")
    # Strip campi non clonabili / auto-generati
    data = {k: v for k, v in src.items() if k not in ("id", "created_at", "updated_at")}
    # Forza nome distinto + niente cron (l'utente lo riconfigura, evita auto-run di duplicati)
    data["name"] = (src.get("name") or "task") + " (copy)"
    data["cron"] = None
    # Status_tag default: tuning (è un task fresco da configurare)
    if data.get("status_tag") == "working":
        data["status_tag"] = "tuning"
    try:
        new_id = db.create_task(data)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"clone fallito: {e}")
    jobs.reload_schedules()
    return RedirectResponse(
        url=f"/tasks/{new_id}/edit?flash=Clonato+da+%23{task_id}",
        status_code=303,
    )


@router.post("/tasks/{task_id}/toggle-disabled")
async def toggle_task_disabled(task_id: int, redirect_to: str = Form("/")):
    """Toggle del flag `disabled` di un task. Bloccato → non si lancia.

    `redirect_to` controlla dove tornare (default: home `/`, ma puo' essere `/tasks/X`).
    """
    t = db.get_task(task_id)
    if t is None:
        return RedirectResponse(url=redirect_to or "/", status_code=303)
    new_val = not bool(t.get("disabled"))
    db.set_task_disabled(task_id, new_val)
    return RedirectResponse(url=redirect_to or "/", status_code=303)


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


@router.post("/artifacts/upload")
async def upload_artifact(file: UploadFile = File(...)) -> JSONResponse:
    """Salva un file caricato dal browser in data/uploads/<ts>/<filename> e
    ritorna il path assoluto. Usato dal file picker del form quando l'utente
    sceglie un file dal proprio filesystem invece di selezionarlo dai task
    già eseguiti.

    Vincoli: solo file .jsonl o .ndjson, max 50 MB.
    """
    from datetime import datetime, timezone
    import re
    from ..config import UPLOADS_DIR

    fname = (file.filename or "").strip()
    if not fname:
        return JSONResponse({"error": "filename mancante"}, status_code=400)
    if not fname.lower().endswith((".jsonl", ".ndjson")):
        return JSONResponse(
            {"error": f"tipo file non supportato: {fname!r}. Solo .jsonl o .ndjson."},
            status_code=400,
        )

    # Sanitizza il filename: tieni solo lettere/numeri/-_.
    safe_name = re.sub(r"[^A-Za-z0-9._-]", "_", fname)[:120]
    if not safe_name:
        safe_name = "uploaded.jsonl"

    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    dest_dir = UPLOADS_DIR / ts
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest_path = dest_dir / safe_name

    MAX_BYTES = 50 * 1024 * 1024
    total = 0
    try:
        with dest_path.open("wb") as out:
            while True:
                chunk = await file.read(1024 * 64)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_BYTES:
                    out.close()
                    dest_path.unlink(missing_ok=True)
                    return JSONResponse(
                        {"error": f"file troppo grande (>{MAX_BYTES // (1024*1024)} MB)"},
                        status_code=413,
                    )
                out.write(chunk)
    except Exception as e:
        dest_path.unlink(missing_ok=True)
        return JSONResponse({"error": f"upload fallito: {e}"}, status_code=500)

    # Conta righe valide (validazione minima del formato jsonl)
    n_lines = 0
    try:
        for line in dest_path.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.strip():
                n_lines += 1
    except Exception:
        pass

    return JSONResponse({
        "path": str(dest_path),
        "filename": safe_name,
        "size_bytes": total,
        "n_lines": n_lines,
    })


@router.get("/artifacts/jsonl", response_class=HTMLResponse)
async def list_jsonl_artifacts(request: Request):
    """Endpoint HTMX: lista i file .jsonl in data/results/ per il file picker.

    Ritorna un <select> da swappare nel form. L'utente sceglie un file e il
    suo path va a finire nel campo `input_artifact_path`.
    """
    from ..config import RESULTS_DIR
    from pathlib import Path

    items: list[dict] = []
    if RESULTS_DIR.exists():
        # struttura: data/results/<task_id>/<timestamp>/*.jsonl
        for task_dir in RESULTS_DIR.iterdir():
            if not task_dir.is_dir():
                continue
            try:
                tid = int(task_dir.name)
            except ValueError:
                continue
            t = db.get_task(tid)
            task_name = t.get("name") if t else f"(task#{tid} eliminato)"
            for run_dir in task_dir.iterdir():
                if not run_dir.is_dir():
                    continue
                for f in run_dir.iterdir():
                    if not f.is_file() or not f.name.endswith(".jsonl"):
                        continue
                    try:
                        n_lines = sum(
                            1 for line in f.read_text(encoding="utf-8").splitlines()
                            if line.strip()
                        )
                    except Exception:
                        n_lines = 0
                    items.append({
                        "path": str(f),
                        "task_id": tid,
                        "task_name": task_name,
                        "run_dir": run_dir.name,
                        "filename": f.name,
                        "n_lines": n_lines,
                        "size_bytes": f.stat().st_size,
                        "mtime": f.stat().st_mtime,
                    })
    # ordina per mtime decrescente (più recenti prima)
    items.sort(key=lambda x: x["mtime"], reverse=True)
    return templates.TemplateResponse(
        request,
        "partials/artifacts_picker.html",
        {"items": items[:200]},
    )


@router.get("/tasks/{task_id}", response_class=HTMLResponse)
async def task_detail(request: Request, task_id: int):
    task = db.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="task non trovato")
    task_jobs = db.list_jobs(task_id)
    latest = db.latest_job(task_id)
    from ..dashboard import compute_dashboard, compute_task_health
    latest_dashboard = compute_dashboard(latest["id"]) if latest else None
    health = compute_task_health(task_id)
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
            "health": health,
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
