from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator


OutputFormat = Literal["txt", "md", "both"]
AgentMode = Literal[
    "react", "browser_use", "bulk_extract", "auto_extract", "site_explorer",
    "qualifier", "outreach", "outreach_social", "outreach_whatsapp", "responder",
    "recon_social",
]
BulkExtractionMethod = Literal["llm_per_page", "css_selectors"]
MessageChannel = Literal["email", "telegram"]
StatusTag = Literal["tuning", "working", "broken", "deprecated", "reference"]


class TaskIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str | None = None
    objective: str = Field(min_length=1)
    seed_queries: list[str] = Field(default_factory=list)
    allowed_domains: list[str] = Field(default_factory=list)
    blocked_domains: list[str] = Field(default_factory=list)
    max_iterations: int = Field(default=10, ge=1, le=100000)
    model: str = "qwen3.5:latest"
    output_format: OutputFormat = "txt"
    cron: str | None = None
    agent_mode: AgentMode = "react"
    extraction_template: str | None = None
    extraction_schema: str | None = None
    llm_provider: str = "ollama"
    llm_base_url: str | None = None
    llm_api_key: str | None = None
    input_artifact_path: str | None = None
    message_template: str | None = None
    message_subject: str | None = None
    message_channels: list[MessageChannel] = Field(default_factory=list)
    responder_system_prompt: str | None = None
    bulk_concurrency: int = Field(default=5, ge=1, le=50)
    target_cap_per_site: int = Field(default=30, ge=0, le=5000)
    refresh_policy_days: int = Field(default=7, ge=-1, le=365)
    bulk_rate_limit_per_sec: float = Field(default=2.0, ge=0.1, le=100.0)
    bulk_extraction_method: BulkExtractionMethod = "llm_per_page"
    bulk_css_selectors: str | None = None
    crawler_enabled: bool = False
    crawler_url_pattern: str | None = None
    crawler_max_depth: int = Field(default=3, ge=1, le=10)
    discovery_llm_provider: str | None = None
    discovery_llm_model: str | None = None
    discovery_llm_api_key: str | None = None
    max_discovery_retries: int = Field(default=3, ge=0, le=10)
    browser_llm_provider: str | None = None
    browser_llm_model: str | None = None
    browser_llm_api_key: str | None = None
    rating: int | None = Field(default=None, ge=1, le=5)
    notes: str | None = None
    status_tag: StatusTag | None = None
    # Outreach social fields (agent_mode=outreach_social)
    social_platform: str | None = None
    outreach_intent: str | None = None
    message_template_variants: str | None = None
    max_dms_per_run: int = Field(default=30, ge=1, le=200)
    max_dms_per_session: int = Field(default=5, ge=1, le=15)
    headed: int = Field(default=1, ge=0, le=1)
    # Selezione esplicita di target outreach_social: lista di contact.id.
    # Se vuota, il runner usa TUTTI i qualified con social[platform] popolato.
    target_contact_ids: list[int] = Field(default_factory=list)
    # Outreach WhatsApp (agent_mode=outreach_whatsapp)
    whatsapp_engine_preference: Literal["auto", "force_A", "force_B"] = "auto"
    whatsapp_dry_run: int = Field(default=0, ge=0, le=1)
    # Single-select sender:
    # - NULL = pool default (tutti gli account/config attivi, comportamento legacy)
    # - id   = SOLO quel sender; fail-fast se è banned/disabled
    whatsapp_account_id: int | None = None
    whatsapp_api_config_id: int | None = None
    # Recon social (R1 url_driven, follower_scrape; R2/R3 nel backlog)
    recon_mode: Literal["url_driven", "exploration", "follower_scrape"] | None = None
    recon_social_account_id: int | None = None
    recon_hypothesis: str | None = None
    recon_max_targets_per_day: int = Field(default=50, ge=1, le=5000)
    recon_score_threshold: int = Field(default=6, ge=0, le=10)
    # Solo per recon_social: nomi/URL da risolvere contro la friend list
    # (FB) o following list (IG/TikTok) dell'account loggato. Zero ambiguità
    # da omonimi rispetto al `seed_queries` con search globale.
    seed_queries_friends: list[str] = Field(default_factory=list)
    # Filtro per leggere asset esistenti dalla tabella `assets` come input al
    # task (alternativa a `input_artifact_path` e a edges upstream).
    # Schema v1: {"asset_type": "palestra"}. Quando valorizzato, il runner
    # qualifier/outreach prima cerca asset DB matching, poi cade su upstream
    # / artifact path.
    input_asset_filter: dict | None = None
    # asset_type assegnato agli asset PRODOTTI dal task (es. 'ig_profile',
    # 'palestra', 'follower'). Lowercase libero. Se None, il runner sceglie
    # un default per il proprio agent_mode (es. recon_social → 'social_profile').
    output_asset_type: str | None = None
    # Profilo velocità (vedi runner_recon_social per applicazione):
    #   'safe'       — default, prudente (pause 30-180s, sub-pages sempre)
    #   'balanced'   — ~40% più veloce (pause 15-60s, skip /tagged se vuoto)
    #   'aggressive' — ~65% più veloce (pause 10-30s, niente sub-pages)
    speed_profile: Literal["safe", "balanced", "aggressive"] = "safe"
    # Filtri per restringere i contatti DESTINATARI di task outreach*.
    # Si combinano AND fra loro. Se `target_contact_ids` è non-vuoto, la
    # selezione manuale vince (i filtri non vengono applicati).
    outreach_filter_source_task_id: int | None = None
    outreach_filter_source_follower_of: str | None = None
    # Lista di {key, value} per multi-tag AND filter sui contatti destinatari.
    # Es: [{key:"interests_inferred", value:"fitness"}, {key:"location", value:"Catania"}]
    # → outreach contatta SOLO chi ha ENTRAMBI i tag. Vale per outreach,
    # outreach_social, outreach_whatsapp. Si combina in AND con i filtri
    # singoli (source_task_id, source_follower_of). target_contact_ids
    # esplicito continua a vincere.
    outreach_filter_tags: list[dict] = Field(default_factory=list)

    @field_validator("rating", mode="before")
    @classmethod
    def parse_rating(cls, v):
        if v in (None, "", "0", 0):
            return None
        return v

    @field_validator("status_tag", mode="before")
    @classmethod
    def parse_status_tag(cls, v):
        if isinstance(v, str) and not v.strip():
            return None
        return v

    @field_validator("message_channels", mode="before")
    @classmethod
    def parse_channels(cls, v):
        if isinstance(v, str):
            return [c.strip() for c in v.split(",") if c.strip()]
        return v or []

    @field_validator("seed_queries", "seed_queries_friends", "allowed_domains", "blocked_domains", mode="before")
    @classmethod
    def split_lines(cls, v):
        if isinstance(v, str):
            return [line.strip() for line in v.splitlines() if line.strip()]
        return v or []

    @field_validator("target_contact_ids", mode="before")
    @classmethod
    def parse_contact_ids(cls, v):
        if v is None:
            return []
        # Accetta: list[int|str], string CSV ("1,2,3"), oppure JSON string
        if isinstance(v, str):
            s = v.strip()
            if not s:
                return []
            # Prova JSON prima ("[1, 2, 3]"), poi CSV ("1,2,3")
            import json as _json
            try:
                parsed = _json.loads(s)
                if isinstance(parsed, list):
                    v = parsed
                else:
                    v = [s]
            except (ValueError, TypeError):
                v = [p.strip() for p in s.split(",") if p.strip()]
        out: list[int] = []
        if isinstance(v, (list, tuple)):
            for item in v:
                try:
                    out.append(int(item))
                except (TypeError, ValueError):
                    continue
        return out

    @field_validator("cron", mode="before")
    @classmethod
    def empty_to_none(cls, v):
        if isinstance(v, str) and not v.strip():
            return None
        return v


class Task(TaskIn):
    id: int
    created_at: str
    updated_at: str


class Job(BaseModel):
    id: int
    task_id: int
    status: Literal["queued", "running", "paused", "done", "error", "cancelled"]
    started_at: str | None = None
    finished_at: str | None = None
    log: str = ""
    result_path: str | None = None
    error: str | None = None


class WorkflowIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str | None = None


class Workflow(WorkflowIn):
    id: int
    created_at: str
    updated_at: str
