"""Postgres backend (no ORM).

Sostituisce il vecchio backend SQLite (Fase 2 multi-tenant). Tutte le funzioni
mantengono le stesse firme dell'API SQLite legacy per compatibilità coi caller
in route/runner. Le tabelle business (tasks, jobs, assets, ecc.) vivono qui;
`tenants` e `users` vivono in `app.db_cloud` ma usano lo stesso pool/DB.

Convenzioni Postgres-ization:
- `BIGSERIAL PRIMARY KEY` invece di `INTEGER PRIMARY KEY AUTOINCREMENT`
- placeholder `%s` invece di `?`
- `RETURNING id` invece di `cur.lastrowid`
- JSON in `TEXT` (non `JSONB`) per backward-compat: i caller fanno
  `json.loads(row["raw_json"])` come prima.
- Boolean in `INTEGER` (0/1) per backward-compat: i caller fanno `if row["disabled"]`
  come prima.
- Timestamp in `TEXT NOT NULL` con ISO format generato lato Python (`now_iso()`).
- BLOB (Fernet) in `BYTEA`.

`tenant_id` e `created_by_user_id` NON sono ancora nelle tabelle business in
questa Fase 2 step A+B. Verranno aggiunti nello Step C dopo lo swap del driver.
"""
from __future__ import annotations

import contextvars
import json
import logging
import os
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator

from .config import DATA_DIR


log = logging.getLogger(__name__)

_pool = None  # type: ignore[var-annotated]


# ---------------------------------------------------------------------------
# Tenant context (Step D)
# ---------------------------------------------------------------------------
# Le funzioni `db.*` tenant-aware (es. list_tasks, create_task) usano
# `_UNSET` come default del parametro `tenant_id`. Se chi chiama NON
# passa esplicitamente tenant_id, la funzione legge dal ContextVar
# `_current_tenant_id_var` settato dal middleware HTTP (app.main).
#
# Tre stati possibili per `tenant_id`:
#   - `_UNSET`  → leggi dal ContextVar (default per chiamate da route)
#   - `None`    → NO filtro (super-admin, test, runner senza tenant)
#   - `int`     → WHERE tenant_id = %s (isolamento per tenant_user)


class _UnsetSentinel:
    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self) -> str:
        return "<UNSET>"

    def __bool__(self) -> bool:
        return False


_UNSET: Any = _UnsetSentinel()


_current_tenant_id_var: contextvars.ContextVar[int | None] = contextvars.ContextVar(
    "agentscraper_current_tenant_id", default=None
)

_current_user_id_var: contextvars.ContextVar[int | None] = contextvars.ContextVar(
    "agentscraper_current_user_id", default=None
)


def set_current_tenant(tenant_id: int | None) -> contextvars.Token:
    """Imposta il tenant_id del contesto corrente. Usato dal middleware HTTP
    di app.main (auth_middleware) e dai runner di job all'avvio dell'esecuzione."""
    return _current_tenant_id_var.set(tenant_id)


def reset_current_tenant(token: contextvars.Token) -> None:
    _current_tenant_id_var.reset(token)


def set_current_user(user_id: int | None) -> contextvars.Token:
    """Imposta lo user_id del contesto corrente (per `created_by_user_id`)."""
    return _current_user_id_var.set(user_id)


def reset_current_user(token: contextvars.Token) -> None:
    _current_user_id_var.reset(token)


def current_tenant_id() -> int | None:
    return _current_tenant_id_var.get()


def current_user_id() -> int | None:
    return _current_user_id_var.get()


def _resolve_tenant(passed: Any) -> int | None:
    if isinstance(passed, _UnsetSentinel):
        return _current_tenant_id_var.get()
    return passed


def _resolve_user(passed: Any) -> int | None:
    if isinstance(passed, _UnsetSentinel):
        return _current_user_id_var.get()
    return passed


def _resolve_dsn() -> str:
    dsn = os.environ.get("DATABASE_URL", "").strip()
    if not dsn:
        raise RuntimeError(
            "DATABASE_URL non impostata. Multi-tenant Postgres è obbligatorio "
            "(SQLite legacy rimosso in Fase 2). Setta DATABASE_URL in .env o "
            "via /dbconfig."
        )
    return dsn


def _get_pool():
    global _pool
    if _pool is not None:
        return _pool
    from psycopg.rows import dict_row
    from psycopg_pool import ConnectionPool

    _pool = ConnectionPool(
        conninfo=_resolve_dsn(),
        min_size=1,
        max_size=10,
        timeout=10,
        kwargs={"row_factory": dict_row},
        open=True,
    )
    return _pool


def reset_pool() -> None:
    """Chiude e dimentica il pool. Utile in test dopo monkeypatch della DSN."""
    global _pool
    if _pool is not None:
        try:
            _pool.close()
        except Exception:
            pass
        _pool = None


@contextmanager
def connect() -> Iterator[Any]:
    """Yield una connessione psycopg dal pool con autocommit OFF.

    NOTA: con il context manager del pool, l'uscita pulita committa
    automaticamente se non c'è eccezione; le funzioni legacy chiamavano
    `con.commit()` esplicito — non serve più ma non fa male.
    """
    pool = _get_pool()
    with pool.connection() as conn:
        yield conn


def now_iso() -> str:
    """Timestamp UTC in formato ISO (compatibile col legacy SQLite)."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# SCHEMA Postgres
# ---------------------------------------------------------------------------
# Ordine topologico (FK risolti). Tutti i CREATE sono IF NOT EXISTS, init_db()
# è idempotente: applicare lo schema a un DB già popolato non distrugge nulla.

SCHEMA_SQL = """
-- ============================================================
-- Tasks (unità di lavoro autonoma)
-- ============================================================
CREATE TABLE IF NOT EXISTS tasks (
  id BIGSERIAL PRIMARY KEY,
  name TEXT NOT NULL,
  description TEXT,
  objective TEXT NOT NULL,
  seed_queries TEXT,
  allowed_domains TEXT,
  blocked_domains TEXT,
  max_iterations INTEGER NOT NULL DEFAULT 10,
  model TEXT NOT NULL DEFAULT 'qwen3.5:latest',
  output_format TEXT NOT NULL DEFAULT 'txt',
  cron TEXT,
  agent_mode TEXT NOT NULL DEFAULT 'react',
  extraction_template TEXT,
  extraction_schema TEXT,
  llm_provider TEXT NOT NULL DEFAULT 'ollama',
  llm_base_url TEXT,
  llm_api_key TEXT,
  input_artifact_path TEXT,
  message_template TEXT,
  message_subject TEXT,
  message_channels TEXT,
  responder_system_prompt TEXT,
  bulk_concurrency INTEGER NOT NULL DEFAULT 5,
  bulk_rate_limit_per_sec DOUBLE PRECISION NOT NULL DEFAULT 2.0,
  bulk_extraction_method TEXT NOT NULL DEFAULT 'llm_per_page',
  bulk_css_selectors TEXT,
  crawler_enabled INTEGER NOT NULL DEFAULT 0,
  crawler_url_pattern TEXT,
  crawler_max_depth INTEGER NOT NULL DEFAULT 3,
  discovery_llm_provider TEXT,
  discovery_llm_model TEXT,
  discovery_llm_api_key TEXT,
  rating INTEGER,
  notes TEXT,
  status_tag TEXT,
  max_discovery_retries INTEGER NOT NULL DEFAULT 3,
  browser_llm_provider TEXT,
  browser_llm_model TEXT,
  browser_llm_api_key TEXT,
  target_cap_per_site INTEGER NOT NULL DEFAULT 30,
  refresh_policy_days INTEGER NOT NULL DEFAULT 7,
  disabled INTEGER NOT NULL DEFAULT 0,
  social_platform TEXT,
  outreach_intent TEXT,
  message_template_variants TEXT,
  max_dms_per_run INTEGER NOT NULL DEFAULT 30,
  max_dms_per_session INTEGER NOT NULL DEFAULT 5,
  headed INTEGER NOT NULL DEFAULT 1,
  target_contact_ids TEXT,
  whatsapp_engine_preference TEXT NOT NULL DEFAULT 'auto',
  whatsapp_dry_run INTEGER NOT NULL DEFAULT 0,
  whatsapp_account_id BIGINT,
  whatsapp_api_config_id BIGINT,
  recon_mode TEXT,
  recon_social_account_id BIGINT,
  recon_hypothesis TEXT,
  recon_max_targets_per_day INTEGER NOT NULL DEFAULT 50,
  recon_score_threshold INTEGER NOT NULL DEFAULT 6,
  seed_queries_friends TEXT,
  input_asset_filter TEXT,
  output_asset_type TEXT,
  speed_profile TEXT NOT NULL DEFAULT 'safe',
  outreach_filter_source_task_id BIGINT,
  outreach_filter_source_follower_of TEXT,
  outreach_filter_tags TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

-- ============================================================
-- Workflows / runs / edges
-- ============================================================
CREATE TABLE IF NOT EXISTS workflows (
  id BIGSERIAL PRIMARY KEY,
  name TEXT NOT NULL,
  description TEXT,
  disabled INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS workflow_runs (
  id BIGSERIAL PRIMARY KEY,
  workflow_id BIGINT NOT NULL REFERENCES workflows(id) ON DELETE CASCADE,
  status TEXT NOT NULL DEFAULT 'running',
  started_at TEXT NOT NULL,
  finished_at TEXT
);

CREATE TABLE IF NOT EXISTS workflow_edges (
  id BIGSERIAL PRIMARY KEY,
  workflow_id   BIGINT REFERENCES workflows(id) ON DELETE CASCADE,
  from_task_id  BIGINT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
  to_task_id    BIGINT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
  trigger_event TEXT NOT NULL DEFAULT 'on_done',
  pass_artifact TEXT,
  enabled       INTEGER NOT NULL DEFAULT 1,
  created_at    TEXT NOT NULL,
  UNIQUE (workflow_id, from_task_id, to_task_id)
);

-- ============================================================
-- Jobs (esecuzioni di task)
-- ============================================================
CREATE TABLE IF NOT EXISTS jobs (
  id BIGSERIAL PRIMARY KEY,
  task_id BIGINT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
  status TEXT NOT NULL,
  started_at TEXT,
  finished_at TEXT,
  log TEXT NOT NULL DEFAULT '',
  result_path TEXT,
  error TEXT,
  control_signal TEXT,
  triggered_by_job_id BIGINT REFERENCES jobs(id) ON DELETE SET NULL,
  workflow_run_id BIGINT REFERENCES workflow_runs(id) ON DELETE SET NULL
);

-- ============================================================
-- Assets + tags
-- ============================================================
CREATE TABLE IF NOT EXISTS assets (
  id BIGSERIAL PRIMARY KEY,
  asset_type           TEXT NOT NULL,
  source_task_id       BIGINT REFERENCES tasks(id) ON DELETE SET NULL,
  source_job_id        BIGINT REFERENCES jobs(id) ON DELETE SET NULL,
  source_url           TEXT,
  source_url_canonical TEXT,
  source_domain        TEXT,
  title                TEXT,
  raw_json             TEXT NOT NULL,
  status               TEXT NOT NULL DEFAULT 'new',
  qualifier_score      INTEGER,
  notes                TEXT,
  created_at           TEXT NOT NULL,
  updated_at           TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS asset_tags (
  asset_id  BIGINT NOT NULL REFERENCES assets(id) ON DELETE CASCADE,
  tag_key   TEXT NOT NULL,
  tag_value TEXT NOT NULL,
  PRIMARY KEY (asset_id, tag_key, tag_value)
);

-- ============================================================
-- Social accounts (Instagram/TikTok/WhatsApp browser-based)
-- ============================================================
CREATE TABLE IF NOT EXISTS social_accounts (
  id BIGSERIAL PRIMARY KEY,
  uuid TEXT UNIQUE NOT NULL,
  platform TEXT NOT NULL,
  username TEXT NOT NULL,
  encrypted_password BYTEA NOT NULL,
  proxy_label TEXT,
  daily_dm_cap INTEGER NOT NULL DEFAULT 10,
  status TEXT NOT NULL DEFAULT 'active',
  warmup_started_at TEXT,
  warmup_days_target INTEGER DEFAULT 30,
  notes TEXT,
  phone_number TEXT,
  auth_method TEXT NOT NULL DEFAULT 'password',
  session_dir TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE (platform, username)
);

-- ============================================================
-- WhatsApp Cloud API config
-- ============================================================
CREATE TABLE IF NOT EXISTS whatsapp_api_config (
  id BIGSERIAL PRIMARY KEY,
  label TEXT NOT NULL,
  phone_number_id TEXT NOT NULL,
  business_account_id TEXT NOT NULL,
  app_id TEXT,
  encrypted_access_token BYTEA NOT NULL,
  default_template_name TEXT,
  default_template_language TEXT NOT NULL DEFAULT 'it',
  status TEXT NOT NULL DEFAULT 'active',
  daily_msg_cap INTEGER NOT NULL DEFAULT 250,
  notes TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

-- ============================================================
-- Contacts (legacy + asset-linked)
-- ============================================================
CREATE TABLE IF NOT EXISTS contacts (
  id BIGSERIAL PRIMARY KEY,
  source_task_id    BIGINT REFERENCES tasks(id) ON DELETE SET NULL,
  source_job_id     BIGINT REFERENCES jobs(id) ON DELETE SET NULL,
  source_url        TEXT,
  source_domain     TEXT,
  display_name      TEXT,
  email             TEXT,
  telegram_username TEXT,
  telegram_chat_id  TEXT,
  whatsapp          TEXT,
  sitoweb           TEXT,
  social_json       TEXT,
  whatsapp_consent  TEXT NOT NULL DEFAULT 'cold',
  whatsapp_last_inbound_at TEXT,
  asset_id          BIGINT REFERENCES assets(id) ON DELETE CASCADE,
  raw_json          TEXT,
  status            TEXT NOT NULL DEFAULT 'new',
  qualifier_score   INTEGER,
  notes             TEXT,
  created_at        TEXT NOT NULL,
  updated_at        TEXT NOT NULL
);

-- ============================================================
-- Threads + Messages
-- ============================================================
CREATE TABLE IF NOT EXISTS threads (
  id BIGSERIAL PRIMARY KEY,
  contact_id   BIGINT NOT NULL REFERENCES contacts(id) ON DELETE CASCADE,
  channel      TEXT NOT NULL,
  external_id  TEXT,
  subject      TEXT,
  status       TEXT NOT NULL DEFAULT 'open',
  task_id      BIGINT REFERENCES tasks(id) ON DELETE SET NULL,
  last_msg_at  TEXT,
  created_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
  id BIGSERIAL PRIMARY KEY,
  thread_id     BIGINT NOT NULL REFERENCES threads(id) ON DELETE CASCADE,
  direction     TEXT NOT NULL,
  body          TEXT NOT NULL,
  llm_generated INTEGER NOT NULL DEFAULT 0,
  external_id   TEXT,
  status        TEXT NOT NULL DEFAULT 'pending',
  error         TEXT,
  sent_at       TEXT,
  created_at    TEXT NOT NULL
);

-- ============================================================
-- Orchestrator chat
-- ============================================================
CREATE TABLE IF NOT EXISTS orchestrator_messages (
  id BIGSERIAL PRIMARY KEY,
  role TEXT NOT NULL,
  body TEXT NOT NULL,
  metadata_json TEXT,
  created_at TEXT NOT NULL
);

-- ============================================================
-- Channel config singleton
-- ============================================================
CREATE TABLE IF NOT EXISTS channel_config (
  channel     TEXT PRIMARY KEY,
  config_json TEXT NOT NULL,
  enabled     INTEGER NOT NULL DEFAULT 0,
  updated_at  TEXT NOT NULL
);

-- ============================================================
-- Site patterns + playbooks (memoria scraping)
-- ============================================================
CREATE TABLE IF NOT EXISTS site_patterns (
  id BIGSERIAL PRIMARY KEY,
  registrable_domain TEXT NOT NULL,
  pattern            TEXT NOT NULL,
  regex              TEXT NOT NULL,
  asset_type         TEXT,
  status             TEXT NOT NULL DEFAULT 'candidate',
  hits               INTEGER NOT NULL DEFAULT 0,
  successes          INTEGER NOT NULL DEFAULT 0,
  failures           INTEGER NOT NULL DEFAULT 0,
  source_task_id     BIGINT REFERENCES tasks(id) ON DELETE SET NULL,
  source_job_id      BIGINT REFERENCES jobs(id) ON DELETE SET NULL,
  notes              TEXT,
  created_at         TEXT NOT NULL,
  updated_at         TEXT NOT NULL,
  UNIQUE (registrable_domain, pattern)
);

CREATE TABLE IF NOT EXISTS site_playbooks (
  id BIGSERIAL PRIMARY KEY,
  registrable_domain TEXT NOT NULL,
  asset_type         TEXT NOT NULL,
  playbook           TEXT NOT NULL,
  source_runner      TEXT NOT NULL,
  source_job_id      BIGINT REFERENCES jobs(id) ON DELETE SET NULL,
  transferable       INTEGER NOT NULL DEFAULT 1,
  status             TEXT NOT NULL DEFAULT 'active',
  hits               INTEGER NOT NULL DEFAULT 0,
  successes          INTEGER NOT NULL DEFAULT 0,
  failures           INTEGER NOT NULL DEFAULT 0,
  created_at         TEXT NOT NULL,
  updated_at         TEXT NOT NULL,
  UNIQUE (registrable_domain, asset_type)
);

-- ============================================================
-- Social DM log (history outreach messaggi)
-- ============================================================
CREATE TABLE IF NOT EXISTS social_dm_log (
  id BIGSERIAL PRIMARY KEY,
  account_id BIGINT REFERENCES social_accounts(id) ON DELETE CASCADE,
  job_id BIGINT REFERENCES jobs(id) ON DELETE SET NULL,
  target_contact_id BIGINT REFERENCES contacts(id) ON DELETE SET NULL,
  target_platform TEXT NOT NULL,
  target_username TEXT NOT NULL,
  message TEXT NOT NULL,
  sent_at TEXT NOT NULL,
  ok INTEGER NOT NULL,
  reason TEXT,
  health_post TEXT,
  engine TEXT,
  api_config_id BIGINT REFERENCES whatsapp_api_config(id) ON DELETE SET NULL
);

-- ============================================================
-- Recon (esplorazione social)
-- ============================================================
CREATE TABLE IF NOT EXISTS recon_runs (
  id BIGSERIAL PRIMARY KEY,
  task_id BIGINT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
  job_id BIGINT REFERENCES jobs(id) ON DELETE SET NULL,
  social_account_id BIGINT REFERENCES social_accounts(id) ON DELETE SET NULL,
  status TEXT NOT NULL DEFAULT 'running',
  started_at TEXT NOT NULL,
  last_active_at TEXT,
  finished_at TEXT,
  target_count INTEGER NOT NULL DEFAULT 0,
  notes TEXT
);

CREATE TABLE IF NOT EXISTS recon_checkpoints (
  id BIGSERIAL PRIMARY KEY,
  run_id BIGINT NOT NULL REFERENCES recon_runs(id) ON DELETE CASCADE,
  snapshot_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS recon_visited (
  id BIGSERIAL PRIMARY KEY,
  run_id BIGINT NOT NULL REFERENCES recon_runs(id) ON DELETE CASCADE,
  target_url TEXT NOT NULL,
  target_platform TEXT NOT NULL,
  visited_at TEXT NOT NULL,
  classified INTEGER NOT NULL DEFAULT 0,
  score INTEGER,
  reason TEXT,
  UNIQUE (run_id, target_url)
);

-- ============================================================
-- Indici (creati DOPO le tabelle per consistency)
-- ============================================================
CREATE INDEX IF NOT EXISTS idx_asset_tags_lookup ON asset_tags(tag_key, tag_value);
CREATE INDEX IF NOT EXISTS idx_assets_source ON assets(source_task_id);
CREATE INDEX IF NOT EXISTS idx_assets_type_status ON assets(asset_type, status);
CREATE INDEX IF NOT EXISTS idx_assets_url ON assets(source_url);
CREATE INDEX IF NOT EXISTS idx_assets_url_canonical ON assets(source_url_canonical, asset_type);
CREATE INDEX IF NOT EXISTS idx_contacts_asset ON contacts(asset_id);
CREATE INDEX IF NOT EXISTS idx_contacts_email ON contacts(email);
CREATE INDEX IF NOT EXISTS idx_contacts_source ON contacts(source_task_id);
CREATE INDEX IF NOT EXISTS idx_contacts_status ON contacts(status);
CREATE INDEX IF NOT EXISTS idx_contacts_telegram_chat ON contacts(telegram_chat_id);
CREATE INDEX IF NOT EXISTS idx_jobs_task ON jobs(task_id, id DESC);
CREATE INDEX IF NOT EXISTS idx_messages_status ON messages(status, direction);
CREATE INDEX IF NOT EXISTS idx_messages_thread ON messages(thread_id, id);
CREATE INDEX IF NOT EXISTS idx_orchestrator_messages_id ON orchestrator_messages(id);
CREATE INDEX IF NOT EXISTS idx_recon_runs_status ON recon_runs(status, task_id);
CREATE INDEX IF NOT EXISTS idx_recon_visited_run ON recon_visited(run_id, visited_at);
CREATE INDEX IF NOT EXISTS idx_site_patterns_asset_type ON site_patterns(asset_type);
CREATE INDEX IF NOT EXISTS idx_site_patterns_domain_status ON site_patterns(registrable_domain, status);
CREATE INDEX IF NOT EXISTS idx_site_playbooks_domain ON site_playbooks(registrable_domain);
CREATE INDEX IF NOT EXISTS idx_social_accounts_platform_status ON social_accounts(platform, status);
CREATE INDEX IF NOT EXISTS idx_social_dm_log_account ON social_dm_log(account_id, sent_at);
CREATE INDEX IF NOT EXISTS idx_social_dm_log_target ON social_dm_log(target_contact_id);
CREATE INDEX IF NOT EXISTS idx_threads_contact ON threads(contact_id);
CREATE INDEX IF NOT EXISTS idx_threads_external ON threads(channel, external_id);
CREATE INDEX IF NOT EXISTS idx_threads_status ON threads(status);
CREATE INDEX IF NOT EXISTS idx_whatsapp_api_config_status ON whatsapp_api_config(status);
CREATE INDEX IF NOT EXISTS idx_workflow_edges_from ON workflow_edges(from_task_id, enabled);
CREATE INDEX IF NOT EXISTS idx_workflow_edges_workflow ON workflow_edges(workflow_id);
"""


# ---------------------------------------------------------------------------
# Colonne multi-tenant (Step C): aggiunte idempotentemente in init_db()
# ---------------------------------------------------------------------------
# Tabelle che ricevono `tenant_id BIGINT REFERENCES tenants(id) ON DELETE CASCADE`
# (nullable per backfill; Step E migrerà i dati legacy a tenant EDG).
# Le tabelle `site_patterns` e `site_playbooks` restano GLOBALI (knowledge base
# scraping condivisa cross-tenant — vedi SETUP_CLOUD_DB_TENANT.md).
_TENANT_AWARE_TABLES = (
    "tasks", "jobs", "workflows", "workflow_runs", "workflow_edges",
    "assets", "asset_tags", "contacts", "threads", "messages",
    "orchestrator_messages", "social_accounts", "social_dm_log",
    "whatsapp_api_config", "channel_config",
    "recon_runs", "recon_checkpoints", "recon_visited",
)

# Tabelle che ricevono `created_by_user_id BIGINT REFERENCES users(id) ON DELETE SET NULL`
# per audit/per-user permissioning futuro.
_USER_OWNERSHIP_TABLES = (
    "tasks", "jobs", "workflows", "assets", "contacts",
    "social_accounts", "whatsapp_api_config",
)


def _apply_multitenant_columns(conn) -> None:
    """Idempotente: ADD COLUMN IF NOT EXISTS per tenant_id + created_by_user_id +
    indici compositi (tenant_id, created_at DESC) o (tenant_id, status)."""
    # tenant_id su tabelle scoped per tenant
    for tbl in _TENANT_AWARE_TABLES:
        conn.execute(
            f"ALTER TABLE {tbl} ADD COLUMN IF NOT EXISTS tenant_id BIGINT "
            f"REFERENCES tenants(id) ON DELETE CASCADE"
        )

    # created_by_user_id (audit / future per-user permissions)
    for tbl in _USER_OWNERSHIP_TABLES:
        conn.execute(
            f"ALTER TABLE {tbl} ADD COLUMN IF NOT EXISTS created_by_user_id BIGINT "
            f"REFERENCES users(id) ON DELETE SET NULL"
        )

    # Indici compositi (tenant_id, ...): velocizzano le query filtrate per tenant.
    tenant_indices = [
        ("idx_tasks_tenant", "tasks(tenant_id, created_at DESC)"),
        ("idx_jobs_tenant", "jobs(tenant_id, id DESC)"),
        ("idx_workflows_tenant", "workflows(tenant_id, created_at DESC)"),
        ("idx_assets_tenant", "assets(tenant_id, asset_type, status)"),
        ("idx_contacts_tenant", "contacts(tenant_id, status)"),
        ("idx_threads_tenant", "threads(tenant_id, status)"),
        ("idx_messages_tenant", "messages(tenant_id, created_at DESC)"),
        ("idx_orchestrator_msg_tenant", "orchestrator_messages(tenant_id, id)"),
        ("idx_social_accounts_tenant", "social_accounts(tenant_id, status)"),
        ("idx_social_dm_log_tenant", "social_dm_log(tenant_id, sent_at DESC)"),
        ("idx_whatsapp_api_tenant", "whatsapp_api_config(tenant_id, status)"),
        ("idx_recon_runs_tenant", "recon_runs(tenant_id, status)"),
    ]
    for name, definition in tenant_indices:
        conn.execute(f"CREATE INDEX IF NOT EXISTS {name} ON {definition}")


def init_db() -> None:
    """Crea (idempotente) tutte le tabelle business + indici + colonne multi-tenant.

    Le tabelle multi-tenant `tenants` e `users` sono create da `app.db_cloud.init_db()`
    (chiamato dal lifespan di `app.main`) PRIMA di questa funzione, perché le FK
    `tenant_id` e `created_by_user_id` aggiunte qui le referenziano.
    """
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with connect() as conn:
        # 1) tabelle business + indici base
        conn.execute(SCHEMA_SQL)
        # 2) colonne multi-tenant + indici compositi (richiede tenants/users già create)
        _apply_multitenant_columns(conn)
        conn.commit()
    log.info("DB business schema applicato (init_db) + colonne multi-tenant.")


def _dump_list(value: list[str] | None) -> str | None:
    return json.dumps(value) if value else None


def _load_list(value: str | None) -> list[str]:
    if not value:
        return []
    try:
        loaded = json.loads(value)
        return loaded if isinstance(loaded, list) else []
    except json.JSONDecodeError:
        return []


VALID_STATUS_TAGS = {"tuning", "working", "broken", "deprecated", "reference"}


def _coerce_rating(value: Any) -> int | None:
    """Vincola il rating a 1-5 (null se vuoto/0/non-int)."""
    if value in (None, "", "0", 0):
        return None
    try:
        n = int(value)
    except (TypeError, ValueError):
        return None
    return n if 1 <= n <= 5 else None


def _coerce_status_tag(value: Any) -> str | None:
    if not value:
        return None
    s = str(value).strip().lower()
    return s if s in VALID_STATUS_TAGS else None


def _coerce_speed_profile(value: Any) -> str:
    """Accetta 'safe', 'balanced', 'aggressive' (case-insensitive). Default 'safe'."""
    s = (value or "").strip().lower() if isinstance(value, str) else ""
    return s if s in ("safe", "balanced", "aggressive") else "safe"


def _serialize_outreach_filter_tags(value: Any) -> str | None:
    """Accetta list[dict({key, value})] o list[tuple(k,v)] o stringa JSON.
    Ritorna JSON string o None se vuoto/non valido. Pulisce keys vuote."""
    if not value:
        return None
    items: list[dict] = []
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return None
        try:
            parsed = json.loads(s)
            if isinstance(parsed, list):
                value = parsed
            else:
                return None
        except Exception:
            return None
    if isinstance(value, list):
        for it in value:
            if isinstance(it, dict):
                k = (it.get("key") or "").strip().lower()
                v = (it.get("value") or "").strip()
            elif isinstance(it, (tuple, list)) and len(it) >= 2:
                k = str(it[0] or "").strip().lower()
                v = str(it[1] or "").strip()
            else:
                continue
            if k and v:
                items.append({"key": k, "value": v})
    if not items:
        return None
    return json.dumps(items, ensure_ascii=False)


def _serialize_input_asset_filter(value: Any) -> str | None:
    """Accetta dict (es. {'asset_type': 'palestra'}) o stringa JSON.
    Ritorna stringa JSON o None se vuoto/non valido."""
    if not value:
        return None
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return None
        # già JSON: validalo
        try:
            parsed = json.loads(s)
            if isinstance(parsed, dict) and parsed:
                return json.dumps(parsed, ensure_ascii=False)
        except Exception:
            return None
        return None
    if isinstance(value, dict):
        clean = {k: v for k, v in value.items() if v}
        if not clean:
            return None
        return json.dumps(clean, ensure_ascii=False)
    return None


def _row_to_task(row: dict[str, Any]) -> dict[str, Any]:
    d = dict(row)
    d["seed_queries"] = _load_list(d.get("seed_queries"))
    d["seed_queries_friends"] = _load_list(d.get("seed_queries_friends"))
    d["allowed_domains"] = _load_list(d.get("allowed_domains"))
    d["blocked_domains"] = _load_list(d.get("blocked_domains"))
    d["message_channels"] = _load_list(d.get("message_channels"))
    raw_filter = d.get("input_asset_filter")
    if raw_filter:
        try:
            d["input_asset_filter"] = json.loads(raw_filter) if isinstance(raw_filter, str) else raw_filter
        except Exception:
            d["input_asset_filter"] = None
    else:
        d["input_asset_filter"] = None
    # outreach_filter_tags: deserializza JSON → list[{key, value}]
    raw_oft = d.get("outreach_filter_tags")
    if raw_oft:
        try:
            parsed_oft = json.loads(raw_oft) if isinstance(raw_oft, str) else raw_oft
            d["outreach_filter_tags"] = parsed_oft if isinstance(parsed_oft, list) else []
        except Exception:
            d["outreach_filter_tags"] = []
    else:
        d["outreach_filter_tags"] = []
    raw_ids = _load_list(d.get("target_contact_ids"))
    coerced_ids: list[int] = []
    for v in raw_ids:
        try:
            coerced_ids.append(int(v))
        except (TypeError, ValueError):
            continue
    d["target_contact_ids"] = coerced_ids
    return d


# ----- Tasks -----
#
# Convenzione tenant filtering (Step D Fase 2):
# - `tenant_id: Any = _UNSET` come kwarg opzionale.
#   * None → NESSUN filtro (comportamento legacy, usato da super-admin o test).
#   * int → WHERE tenant_id = %s (isolamento tenant_user).
# - `get_*` con tenant_id filtra anche su id → previene IDOR (un utente non può
#   leggere risorse di altri tenant indovinando l'id).
# - `create_*` accetta tenant_id + created_by_user_id come kwargs e li
#   inserisce; un super-admin che crea senza specificare lascia tenant_id NULL
#   (il record sarà invisibile a tutti i tenant_user finché non assegnato).

def list_tasks(tenant_id: Any = _UNSET) -> list[dict[str, Any]]:
    tenant_id = _resolve_tenant(tenant_id)
    with connect() as con:
        if tenant_id is None:
            rows = con.execute("SELECT * FROM tasks ORDER BY id DESC").fetchall()
        else:
            rows = con.execute(
                "SELECT * FROM tasks WHERE tenant_id = %s ORDER BY id DESC",
                (tenant_id,),
            ).fetchall()
    return [_row_to_task(r) for r in rows]


def get_task(task_id: int, tenant_id: Any = _UNSET) -> dict[str, Any] | None:
    tenant_id = _resolve_tenant(tenant_id)
    with connect() as con:
        if tenant_id is None:
            row = con.execute("SELECT * FROM tasks WHERE id = %s", (task_id,)).fetchone()
        else:
            row = con.execute(
                "SELECT * FROM tasks WHERE id = %s AND tenant_id = %s",
                (task_id, tenant_id),
            ).fetchone()
    return _row_to_task(row) if row else None


def set_task_disabled(task_id: int, disabled: bool, tenant_id: Any = _UNSET) -> None:
    """Imposta il flag `disabled` (0/1) per un task. Filtra per tenant se passato."""
    tenant_id = _resolve_tenant(tenant_id)
    with connect() as con:
        if tenant_id is None:
            con.execute(
                "UPDATE tasks SET disabled = %s WHERE id = %s",
                (1 if disabled else 0, task_id),
            )
        else:
            con.execute(
                "UPDATE tasks SET disabled = %s WHERE id = %s AND tenant_id = %s",
                (1 if disabled else 0, task_id, tenant_id),
            )


def set_workflow_disabled(workflow_id: int, disabled: bool, tenant_id: Any = _UNSET) -> None:
    """Imposta il flag `disabled` (0/1) per un workflow. Filtra per tenant se passato."""
    tenant_id = _resolve_tenant(tenant_id)
    with connect() as con:
        if tenant_id is None:
            con.execute(
                "UPDATE workflows SET disabled = %s WHERE id = %s",
                (1 if disabled else 0, workflow_id),
            )
        else:
            con.execute(
                "UPDATE workflows SET disabled = %s WHERE id = %s AND tenant_id = %s",
                (1 if disabled else 0, workflow_id, tenant_id),
            )


def create_task(
    data: dict[str, Any],
    *,
    tenant_id: Any = _UNSET,
    created_by_user_id: Any = _UNSET,
) -> int:
    tenant_id = _resolve_tenant(tenant_id)
    created_by_user_id = _resolve_user(created_by_user_id)
    ts = now_iso()
    with connect() as con:
        cur = con.execute(
            """
            INSERT INTO tasks (name, description, objective, seed_queries, allowed_domains,
                                  blocked_domains, max_iterations, model, output_format, cron,
                                  agent_mode, extraction_template, extraction_schema,
                                  llm_provider, llm_base_url, llm_api_key,
                                  input_artifact_path, message_template, message_subject,
                                  message_channels, responder_system_prompt,
                                  bulk_concurrency, target_cap_per_site, refresh_policy_days,
                                  bulk_rate_limit_per_sec,
                                  bulk_extraction_method, bulk_css_selectors,
                                  crawler_enabled, crawler_url_pattern, crawler_max_depth,
                                  discovery_llm_provider, discovery_llm_model,
                                  discovery_llm_api_key, max_discovery_retries,
                                  browser_llm_provider, browser_llm_model, browser_llm_api_key,
                                  rating, notes, status_tag,
                                  social_platform, outreach_intent, message_template_variants,
                                  max_dms_per_run, max_dms_per_session, headed,
                                  target_contact_ids,
                                  whatsapp_engine_preference, whatsapp_dry_run,
                                  whatsapp_account_id, whatsapp_api_config_id,
                                  recon_mode, recon_social_account_id, recon_hypothesis,
                                  recon_max_targets_per_day, recon_score_threshold,
                                  seed_queries_friends,
                                  input_asset_filter,
                                  output_asset_type,
                                  speed_profile,
                                  outreach_filter_source_task_id,
                                  outreach_filter_source_follower_of,
                                  outreach_filter_tags,
                                  tenant_id, created_by_user_id,
                                  created_at, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id
            """,
            (
                data["name"],
                data.get("description"),
                data["objective"],
                _dump_list(data.get("seed_queries")),
                _dump_list(data.get("allowed_domains")),
                _dump_list(data.get("blocked_domains")),
                int(data.get("max_iterations") or 10),
                data.get("model") or "qwen3.5:latest",
                data.get("output_format") or "txt",
                data.get("cron") or None,
                data.get("agent_mode") or "react",
                data.get("extraction_template") or None,
                data.get("extraction_schema") or None,
                data.get("llm_provider") or "ollama",
                data.get("llm_base_url") or None,
                data.get("llm_api_key") or None,
                data.get("input_artifact_path") or None,
                data.get("message_template") or None,
                data.get("message_subject") or None,
                _dump_list(data.get("message_channels")),
                data.get("responder_system_prompt") or None,
                int(data.get("bulk_concurrency") or 5),
                int(data.get("target_cap_per_site") if data.get("target_cap_per_site") is not None else 30),
                int(data.get("refresh_policy_days") if data.get("refresh_policy_days") is not None else 7),
                float(data.get("bulk_rate_limit_per_sec") or 2.0),
                data.get("bulk_extraction_method") or "llm_per_page",
                data.get("bulk_css_selectors") or None,
                1 if data.get("crawler_enabled") else 0,
                data.get("crawler_url_pattern") or None,
                int(data.get("crawler_max_depth") or 3),
                data.get("discovery_llm_provider") or None,
                data.get("discovery_llm_model") or None,
                data.get("discovery_llm_api_key") or None,
                int(data.get("max_discovery_retries") or 3),
                data.get("browser_llm_provider") or None,
                data.get("browser_llm_model") or None,
                data.get("browser_llm_api_key") or None,
                _coerce_rating(data.get("rating")),
                (data.get("notes") or "").strip() or None,
                _coerce_status_tag(data.get("status_tag")),
                data.get("social_platform") or None,
                data.get("outreach_intent") or None,
                data.get("message_template_variants") or None,
                int(data.get("max_dms_per_run") or 30),
                int(data.get("max_dms_per_session") or 5),
                int(data.get("headed") or 0),
                _dump_list([int(x) for x in (data.get("target_contact_ids") or []) if str(x).strip().lstrip("-").isdigit()]),
                (data.get("whatsapp_engine_preference") or "auto"),
                1 if data.get("whatsapp_dry_run") else 0,
                int(data["whatsapp_account_id"]) if data.get("whatsapp_account_id") else None,
                int(data["whatsapp_api_config_id"]) if data.get("whatsapp_api_config_id") else None,
                (data.get("recon_mode") or None),
                int(data["recon_social_account_id"]) if data.get("recon_social_account_id") else None,
                (data.get("recon_hypothesis") or "").strip() or None,
                int(data.get("recon_max_targets_per_day") or 50),
                int(data.get("recon_score_threshold") or 6),
                _dump_list(data.get("seed_queries_friends")),
                _serialize_input_asset_filter(data.get("input_asset_filter")),
                (data.get("output_asset_type") or "").strip().lower() or None,
                _coerce_speed_profile(data.get("speed_profile")),
                int(data["outreach_filter_source_task_id"]) if data.get("outreach_filter_source_task_id") else None,
                (data.get("outreach_filter_source_follower_of") or "").strip() or None,
                _serialize_outreach_filter_tags(data.get("outreach_filter_tags")),
                tenant_id,
                created_by_user_id,
                ts,
                ts,
            ),
        )
        return int(cur.fetchone()['id'])


def update_task(task_id: int, data: dict[str, Any], tenant_id: Any = _UNSET) -> None:
    tenant_id = _resolve_tenant(tenant_id)
    with connect() as con:
        con.execute(
            """
            UPDATE tasks
            SET name = %s, description = %s, objective = %s, seed_queries = %s,
                allowed_domains = %s, blocked_domains = %s, max_iterations = %s,
                model = %s, output_format = %s, cron = %s, agent_mode = %s,
                extraction_template = %s, extraction_schema = %s,
                llm_provider = %s, llm_base_url = %s, llm_api_key = %s,
                input_artifact_path = %s, message_template = %s, message_subject = %s,
                message_channels = %s, responder_system_prompt = %s,
                bulk_concurrency = %s, target_cap_per_site = %s, refresh_policy_days = %s,
                bulk_rate_limit_per_sec = %s,
                bulk_extraction_method = %s, bulk_css_selectors = %s,
                crawler_enabled = %s, crawler_url_pattern = %s, crawler_max_depth = %s,
                discovery_llm_provider = %s, discovery_llm_model = %s,
                discovery_llm_api_key = %s, max_discovery_retries = %s,
                browser_llm_provider = %s, browser_llm_model = %s, browser_llm_api_key = %s,
                rating = %s, notes = %s, status_tag = %s,
                social_platform = %s, outreach_intent = %s, message_template_variants = %s,
                max_dms_per_run = %s, max_dms_per_session = %s, headed = %s,
                target_contact_ids = %s,
                whatsapp_engine_preference = %s, whatsapp_dry_run = %s,
                whatsapp_account_id = %s, whatsapp_api_config_id = %s,
                recon_mode = %s, recon_social_account_id = %s, recon_hypothesis = %s,
                recon_max_targets_per_day = %s, recon_score_threshold = %s,
                seed_queries_friends = %s,
                input_asset_filter = %s,
                output_asset_type = %s,
                speed_profile = %s,
                outreach_filter_source_task_id = %s,
                outreach_filter_source_follower_of = %s,
                outreach_filter_tags = %s,
                updated_at = %s
            WHERE id = %s
              AND (%s::bigint IS NULL OR tenant_id = %s)
            """,
            (
                data["name"],
                data.get("description"),
                data["objective"],
                _dump_list(data.get("seed_queries")),
                _dump_list(data.get("allowed_domains")),
                _dump_list(data.get("blocked_domains")),
                int(data.get("max_iterations") or 10),
                data.get("model") or "qwen3.5:latest",
                data.get("output_format") or "txt",
                data.get("cron") or None,
                data.get("agent_mode") or "react",
                data.get("extraction_template") or None,
                data.get("extraction_schema") or None,
                data.get("llm_provider") or "ollama",
                data.get("llm_base_url") or None,
                data.get("llm_api_key") or None,
                data.get("input_artifact_path") or None,
                data.get("message_template") or None,
                data.get("message_subject") or None,
                _dump_list(data.get("message_channels")),
                data.get("responder_system_prompt") or None,
                int(data.get("bulk_concurrency") or 5),
                int(data.get("target_cap_per_site") if data.get("target_cap_per_site") is not None else 30),
                int(data.get("refresh_policy_days") if data.get("refresh_policy_days") is not None else 7),
                float(data.get("bulk_rate_limit_per_sec") or 2.0),
                data.get("bulk_extraction_method") or "llm_per_page",
                data.get("bulk_css_selectors") or None,
                1 if data.get("crawler_enabled") else 0,
                data.get("crawler_url_pattern") or None,
                int(data.get("crawler_max_depth") or 3),
                data.get("discovery_llm_provider") or None,
                data.get("discovery_llm_model") or None,
                data.get("discovery_llm_api_key") or None,
                int(data.get("max_discovery_retries") or 3),
                data.get("browser_llm_provider") or None,
                data.get("browser_llm_model") or None,
                data.get("browser_llm_api_key") or None,
                _coerce_rating(data.get("rating")),
                (data.get("notes") or "").strip() or None,
                _coerce_status_tag(data.get("status_tag")),
                data.get("social_platform") or None,
                data.get("outreach_intent") or None,
                data.get("message_template_variants") or None,
                int(data.get("max_dms_per_run") or 30),
                int(data.get("max_dms_per_session") or 5),
                int(data.get("headed") or 0),
                _dump_list([int(x) for x in (data.get("target_contact_ids") or []) if str(x).strip().lstrip("-").isdigit()]),
                (data.get("whatsapp_engine_preference") or "auto"),
                1 if data.get("whatsapp_dry_run") else 0,
                int(data["whatsapp_account_id"]) if data.get("whatsapp_account_id") else None,
                int(data["whatsapp_api_config_id"]) if data.get("whatsapp_api_config_id") else None,
                (data.get("recon_mode") or None),
                int(data["recon_social_account_id"]) if data.get("recon_social_account_id") else None,
                (data.get("recon_hypothesis") or "").strip() or None,
                int(data.get("recon_max_targets_per_day") or 50),
                int(data.get("recon_score_threshold") or 6),
                _dump_list(data.get("seed_queries_friends")),
                _serialize_input_asset_filter(data.get("input_asset_filter")),
                (data.get("output_asset_type") or "").strip().lower() or None,
                _coerce_speed_profile(data.get("speed_profile")),
                int(data["outreach_filter_source_task_id"]) if data.get("outreach_filter_source_task_id") else None,
                (data.get("outreach_filter_source_follower_of") or "").strip() or None,
                _serialize_outreach_filter_tags(data.get("outreach_filter_tags")),
                now_iso(),
                task_id,
                tenant_id,
                tenant_id,
            ),
        )


def delete_task(task_id: int, tenant_id: Any = _UNSET) -> None:
    tenant_id = _resolve_tenant(tenant_id)
    with connect() as con:
        if tenant_id is None:
            con.execute("DELETE FROM tasks WHERE id = %s", (task_id,))
        else:
            con.execute(
                "DELETE FROM tasks WHERE id = %s AND tenant_id = %s",
                (task_id, tenant_id),
            )


# ----- Jobs -----

def create_job(
    task_id: int,
    triggered_by_job_id: int | None = None,
    workflow_run_id: int | None = None,
    *,
    tenant_id: Any = _UNSET,
    created_by_user_id: Any = _UNSET,
) -> int:
    tenant_id = _resolve_tenant(tenant_id)
    created_by_user_id = _resolve_user(created_by_user_id)
    with connect() as con:
        cur = con.execute(
            "INSERT INTO jobs (task_id, status, log, triggered_by_job_id, workflow_run_id, "
            "tenant_id, created_by_user_id) "
            "VALUES (%s, 'queued', '', %s, %s, %s, %s) RETURNING id",
            (task_id, triggered_by_job_id, workflow_run_id, tenant_id, created_by_user_id),
        )
        return int(cur.fetchone()['id'])


def get_job(job_id: int, tenant_id: Any = _UNSET) -> dict[str, Any] | None:
    tenant_id = _resolve_tenant(tenant_id)
    with connect() as con:
        if tenant_id is None:
            row = con.execute("SELECT * FROM jobs WHERE id = %s", (job_id,)).fetchone()
        else:
            row = con.execute(
                "SELECT * FROM jobs WHERE id = %s AND tenant_id = %s",
                (job_id, tenant_id),
            ).fetchone()
    return dict(row) if row else None


def list_jobs(task_id: int, tenant_id: Any = _UNSET) -> list[dict[str, Any]]:
    tenant_id = _resolve_tenant(tenant_id)
    with connect() as con:
        if tenant_id is None:
            rows = con.execute(
                "SELECT * FROM jobs WHERE task_id = %s ORDER BY id DESC LIMIT 100",
                (task_id,),
            ).fetchall()
        else:
            rows = con.execute(
                "SELECT * FROM jobs WHERE task_id = %s AND tenant_id = %s "
                "ORDER BY id DESC LIMIT 100",
                (task_id, tenant_id),
            ).fetchall()
    return [dict(r) for r in rows]


def list_jobs_for_workflow_run(
    workflow_run_id: int, tenant_id: Any = _UNSET
) -> list[dict[str, Any]]:
    tenant_id = _resolve_tenant(tenant_id)
    with connect() as con:
        if tenant_id is None:
            rows = con.execute(
                "SELECT * FROM jobs WHERE workflow_run_id = %s ORDER BY id ASC",
                (workflow_run_id,),
            ).fetchall()
        else:
            rows = con.execute(
                "SELECT * FROM jobs WHERE workflow_run_id = %s AND tenant_id = %s "
                "ORDER BY id ASC",
                (workflow_run_id, tenant_id),
            ).fetchall()
    return [dict(r) for r in rows]


def update_job(job_id: int, **fields: Any) -> None:
    if not fields:
        return
    cols = ", ".join(f"{k} = %s" for k in fields)
    values = list(fields.values()) + [job_id]
    with connect() as con:
        con.execute(f"UPDATE jobs SET {cols} WHERE id = %s", values)


def append_job_log(job_id: int, line: str) -> None:
    stamp = now_iso()
    entry = f"[{stamp}] {line}\n"
    with connect() as con:
        con.execute(
            "UPDATE jobs SET log = COALESCE(log, '') || %s WHERE id = %s",
            (entry, job_id),
        )


def latest_job(task_id: int, tenant_id: Any = _UNSET) -> dict[str, Any] | None:
    tenant_id = _resolve_tenant(tenant_id)
    with connect() as con:
        if tenant_id is None:
            row = con.execute(
                "SELECT * FROM jobs WHERE task_id = %s ORDER BY id DESC LIMIT 1",
                (task_id,),
            ).fetchone()
        else:
            row = con.execute(
                "SELECT * FROM jobs WHERE task_id = %s AND tenant_id = %s "
                "ORDER BY id DESC LIMIT 1",
                (task_id, tenant_id),
            ).fetchone()
    return dict(row) if row else None


def set_control_signal(job_id: int, signal: str | None) -> None:
    with connect() as con:
        con.execute("UPDATE jobs SET control_signal = %s WHERE id = %s", (signal, job_id))


def get_control_signal(job_id: int) -> str | None:
    with connect() as con:
        row = con.execute("SELECT control_signal FROM jobs WHERE id = %s", (job_id,)).fetchone()
    if not row:
        return None
    return row["control_signal"]


# ===========================================================================
# Workflows (entità di prima classe)
# ===========================================================================

def list_workflows(tenant_id: Any = _UNSET) -> list[dict[str, Any]]:
    tenant_id = _resolve_tenant(tenant_id)
    with connect() as con:
        if tenant_id is None:
            rows = con.execute("SELECT * FROM workflows ORDER BY id DESC").fetchall()
        else:
            rows = con.execute(
                "SELECT * FROM workflows WHERE tenant_id = %s ORDER BY id DESC",
                (tenant_id,),
            ).fetchall()
    return [dict(r) for r in rows]


def get_workflow(workflow_id: int, tenant_id: Any = _UNSET) -> dict[str, Any] | None:
    tenant_id = _resolve_tenant(tenant_id)
    with connect() as con:
        if tenant_id is None:
            row = con.execute(
                "SELECT * FROM workflows WHERE id = %s", (workflow_id,)
            ).fetchone()
        else:
            row = con.execute(
                "SELECT * FROM workflows WHERE id = %s AND tenant_id = %s",
                (workflow_id, tenant_id),
            ).fetchone()
    return dict(row) if row else None


def create_workflow(
    name: str,
    description: str | None = None,
    *,
    tenant_id: Any = _UNSET,
    created_by_user_id: Any = _UNSET,
) -> int:
    tenant_id = _resolve_tenant(tenant_id)
    created_by_user_id = _resolve_user(created_by_user_id)
    ts = now_iso()
    with connect() as con:
        cur = con.execute(
            "INSERT INTO workflows (name, description, tenant_id, created_by_user_id, "
            "created_at, updated_at) VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
            (name, description, tenant_id, created_by_user_id, ts, ts),
        )
        return int(cur.fetchone()['id'])


def update_workflow(
    workflow_id: int, name: str, description: str | None, tenant_id: Any = _UNSET
) -> None:
    tenant_id = _resolve_tenant(tenant_id)
    with connect() as con:
        if tenant_id is None:
            con.execute(
                "UPDATE workflows SET name = %s, description = %s, updated_at = %s "
                "WHERE id = %s",
                (name, description, now_iso(), workflow_id),
            )
        else:
            con.execute(
                "UPDATE workflows SET name = %s, description = %s, updated_at = %s "
                "WHERE id = %s AND tenant_id = %s",
                (name, description, now_iso(), workflow_id, tenant_id),
            )


def delete_workflow(workflow_id: int, tenant_id: Any = _UNSET) -> None:
    tenant_id = _resolve_tenant(tenant_id)
    with connect() as con:
        if tenant_id is None:
            con.execute("DELETE FROM workflows WHERE id = %s", (workflow_id,))
        else:
            con.execute(
                "DELETE FROM workflows WHERE id = %s AND tenant_id = %s",
                (workflow_id, tenant_id),
            )


# ----- Workflow runs (executions of a workflow) -----

def create_workflow_run(workflow_id: int, *, tenant_id: Any = _UNSET) -> int:
    tenant_id = _resolve_tenant(tenant_id)
    with connect() as con:
        cur = con.execute(
            "INSERT INTO workflow_runs (workflow_id, status, started_at, tenant_id) "
            "VALUES (%s, 'running', %s, %s) RETURNING id",
            (workflow_id, now_iso(), tenant_id),
        )
        return int(cur.fetchone()['id'])


def update_workflow_run_status(run_id: int, status: str) -> None:
    with connect() as con:
        if status in ("done", "error", "cancelled"):
            con.execute(
                "UPDATE workflow_runs SET status = %s, finished_at = %s WHERE id = %s",
                (status, now_iso(), run_id),
            )
        else:
            con.execute(
                "UPDATE workflow_runs SET status = %s WHERE id = %s",
                (status, run_id),
            )


def list_workflow_runs(
    workflow_id: int, limit: int = 50, tenant_id: Any = _UNSET
) -> list[dict[str, Any]]:
    tenant_id = _resolve_tenant(tenant_id)
    with connect() as con:
        if tenant_id is None:
            rows = con.execute(
                "SELECT * FROM workflow_runs WHERE workflow_id = %s "
                "ORDER BY id DESC LIMIT %s",
                (workflow_id, limit),
            ).fetchall()
        else:
            rows = con.execute(
                "SELECT * FROM workflow_runs WHERE workflow_id = %s AND tenant_id = %s "
                "ORDER BY id DESC LIMIT %s",
                (workflow_id, tenant_id, limit),
            ).fetchall()
    return [dict(r) for r in rows]


# ===========================================================================
# Workflow edges (pipeline DAG, scoped per workflow)
# ===========================================================================

def list_edges(
    workflow_id: int | None = None,
    from_task_id: int | None = None,
    to_task_id: int | None = None,
) -> list[dict[str, Any]]:
    sql = "SELECT * FROM workflow_edges WHERE 1=1"
    args: list[Any] = []
    if workflow_id is not None:
        sql += " AND workflow_id = %s"
        args.append(workflow_id)
    if from_task_id is not None:
        sql += " AND from_task_id = %s"
        args.append(from_task_id)
    if to_task_id is not None:
        sql += " AND to_task_id = %s"
        args.append(to_task_id)
    sql += " ORDER BY id"
    with connect() as con:
        rows = con.execute(sql, args).fetchall()
    return [dict(r) for r in rows]


def list_all_edges() -> list[dict[str, Any]]:
    with connect() as con:
        rows = con.execute("SELECT * FROM workflow_edges ORDER BY id").fetchall()
    return [dict(r) for r in rows]


def create_edge(
    from_task_id: int,
    to_task_id: int,
    workflow_id: int | None = None,
    trigger_event: str = "on_done",
    pass_artifact: str | None = None,
    enabled: bool = True,
) -> int:
    if from_task_id == to_task_id:
        raise ValueError("Self-edge non permesso")
    if _would_create_cycle(workflow_id, from_task_id, to_task_id):
        raise ValueError("Edge crea un ciclo nel DAG di questo workflow")
    with connect() as con:
        cur = con.execute(
            """
            INSERT INTO workflow_edges
            (workflow_id, from_task_id, to_task_id, trigger_event, pass_artifact, enabled, created_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id
            """,
            (
                workflow_id,
                from_task_id,
                to_task_id,
                trigger_event,
                pass_artifact,
                1 if enabled else 0,
                now_iso(),
            ),
        )
        return int(cur.fetchone()['id'])


def delete_edge(edge_id: int) -> None:
    with connect() as con:
        con.execute("DELETE FROM workflow_edges WHERE id = %s", (edge_id,))


def toggle_edge(edge_id: int, enabled: bool) -> None:
    with connect() as con:
        con.execute(
            "UPDATE workflow_edges SET enabled = %s WHERE id = %s",
            (1 if enabled else 0, edge_id),
        )


def _would_create_cycle(workflow_id: int | None, from_task_id: int, to_task_id: int) -> bool:
    """Cycle check scoped sul workflow: simula l'aggiunta dell'edge e verifica
    se da `to_task_id` si può raggiungere `from_task_id` seguendo gli edge dello
    stesso workflow.
    """
    with connect() as con:
        if workflow_id is None:
            rows = con.execute(
                "SELECT from_task_id, to_task_id FROM workflow_edges "
                "WHERE workflow_id IS NULL AND enabled = 1"
            ).fetchall()
        else:
            rows = con.execute(
                "SELECT from_task_id, to_task_id FROM workflow_edges "
                "WHERE workflow_id = %s AND enabled = 1",
                (workflow_id,),
            ).fetchall()
    adj: dict[int, list[int]] = {}
    for r in rows:
        adj.setdefault(r["from_task_id"], []).append(r["to_task_id"])
    adj.setdefault(from_task_id, []).append(to_task_id)
    seen: set[int] = set()
    stack = [to_task_id]
    while stack:
        n = stack.pop()
        if n == from_task_id:
            return True
        if n in seen:
            continue
        seen.add(n)
        stack.extend(adj.get(n, []))
    return False


def find_workflow_roots(workflow_id: int) -> list[int]:
    """Task del workflow che NON hanno incoming edges (cioè sono i punti di partenza)."""
    edges = list_edges(workflow_id=workflow_id)
    if not edges:
        return []
    has_incoming = {e["to_task_id"] for e in edges}
    all_tasks = set()
    for e in edges:
        all_tasks.add(e["from_task_id"])
        all_tasks.add(e["to_task_id"])
    return sorted(all_tasks - has_incoming)


# ===========================================================================
# Contacts
# ===========================================================================

def upsert_contact(data: dict[str, Any]) -> int:
    """Insert o update by (email, telegram_username) per evitare duplicati. Ritorna id.

    Accetta sia 'source_task_id' (nuovo) sia 'source_project_id' (legacy alias).
    Supporta anche canali secondari (whatsapp, sitoweb, social_json) — quando
    presenti senza email/telegram, dedup avviene per `source_url`.
    """
    import json as _json
    ts = now_iso()
    email = (data.get("email") or "").strip().lower() or None
    tg_user = (data.get("telegram_username") or "").strip().lstrip("@").lower() or None
    whatsapp = (data.get("whatsapp") or "").strip() or None
    sitoweb = (data.get("sitoweb") or "").strip() or None
    social = data.get("social")
    if isinstance(social, list) and social:
        social_json = _json.dumps(social, ensure_ascii=False)
    elif isinstance(social, str) and social.strip():
        social_json = social.strip()
    else:
        social_json = None
    source_task = data.get("source_task_id") or data.get("source_project_id")
    source_url = data.get("source_url")
    with connect() as con:
        existing = None
        if email:
            existing = con.execute(
                "SELECT id FROM contacts WHERE LOWER(email) = %s LIMIT 1", (email,)
            ).fetchone()
        if not existing and tg_user:
            existing = con.execute(
                "SELECT id FROM contacts WHERE LOWER(telegram_username) = %s LIMIT 1",
                (tg_user,),
            ).fetchone()
        if not existing and source_url:
            # Fallback dedup per source_url: necessario per profili con solo
            # social/whatsapp/sitoweb (no email/tg) che altrimenti verrebbero
            # ri-inseriti ad ogni run del qualifier.
            existing = con.execute(
                "SELECT id FROM contacts WHERE source_url = %s LIMIT 1", (source_url,)
            ).fetchone()
        asset_id = data.get("asset_id")
        if asset_id is not None:
            try:
                asset_id = int(asset_id)
            except (TypeError, ValueError):
                asset_id = None
        if existing:
            cid = int(existing["id"])
            con.execute(
                """
                UPDATE contacts SET
                  source_task_id    = COALESCE(%s, source_task_id),
                  source_job_id     = COALESCE(%s, source_job_id),
                  source_url        = COALESCE(%s, source_url),
                  source_domain     = COALESCE(%s, source_domain),
                  display_name      = COALESCE(%s, display_name),
                  email             = COALESCE(%s, email),
                  telegram_username = COALESCE(%s, telegram_username),
                  whatsapp          = COALESCE(%s, whatsapp),
                  sitoweb           = COALESCE(%s, sitoweb),
                  social_json       = COALESCE(%s, social_json),
                  raw_json          = COALESCE(%s, raw_json),
                  asset_id          = COALESCE(%s, asset_id),
                  updated_at        = %s
                WHERE id = %s
                """,
                (
                    source_task,
                    data.get("source_job_id"),
                    data.get("source_url"),
                    data.get("source_domain"),
                    data.get("display_name"),
                    email,
                    tg_user,
                    whatsapp,
                    sitoweb,
                    social_json,
                    data.get("raw_json"),
                    asset_id,
                    ts,
                    cid,
                ),
            )
            return cid
        cur = con.execute(
            """
            INSERT INTO contacts
            (source_task_id, source_job_id, source_url, source_domain,
             display_name, email, telegram_username, telegram_chat_id,
             whatsapp, sitoweb, social_json,
             raw_json, status, qualifier_score, notes, asset_id, created_at, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id
            """,
            (
                source_task,
                data.get("source_job_id"),
                data.get("source_url"),
                data.get("source_domain"),
                data.get("display_name"),
                email,
                tg_user,
                data.get("telegram_chat_id"),
                whatsapp,
                sitoweb,
                social_json,
                data.get("raw_json"),
                data.get("status") or "new",
                data.get("qualifier_score"),
                data.get("notes"),
                asset_id,
                ts,
                ts,
            ),
        )
        return int(cur.fetchone()['id'])


def get_contact(contact_id: int) -> dict[str, Any] | None:
    with connect() as con:
        row = con.execute("SELECT * FROM contacts WHERE id = %s", (contact_id,)).fetchone()
    return dict(row) if row else None


def _build_contacts_filters(
    status: str | None,
    source_task_id: int | None,
    source_domain: str | None,
    search: str | None,
    channel: str | None,
    score_min: int | None,
) -> tuple[str, list[Any]]:
    """Costruisce la clausola WHERE condivisa da list_contacts + count_contacts.

    `channel`: filtra contacts con almeno un canale del tipo specificato.
       - "email": email NOT NULL/empty
       - "telegram": telegram_username o telegram_chat_id
       - "whatsapp": whatsapp NOT NULL/empty
       - "sitoweb": sitoweb NOT NULL/empty
       - "social": social_json valorizzato (lista non vuota)
       - "instagram"|"tiktok"|"facebook": social_json contiene quella platform
       - "any": almeno UN canale di contatto (esclude contatti "vuoti")
    `search`: LIKE %q% su display_name, email, telegram_username, whatsapp,
       sitoweb, source_url, source_domain, notes.
    """
    sql = " WHERE 1=1"
    args: list[Any] = []
    if status:
        sql += " AND status = %s"
        args.append(status)
    if source_task_id is not None:
        sql += " AND source_task_id = %s"
        args.append(source_task_id)
    if source_domain:
        sql += " AND source_domain = %s"
        args.append(source_domain)
    if search:
        like = f"%{search.strip()}%"
        sql += (
            " AND ("
            "  LOWER(COALESCE(display_name,'')) LIKE LOWER(%s) OR"
            "  LOWER(COALESCE(email,'')) LIKE LOWER(%s) OR"
            "  LOWER(COALESCE(telegram_username,'')) LIKE LOWER(%s) OR"
            "  LOWER(COALESCE(whatsapp,'')) LIKE LOWER(%s) OR"
            "  LOWER(COALESCE(sitoweb,'')) LIKE LOWER(%s) OR"
            "  LOWER(COALESCE(source_url,'')) LIKE LOWER(%s) OR"
            "  LOWER(COALESCE(source_domain,'')) LIKE LOWER(%s) OR"
            "  LOWER(COALESCE(notes,'')) LIKE LOWER(%s) OR"
            "  LOWER(COALESCE(social_json,'')) LIKE LOWER(%s)"
            ")"
        )
        args.extend([like] * 9)
    if channel:
        ch = channel.strip().lower()
        if ch == "email":
            sql += " AND email IS NOT NULL AND email != ''"
        elif ch == "telegram":
            sql += " AND ((telegram_username IS NOT NULL AND telegram_username != '') OR telegram_chat_id IS NOT NULL)"
        elif ch == "whatsapp":
            sql += " AND whatsapp IS NOT NULL AND whatsapp != ''"
        elif ch == "sitoweb":
            sql += " AND sitoweb IS NOT NULL AND sitoweb != ''"
        elif ch == "social":
            sql += " AND social_json IS NOT NULL AND social_json != '' AND social_json != '[]'"
        elif ch in ("instagram", "tiktok", "facebook"):
            # JSON search semplice: cerca '"platform": "<name>"' nella stringa
            sql += " AND social_json LIKE %s"
            args.append(f'%"platform": "{ch}"%')
        elif ch == "any":
            sql += (
                " AND ("
                "  (email IS NOT NULL AND email != '') OR"
                "  (telegram_username IS NOT NULL AND telegram_username != '') OR"
                "  telegram_chat_id IS NOT NULL OR"
                "  (whatsapp IS NOT NULL AND whatsapp != '') OR"
                "  (sitoweb IS NOT NULL AND sitoweb != '') OR"
                "  (social_json IS NOT NULL AND social_json != '' AND social_json != '[]')"
                ")"
            )
    if score_min is not None:
        sql += " AND qualifier_score >= %s"
        args.append(int(score_min))
    return sql, args


def _add_source_follower_of_filter(where_sql: str, args: list, source_follower_of: str | None) -> tuple[str, list]:
    """Aggiunge una clausola EXISTS per filtrare contacts il cui asset linkato
    ha un asset_tag (source_follower_of, value). Usato per IG follower_scrape
    (es. 'mostrami i contatti raccolti come follower di @ekipe_club')."""
    if not source_follower_of:
        return where_sql, args
    where_sql += (
        " AND EXISTS (SELECT 1 FROM asset_tags t "
        " WHERE t.asset_id = contacts.asset_id "
        "   AND t.tag_key = 'source_follower_of' "
        "   AND t.tag_value = %s)"
    )
    args.append(source_follower_of.strip())
    return where_sql, args


def _add_contact_tag_filters_clause(
    where_sql: str, args: list,
    tag_filters: list[tuple[str, str]] | list[dict] | None,
) -> tuple[str, list]:
    """Per ogni (key, value) aggiunge una clausola EXISTS sull'asset linkato.
    AND fra le clausole. Es. interests_inferred=fitness AND location=Catania.

    Accetta sia list di tuple (k,v) sia list di dict {key, value}.
    """
    if not tag_filters:
        return where_sql, args
    for tf in tag_filters:
        if isinstance(tf, dict):
            tk = (tf.get("key") or "").strip().lower()
            tv = (tf.get("value") or "").strip()
        elif isinstance(tf, (tuple, list)) and len(tf) >= 2:
            tk = str(tf[0] or "").strip().lower()
            tv = str(tf[1] or "").strip()
        else:
            continue
        if not tk or not tv:
            continue
        where_sql += (
            " AND EXISTS (SELECT 1 FROM asset_tags t "
            " WHERE t.asset_id = contacts.asset_id "
            "   AND t.tag_key = %s AND t.tag_value = %s)"
        )
        args.extend([tk, tv])
    return where_sql, args


def list_distinct_tag_keys_for_contacts() -> list[dict[str, Any]]:
    """Tag keys disponibili sui contatti (via asset_tags join), con count.
    Esclude 'source_follower_of' (gestito dal suo filtro dedicato)."""
    with connect() as con:
        rows = con.execute(
            "SELECT t.tag_key AS k, COUNT(DISTINCT c.id) AS n "
            "FROM asset_tags t JOIN contacts c ON c.asset_id = t.asset_id "
            "WHERE t.tag_key != 'source_follower_of' "
            "GROUP BY t.tag_key ORDER BY n DESC, t.tag_key"
        ).fetchall()
    return [{"key": r["k"], "count": int(r["n"])} for r in rows]


def list_distinct_tag_values_for_contacts(tag_key: str, limit: int = 100) -> list[dict[str, Any]]:
    """Valori distinct per una tag_key, con count di contatti che la hanno.
    Usato dal dropdown HTMX quando l'utente cambia la tag_key in UI."""
    tk = (tag_key or "").strip().lower()
    if not tk:
        return []
    with connect() as con:
        rows = con.execute(
            "SELECT t.tag_value AS v, COUNT(DISTINCT c.id) AS n "
            "FROM asset_tags t JOIN contacts c ON c.asset_id = t.asset_id "
            "WHERE t.tag_key = %s "
            "GROUP BY t.tag_value ORDER BY n DESC, t.tag_value LIMIT %s",
            (tk, int(limit)),
        ).fetchall()
    return [{"value": r["v"], "count": int(r["n"])} for r in rows]


def count_contacts(
    status: str | None = None,
    source_task_id: int | None = None,
    source_domain: str | None = None,
    search: str | None = None,
    channel: str | None = None,
    score_min: int | None = None,
    source_follower_of: str | None = None,
    contact_tag_filters: list | None = None,
) -> int:
    """Conta i contatti che matchano i filtri di list_contacts. Per paginazione UI."""
    where_sql, args = _build_contacts_filters(
        status, source_task_id, source_domain, search, channel, score_min
    )
    where_sql, args = _add_source_follower_of_filter(where_sql, args, source_follower_of)
    where_sql, args = _add_contact_tag_filters_clause(where_sql, args, contact_tag_filters)
    sql = "SELECT COUNT(*) FROM contacts" + where_sql
    with connect() as con:
        return int(con.execute(sql, args).fetchone()[0])


def list_contacts(
    status: str | None = None,
    source_task_id: int | None = None,
    source_domain: str | None = None,
    search: str | None = None,
    channel: str | None = None,
    score_min: int | None = None,
    limit: int = 500,
    offset: int = 0,
    source_follower_of: str | None = None,
    contact_tag_filters: list | None = None,
) -> list[dict[str, Any]]:
    where_sql, args = _build_contacts_filters(
        status, source_task_id, source_domain, search, channel, score_min
    )
    where_sql, args = _add_source_follower_of_filter(where_sql, args, source_follower_of)
    where_sql, args = _add_contact_tag_filters_clause(where_sql, args, contact_tag_filters)
    sql = "SELECT * FROM contacts" + where_sql + " ORDER BY id DESC LIMIT %s OFFSET %s"
    args.extend([limit, max(0, int(offset))])
    with connect() as con:
        rows = con.execute(sql, args).fetchall()
    return [dict(r) for r in rows]


def list_distinct_source_follower_of() -> list[dict[str, Any]]:
    """Ritorna i valori distinct di asset_tags.tag_value WHERE tag_key='source_follower_of',
    con count di contacts che li hanno (via asset linkato). Per popolare il
    filtro dropdown in /inbox/contacts."""
    with connect() as con:
        rows = con.execute(
            "SELECT t.tag_value AS v, COUNT(DISTINCT c.id) AS n "
            "FROM asset_tags t JOIN contacts c ON c.asset_id = t.asset_id "
            "WHERE t.tag_key = 'source_follower_of' "
            "GROUP BY t.tag_value ORDER BY n DESC, t.tag_value"
        ).fetchall()
    return [{"value": r["v"], "count": int(r["n"])} for r in rows]


def list_distinct_contact_source_tasks() -> list[dict[str, Any]]:
    """Ritorna i task che hanno generato contacts (distinct source_task_id),
    con count. Joina con tasks per arricchire col nome."""
    with connect() as con:
        rows = con.execute(
            "SELECT c.source_task_id AS tid, t.name AS tname, t.agent_mode AS amode, "
            "       COUNT(*) AS n "
            "FROM contacts c LEFT JOIN tasks t ON t.id = c.source_task_id "
            "WHERE c.source_task_id IS NOT NULL "
            "GROUP BY c.source_task_id ORDER BY n DESC"
        ).fetchall()
    return [
        {
            "task_id": r["tid"],
            "name": r["tname"] or f"task #{r['tid']}",
            "agent_mode": r["amode"] or "?",
            "count": int(r["n"]),
        }
        for r in rows
    ]


def list_contacts_with_social_platform(
    platform: str, limit: int = 500
) -> list[dict[str, Any]]:
    """Ritorna contacts (qualsiasi status, esclusi opt-out/banned) il cui
    `social_json` contiene almeno un entry per `platform`.

    Filtro fatto in Python perché social_json è opaco a SQL. Pensato per
    popolare la UI di selezione target outreach_social: l'utente sceglie
    esplicitamente quali contattare fra quelli disponibili per la piattaforma.
    """
    plat = (platform or "").strip().lower()
    if plat not in ("instagram", "tiktok", "facebook"):
        return []
    sql = (
        "SELECT * FROM contacts "
        "WHERE social_json IS NOT NULL AND social_json != '' "
        "AND status NOT IN ('optedout','banned') "
        "ORDER BY id DESC LIMIT %s"
    )
    with connect() as con:
        rows = con.execute(sql, (max(1, int(limit)),)).fetchall()
    out: list[dict[str, Any]] = []
    for r in rows:
        d = dict(r)
        soc_raw = d.get("social_json")
        try:
            socials = json.loads(soc_raw) if soc_raw else []
        except (json.JSONDecodeError, TypeError):
            continue
        platform_url: str | None = None
        for s in (socials or []):
            if not isinstance(s, dict):
                continue
            if (s.get("platform") or "").lower() != plat:
                continue
            u = (s.get("url") or "").strip()
            if u:
                platform_url = u
                break
        if platform_url:
            d["_platform_url"] = platform_url
            out.append(d)
    return out


def list_contacts_with_whatsapp(limit: int = 500) -> list[dict[str, Any]]:
    """Ritorna contacts con campo `whatsapp` popolato (qualsiasi status,
    esclusi optedout). Usato dalla UI del task `outreach_whatsapp` per il
    selettore esplicito di target.
    """
    sql = (
        "SELECT * FROM contacts "
        "WHERE whatsapp IS NOT NULL AND whatsapp != '' "
        "AND (whatsapp_consent IS NULL OR whatsapp_consent != 'optedout') "
        "AND status NOT IN ('optedout','banned') "
        "ORDER BY id DESC LIMIT %s"
    )
    with connect() as con:
        rows = con.execute(sql, (max(1, int(limit)),)).fetchall()
    return [dict(r) for r in rows]


def get_contacts_by_ids(ids: list[int]) -> list[dict[str, Any]]:
    """Recupera contatti per lista di ID. Niente filtri su status."""
    clean = [int(i) for i in ids if str(i).strip().lstrip("-").isdigit()]
    if not clean:
        return []
    placeholders = ",".join("%s" for _ in clean)
    sql = f"SELECT * FROM contacts WHERE id IN ({placeholders})"
    with connect() as con:
        rows = con.execute(sql, clean).fetchall()
    return [dict(r) for r in rows]


def list_contact_source_domains(limit: int = 100) -> list[tuple[str, int]]:
    """Lista dei domini di provenienza piu' frequenti (per dropdown filtro UI)."""
    sql = (
        "SELECT source_domain, COUNT(*) AS n FROM contacts "
        "WHERE source_domain IS NOT NULL AND source_domain != '' "
        "GROUP BY source_domain ORDER BY n DESC LIMIT %s"
    )
    with connect() as con:
        rows = con.execute(sql, (limit,)).fetchall()
    return [(r["source_domain"], int(r["n"])) for r in rows]


def update_contact(contact_id: int, fields: dict[str, Any]) -> None:
    """Update generico di un contatto.

    Accetta SOLO le colonne whitelisted (no SQL injection via key utente). Le
    chiavi non riconosciute vengono ignorate. `updated_at` viene aggiornato
    sempre. Per cambi specifici di status/qualifier_score/whatsapp_consent ci
    sono helper dedicati che è meglio preferire.
    """
    if not fields:
        return
    ALLOWED = {
        "display_name", "email", "telegram_username", "telegram_chat_id",
        "whatsapp", "sitoweb", "social_json", "source_url", "source_domain",
        "status", "qualifier_score", "notes", "raw_json",
        "whatsapp_consent", "whatsapp_last_inbound_at",
    }
    sets: list[str] = []
    vals: list[Any] = []
    for k, v in fields.items():
        if k not in ALLOWED:
            continue
        sets.append(f"{k} = %s")
        vals.append(v)
    if not sets:
        return
    sets.append("updated_at = %s")
    vals.append(now_iso())
    vals.append(contact_id)
    sql = f"UPDATE contacts SET {', '.join(sets)} WHERE id = %s"
    with connect() as con:
        con.execute(sql, vals)


def update_contact_status(contact_id: int, status: str, notes: str | None = None) -> None:
    with connect() as con:
        if notes is not None:
            con.execute(
                "UPDATE contacts SET status = %s, notes = %s, updated_at = %s WHERE id = %s",
                (status, notes, now_iso(), contact_id),
            )
        else:
            con.execute(
                "UPDATE contacts SET status = %s, updated_at = %s WHERE id = %s",
                (status, now_iso(), contact_id),
            )


def update_contact_qualifier(contact_id: int, score: int, status: str) -> None:
    with connect() as con:
        con.execute(
            "UPDATE contacts SET qualifier_score = %s, status = %s, updated_at = %s WHERE id = %s",
            (score, status, now_iso(), contact_id),
        )


def find_contact_by_email(email: str) -> dict[str, Any] | None:
    with connect() as con:
        row = con.execute(
            "SELECT * FROM contacts WHERE LOWER(email) = %s LIMIT 1",
            (email.strip().lower(),),
        ).fetchone()
    return dict(row) if row else None


def find_contact_by_telegram_chat(chat_id: str) -> dict[str, Any] | None:
    with connect() as con:
        row = con.execute(
            "SELECT * FROM contacts WHERE telegram_chat_id = %s LIMIT 1",
            (str(chat_id),),
        ).fetchone()
    return dict(row) if row else None


def set_contact_telegram_chat(contact_id: int, chat_id: str) -> None:
    with connect() as con:
        con.execute(
            "UPDATE contacts SET telegram_chat_id = %s, updated_at = %s WHERE id = %s",
            (str(chat_id), now_iso(), contact_id),
        )


def delete_contact(contact_id: int) -> int:
    """Cancella un contatto. Threads e messages cascade-deletono per FK ON DELETE CASCADE."""
    with connect() as con:
        cur = con.execute("DELETE FROM contacts WHERE id = %s", (contact_id,))
        return cur.rowcount


def delete_contacts_bulk(contact_ids: list[int]) -> int:
    ids = [int(i) for i in contact_ids if i]
    if not ids:
        return 0
    placeholders = ",".join("%s" for _ in ids)
    with connect() as con:
        cur = con.execute(f"DELETE FROM contacts WHERE id IN ({placeholders})", ids)
        return cur.rowcount


# ===========================================================================
# Assets — modello generale per profili/annunci/prodotti/articoli/eventi/...
# ===========================================================================

def has_recent_asset(
    source_url: str,
    asset_type: str,
    max_age_days: int = 7,
) -> bool:
    """True se esiste un asset con questo source_url (canonical) + asset_type
    aggiornato negli ultimi `max_age_days` giorni. Usato dai runner per skip-pare
    re-extract di asset gia' freschi in DB.

    Semantica di max_age_days:
      0  → "mai re-extract": skip se l'asset esiste in DB (qualunque eta')
      -1 → "sempre re-extract": ritorna sempre False (no skip)
      N>0 → skip se updated_at >= now - N giorni
    """
    if max_age_days < 0:
        return False
    if not source_url or not asset_type:
        return False
    from datetime import datetime, timedelta, timezone
    from .agent.url_canonical import canonical_url as _canon
    canonical = _canon(source_url)
    with connect() as con:
        row = con.execute(
            """SELECT updated_at FROM assets
               WHERE asset_type = %s
                     AND (source_url_canonical = %s OR source_url = %s)
               ORDER BY updated_at DESC
               LIMIT 1""",
            (asset_type, canonical, source_url),
        ).fetchone()
    if not row:
        return False
    if max_age_days == 0:
        return True
    try:
        ts_str = row["updated_at"]
        ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        return (now - ts) <= timedelta(days=max_age_days)
    except Exception:
        # parse fail: meglio ri-extract (cosa giusta)
        return False


def upsert_asset(
    data: dict[str, Any],
    tags: dict[str, list[str]] | None = None,
) -> int:
    """Inserisce o aggiorna un asset.
    Chiave di dedup: source_url_canonical (cross-lingua/paginazione) + asset_type.
    Fallback: source_url letterale se canonical non calcolabile.
    Ritorna asset_id. I tag sostituiscono quelli precedenti.
    """
    from .agent.url_canonical import canonical_url as _canon
    asset_type = (data.get("asset_type") or "").strip() or "generic"
    source_url = (data.get("source_url") or "").strip() or None
    source_url_canonical = _canon(source_url) if source_url else None
    raw_json = data.get("raw_json") or "{}"
    if not isinstance(raw_json, str):
        raw_json = json.dumps(raw_json, ensure_ascii=False)
    title = data.get("title")
    source_domain = data.get("source_domain")
    source_task_id = data.get("source_task_id")
    source_job_id = data.get("source_job_id")
    notes = data.get("notes")
    ts = now_iso()

    with connect() as con:
        existing = None
        # Prima cerca per canonical (dedup cross-lingua), poi fallback su source_url
        if source_url_canonical:
            row = con.execute(
                "SELECT id FROM assets WHERE source_url_canonical = %s AND asset_type = %s LIMIT 1",
                (source_url_canonical, asset_type),
            ).fetchone()
            existing = int(row["id"]) if row else None
        if existing is None and source_url:
            row = con.execute(
                "SELECT id FROM assets WHERE source_url = %s AND asset_type = %s LIMIT 1",
                (source_url, asset_type),
            ).fetchone()
            existing = int(row["id"]) if row else None

        if existing:
            con.execute(
                """
                UPDATE assets SET
                  source_task_id       = COALESCE(%s, source_task_id),
                  source_job_id        = COALESCE(%s, source_job_id),
                  source_url_canonical = COALESCE(%s, source_url_canonical),
                  source_domain        = COALESCE(%s, source_domain),
                  title                = COALESCE(%s, title),
                  raw_json             = %s,
                  notes                = COALESCE(%s, notes),
                  updated_at           = %s
                WHERE id = %s
                """,
                (
                    source_task_id,
                    source_job_id,
                    source_url_canonical,
                    source_domain,
                    title,
                    raw_json,
                    notes,
                    ts,
                    existing,
                ),
            )
            asset_id = existing
        else:
            cur = con.execute(
                """
                INSERT INTO assets (
                  asset_type, source_task_id, source_job_id, source_url, source_url_canonical,
                  source_domain, title, raw_json, status, qualifier_score, notes,
                  created_at, updated_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'new', NULL, %s, %s, %s) RETURNING id
                """,
                (
                    asset_type,
                    source_task_id,
                    source_job_id,
                    source_url,
                    source_url_canonical,
                    source_domain,
                    title,
                    raw_json,
                    notes,
                    ts,
                    ts,
                ),
            )
            asset_id = int(cur.fetchone()['id'])

        # Tag: sostituiamo l'intero set per asset (semantica idempotente)
        if tags is not None:
            con.execute("DELETE FROM asset_tags WHERE asset_id = %s", (asset_id,))
            seen: set[tuple[str, str]] = set()
            for tag_key, tag_values in (tags or {}).items():
                if not tag_key:
                    continue
                tk = str(tag_key).strip().lower()
                if not tk:
                    continue
                for v in tag_values or []:
                    if v is None:
                        continue
                    tv = str(v).strip()
                    if not tv:
                        continue
                    pair = (tk, tv)
                    if pair in seen:
                        continue
                    seen.add(pair)
                    con.execute(
                        "INSERT OR IGNORE INTO asset_tags (asset_id, tag_key, tag_value) VALUES (%s, %s, %s)",
                        (asset_id, tk, tv),
                    )

    return asset_id


def get_asset(asset_id: int) -> dict[str, Any] | None:
    with connect() as con:
        row = con.execute("SELECT * FROM assets WHERE id = %s", (asset_id,)).fetchone()
        if not row:
            return None
        asset = dict(row)
        tag_rows = con.execute(
            "SELECT tag_key, tag_value FROM asset_tags WHERE asset_id = %s ORDER BY tag_key, tag_value",
            (asset_id,),
        ).fetchall()
    tags: dict[str, list[str]] = {}
    for t in tag_rows:
        tags.setdefault(t["tag_key"], []).append(t["tag_value"])
    asset["tags"] = tags
    return asset


def count_assets(
    asset_type: str | None = None,
    status: str | None = None,
    source_task_id: int | None = None,
    tag_filters: list[tuple[str, str]] | None = None,
) -> int:
    """Conta gli asset matchando gli stessi filtri di `list_assets`. Usato dalla
    paginazione UI per calcolare il numero di pagine."""
    sql = "SELECT COUNT(DISTINCT a.id) FROM assets a"
    args: list[Any] = []
    if tag_filters:
        for i, (k, v) in enumerate(tag_filters):
            alias = f"t{i}"
            sql += (
                f" JOIN asset_tags {alias} ON {alias}.asset_id = a.id "
                f"AND {alias}.tag_key = %s AND {alias}.tag_value = %s"
            )
            args.extend([k.lower(), v])
    sql += " WHERE 1=1"
    if asset_type:
        sql += " AND a.asset_type = %s"
        args.append(asset_type)
    if status:
        sql += " AND a.status = %s"
        args.append(status)
    if source_task_id is not None:
        sql += " AND a.source_task_id = %s"
        args.append(source_task_id)
    with connect() as con:
        return int(con.execute(sql, args).fetchone()[0])


def list_assets(
    asset_type: str | None = None,
    status: str | None = None,
    source_task_id: int | None = None,
    tag_filters: list[tuple[str, str]] | None = None,
    limit: int = 200,
    offset: int = 0,
) -> list[dict[str, Any]]:
    sql = "SELECT a.* FROM assets a"
    args: list[Any] = []
    if tag_filters:
        # Ogni filtro (k, v) richiede una JOIN distinta
        for i, (k, v) in enumerate(tag_filters):
            alias = f"t{i}"
            sql += (
                f" JOIN asset_tags {alias} ON {alias}.asset_id = a.id "
                f"AND {alias}.tag_key = %s AND {alias}.tag_value = %s"
            )
            args.extend([k.lower(), v])
    sql += " WHERE 1=1"
    if asset_type:
        sql += " AND a.asset_type = %s"
        args.append(asset_type)
    if status:
        sql += " AND a.status = %s"
        args.append(status)
    if source_task_id is not None:
        sql += " AND a.source_task_id = %s"
        args.append(source_task_id)
    sql += " ORDER BY a.id DESC LIMIT %s OFFSET %s"
    args.append(limit)
    args.append(max(0, int(offset)))
    with connect() as con:
        rows = con.execute(sql, args).fetchall()
        results = [dict(r) for r in rows]
        if results:
            ids = [r["id"] for r in results]
            placeholders = ",".join("%s" for _ in ids)
            tag_rows = con.execute(
                f"SELECT asset_id, tag_key, tag_value FROM asset_tags "
                f"WHERE asset_id IN ({placeholders}) ORDER BY tag_key",
                ids,
            ).fetchall()
        else:
            tag_rows = []
    by_id: dict[int, dict[str, list[str]]] = {}
    for t in tag_rows:
        d = by_id.setdefault(int(t["asset_id"]), {})
        d.setdefault(t["tag_key"], []).append(t["tag_value"])
    for a in results:
        a["tags"] = by_id.get(int(a["id"]), {})
    return results


def update_asset_status(asset_id: int, status: str, notes: str | None = None) -> None:
    with connect() as con:
        if notes is not None:
            con.execute(
                "UPDATE assets SET status = %s, notes = %s, updated_at = %s WHERE id = %s",
                (status, notes, now_iso(), asset_id),
            )
        else:
            con.execute(
                "UPDATE assets SET status = %s, updated_at = %s WHERE id = %s",
                (status, now_iso(), asset_id),
            )


def update_asset(asset_id: int, **fields: Any) -> None:
    """Update generico per i campi base di un asset. Salta `id`, `created_at`,
    `qualifier_score`. Se `source_url` cambia, ricalcola anche `source_url_canonical`.
    Ignora field vuoti/None (per non azzerare per errore)."""
    if not fields:
        return
    # Filtra solo campi modificabili in edit manuale
    ALLOWED = {"asset_type", "source_url", "source_domain", "title", "raw_json", "notes", "status"}
    safe: dict[str, Any] = {}
    for k, v in fields.items():
        if k not in ALLOWED:
            continue
        safe[k] = v
    if not safe:
        return
    # Se source_url è cambiato, aggiorna anche source_url_canonical
    if "source_url" in safe:
        try:
            from .agent.url_canonical import canonical_url as _canon
            safe["source_url_canonical"] = _canon(safe["source_url"]) if safe["source_url"] else None
        except Exception:
            pass
    # Se raw_json è dict, serializza
    if "raw_json" in safe and not isinstance(safe["raw_json"], str):
        safe["raw_json"] = json.dumps(safe["raw_json"], ensure_ascii=False)
    sets = [f"{k} = %s" for k in safe]
    sql = f"UPDATE assets SET {', '.join(sets)}, updated_at = %s WHERE id = %s"
    with connect() as con:
        con.execute(sql, (*safe.values(), now_iso(), asset_id))


def add_asset_tag(asset_id: int, tag_key: str, tag_value: str) -> bool:
    """Aggiunge un (tag_key, tag_value) a un asset. Ritorna True se inserito,
    False se duplicato. PK(asset_id, tag_key, tag_value) impone univocità."""
    tag_key = (tag_key or "").strip().lower()
    tag_value = (tag_value or "").strip()
    if not tag_key or not tag_value:
        return False
    try:
        with connect() as con:
            con.execute(
                "INSERT OR IGNORE INTO asset_tags (asset_id, tag_key, tag_value) VALUES (%s, %s, %s)",
                (asset_id, tag_key, tag_value),
            )
            # rowcount per detect duplicato
            cur = con.execute(
                "SELECT 1 FROM asset_tags WHERE asset_id=%s AND tag_key=%s AND tag_value=%s",
                (asset_id, tag_key, tag_value),
            ).fetchone()
            return cur is not None
    except Exception:
        return False


def remove_asset_tag(asset_id: int, tag_key: str, tag_value: str) -> int:
    """Rimuove un tag puntuale (asset_id, tag_key, tag_value). Ritorna n righe cancellate."""
    with connect() as con:
        cur = con.execute(
            "DELETE FROM asset_tags WHERE asset_id=%s AND tag_key=%s AND tag_value=%s",
            (asset_id, tag_key, tag_value),
        )
        return cur.rowcount


def set_asset_tag(asset_id: int, tag_key: str, tag_value: str) -> bool:
    """Set 'singleton' di un tag: rimuove ogni precedente (asset_id, tag_key) e
    inserisce (asset_id, tag_key, tag_value). Pattern per multi-qualifier:
    se task#41 tagga 'qualifier_palestra:qualified' e poi rilanci task#41 con
    risultato diverso 'rejected', vogliamo SOSTITUIRE non accumulare.
    Ritorna True se inserito."""
    tag_key = (tag_key or "").strip().lower()
    tag_value = (tag_value or "").strip()
    if not tag_key or not tag_value:
        return False
    try:
        with connect() as con:
            con.execute(
                "DELETE FROM asset_tags WHERE asset_id=%s AND tag_key=%s",
                (asset_id, tag_key),
            )
            con.execute(
                "INSERT INTO asset_tags (asset_id, tag_key, tag_value) VALUES (%s, %s, %s)",
                (asset_id, tag_key, tag_value),
            )
            return True
    except Exception:
        return False


def delete_asset(asset_id: int) -> int:
    with connect() as con:
        cur = con.execute("DELETE FROM assets WHERE id = %s", (asset_id,))
        return cur.rowcount


def delete_assets_bulk(asset_ids: list[int]) -> int:
    """Cancella in massa gli asset indicati. Le `asset_tags` cascade-deletono per FK.
    Ritorna il numero di righe rimosse.
    """
    ids = [int(i) for i in asset_ids if i]
    if not ids:
        return 0
    placeholders = ",".join("%s" for _ in ids)
    with connect() as con:
        cur = con.execute(f"DELETE FROM assets WHERE id IN ({placeholders})", ids)
        return cur.rowcount


def update_asset_qualifier(asset_id: int, score: int, status: str, notes: str | None = None) -> None:
    """Aggiorna asset.qualifier_score + status (qualified/rejected) in un colpo.

    **Multi-qualifier safe (2026-05-16)**: se l'asset (o il contact) era già
    `qualified` da un task qualifier precedente, NON viene downgradato a
    `rejected` dal task corrente. Logica: "qualified per ALMENO un criterio
    = qualified globale". Il dettaglio per-task-qualifier viene salvato nei
    tag `qualifier_<slug>` su `asset_tags` (vedi runner_qualifier).

    Lo `qualifier_score` globale rappresenta lo score del TASK CORRENTE (non
    aggregato). Per score per-task usare i tag `qualifier_score_<slug>`.

    CASCATA su contacts: stesso pattern (no downgrade qualified).
    """
    ts = now_iso()
    with connect() as con:
        # Asset: status preserva qualified
        if notes is not None:
            con.execute(
                """UPDATE assets SET
                     qualifier_score = %s,
                     status = CASE WHEN status = 'qualified' OR %s = 'qualified' THEN 'qualified' ELSE %s END,
                     notes = %s,
                     updated_at = %s
                   WHERE id = %s""",
                (int(score), status, status, notes, ts, asset_id),
            )
        else:
            con.execute(
                """UPDATE assets SET
                     qualifier_score = %s,
                     status = CASE WHEN status = 'qualified' OR %s = 'qualified' THEN 'qualified' ELSE %s END,
                     updated_at = %s
                   WHERE id = %s""",
                (int(score), status, status, ts, asset_id),
            )
        # Cascata sui contacts. Update solo se lo status del contact e' ancora 'new',
        # 'qualified' o 'rejected' (preserva 'optedout' e altri stati terminali).
        # Logica preserve qualified identica all'asset.
        con.execute(
            """
            UPDATE contacts SET
              status = CASE WHEN status = 'qualified' OR %s = 'qualified' THEN 'qualified' ELSE %s END,
              qualifier_score = %s,
              updated_at = %s
            WHERE asset_id = %s
              AND status IN ('new', 'qualified', 'rejected')
            """,
            (status, status, int(score), ts, asset_id),
        )


def list_asset_types_in_use() -> list[dict[str, Any]]:
    with connect() as con:
        rows = con.execute(
            "SELECT asset_type, COUNT(*) AS n FROM assets GROUP BY asset_type ORDER BY n DESC"
        ).fetchall()
    return [{"asset_type": r["asset_type"], "count": int(r["n"])} for r in rows]


def list_asset_tag_keys(asset_type: str | None = None) -> list[str]:
    sql = (
        "SELECT DISTINCT t.tag_key FROM asset_tags t "
        "JOIN assets a ON a.id = t.asset_id"
    )
    args: list[Any] = []
    if asset_type:
        sql += " WHERE a.asset_type = %s"
        args.append(asset_type)
    sql += " ORDER BY t.tag_key"
    with connect() as con:
        rows = con.execute(sql, args).fetchall()
    return [r["tag_key"] for r in rows]


def list_asset_tag_values(tag_key: str, asset_type: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
    sql = (
        "SELECT t.tag_value AS v, COUNT(*) AS n FROM asset_tags t "
        "JOIN assets a ON a.id = t.asset_id WHERE t.tag_key = %s"
    )
    args: list[Any] = [tag_key.lower()]
    if asset_type:
        sql += " AND a.asset_type = %s"
        args.append(asset_type)
    sql += " GROUP BY t.tag_value ORDER BY n DESC, t.tag_value LIMIT %s"
    args.append(limit)
    with connect() as con:
        rows = con.execute(sql, args).fetchall()
    return [{"value": r["v"], "count": int(r["n"])} for r in rows]


# ===========================================================================
# Site patterns — memoria pattern URL "target" per dominio
# ===========================================================================

def find_site_patterns(
    registrable_domain: str,
    asset_type: str | None = None,
    status: str | None = None,
) -> list[dict[str, Any]]:
    sql = "SELECT * FROM site_patterns WHERE registrable_domain = %s"
    args: list[Any] = [registrable_domain.lower()]
    if asset_type:
        sql += " AND (asset_type = %s OR asset_type IS NULL)"
        args.append(asset_type)
    if status:
        sql += " AND status = %s"
        args.append(status)
    sql += " ORDER BY (status='confirmed') DESC, successes DESC, hits DESC"
    with connect() as con:
        rows = con.execute(sql, args).fetchall()
    return [dict(r) for r in rows]


def upsert_site_pattern(
    registrable_domain: str,
    pattern: str,
    regex: str,
    asset_type: str | None = None,
    source_task_id: int | None = None,
    source_job_id: int | None = None,
    notes: str | None = None,
) -> int:
    """Inserisce un pattern se nuovo (status='candidate'); altrimenti ritorna l'id esistente
    e aggiorna asset_type/regex se erano vuoti.
    """
    ts = now_iso()
    rd = registrable_domain.lower()
    with connect() as con:
        row = con.execute(
            "SELECT id, asset_type, regex FROM site_patterns "
            "WHERE registrable_domain = %s AND pattern = %s",
            (rd, pattern),
        ).fetchone()
        if row:
            pid = int(row["id"])
            updates: list[str] = []
            args: list[Any] = []
            if not row["asset_type"] and asset_type:
                updates.append("asset_type = %s")
                args.append(asset_type)
            if (not row["regex"]) and regex:
                updates.append("regex = %s")
                args.append(regex)
            if updates:
                args.extend([ts, pid])
                con.execute(
                    f"UPDATE site_patterns SET {', '.join(updates)}, updated_at = %s WHERE id = %s",
                    args,
                )
            return pid
        cur = con.execute(
            """
            INSERT INTO site_patterns (
              registrable_domain, pattern, regex, asset_type, status,
              hits, successes, failures,
              source_task_id, source_job_id, notes, created_at, updated_at
            ) VALUES (%s, %s, %s, %s, 'candidate', 0, 0, 0, %s, %s, %s, %s, %s) RETURNING id
            """,
            (rd, pattern, regex, asset_type, source_task_id, source_job_id, notes, ts, ts),
        )
        return int(cur.fetchone()['id'])


def record_pattern_run(pattern_id: int, hits: int = 0, successes: int = 0, failures: int = 0) -> None:
    with connect() as con:
        con.execute(
            """
            UPDATE site_patterns
               SET hits      = hits + %s,
                   successes = successes + %s,
                   failures  = failures + %s,
                   updated_at = %s
             WHERE id = %s
            """,
            (int(hits), int(successes), int(failures), now_iso(), pattern_id),
        )


def set_site_pattern_status(pattern_id: int, status: str, notes: str | None = None) -> None:
    with connect() as con:
        if notes is not None:
            con.execute(
                "UPDATE site_patterns SET status = %s, notes = %s, updated_at = %s WHERE id = %s",
                (status, notes, now_iso(), pattern_id),
            )
        else:
            con.execute(
                "UPDATE site_patterns SET status = %s, updated_at = %s WHERE id = %s",
                (status, now_iso(), pattern_id),
            )


def list_site_patterns(
    registrable_domain: str | None = None,
    status: str | None = None,
    limit: int = 200,
) -> list[dict[str, Any]]:
    sql = "SELECT * FROM site_patterns WHERE 1=1"
    args: list[Any] = []
    if registrable_domain:
        sql += " AND registrable_domain = %s"
        args.append(registrable_domain.lower())
    if status:
        sql += " AND status = %s"
        args.append(status)
    sql += " ORDER BY registrable_domain, (status='confirmed') DESC, successes DESC, id DESC LIMIT %s"
    args.append(limit)
    with connect() as con:
        rows = con.execute(sql, args).fetchall()
    return [dict(r) for r in rows]


def maybe_promote_pattern(pattern_id: int, min_successes: int = 3, min_ratio: float = 0.4) -> str | None:
    """Promuove a 'confirmed' un pattern candidate se ha abbastanza successi.
    Retrocede a 'candidate' un confirmed che inizia ad accumulare failures.
    Ritorna lo stato nuovo se cambia, altrimenti None.
    """
    with connect() as con:
        row = con.execute(
            "SELECT id, status, hits, successes, failures FROM site_patterns WHERE id = %s",
            (pattern_id,),
        ).fetchone()
    if not row:
        return None
    status = row["status"]
    successes = int(row["successes"])
    failures = int(row["failures"])
    total = successes + failures
    ratio = (successes / total) if total else 0.0
    new_status: str | None = None
    if status == "candidate" and successes >= min_successes and ratio >= min_ratio:
        new_status = "confirmed"
    elif status == "confirmed" and total >= 5 and ratio < 0.2:
        new_status = "candidate"
    if new_status and new_status != status:
        set_site_pattern_status(pattern_id, new_status)
        return new_status
    return None


# ===========================================================================
# Site playbooks (Stage 2 — knowledge transfer cross-runner)
# ===========================================================================

def get_site_playbook(registrable_domain: str, asset_type: str) -> dict[str, Any] | None:
    """Ritorna il playbook ATTIVO per (dominio, asset_type) o None.
    Auto-skip se status != 'active' o transferable=0."""
    if not registrable_domain or not asset_type:
        return None
    with connect() as con:
        row = con.execute(
            """SELECT * FROM site_playbooks
               WHERE registrable_domain = %s AND asset_type = %s
                     AND status = 'active' AND transferable = 1""",
            (registrable_domain.lower(), asset_type),
        ).fetchone()
    return dict(row) if row else None


def upsert_site_playbook(
    *,
    registrable_domain: str,
    asset_type: str,
    playbook: str,
    source_runner: str,
    source_job_id: int | None,
    transferable: bool,
) -> int:
    """Crea o aggiorna il playbook per (dominio, asset_type).
    Resetta `failures` a 0 (e' una nuova versione). Ritorna l'id."""
    ts = now_iso()
    domain_l = registrable_domain.lower()
    with connect() as con:
        existing = con.execute(
            "SELECT id FROM site_playbooks WHERE registrable_domain = %s AND asset_type = %s",
            (domain_l, asset_type),
        ).fetchone()
        if existing:
            pb_id = int(existing["id"])
            con.execute(
                """UPDATE site_playbooks
                   SET playbook = %s, source_runner = %s, source_job_id = %s,
                       transferable = %s, status = 'active', failures = 0,
                       updated_at = %s
                   WHERE id = %s""",
                (
                    playbook, source_runner, source_job_id,
                    1 if transferable else 0, ts, pb_id,
                ),
            )
            return pb_id
        cur = con.execute(
            """INSERT INTO site_playbooks (
                registrable_domain, asset_type, playbook, source_runner, source_job_id,
                transferable, status, hits, successes, failures, created_at, updated_at) RETURNING id
               VALUES (%s, %s, %s, %s, %s, %s, 'active', 0, 0, 0, %s, %s)""",
            (
                domain_l, asset_type, playbook, source_runner, source_job_id,
                1 if transferable else 0, ts, ts,
            ),
        )
        return int(cur.fetchone()['id'])


def bump_playbook_hits(playbook_id: int) -> None:
    with connect() as con:
        con.execute(
            "UPDATE site_playbooks SET hits = hits + 1, updated_at = %s WHERE id = %s",
            (now_iso(), playbook_id),
        )


def bump_playbook_outcome(playbook_id: int, *, success: bool, stale_threshold: int = 3) -> str | None:
    """Bump successes o failures. Auto-stale a `failures >= stale_threshold`.
    Ritorna 'stale' se il playbook e' stato auto-archiviato, None altrimenti."""
    ts = now_iso()
    with connect() as con:
        if success:
            con.execute(
                "UPDATE site_playbooks SET successes = successes + 1, failures = 0, updated_at = %s WHERE id = %s",
                (ts, playbook_id),
            )
            return None
        # failure: bump e check soglia
        con.execute(
            "UPDATE site_playbooks SET failures = failures + 1, updated_at = %s WHERE id = %s",
            (ts, playbook_id),
        )
        row = con.execute(
            "SELECT failures, status FROM site_playbooks WHERE id = %s", (playbook_id,)
        ).fetchone()
        if row and int(row["failures"]) >= stale_threshold and row["status"] == "active":
            con.execute(
                "UPDATE site_playbooks SET status = 'stale', updated_at = %s WHERE id = %s",
                (ts, playbook_id),
            )
            return "stale"
    return None


def list_site_playbooks(
    *,
    registrable_domain: str | None = None,
    status: str | None = None,
    limit: int = 200,
) -> list[dict[str, Any]]:
    sql = "SELECT * FROM site_playbooks WHERE 1=1"
    params: list[Any] = []
    if registrable_domain:
        sql += " AND registrable_domain = %s"
        params.append(registrable_domain.lower())
    if status:
        sql += " AND status = %s"
        params.append(status)
    sql += " ORDER BY updated_at DESC LIMIT %s"
    params.append(int(limit))
    with connect() as con:
        rows = con.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def delete_site_playbook(playbook_id: int) -> None:
    with connect() as con:
        con.execute("DELETE FROM site_playbooks WHERE id = %s", (playbook_id,))


def delete_site_pattern(pattern_id: int) -> None:
    with connect() as con:
        con.execute("DELETE FROM site_patterns WHERE id = %s", (pattern_id,))


def delete_site_patterns_by_domain(registrable_domain: str) -> int:
    """Cancella tutti i pattern di un dominio. Ritorna il numero di righe cancellate."""
    if not registrable_domain:
        return 0
    with connect() as con:
        cur = con.execute(
            "DELETE FROM site_patterns WHERE registrable_domain = %s",
            (registrable_domain.lower(),),
        )
        return int(cur.rowcount or 0)


def delete_site_playbooks_by_domain(registrable_domain: str) -> int:
    """Cancella tutti i playbook di un dominio. Ritorna n righe cancellate."""
    if not registrable_domain:
        return 0
    with connect() as con:
        cur = con.execute(
            "DELETE FROM site_playbooks WHERE registrable_domain = %s",
            (registrable_domain.lower(),),
        )
        return int(cur.rowcount or 0)


def truncate_site_memory() -> dict[str, int]:
    """Svuota completamente la memoria sito (pattern + playbook).
    Ritorna un dict con n righe cancellate per tabella."""
    with connect() as con:
        n_pat = int(con.execute("DELETE FROM site_patterns").rowcount or 0)
        n_pb = int(con.execute("DELETE FROM site_playbooks").rowcount or 0)
    return {"site_patterns": n_pat, "site_playbooks": n_pb}


# ===========================================================================
# Threads
# ===========================================================================

def get_or_create_thread(
    contact_id: int,
    channel: str,
    external_id: str | None = None,
    subject: str | None = None,
    task_id: int | None = None,
) -> int:
    with connect() as con:
        if external_id:
            row = con.execute(
                "SELECT id FROM threads WHERE channel = %s AND external_id = %s LIMIT 1",
                (channel, external_id),
            ).fetchone()
            if row:
                return int(row["id"])
        # cerca un thread aperto sullo stesso contatto/canale come fallback
        row = con.execute(
            "SELECT id FROM threads WHERE contact_id = %s AND channel = %s AND status='open' "
            "ORDER BY id DESC LIMIT 1",
            (contact_id, channel),
        ).fetchone()
        if row:
            return int(row["id"])
        cur = con.execute(
            """
            INSERT INTO threads (contact_id, channel, external_id, subject, status,
                                 task_id, created_at)
            VALUES (%s, %s, %s, %s, 'open', %s, %s) RETURNING id
            """,
            (contact_id, channel, external_id, subject, task_id, now_iso()),
        )
        return int(cur.fetchone()['id'])


def get_thread(thread_id: int) -> dict[str, Any] | None:
    with connect() as con:
        row = con.execute("SELECT * FROM threads WHERE id = %s", (thread_id,)).fetchone()
    return dict(row) if row else None


def list_threads(
    channel: str | None = None,
    status: str | None = None,
    task_id: int | None = None,
    limit: int = 200,
) -> list[dict[str, Any]]:
    sql = """
        SELECT t.*, c.email, c.telegram_username, c.display_name, c.status as contact_status
        FROM threads t LEFT JOIN contacts c ON c.id = t.contact_id
        WHERE 1=1
    """
    args: list[Any] = []
    if channel:
        sql += " AND t.channel = %s"
        args.append(channel)
    if status:
        sql += " AND t.status = %s"
        args.append(status)
    if task_id is not None:
        sql += " AND t.task_id = %s"
        args.append(task_id)
    sql += " ORDER BY COALESCE(t.last_msg_at, t.created_at) DESC LIMIT %s"
    args.append(limit)
    with connect() as con:
        rows = con.execute(sql, args).fetchall()
    return [dict(r) for r in rows]


def update_thread_status(thread_id: int, status: str) -> None:
    with connect() as con:
        con.execute(
            "UPDATE threads SET status = %s WHERE id = %s", (status, thread_id)
        )


def touch_thread(thread_id: int) -> None:
    with connect() as con:
        con.execute(
            "UPDATE threads SET last_msg_at = %s WHERE id = %s", (now_iso(), thread_id)
        )


# ===========================================================================
# Messages
# ===========================================================================

def insert_message(
    thread_id: int,
    direction: str,
    body: str,
    llm_generated: bool = False,
    external_id: str | None = None,
    status: str = "pending",
    error: str | None = None,
    sent_at: str | None = None,
) -> int:
    with connect() as con:
        cur = con.execute(
            """
            INSERT INTO messages (thread_id, direction, body, llm_generated, external_id,
                                  status, error, sent_at, created_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id
            """,
            (
                thread_id,
                direction,
                body,
                1 if llm_generated else 0,
                external_id,
                status,
                error,
                sent_at,
                now_iso(),
            ),
        )
        return int(cur.fetchone()['id'])


def update_message(message_id: int, **fields: Any) -> None:
    if not fields:
        return
    cols = ", ".join(f"{k} = %s" for k in fields)
    values = list(fields.values()) + [message_id]
    with connect() as con:
        con.execute(f"UPDATE messages SET {cols} WHERE id = %s", values)


def list_messages(thread_id: int) -> list[dict[str, Any]]:
    with connect() as con:
        rows = con.execute(
            "SELECT * FROM messages WHERE thread_id = %s ORDER BY id",
            (thread_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def find_unprocessed_inbound() -> list[dict[str, Any]]:
    """Messaggi inbound senza una reply outbound successiva nello stesso thread."""
    sql = """
        SELECT m.*, t.contact_id, t.channel, t.subject, t.task_id
        FROM messages m
        JOIN threads t ON t.id = m.thread_id
        WHERE m.direction = 'in' AND m.status = 'received'
          AND NOT EXISTS (
            SELECT 1 FROM messages m2
            WHERE m2.thread_id = m.thread_id
              AND m2.direction = 'out' AND m2.id > m.id
          )
        ORDER BY m.created_at
    """
    with connect() as con:
        rows = con.execute(sql).fetchall()
    return [dict(r) for r in rows]


# ===========================================================================
# Channel config (singleton per channel)
# ===========================================================================

def get_channel_config(channel: str) -> dict[str, Any] | None:
    with connect() as con:
        row = con.execute(
            "SELECT * FROM channel_config WHERE channel = %s", (channel,)
        ).fetchone()
    if not row:
        return None
    d = dict(row)
    try:
        d["config"] = json.loads(d.get("config_json") or "{}")
    except json.JSONDecodeError:
        d["config"] = {}
    return d


def save_channel_config(channel: str, config: dict[str, Any], enabled: bool) -> None:
    payload = json.dumps(config)
    with connect() as con:
        con.execute(
            """
            INSERT INTO channel_config (channel, config_json, enabled, updated_at)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT(channel) DO UPDATE SET
              config_json = excluded.config_json,
              enabled     = excluded.enabled,
              updated_at  = excluded.updated_at
            """,
            (channel, payload, 1 if enabled else 0, now_iso()),
        )


# ===========================================================================
# Orchestrator persistent chat
# ===========================================================================

def add_orchestrator_message(
    role: str,
    body: str,
    metadata: dict[str, Any] | None = None,
) -> int:
    payload = json.dumps(metadata or {})
    with connect() as con:
        cur = con.execute(
            """
            INSERT INTO orchestrator_messages (role, body, metadata_json, created_at)
            VALUES (%s, %s, %s, %s) RETURNING id
            """,
            (role, body, payload, now_iso()),
        )
        return int(cur.fetchone()['id'])


def list_orchestrator_messages(limit: int = 100) -> list[dict[str, Any]]:
    with connect() as con:
        rows = con.execute(
            """
            SELECT * FROM orchestrator_messages
            ORDER BY id DESC
            LIMIT %s
            """,
            (limit,),
        ).fetchall()
    out: list[dict[str, Any]] = []
    for r in reversed(rows):
        d = dict(r)
        try:
            d["metadata"] = json.loads(d.get("metadata_json") or "{}")
        except json.JSONDecodeError:
            d["metadata"] = {}
        out.append(d)
    return out


def clear_orchestrator_messages() -> None:
    with connect() as con:
        con.execute("DELETE FROM orchestrator_messages")


# ===========================================================================
# Social accounts + DM log (outreach social runner)
# ===========================================================================

def create_social_account(data: dict) -> int:
    """Insert social_account.  deve essere bytes Fernet.

    Required keys: uuid, platform, username, encrypted_password.
    Optional: proxy_label, daily_dm_cap, status, notes.
    """
    ts = now_iso()
    with connect() as con:
        cur = con.execute(
            """INSERT INTO social_accounts
            (uuid, platform, username, encrypted_password, proxy_label,
             daily_dm_cap, status, warmup_started_at, warmup_days_target,
             notes, created_at, updated_at) RETURNING id
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            (
                data["uuid"], data["platform"], data["username"],
                data["encrypted_password"], data.get("proxy_label"),
                int(data.get("daily_dm_cap", 10)),
                data.get("status", "warming_up"),
                data.get("warmup_started_at"),
                int(data.get("warmup_days_target", 30)),
                data.get("notes"),
                ts, ts,
            ),
        )
        return int(cur.fetchone()['id'])


def list_social_accounts(platform: str | None = None, status: str | None = None) -> list[dict]:
    sql = "SELECT * FROM social_accounts WHERE 1=1"
    args: list = []
    if platform:
        sql += " AND platform = %s"; args.append(platform)
    if status:
        sql += " AND status = %s"; args.append(status)
    sql += " ORDER BY id DESC"
    with connect() as con:
        rows = con.execute(sql, args).fetchall()
    return [dict(r) for r in rows]


def get_social_account(account_id: int) -> dict | None:
    with connect() as con:
        r = con.execute("SELECT * FROM social_accounts WHERE id = %s", (account_id,)).fetchone()
    return dict(r) if r else None


def update_social_account(account_id: int, **fields) -> None:
    if not fields: return
    sets = [f"{k} = %s" for k in fields]
    sql = f"UPDATE social_accounts SET {', '.join(sets)}, updated_at = %s WHERE id = %s"
    with connect() as con:
        con.execute(sql, (*fields.values(), now_iso(), account_id))


def delete_social_account(account_id: int) -> None:
    with connect() as con:
        con.execute("DELETE FROM social_accounts WHERE id = %s", (account_id,))


def insert_social_dm_log(data: dict) -> int:
    """Log di un singolo DM inviato.

    Per IG/TikTok/Facebook e WhatsApp browser (Motore A): popolare `account_id`,
    lasciare `api_config_id` e `engine` (eventualmente 'A_browser') a None/scelta.
    Per WhatsApp API (Motore B): popolare `api_config_id` + `engine='B_api'`,
    lasciare `account_id=None`.
    """
    account_id = data.get("account_id")
    with connect() as con:
        cur = con.execute(
            """INSERT INTO social_dm_log
            (account_id, job_id, target_contact_id, target_platform,
             target_username, message, sent_at, ok, reason, health_post,
             engine, api_config_id) RETURNING id
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            (
                int(account_id) if account_id is not None else None,
                data.get("job_id"),
                data.get("target_contact_id"),
                data["target_platform"],
                data["target_username"],
                data["message"],
                data.get("sent_at") or now_iso(),
                1 if data.get("ok") else 0,
                data.get("reason"),
                data.get("health_post"),
                data.get("engine"),
                data.get("api_config_id"),
            ),
        )
        return int(cur.fetchone()['id'])


def list_social_dm_log(account_id: int | None = None, limit: int = 100) -> list[dict]:
    sql = "SELECT * FROM social_dm_log"
    args: list = []
    if account_id is not None:
        sql += " WHERE account_id = %s"; args.append(account_id)
    sql += " ORDER BY id DESC LIMIT %s"; args.append(limit)
    with connect() as con:
        rows = con.execute(sql, args).fetchall()
    return [dict(r) for r in rows]


def count_social_dms_today(account_id: int) -> int:
    """Numero di DM inviati con ok=1 nelle ultime 24h da questo account."""
    from datetime import datetime, timedelta, timezone
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    with connect() as con:
        r = con.execute(
            "SELECT COUNT(*) FROM social_dm_log WHERE account_id = %s AND ok = 1 AND sent_at >= %s",
            (account_id, cutoff),
        ).fetchone()
    return int(r[0]) if r else 0


# ===========================================================================
# WhatsApp API config (Motore B — Meta Cloud API)
# ===========================================================================

def insert_whatsapp_api_config(data: dict) -> int:
    """Crea una nuova configurazione Meta Cloud API.

    Required: label, phone_number_id, business_account_id, encrypted_access_token (bytes).
    Optional: app_id, default_template_name, default_template_language ('it' default),
              status ('active' default), daily_msg_cap (250 default), notes.
    """
    ts = now_iso()
    with connect() as con:
        cur = con.execute(
            """INSERT INTO whatsapp_api_config
            (label, phone_number_id, business_account_id, app_id,
             encrypted_access_token, default_template_name, default_template_language,
             status, daily_msg_cap, notes, created_at, updated_at) RETURNING id
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            (
                data["label"],
                data["phone_number_id"],
                data["business_account_id"],
                data.get("app_id"),
                data["encrypted_access_token"],
                data.get("default_template_name"),
                data.get("default_template_language", "it"),
                data.get("status", "active"),
                int(data.get("daily_msg_cap", 250)),
                data.get("notes"),
                ts, ts,
            ),
        )
        return int(cur.fetchone()['id'])


def list_whatsapp_api_config(status: str | None = None) -> list[dict]:
    sql = "SELECT * FROM whatsapp_api_config"
    args: list = []
    if status:
        sql += " WHERE status = %s"; args.append(status)
    sql += " ORDER BY id DESC"
    with connect() as con:
        rows = con.execute(sql, args).fetchall()
    return [dict(r) for r in rows]


def get_whatsapp_api_config(config_id: int) -> dict | None:
    with connect() as con:
        r = con.execute(
            "SELECT * FROM whatsapp_api_config WHERE id = %s", (config_id,)
        ).fetchone()
    return dict(r) if r else None


def update_whatsapp_api_config(config_id: int, **fields) -> None:
    if not fields:
        return
    sets = [f"{k} = %s" for k in fields]
    sql = f"UPDATE whatsapp_api_config SET {', '.join(sets)}, updated_at = %s WHERE id = %s"
    with connect() as con:
        con.execute(sql, (*fields.values(), now_iso(), config_id))


def delete_whatsapp_api_config(config_id: int) -> None:
    with connect() as con:
        con.execute("DELETE FROM whatsapp_api_config WHERE id = %s", (config_id,))


def count_whatsapp_api_msgs_today(api_config_id: int) -> int:
    """Numero di messaggi Motore B (engine='B_api') inviati con ok=1 oggi (UTC) da
    questa config. Usato per il daily_msg_cap.
    """
    from datetime import datetime, timedelta, timezone
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    with connect() as con:
        r = con.execute(
            "SELECT COUNT(*) FROM social_dm_log "
            "WHERE api_config_id = %s AND engine = 'B_api' AND ok = 1 AND sent_at >= %s",
            (api_config_id, cutoff),
        ).fetchone()
    return int(r[0]) if r else 0


# ===========================================================================
# Contacts — helpers WhatsApp (consent + inbound tracking)
# ===========================================================================

def update_contact_whatsapp_consent(contact_id: int, consent: str) -> None:
    """Aggiorna `contacts.whatsapp_consent`. Valori validi: 'cold'|'opt_in'|'optedout'."""
    if consent not in ("cold", "opt_in", "optedout"):
        raise ValueError(f"whatsapp_consent invalido: {consent!r}")
    with connect() as con:
        con.execute(
            "UPDATE contacts SET whatsapp_consent = %s, updated_at = %s WHERE id = %s",
            (consent, now_iso(), contact_id),
        )


def touch_contact_whatsapp_inbound(contact_id: int) -> None:
    """Segna che il contatto ha appena scritto al business number — abilita la
    24h-window per messaggi free-form via Motore B.
    """
    ts = now_iso()
    with connect() as con:
        con.execute(
            "UPDATE contacts SET whatsapp_last_inbound_at = %s, updated_at = %s WHERE id = %s",
            (ts, ts, contact_id),
        )


def list_contacts_for_whatsapp_outreach(
    only_qualified: bool = True,
    exclude_optedout: bool = True,
    exclude_contacted: bool = True,
    limit: int = 1000,
    contact_tag_filters: list[tuple[str, str]] | list[dict] | None = None,
) -> list[dict]:
    """Carica i contatti idonei a outreach_whatsapp.

    Filtri di default: status='qualified', whatsapp_consent != 'optedout',
    status != 'contacted', whatsapp IS NOT NULL.

    `contact_tag_filters`: lista di (tag_key, tag_value) o {key, value}.
    Multipli filtri sono combinati in AND tramite EXISTS join su asset_tags.
    """
    sql = "SELECT * FROM contacts WHERE whatsapp IS NOT NULL AND whatsapp != ''"
    args: list = []
    if only_qualified:
        sql += " AND status = 'qualified'"
    if exclude_optedout:
        sql += " AND (whatsapp_consent IS NULL OR whatsapp_consent != 'optedout')"
    if exclude_contacted:
        sql += " AND status != 'contacted'"
    sql, args = _add_contact_tag_filters_clause(sql, args, contact_tag_filters)
    sql += " ORDER BY id DESC LIMIT %s"
    args.append(limit)
    with connect() as con:
        rows = con.execute(sql, args).fetchall()
    return [dict(r) for r in rows]
