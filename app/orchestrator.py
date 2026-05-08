from __future__ import annotations

import json
import re
from typing import Any, Literal

import httpx
from pydantic import BaseModel, Field

from . import db, jobs
from .agent.extraction_templates import get_schema
from .agent.llm_providers import resolve_api_key, resolve_base_url
from .config import settings


AgentMode = Literal[
    "react",
    "browser_use",
    "bulk_extract",
    "auto_extract",
    "qualifier",
    "outreach",
    "responder",
]

AutonomyLevel = Literal["advisor", "builder", "supervised", "autonomous"]
RiskLevel = Literal["low", "medium", "high"]

AUTONOMY_LEVELS: dict[str, dict[str, Any]] = {
    "advisor": {
        "label": "Consigliere",
        "description": "Propone un piano leggibile, senza creare o lanciare nulla.",
        "can_create": False,
        "can_run": False,
    },
    "builder": {
        "label": "Builder",
        "description": "Crea task e workflow solo dopo conferma; non lancia job.",
        "can_create": True,
        "can_run": False,
    },
    "supervised": {
        "label": "Supervisionato",
        "description": "Crea e puo lanciare workflow dopo conferma esplicita.",
        "can_create": True,
        "can_run": True,
    },
    "autonomous": {
        "label": "Autonomo controllato",
        "description": "Crea e lancia il piano dopo conferma iniziale; messaggistica richiede un consenso dedicato.",
        "can_create": True,
        "can_run": True,
    },
}

RISKY_AGENT_MODES = {"outreach", "responder"}


class PlannedTask(BaseModel):
    key: str = Field(min_length=1, max_length=60)
    name: str = Field(min_length=1, max_length=200)
    agent_mode: AgentMode = "react"
    objective: str = Field(min_length=1)
    description: str | None = None
    seed_queries: list[str] = Field(default_factory=list)
    allowed_domains: list[str] = Field(default_factory=list)
    blocked_domains: list[str] = Field(default_factory=list)
    max_iterations: int = Field(default=10, ge=1, le=100000)
    model: str = "qwen3.5:latest"
    output_format: Literal["txt", "md", "both"] = "md"
    extraction_template: str | None = None
    extraction_schema: str | None = None
    llm_provider: str = "ollama"
    llm_base_url: str | None = None
    input_artifact_path: str | None = None
    message_subject: str | None = None
    message_template: str | None = None
    message_channels: list[str] = Field(default_factory=list)
    responder_system_prompt: str | None = None
    bulk_concurrency: int = 5
    bulk_rate_limit_per_sec: float = 2.0
    crawler_enabled: bool = False
    crawler_max_depth: int = 3
    notes: str | None = None
    status_tag: str | None = "tuning"


class PlannedEdge(BaseModel):
    from_key: str
    to_key: str
    pass_artifact: str | None = None


class OrchestratorPlan(BaseModel):
    title: str = "Piano orchestrator"
    summary: str
    autonomy_level: AutonomyLevel = "builder"
    risk_level: RiskLevel = "medium"
    assumptions: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    tasks: list[PlannedTask] = Field(default_factory=list)
    edges: list[PlannedEdge] = Field(default_factory=list)
    run_after_create: bool = False
    planner_used: str = "heuristic"

    @property
    def has_risky_modes(self) -> bool:
        return any(t.agent_mode in RISKY_AGENT_MODES for t in self.tasks)


class ExecutionResult(BaseModel):
    created_task_ids: dict[str, int] = Field(default_factory=dict)
    workflow_id: int | None = None
    workflow_run_id: int | None = None
    started_jobs: list[int] = Field(default_factory=list)
    redirect_url: str
    message: str


URL_RE = re.compile(r"https?://[^\s<>\]\)\"']+", re.IGNORECASE)
DOMAIN_RE = re.compile(r"\b(?:[a-z0-9-]+\.)+[a-z]{2,}\b", re.IGNORECASE)


def autonomy_meta(level: str) -> dict[str, Any]:
    return AUTONOMY_LEVELS.get(level, AUTONOMY_LEVELS["builder"])


def extract_urls(text: str) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for m in URL_RE.finditer(text or ""):
        url = m.group(0).rstrip(".,;")
        if url not in seen:
            seen.add(url)
            out.append(url)
    return out


def _extract_allowed_domains(text: str, urls: list[str]) -> list[str]:
    domains: list[str] = []
    for url in urls:
        try:
            from urllib.parse import urlparse

            host = (urlparse(url).hostname or "").lower()
        except Exception:
            host = ""
        if host and host not in domains:
            domains.append(host)
    if domains:
        return domains
    # fallback: only use bare domains if the prompt sounds like a site list
    lowered = (text or "").lower()
    if any(k in lowered for k in ("sito", "siti", "dominio", "url", "catalogo")):
        for m in DOMAIN_RE.finditer(text or ""):
            d = m.group(0).lower()
            if d not in domains:
                domains.append(d)
    return domains[:20]


def _wants_any(text: str, keywords: tuple[str, ...]) -> bool:
    t = (text or "").lower()
    return any(k in t for k in keywords)


def _safe_key(base: str, used: set[str]) -> str:
    key = re.sub(r"[^a-z0-9_]+", "_", base.lower()).strip("_") or "task"
    candidate = key
    i = 2
    while candidate in used:
        candidate = f"{key}_{i}"
        i += 1
    used.add(candidate)
    return candidate


def _edge_artifact(from_mode: str, to_mode: str) -> str | None:
    if from_mode in {"browser_use", "bulk_extract", "auto_extract"}:
        return "profiles.jsonl"
    if from_mode == "qualifier":
        return "qualified.jsonl"
    return None


def _default_model_for_mode(mode: str, provider: str, requested_model: str | None) -> str:
    if mode == "react":
        return settings.default_model
    return requested_model or settings.default_model


def _planned_task(
    *,
    key: str,
    name: str,
    agent_mode: AgentMode,
    objective: str,
    brief: str,
    urls: list[str],
    domains: list[str],
    provider: str,
    model: str | None,
    llm_base_url: str | None,
) -> PlannedTask:
    task_model = _default_model_for_mode(agent_mode, provider, model)
    llm_provider = "ollama" if agent_mode == "react" else provider
    extraction_template = "profile_contacts" if agent_mode in {"browser_use", "bulk_extract", "auto_extract"} else None
    extraction_schema = get_schema(extraction_template) if extraction_template else None
    max_iterations = 10
    if agent_mode == "browser_use":
        max_iterations = 30
    elif agent_mode in {"bulk_extract", "auto_extract"}:
        max_iterations = 200
    elif agent_mode in {"qualifier", "outreach", "responder"}:
        max_iterations = 10

    return PlannedTask(
        key=key,
        name=name,
        agent_mode=agent_mode,
        objective=objective,
        description=f"Creato da Orchestrator dal brief: {brief[:300]}",
        seed_queries=urls if agent_mode in {"browser_use", "bulk_extract", "auto_extract"} else [],
        allowed_domains=domains if agent_mode in {"browser_use", "bulk_extract", "auto_extract"} else [],
        max_iterations=max_iterations,
        model=task_model,
        output_format="md",
        extraction_template=extraction_template,
        extraction_schema=extraction_schema,
        llm_provider=llm_provider,
        llm_base_url=llm_base_url if llm_provider == "custom" else None,
        bulk_concurrency=5,
        bulk_rate_limit_per_sec=2.0,
        crawler_enabled=agent_mode in {"bulk_extract", "auto_extract"} and bool(urls),
        crawler_max_depth=3,
        notes="Creato da Orchestrator. Verifica schema, modelli e limiti prima di produzioni grandi.",
        status_tag="tuning",
    )


def build_heuristic_plan(
    brief: str,
    autonomy_level: AutonomyLevel,
    provider: str = "ollama",
    model: str | None = None,
    llm_base_url: str | None = None,
) -> OrchestratorPlan:
    text = brief.strip()
    urls = extract_urls(text)
    domains = _extract_allowed_domains(text, urls)
    used: set[str] = set()
    tasks: list[PlannedTask] = []

    wants_extract = _wants_any(
        text,
        (
            "estrai",
            "estrarre",
            "scrape",
            "scraping",
            "contatti",
            "lead",
            "profili",
            "catalogo",
            "prodotti",
            "annunci",
        ),
    )
    wants_qualifier = _wants_any(text, ("qualifica", "filtra", "scora", "score", "valuta", "validi"))
    wants_outreach = _wants_any(text, ("outreach", "email", "telegram", "contatta", "messaggi", "campagna"))
    wants_responder = _wants_any(text, ("rispondi", "reply", "auto-reply", "inbound", "posta in arrivo"))
    wants_research = _wants_any(text, ("cerca", "ricerca", "report", "analizza", "trova informazioni"))

    if wants_responder and not wants_extract:
        key = _safe_key("responder", used)
        tasks.append(
            PlannedTask(
                key=key,
                name="Responder automatico",
                agent_mode="responder",
                objective=text,
                description="Auto-reply orchestrata sui messaggi inbound.",
                model=model or settings.default_model,
                llm_provider=provider,
                llm_base_url=llm_base_url if provider == "custom" else None,
                responder_system_prompt=(
                    "Rispondi in italiano con tono professionale e sintetico. "
                    "Non inventare dati. Se il messaggio contiene opt-out, non rispondere."
                ),
                notes="Creato da Orchestrator. Modalita rischiosa: controlla bene prima di lanciare.",
                status_tag="tuning",
            )
        )
    elif wants_extract or urls:
        mode: AgentMode = "auto_extract" if len(urls) != 1 else "bulk_extract"
        if _wants_any(text, ("browser", "javascript", "js", "dinamico", "click", "scroll")):
            mode = "browser_use"
        key = _safe_key("extract", used)
        tasks.append(
            _planned_task(
                key=key,
                name="Estrazione dati",
                agent_mode=mode,
                objective=text,
                brief=text,
                urls=urls,
                domains=domains,
                provider=provider,
                model=model,
                llm_base_url=llm_base_url,
            )
        )
    elif wants_research:
        key = _safe_key("research", used)
        tasks.append(
            PlannedTask(
                key=key,
                name="Ricerca web",
                agent_mode="react",
                objective=text,
                description="Ricerca leggera HTTP + DuckDuckGo creata da Orchestrator.",
                seed_queries=[],
                max_iterations=10,
                model=settings.default_model,
                output_format="md",
                llm_provider="ollama",
                notes="Creato da Orchestrator. Il runner react usa Ollama locale.",
                status_tag="tuning",
            )
        )
    else:
        key = _safe_key("research", used)
        tasks.append(
            PlannedTask(
                key=key,
                name="Analisi iniziale",
                agent_mode="react",
                objective=text,
                description="Task iniziale creato da Orchestrator quando il bisogno e ambiguo.",
                max_iterations=8,
                model=settings.default_model,
                output_format="md",
                llm_provider="ollama",
                notes="Brief ambiguo: usa questo task per raccogliere contesto, poi raffina il workflow.",
                status_tag="tuning",
            )
        )

    if wants_qualifier and tasks and tasks[-1].agent_mode != "responder":
        key = _safe_key("qualifier", used)
        tasks.append(
            PlannedTask(
                key=key,
                name="Qualifica contatti",
                agent_mode="qualifier",
                objective=(
                    "Valuta i profili estratti e tieni solo contatti coerenti con il brief: "
                    + text
                ),
                description="Filtro/scoring LLM dei profili estratti.",
                model=model or settings.default_model,
                llm_provider=provider,
                llm_base_url=llm_base_url if provider == "custom" else None,
                output_format="md",
                notes="Creato da Orchestrator. Ricevera profiles.jsonl dal task upstream.",
                status_tag="tuning",
            )
        )

    if wants_outreach:
        if not wants_qualifier and tasks and tasks[-1].agent_mode not in {"outreach", "responder"}:
            key = _safe_key("qualifier", used)
            tasks.append(
                PlannedTask(
                    key=key,
                    name="Qualifica contatti",
                    agent_mode="qualifier",
                    objective="Filtra lead contattabili e pertinenti prima dell'outreach.",
                    description="Filtro minimo aggiunto per sicurezza prima dell'outreach.",
                    model=model or settings.default_model,
                    llm_provider=provider,
                    llm_base_url=llm_base_url if provider == "custom" else None,
                    output_format="md",
                    notes="Creato da Orchestrator come guardrail prima dell'outreach.",
                    status_tag="tuning",
                )
            )
        key = _safe_key("outreach", used)
        tasks.append(
            PlannedTask(
                key=key,
                name="Outreach controllato",
                agent_mode="outreach",
                objective="Invia messaggi solo ai contatti qualificati, rispettando opt-out e limiti canale.",
                description="Invio messaggi creato da Orchestrator. Richiede canali configurati in Settings.",
                output_format="md",
                message_subject="Una proposta per {display_name}",
                message_template=(
                    "Ciao {display_name},\n\n"
                    "ho visto la tua pagina su {source_url} e penso ci sia spazio per migliorarne "
                    "presentazione e contenuti.\n\n"
                    "Se ti va, posso mandarti 2-3 spunti concreti.\n\n"
                    "A presto"
                ),
                message_channels=["email"],
                notes="Creato da Orchestrator. Rivedi subject/template e consenso prima di lanciare.",
                status_tag="tuning",
            )
        )

    edges = [
        PlannedEdge(
            from_key=tasks[i].key,
            to_key=tasks[i + 1].key,
            pass_artifact=_edge_artifact(tasks[i].agent_mode, tasks[i + 1].agent_mode),
        )
        for i in range(len(tasks) - 1)
    ]

    warnings: list[str] = []
    if any(t.agent_mode in RISKY_AGENT_MODES for t in tasks):
        warnings.append(
            "Il piano contiene messaggistica outbound/auto-reply: serve consenso esplicito prima del lancio."
        )
    if any(t.agent_mode == "react" for t in tasks) and provider != "ollama":
        warnings.append("I task react usano Ollama locale anche se il planner usa un provider remoto.")
    if any(t.agent_mode in {"bulk_extract", "auto_extract", "browser_use"} for t in tasks) and not urls:
        warnings.append("Non ho trovato URL nel brief: compila seed URL prima di lanciare lo scraping.")

    run_after_create = autonomy_level in {"supervised", "autonomous"}
    if any(t.agent_mode in RISKY_AGENT_MODES for t in tasks):
        run_after_create = False

    return OrchestratorPlan(
        title=_make_title(text),
        summary=_make_summary(tasks, edges, autonomy_level),
        autonomy_level=autonomy_level,
        risk_level="high" if any(t.agent_mode in RISKY_AGENT_MODES for t in tasks) else "medium",
        assumptions=_make_assumptions(tasks, urls),
        warnings=warnings,
        tasks=tasks,
        edges=edges,
        run_after_create=run_after_create,
        planner_used="heuristic",
    )


async def build_plan(
    brief: str,
    autonomy_level: AutonomyLevel,
    provider: str = "ollama",
    model: str | None = None,
    llm_base_url: str | None = None,
    llm_api_key: str | None = None,
    use_llm: bool = False,
) -> OrchestratorPlan:
    fallback = build_heuristic_plan(brief, autonomy_level, provider, model, llm_base_url)
    if not use_llm:
        return fallback
    try:
        llm_plan = await _build_llm_plan(
            brief=brief,
            autonomy_level=autonomy_level,
            provider=provider,
            model=model or settings.default_model,
            llm_base_url=llm_base_url,
            llm_api_key=llm_api_key,
            fallback=fallback,
        )
        return llm_plan
    except Exception as e:
        fallback.planner_used = "heuristic_fallback"
        fallback.warnings.append(
            f"Planner LLM non riuscito ({type(e).__name__}: {str(e)[:180]}). Uso piano euristico."
        )
        return fallback


async def _build_llm_plan(
    *,
    brief: str,
    autonomy_level: AutonomyLevel,
    provider: str,
    model: str,
    llm_base_url: str | None,
    llm_api_key: str | None,
    fallback: OrchestratorPlan,
) -> OrchestratorPlan:
    base_url = resolve_base_url(provider, llm_base_url)
    api_key = resolve_api_key(provider, llm_api_key)
    prompt = _planner_prompt(brief, autonomy_level, fallback)
    payload = {
        "model": model,
        "messages": [
            {
                "role": "system",
                "content": (
                    "Sei l'orchestrator di AgentScraper. Devi progettare task e workflow "
                    "usando solo le capacita disponibili. Rispondi sempre in JSON valido."
                ),
            },
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.1,
        "max_tokens": 2200,
        "response_format": {"type": "json_object"},
    }
    headers = {"Authorization": f"Bearer {api_key}"}
    async with httpx.AsyncClient(timeout=120) as client:
        r = await client.post(f"{base_url.rstrip('/')}/chat/completions", json=payload, headers=headers)
        r.raise_for_status()
        data = r.json()
    raw = (data.get("choices", [{}])[0].get("message", {}).get("content") or "").strip()
    parsed = _parse_json_object(raw)
    plan = _normalize_llm_plan(parsed, fallback, provider, model, llm_base_url)
    plan.planner_used = f"llm:{provider}/{model}"
    return plan


def _planner_prompt(brief: str, autonomy_level: str, fallback: OrchestratorPlan) -> str:
    return (
        "Brief utente:\n"
        f"{brief}\n\n"
        f"Livello autonomia richiesto: {autonomy_level}\n\n"
        "Capacita disponibili:\n"
        "- react: ricerca web leggera con DuckDuckGo+HTTP, usa Ollama locale.\n"
        "- browser_use: browser reale, siti dinamici, output profiles.jsonl.\n"
        "- bulk_extract: URL noti/crawler statico, output profiles.jsonl.\n"
        "- auto_extract: lista siti, decide bulk/browser/skip, output profiles.jsonl.\n"
        "- qualifier: legge profiles.jsonl, produce qualified.jsonl e contacts.\n"
        "- outreach: invia email/telegram ai contatti qualified. Rischioso.\n"
        "- responder: auto-reply inbound. Rischioso.\n\n"
        "Formato JSON richiesto:\n"
        "{\n"
        '  "title": "nome breve",\n'
        '  "summary": "cosa fara il workflow",\n'
        '  "risk_level": "low|medium|high",\n'
        '  "assumptions": ["..."],\n'
        '  "warnings": ["..."],\n'
        '  "run_after_create": false,\n'
        '  "tasks": [\n'
        "    {\n"
        '      "key": "extract", "name": "...", "agent_mode": "auto_extract",\n'
        '      "objective": "...", "seed_queries": ["https://..."],\n'
        '      "allowed_domains": ["example.com"], "max_iterations": 200,\n'
        '      "extraction_template": "profile_contacts"\n'
        "    }\n"
        "  ],\n"
        '  "edges": [{"from_key": "extract", "to_key": "qualifier", "pass_artifact": "profiles.jsonl"}]\n'
        "}\n\n"
        "Regole:\n"
        "- Non inventare agent_mode inesistenti.\n"
        "- Per extraction di contatti usa extraction_template=profile_contacts.\n"
        "- Per outreach/responder aggiungi sempre warning di consenso.\n"
        "- Non mettere API key nel JSON.\n"
        "- Se il brief e ambiguo, tieni un piano piccolo.\n\n"
        "Piano euristico di partenza che puoi migliorare:\n"
        + fallback.model_dump_json(exclude={"planner_used"})
    )


def _parse_json_object(raw: str) -> dict[str, Any]:
    try:
        obj = json.loads(raw)
        if isinstance(obj, dict):
            return obj
    except json.JSONDecodeError:
        pass
    m = re.search(r"\{[\s\S]+\}", raw or "")
    if not m:
        raise ValueError("LLM non ha restituito JSON")
    obj = json.loads(m.group(0))
    if not isinstance(obj, dict):
        raise ValueError("JSON planner non e un oggetto")
    return obj


def _normalize_llm_plan(
    raw: dict[str, Any],
    fallback: OrchestratorPlan,
    provider: str,
    model: str,
    llm_base_url: str | None,
) -> OrchestratorPlan:
    tasks: list[PlannedTask] = []
    used: set[str] = set()
    fallback_by_key = {t.key: t for t in fallback.tasks}

    for idx, item in enumerate(raw.get("tasks") or []):
        if not isinstance(item, dict):
            continue
        mode = item.get("agent_mode") or item.get("mode") or "react"
        if mode not in AgentMode.__args__:  # type: ignore[attr-defined]
            continue
        key = _safe_key(str(item.get("key") or f"task_{idx+1}"), used)
        base = fallback_by_key.get(str(item.get("key") or ""))
        urls = [str(x).strip() for x in (item.get("seed_queries") or []) if str(x).strip()]
        domains = [str(x).strip().lower() for x in (item.get("allowed_domains") or []) if str(x).strip()]
        if base:
            data = base.model_dump()
            data.update(item)
            data["key"] = key
            data["agent_mode"] = mode
            data["seed_queries"] = urls or base.seed_queries
            data["allowed_domains"] = domains or base.allowed_domains
        else:
            data = _planned_task(
                key=key,
                name=str(item.get("name") or f"Task {idx + 1}"),
                agent_mode=mode,
                objective=str(item.get("objective") or fallback.summary),
                brief=fallback.summary,
                urls=urls,
                domains=domains,
                provider=provider,
                model=model,
                llm_base_url=llm_base_url,
            ).model_dump()
            data.update(item)
            data["key"] = key
            data["agent_mode"] = mode

        if data.get("extraction_template") and not data.get("extraction_schema"):
            data["extraction_schema"] = get_schema(data["extraction_template"])
        data["model"] = data.get("model") or _default_model_for_mode(mode, provider, model)
        data["llm_provider"] = "ollama" if mode == "react" else (data.get("llm_provider") or provider)
        if data["llm_provider"] != "custom":
            data["llm_base_url"] = None
        tasks.append(PlannedTask(**data))

    if not tasks:
        tasks = fallback.tasks

    valid_keys = {t.key for t in tasks}
    edges: list[PlannedEdge] = []
    for e in raw.get("edges") or []:
        if not isinstance(e, dict):
            continue
        from_key = str(e.get("from_key") or "")
        to_key = str(e.get("to_key") or "")
        if from_key in valid_keys and to_key in valid_keys and from_key != to_key:
            edges.append(
                PlannedEdge(
                    from_key=from_key,
                    to_key=to_key,
                    pass_artifact=e.get("pass_artifact") or None,
                )
            )
    if len(tasks) > 1 and not edges:
        edges = [
            PlannedEdge(
                from_key=tasks[i].key,
                to_key=tasks[i + 1].key,
                pass_artifact=_edge_artifact(tasks[i].agent_mode, tasks[i + 1].agent_mode),
            )
            for i in range(len(tasks) - 1)
        ]

    risk = raw.get("risk_level")
    if risk not in {"low", "medium", "high"}:
        risk = "high" if any(t.agent_mode in RISKY_AGENT_MODES for t in tasks) else fallback.risk_level

    warnings = [str(x) for x in (raw.get("warnings") or []) if str(x).strip()]
    if any(t.agent_mode in RISKY_AGENT_MODES for t in tasks) and not warnings:
        warnings.append("Il piano contiene messaggistica: richiedi consenso prima di lanciare.")

    return OrchestratorPlan(
        title=str(raw.get("title") or fallback.title)[:120],
        summary=str(raw.get("summary") or fallback.summary),
        autonomy_level=fallback.autonomy_level,
        risk_level=risk,
        assumptions=[str(x) for x in (raw.get("assumptions") or fallback.assumptions)],
        warnings=warnings or fallback.warnings,
        tasks=tasks,
        edges=edges,
        run_after_create=bool(raw.get("run_after_create", fallback.run_after_create)),
        planner_used="llm",
    )


def execute_plan(
    plan: OrchestratorPlan,
    *,
    run_now: bool = False,
    confirm_risky: bool = False,
) -> ExecutionResult:
    meta = autonomy_meta(plan.autonomy_level)
    if not meta["can_create"]:
        raise ValueError("Questo livello di autonomia non consente modifiche al progetto.")
    if run_now and not meta["can_run"]:
        raise ValueError("Questo livello di autonomia consente la creazione, ma non il lancio dei job.")
    if run_now and plan.has_risky_modes and not confirm_risky:
        raise ValueError(
            "Il piano contiene outreach/responder: conferma esplicitamente la messaggistica prima del lancio."
        )

    created: dict[str, int] = {}
    for task in plan.tasks:
        data = _task_to_db_payload(task)
        created[task.key] = db.create_task(data)

    workflow_id: int | None = None
    workflow_run_id: int | None = None
    started_jobs: list[int] = []

    if plan.edges:
        workflow_id = db.create_workflow(
            plan.title,
            f"Creato da Orchestrator. Autonomy={plan.autonomy_level}. {plan.summary[:400]}",
        )
        for edge in plan.edges:
            from_id = created.get(edge.from_key)
            to_id = created.get(edge.to_key)
            if not from_id or not to_id:
                continue
            db.create_edge(
                from_task_id=from_id,
                to_task_id=to_id,
                workflow_id=workflow_id,
                pass_artifact=edge.pass_artifact,
                enabled=True,
            )
        if run_now:
            result = jobs.start_workflow(workflow_id)
            workflow_run_id = int(result["workflow_run_id"])
            started_jobs = [int(j) for j in result.get("started_jobs") or []]
    elif created:
        task_id = next(iter(created.values()))
        if run_now:
            started_jobs = [jobs.start_job(task_id)]

    jobs.reload_schedules()

    if workflow_id:
        redirect_url = f"/workflows/{workflow_id}"
        message = f"Creato workflow #{workflow_id} con {len(created)} task."
    else:
        only_id = next(iter(created.values())) if created else 0
        redirect_url = f"/tasks/{only_id}" if only_id else "/"
        message = f"Creato task #{only_id}."
    if started_jobs:
        message += f" Avviati job: {', '.join(str(x) for x in started_jobs)}."

    return ExecutionResult(
        created_task_ids=created,
        workflow_id=workflow_id,
        workflow_run_id=workflow_run_id,
        started_jobs=started_jobs,
        redirect_url=redirect_url,
        message=message,
    )


def _task_to_db_payload(task: PlannedTask) -> dict[str, Any]:
    return {
        "name": task.name,
        "description": task.description,
        "objective": task.objective,
        "seed_queries": task.seed_queries,
        "allowed_domains": task.allowed_domains,
        "blocked_domains": task.blocked_domains,
        "max_iterations": task.max_iterations,
        "model": task.model,
        "output_format": task.output_format,
        "cron": None,
        "agent_mode": task.agent_mode,
        "extraction_template": task.extraction_template,
        "extraction_schema": task.extraction_schema,
        "llm_provider": task.llm_provider,
        "llm_base_url": task.llm_base_url,
        "llm_api_key": None,
        "input_artifact_path": task.input_artifact_path,
        "message_template": task.message_template,
        "message_subject": task.message_subject,
        "message_channels": task.message_channels,
        "responder_system_prompt": task.responder_system_prompt,
        "bulk_concurrency": task.bulk_concurrency,
        "bulk_rate_limit_per_sec": task.bulk_rate_limit_per_sec,
        "bulk_extraction_method": "llm_per_page",
        "bulk_css_selectors": None,
        "crawler_enabled": task.crawler_enabled,
        "crawler_url_pattern": None,
        "crawler_max_depth": task.crawler_max_depth,
        "discovery_llm_provider": None,
        "discovery_llm_model": None,
        "discovery_llm_api_key": None,
        "max_discovery_retries": 3,
        "browser_llm_provider": None,
        "browser_llm_model": None,
        "browser_llm_api_key": None,
        "rating": None,
        "notes": task.notes,
        "status_tag": task.status_tag,
    }


def _make_title(brief: str) -> str:
    cleaned = re.sub(r"\s+", " ", brief).strip()
    if not cleaned:
        return "Piano orchestrator"
    return (cleaned[:70] + ("..." if len(cleaned) > 70 else "")).capitalize()


def _make_summary(tasks: list[PlannedTask], edges: list[PlannedEdge], autonomy_level: str) -> str:
    modes = " -> ".join(t.agent_mode for t in tasks)
    if edges:
        return f"Creo una pipeline {modes} con passaggio artifact automatico. Autonomia: {autonomy_level}."
    return f"Creo un task {tasks[0].agent_mode if tasks else 'react'}. Autonomia: {autonomy_level}."


def _make_assumptions(tasks: list[PlannedTask], urls: list[str]) -> list[str]:
    out = []
    if any(t.agent_mode in {"bulk_extract", "auto_extract", "browser_use"} for t in tasks):
        out.append("Lo scraping parte dagli URL trovati nel brief; puoi modificarli nel task prima del lancio.")
        out.append("Lo schema iniziale usa il template profile_contacts se il brief parla di lead/contatti.")
    if not urls and any(t.agent_mode in {"bulk_extract", "auto_extract", "browser_use"} for t in tasks):
        out.append("Non sono stati rilevati URL: il task andra completato con seed URL.")
    if any(t.agent_mode == "outreach" for t in tasks):
        out.append("I canali email/Telegram devono essere configurati in Settings prima dell'esecuzione.")
    return out
