from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator


OutputFormat = Literal["txt", "md", "both"]
AgentMode = Literal[
    "react", "browser_use", "bulk_extract", "auto_extract", "site_explorer",
    "qualifier", "outreach", "responder",
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

    @field_validator("seed_queries", "allowed_domains", "blocked_domains", mode="before")
    @classmethod
    def split_lines(cls, v):
        if isinstance(v, str):
            return [line.strip() for line in v.splitlines() if line.strip()]
        return v or []

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
