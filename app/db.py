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
    "argos_current_tenant_id", default=None
)

_current_user_id_var: contextvars.ContextVar[int | None] = contextvars.ContextVar(
    "argos_current_user_id", default=None
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


_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "host.docker.internal"}


# ---------------------------------------------------------------------------
# Pool tuning (Postgres remoto: Neon, Azure, Supabase, ...)
# ---------------------------------------------------------------------------
# Su DB remoto la latenza per round-trip è 30-60 ms (vs ~0.1 ms su localhost).
# Ogni query separata paga il RTT, ogni NUOVA connessione paga handshake TLS
# completo (200-500 ms). Per non sprecare prestazioni:
#
# - min_size alto → connessioni WARM nel pool, niente handshake nel critical
#   path quando più request HTMX arrivano in parallelo.
# - keepalive TCP → evita che il middlebox/load-balancer killi connessioni
#   idle (Neon auto-suspend, Azure Gateway, NAT).
# - prepare_threshold=None se il DB è dietro PgBouncer/Neon pooler
#   (transaction-mode pooler non supporta i prepared statements server-side
#   di psycopg3 → ogni query darebbe DuplicatePreparedStatement).
#
# I default sono pensati per uso single-user single-tenant tipico (5 PC, ~10
# request HTMX paralleli). Override via env per scenari più grandi.

_DEFAULT_POOL_MIN = 4   # connessioni warm sempre disponibili
_DEFAULT_POOL_MAX = 10  # tetto, scelto sotto i limiti free di Neon/Azure
_DEFAULT_POOL_TIMEOUT = 10.0  # attesa massima per ottenere una conn dal pool
_DEFAULT_POOL_MAX_IDLE = 300.0  # ricicla conn idle dopo 5 min (anti-killer)

# Parametri TCP keepalive di libpq. Sono aggiunti alla conninfo se l'utente
# non li ha già settati. Funzionano sia su Neon che su Azure Postgres.
_KEEPALIVE_PARAMS = {
    "keepalives": "1",
    "keepalives_idle": "30",      # primo keepalive dopo 30s di idle
    "keepalives_interval": "10",  # successivi ogni 10s
    "keepalives_count": "3",      # 3 fallimenti consecutivi → chiudi
}


def _is_pgbouncer_dsn(dsn: str) -> bool:
    """True se il DB sembra essere dietro un pooler PgBouncer-like
    (transaction-mode). In tal caso `prepare_threshold` va disabilitato per
    evitare 'prepared statement does not exist' al cambio di backend.

    Detection heuristica:
      - DATABASE_DISABLE_PREPARED=1 in env → opt-in esplicito
      - hostname contiene '-pooler' (Neon pooled endpoint)
      - hostname contiene 'pgbouncer'
      - porta 6543 (Supabase pooler default)
    """
    if (os.environ.get("DATABASE_DISABLE_PREPARED") or "").strip() == "1":
        return True
    try:
        from urllib.parse import urlparse
        parsed = urlparse(dsn)
        host = (parsed.hostname or "").lower()
        port = parsed.port
    except Exception:
        return False
    if "-pooler" in host or "pgbouncer" in host:
        return True
    if port == 6543:
        return True
    return False


def _compose_conninfo(dsn: str) -> str:
    """Restituisce la DSN con i parametri di keepalive aggiunti (se mancanti).

    Idempotente: se la DSN dell'utente già contiene `keepalives=...` non
    sovrascrive nulla. Lasciamo `sslmode` invariato (l'utente lo controlla).
    """
    try:
        from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse
        parsed = urlparse(dsn)
        existing = dict(parse_qsl(parsed.query, keep_blank_values=True))
        for k, v in _KEEPALIVE_PARAMS.items():
            existing.setdefault(k, v)
        new_query = urlencode(existing)
        return urlunparse(parsed._replace(query=new_query))
    except Exception:
        # In caso di DSN esotica, restituisci l'originale: meglio funzionare
        # senza keepalive che fallire del tutto.
        return dsn


def _env_int(name: str, default: int) -> int:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _pool_settings() -> dict[str, Any]:
    """Parametri del ConnectionPool letti da env (con default ragionevoli)."""
    return {
        "min_size": max(0, _env_int("DATABASE_POOL_MIN_SIZE", _DEFAULT_POOL_MIN)),
        "max_size": max(1, _env_int("DATABASE_POOL_MAX_SIZE", _DEFAULT_POOL_MAX)),
        "timeout": _env_float("DATABASE_POOL_TIMEOUT", _DEFAULT_POOL_TIMEOUT),
        "max_idle": _env_float("DATABASE_POOL_MAX_IDLE", _DEFAULT_POOL_MAX_IDLE),
    }


def _conn_kwargs(dsn: str) -> dict[str, Any]:
    """kwargs passati ad ogni nuova connessione del pool. Include
    `prepare_threshold=None` se il DB è dietro PgBouncer-like pooler."""
    from psycopg.rows import dict_row

    kw: dict[str, Any] = {"row_factory": dict_row}
    if _is_pgbouncer_dsn(dsn):
        # None disattiva del tutto i prepared statements server-side.
        kw["prepare_threshold"] = None
    return kw


def describe_active_dsn() -> str:
    """Una riga riepilogativa del DB target attivo, sicura da loggare (password
    mascherata). Include la categoria LOCALE/REMOTO e l'origine (.env vs /dbconfig).

    Usata da `app.main` nel lifespan per stampare:
      [DB] attivo: LOCALE -postgresql://postgres:****@localhost:5432/foo (origine: .env)
    """
    import re
    from urllib.parse import urlparse

    from . import _runtime_db_override

    dsn = os.environ.get("DATABASE_URL", "").strip()
    if not dsn:
        return "[DB] attivo: NESSUNO -DATABASE_URL non impostata"

    # Maschera password
    masked = re.sub(r"(postgres(?:ql)?://[^:]+:)([^@]+)(@)", r"\1****\3", dsn)

    # Categoria
    try:
        host = (urlparse(dsn).hostname or "").lower()
    except Exception:
        host = ""
    category = "LOCALE" if host in _LOCAL_HOSTS else "REMOTO"

    # Origine
    override = _runtime_db_override.read_override()
    if override and override.get("database_url"):
        label = override.get("active_label") or "(no label)"
        origin = f"/dbconfig, label='{label}'"
    else:
        origin = ".env"

    return f"[DB] attivo: {category} - {masked} (origine: {origin})"


def _get_pool():
    """Singleton lazy del ConnectionPool.

    Le impostazioni del pool (min/max/timeout/max_idle) sono override-abili via
    env (`DATABASE_POOL_*`), così su DB remoto si può alzare min_size senza
    toccare codice. Il PgBouncer-like pooling è auto-rilevato per evitare il
    bug dei prepared statements server-side.
    """
    global _pool
    if _pool is not None:
        return _pool
    from psycopg_pool import ConnectionPool

    raw_dsn = _resolve_dsn()
    conninfo = _compose_conninfo(raw_dsn)
    settings = _pool_settings()
    log.info(
        "[DB] pool init: min=%s max=%s timeout=%.1fs max_idle=%.0fs pgbouncer_mode=%s",
        settings["min_size"],
        settings["max_size"],
        settings["timeout"],
        settings["max_idle"],
        _is_pgbouncer_dsn(raw_dsn),
    )
    _pool = ConnectionPool(
        conninfo=conninfo,
        kwargs=_conn_kwargs(raw_dsn),
        open=True,
        **settings,
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
    `con.commit()` esplicito -non serve più ma non fa male.
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
  target_asset_ids TEXT,
  whatsapp_engine_preference TEXT NOT NULL DEFAULT 'auto',
  whatsapp_dry_run INTEGER NOT NULL DEFAULT 0,
  whatsapp_account_id BIGINT,
  whatsapp_api_config_id BIGINT,
  social_account_id BIGINT,
  email_account_id BIGINT,
  telegram_bot_id BIGINT,
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
  gap_between_dms_min REAL,
  gap_between_dms_max REAL,
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
  -- Contact channels (aggiunte in Fase 2A — Alembic 1e9265f53339)
  display_name         TEXT,
  email                TEXT,
  telegram_username    TEXT,
  telegram_chat_id     TEXT,
  whatsapp             TEXT,
  whatsapp_consent     TEXT DEFAULT 'cold',
  whatsapp_last_inbound_at TEXT,
  social_json          TEXT,
  sitoweb              TEXT,
  outreach_status      TEXT DEFAULT 'pending',
  -- B-016: dedup cross-task. NULL/unique = standalone. 'merged_into:<id>' =
  -- record secondario di un cluster (queries di default filtrano via WHERE).
  -- dedup_canonical_id = primary del cluster (NULL se questo e' primary o
  -- ancora non dedupato).
  dedup_status         TEXT,
  dedup_canonical_id   BIGINT,
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
-- B-016: Asset dedup candidates (cross-task duplicate detection)
-- Una riga per coppia (primary, candidate) di asset sospetti duplicati.
-- Popolata da app.agent.asset_dedup.find_dedup_candidates() ogni volta
-- che un asset viene insertato/updato. Risolta via UI /assets/duplicates.
-- ============================================================
CREATE TABLE IF NOT EXISTS asset_dedup_candidates (
  id BIGSERIAL PRIMARY KEY,
  primary_asset_id   BIGINT NOT NULL REFERENCES assets(id) ON DELETE CASCADE,
  candidate_asset_id BIGINT NOT NULL REFERENCES assets(id) ON DELETE CASCADE,
  match_keys         TEXT NOT NULL,   -- JSON: [{"key":"whatsapp","value":"+393..","weight":"strong"}, ...]
  match_score        REAL NOT NULL,   -- 0.0-1.0
  status             TEXT NOT NULL DEFAULT 'pending',  -- 'pending'|'merged'|'rejected'|'ignored'
  detected_at        TEXT NOT NULL,
  resolved_at        TEXT,
  resolved_by_user_id BIGINT,
  -- (primary, candidate) unico: idempotenza re-scan. Mettiamo primary<candidate
  -- come convenzione applicativa per evitare duplicati specchio (A,B) vs (B,A).
  UNIQUE (primary_asset_id, candidate_asset_id)
);
CREATE INDEX IF NOT EXISTS idx_asset_dedup_status
  ON asset_dedup_candidates(status, detected_at DESC);
CREATE INDEX IF NOT EXISTS idx_asset_dedup_primary
  ON asset_dedup_candidates(primary_asset_id) WHERE status = 'pending';

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
-- Email accounts (SMTP/IMAP multi-account)
-- ============================================================
CREATE TABLE IF NOT EXISTS email_accounts (
  id BIGSERIAL PRIMARY KEY,
  uuid TEXT UNIQUE NOT NULL,
  label TEXT NOT NULL,
  from_address TEXT NOT NULL,
  reply_to TEXT,
  smtp_host TEXT NOT NULL,
  smtp_port INTEGER NOT NULL DEFAULT 587,
  smtp_user TEXT NOT NULL,
  encrypted_smtp_password BYTEA NOT NULL,
  smtp_use_tls INTEGER NOT NULL DEFAULT 1,
  imap_host TEXT,
  imap_port INTEGER DEFAULT 993,
  imap_user TEXT,
  encrypted_imap_password BYTEA,
  imap_folder TEXT NOT NULL DEFAULT 'INBOX',
  status TEXT NOT NULL DEFAULT 'active',
  daily_send_cap INTEGER NOT NULL DEFAULT 200,
  rate_limit_per_minute INTEGER NOT NULL DEFAULT 10,
  notes TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE (from_address)
);

-- ============================================================
-- Telegram bots (Bot API multi-bot)
-- ============================================================
CREATE TABLE IF NOT EXISTS telegram_bots (
  id BIGSERIAL PRIMARY KEY,
  uuid TEXT UNIQUE NOT NULL,
  label TEXT NOT NULL,
  bot_username TEXT,
  encrypted_bot_token BYTEA NOT NULL,
  status TEXT NOT NULL DEFAULT 'active',
  daily_msg_cap INTEGER NOT NULL DEFAULT 500,
  poll_interval_seconds INTEGER NOT NULL DEFAULT 30,
  last_update_id BIGINT,
  notes TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE (bot_username)
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
  contact_id   BIGINT REFERENCES contacts(id) ON DELETE CASCADE,
  asset_id     BIGINT REFERENCES assets(id) ON DELETE CASCADE,
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
  target_asset_id BIGINT REFERENCES assets(id) ON DELETE SET NULL,
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
CREATE INDEX IF NOT EXISTS idx_assets_email ON assets(email);
CREATE INDEX IF NOT EXISTS idx_assets_telegram_chat ON assets(telegram_chat_id);
CREATE INDEX IF NOT EXISTS idx_assets_outreach_status ON assets(outreach_status);
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
CREATE INDEX IF NOT EXISTS idx_social_dm_log_target_asset ON social_dm_log(target_asset_id);
CREATE INDEX IF NOT EXISTS idx_threads_asset ON threads(asset_id);
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
# scraping condivisa cross-tenant -vedi SETUP_CLOUD_DB_TENANT.md).
_TENANT_AWARE_TABLES = (
    "tasks", "jobs", "workflows", "workflow_runs", "workflow_edges",
    "assets", "asset_tags", "contacts", "threads", "messages",
    "orchestrator_messages", "social_accounts", "social_dm_log",
    "whatsapp_api_config", "channel_config",
    "email_accounts", "telegram_bots",
    "recon_runs", "recon_checkpoints", "recon_visited",
)

# Tabelle che ricevono `created_by_user_id BIGINT REFERENCES users(id) ON DELETE SET NULL`
# per audit/per-user permissioning futuro.
_USER_OWNERSHIP_TABLES = (
    "tasks", "jobs", "workflows", "assets", "contacts",
    "social_accounts", "whatsapp_api_config",
    "email_accounts", "telegram_bots",
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
        ("idx_email_accounts_tenant", "email_accounts(tenant_id, status)"),
        ("idx_telegram_bots_tenant", "telegram_bots(tenant_id, status)"),
        ("idx_recon_runs_tenant", "recon_runs(tenant_id, status)"),
    ]
    for name, definition in tenant_indices:
        conn.execute(f"CREATE INDEX IF NOT EXISTS {name} ON {definition}")

    # Audience snapshot per task outreach: lista asset.id come JSON.
    # Indipendente dal multi-tenant ma idempotente nello stesso pattern.
    conn.execute(
        "ALTER TABLE tasks ADD COLUMN IF NOT EXISTS target_asset_ids TEXT"
    )
    # Sender single-select per outreach_social: social_account_id (IG/TT/FB).
    # NULL = pool default (tutti gli account active per quella platform).
    conn.execute(
        "ALTER TABLE tasks ADD COLUMN IF NOT EXISTS social_account_id BIGINT"
    )
    # Sender single-select per outreach mode email/telegram. NULL = primo account
    # active del tenant (vedi get_default_email_account / get_default_telegram_bot).
    conn.execute(
        "ALTER TABLE tasks ADD COLUMN IF NOT EXISTS email_account_id BIGINT"
    )
    conn.execute(
        "ALTER TABLE tasks ADD COLUMN IF NOT EXISTS telegram_bot_id BIGINT"
    )
    # Gap anti-ban tra DM consecutivi (B-011).
    # NULL = usa default per-platform da humanize.default_gap_range_min().
    # Range espresso in minuti (float): es. 0.15-0.35 = 9-21s.
    conn.execute(
        "ALTER TABLE tasks ADD COLUMN IF NOT EXISTS gap_between_dms_min REAL"
    )
    conn.execute(
        "ALTER TABLE tasks ADD COLUMN IF NOT EXISTS gap_between_dms_max REAL"
    )
    # B-016: asset dedup cross-task. Colonne idempotenti su assets esistenti.
    conn.execute(
        "ALTER TABLE assets ADD COLUMN IF NOT EXISTS dedup_status TEXT"
    )
    conn.execute(
        "ALTER TABLE assets ADD COLUMN IF NOT EXISTS dedup_canonical_id BIGINT"
    )


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


def _coerce_gap_minutes(value: Any) -> float | None:
    """Gap anti-ban minuti (B-011). Accetta None/empty/numero/str-numero.
    Clamp [0.05, 60.0]. None se vuoto o non parsabile."""
    if value is None:
        return None
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return None
        try:
            value = float(s.replace(",", "."))
        except ValueError:
            return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if f < 0.05:
        f = 0.05
    if f > 60.0:
        f = 60.0
    return f


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
    # target_asset_ids: stesso pattern, lista di asset.id (snapshot audience).
    raw_aids = _load_list(d.get("target_asset_ids"))
    coerced_aids: list[int] = []
    for v in raw_aids:
        try:
            coerced_aids.append(int(v))
        except (TypeError, ValueError):
            continue
    d["target_asset_ids"] = coerced_aids
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

def list_tasks(
    tenant_id: Any = _UNSET,
    created_by_user_id: Any = None,
) -> list[dict[str, Any]]:
    """Lista task del tenant. Se `created_by_user_id` valorizzato (int),
    filtra ulteriormente per autore (uso 'I miei task'). None = no filtro
    autore (mostra tutti i task del tenant)."""
    tenant_id = _resolve_tenant(tenant_id)
    clauses: list[str] = []
    args: list[Any] = []
    if tenant_id is not None:
        clauses.append("tenant_id = %s")
        args.append(tenant_id)
    if created_by_user_id is not None:
        clauses.append("created_by_user_id = %s")
        args.append(int(created_by_user_id))
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    with connect() as con:
        rows = con.execute(
            f"SELECT * FROM tasks{where} ORDER BY id DESC",
            tuple(args),
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


def get_tasks_by_ids(
    task_ids: list[int], tenant_id: Any = _UNSET
) -> dict[int, dict[str, Any]]:
    """Batch lookup: ritorna dict {task_id: task_row}, una sola query. Usato
    dalle view che devono mostrare i nomi/agent_mode di molti task in lista
    (es. workflow_live_view) — su DB remoto N round-trip diventano insostenibili.
    """
    tenant_id = _resolve_tenant(tenant_id)
    clean = [int(t) for t in task_ids if str(t).strip().lstrip("-").isdigit()]
    if not clean:
        return {}
    placeholders = ",".join(["%s"] * len(clean))
    args: list[Any] = list(clean)
    where = f"id IN ({placeholders})"
    if tenant_id is not None:
        where += " AND tenant_id = %s"
        args.append(tenant_id)
    with connect() as con:
        rows = con.execute(f"SELECT * FROM tasks WHERE {where}", args).fetchall()
    return {int(r["id"]): _row_to_task(r) for r in rows}


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
                                  target_contact_ids, target_asset_ids,
                                  whatsapp_engine_preference, whatsapp_dry_run,
                                  whatsapp_account_id, whatsapp_api_config_id,
                                  social_account_id,
                                  email_account_id, telegram_bot_id,
                                  recon_mode, recon_social_account_id, recon_hypothesis,
                                  recon_max_targets_per_day, recon_score_threshold,
                                  seed_queries_friends,
                                  input_asset_filter,
                                  output_asset_type,
                                  speed_profile,
                                  outreach_filter_source_task_id,
                                  outreach_filter_source_follower_of,
                                  outreach_filter_tags,
                                  gap_between_dms_min, gap_between_dms_max,
                                  tenant_id, created_by_user_id,
                                  created_at, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id
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
                _dump_list([int(x) for x in (data.get("target_asset_ids") or []) if str(x).strip().lstrip("-").isdigit()]),
                (data.get("whatsapp_engine_preference") or "auto"),
                1 if data.get("whatsapp_dry_run") else 0,
                int(data["whatsapp_account_id"]) if data.get("whatsapp_account_id") else None,
                int(data["whatsapp_api_config_id"]) if data.get("whatsapp_api_config_id") else None,
                int(data["social_account_id"]) if data.get("social_account_id") else None,
                int(data["email_account_id"]) if data.get("email_account_id") else None,
                int(data["telegram_bot_id"]) if data.get("telegram_bot_id") else None,
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
                _coerce_gap_minutes(data.get("gap_between_dms_min")),
                _coerce_gap_minutes(data.get("gap_between_dms_max")),
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
                target_contact_ids = %s, target_asset_ids = %s,
                whatsapp_engine_preference = %s, whatsapp_dry_run = %s,
                whatsapp_account_id = %s, whatsapp_api_config_id = %s,
                social_account_id = %s,
                email_account_id = %s, telegram_bot_id = %s,
                recon_mode = %s, recon_social_account_id = %s, recon_hypothesis = %s,
                recon_max_targets_per_day = %s, recon_score_threshold = %s,
                seed_queries_friends = %s,
                input_asset_filter = %s,
                output_asset_type = %s,
                speed_profile = %s,
                outreach_filter_source_task_id = %s,
                outreach_filter_source_follower_of = %s,
                outreach_filter_tags = %s,
                gap_between_dms_min = %s,
                gap_between_dms_max = %s,
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
                _dump_list([int(x) for x in (data.get("target_asset_ids") or []) if str(x).strip().lstrip("-").isdigit()]),
                (data.get("whatsapp_engine_preference") or "auto"),
                1 if data.get("whatsapp_dry_run") else 0,
                int(data["whatsapp_account_id"]) if data.get("whatsapp_account_id") else None,
                int(data["whatsapp_api_config_id"]) if data.get("whatsapp_api_config_id") else None,
                int(data["social_account_id"]) if data.get("social_account_id") else None,
                int(data["email_account_id"]) if data.get("email_account_id") else None,
                int(data["telegram_bot_id"]) if data.get("telegram_bot_id") else None,
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
                _coerce_gap_minutes(data.get("gap_between_dms_min")),
                _coerce_gap_minutes(data.get("gap_between_dms_max")),
                now_iso(),
                task_id,
                tenant_id,
                tenant_id,
            ),
        )


def update_task_target_asset_ids(task_id: int, asset_ids: list[int], tenant_id: Any = _UNSET) -> None:
    """Patch minimale: aggiorna SOLO `target_asset_ids` di un task (audience
    snapshot). Usato dai handler HTMX add/remove + da `append_qualified_set`."""
    tenant_id = _resolve_tenant(tenant_id)
    clean_ids = [int(x) for x in (asset_ids or []) if str(x).strip().lstrip("-").isdigit()]
    payload = _dump_list(clean_ids)
    with connect() as con:
        if tenant_id is None:
            con.execute(
                "UPDATE tasks SET target_asset_ids = %s, updated_at = %s WHERE id = %s",
                (payload, now_iso(), task_id),
            )
        else:
            con.execute(
                "UPDATE tasks SET target_asset_ids = %s, updated_at = %s "
                "WHERE id = %s AND tenant_id = %s",
                (payload, now_iso(), task_id, tenant_id),
            )


def update_task_target_contact_ids(task_id: int, contact_ids: list[int], tenant_id: Any = _UNSET) -> None:
    """Patch minimale: aggiorna SOLO `target_contact_ids` (legacy audience).
    Usato dal handler 'Promuovi a asset' per svuotare i legacy dopo lo split."""
    tenant_id = _resolve_tenant(tenant_id)
    clean_ids = [int(x) for x in (contact_ids or []) if str(x).strip().lstrip("-").isdigit()]
    payload = _dump_list(clean_ids)
    with connect() as con:
        if tenant_id is None:
            con.execute(
                "UPDATE tasks SET target_contact_ids = %s, updated_at = %s WHERE id = %s",
                (payload, now_iso(), task_id),
            )
        else:
            con.execute(
                "UPDATE tasks SET target_contact_ids = %s, updated_at = %s "
                "WHERE id = %s AND tenant_id = %s",
                (payload, now_iso(), task_id, tenant_id),
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


def list_recent_jobs_for_tasks(
    task_ids: list[int],
    last_n: int = 5,
    tenant_id: Any = _UNSET,
) -> dict[int, list[dict[str, Any]]]:
    """Batch helper: ritorna gli ULTIMI `last_n` job per ciascun task_id, in
    UNA SOLA query (CTE + ROW_NUMBER). Senza questa, la dashboard delle
    liste task farebbe N query separate (una `latest_jobs(task_id)` per ogni
    task), e su DB remoto ogni query costa un round-trip → la pagina /tasks
    diventava lentissima sopra ~10 task. Vedi
    `app.dashboard.compute_task_health_batch`.
    """
    tenant_id = _resolve_tenant(tenant_id)
    clean_ids = [int(t) for t in task_ids if isinstance(t, (int,)) or str(t).strip().lstrip("-").isdigit()]
    if not clean_ids:
        return {}
    placeholders = ",".join(["%s"] * len(clean_ids))
    args: list[Any] = list(clean_ids)
    tenant_clause = ""
    if tenant_id is not None:
        tenant_clause = " AND tenant_id = %s"
        args.append(tenant_id)
    args.append(int(last_n))
    sql = (
        f"SELECT id, task_id, status, started_at, finished_at, error "
        f"FROM ("
        f"  SELECT id, task_id, status, started_at, finished_at, error, "
        f"         ROW_NUMBER() OVER (PARTITION BY task_id ORDER BY id DESC) AS rn "
        f"  FROM jobs WHERE task_id IN ({placeholders}){tenant_clause}"
        f") sub WHERE rn <= %s ORDER BY task_id, id DESC"
    )
    with connect() as con:
        rows = con.execute(sql, args).fetchall()
    out: dict[int, list[dict[str, Any]]] = {tid: [] for tid in clean_ids}
    for r in rows:
        out.setdefault(int(r["task_id"]), []).append(dict(r))
    return out


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

def list_workflows(
    tenant_id: Any = _UNSET,
    created_by_user_id: Any = None,
) -> list[dict[str, Any]]:
    """Lista workflow del tenant. Se `created_by_user_id` valorizzato (int),
    filtra per autore (uso 'I miei workflow')."""
    tenant_id = _resolve_tenant(tenant_id)
    clauses: list[str] = []
    args: list[Any] = []
    if tenant_id is not None:
        clauses.append("tenant_id = %s")
        args.append(tenant_id)
    if created_by_user_id is not None:
        clauses.append("created_by_user_id = %s")
        args.append(int(created_by_user_id))
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    with connect() as con:
        rows = con.execute(
            f"SELECT * FROM workflows{where} ORDER BY id DESC",
            tuple(args),
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


def list_edges_by_workflow_ids(
    workflow_ids: list[int],
) -> dict[int, list[dict[str, Any]]]:
    """Batch helper: ritorna gli edges raggruppati per workflow_id, in UNA
    sola query. Usato dalla route /workflows che senza questo faceva N query.
    Il filtro per tenant è implicito: passa solo workflow_id già filtrati."""
    clean = [int(w) for w in workflow_ids if str(w).strip().lstrip("-").isdigit()]
    if not clean:
        return {}
    placeholders = ",".join(["%s"] * len(clean))
    with connect() as con:
        rows = con.execute(
            f"SELECT * FROM workflow_edges WHERE workflow_id IN ({placeholders}) "
            f"ORDER BY workflow_id, id",
            clean,
        ).fetchall()
    out: dict[int, list[dict[str, Any]]] = {w: [] for w in clean}
    for r in rows:
        out.setdefault(int(r["workflow_id"]), []).append(dict(r))
    return out


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

def upsert_contact(
    data: dict[str, Any],
    *,
    tenant_id: Any = _UNSET,
    created_by_user_id: Any = _UNSET,
) -> int:
    """Insert o update by (email, telegram_username) per evitare duplicati."""
    tenant_id = _resolve_tenant(tenant_id)
    created_by_user_id = _resolve_user(created_by_user_id)
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
             raw_json, status, qualifier_score, notes, asset_id,
             tenant_id, created_by_user_id,
             created_at, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
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
                tenant_id,
                created_by_user_id,
                ts,
                ts,
            ),
        )
        return int(cur.fetchone()['id'])


def get_contact(contact_id: int, tenant_id: Any = _UNSET) -> dict[str, Any] | None:
    tenant_id = _resolve_tenant(tenant_id)
    with connect() as con:
        if tenant_id is None:
            row = con.execute(
                "SELECT * FROM contacts WHERE id = %s", (contact_id,)
            ).fetchone()
        else:
            row = con.execute(
                "SELECT * FROM contacts WHERE id = %s AND tenant_id = %s",
                (contact_id, tenant_id),
            ).fetchone()
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


def list_distinct_tag_keys_for_assets(
    exclude_qualifier_tags: bool = True,
    asset_type: str | None = None,
) -> list[dict[str, Any]]:
    """Tag keys disponibili su asset_tags (count = N asset distinti per key).
    Per default esclude i tag `qualifier_*` e `qualifier_score_*` (sono gestiti
    dalla sidebar qualifier di /qualified, non come filtri tag generici).

    Se `asset_type` valorizzato, restringe ai tag presenti su asset di quel tipo.
    """
    sql = "SELECT t.tag_key AS k, COUNT(DISTINCT t.asset_id) AS n FROM asset_tags t "
    args: list[Any] = []
    if asset_type:
        sql += "JOIN assets a ON a.id = t.asset_id "
    sql += "WHERE 1=1 "
    if exclude_qualifier_tags:
        sql += "AND t.tag_key NOT LIKE 'qualifier\\_%%' ESCAPE '\\' "
    if asset_type:
        sql += "AND a.asset_type = %s "
        args.append(asset_type)
    sql += "GROUP BY t.tag_key ORDER BY n DESC, t.tag_key"
    with connect() as con:
        rows = con.execute(sql, args).fetchall()
    return [{"key": r["k"], "count": int(r["n"])} for r in rows]


def list_distinct_tag_values_for_assets(
    tag_key: str, limit: int = 100, asset_type: str | None = None,
    q: str | None = None,
) -> list[dict[str, Any]]:
    """Valori distinct per una tag_key, con count di asset che la hanno.
    Se `asset_type` valorizzato, restringe agli asset di quel tipo.
    Se `q` valorizzato, filtra i value con LIKE %q% (case-insensitive) —
    usato per typeahead datalist nel widget tag filter (può esserci migliaia
    di value distinti, server-side search evita di trasferirli tutti)."""
    tk = (tag_key or "").strip().lower()
    if not tk:
        return []
    sql = "SELECT t.tag_value AS v, COUNT(DISTINCT t.asset_id) AS n FROM asset_tags t "
    args: list[Any] = []
    if asset_type:
        sql += "JOIN assets a ON a.id = t.asset_id "
    sql += "WHERE t.tag_key = %s "
    args.append(tk)
    if asset_type:
        sql += "AND a.asset_type = %s "
        args.append(asset_type)
    if q:
        q_clean = q.strip()
        if q_clean:
            sql += "AND LOWER(t.tag_value) LIKE LOWER(%s) "
            args.append(f"%{q_clean}%")
    sql += "GROUP BY t.tag_value ORDER BY n DESC, t.tag_value LIMIT %s"
    args.append(int(limit))
    with connect() as con:
        rows = con.execute(sql, args).fetchall()
    return [{"value": r["v"], "count": int(r["n"])} for r in rows]


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
    tenant_id: Any = _UNSET,
) -> int:
    """Conta i contatti che matchano i filtri di list_contacts. Per paginazione UI."""
    tenant_id = _resolve_tenant(tenant_id)
    where_sql, args = _build_contacts_filters(
        status, source_task_id, source_domain, search, channel, score_min
    )
    where_sql, args = _add_source_follower_of_filter(where_sql, args, source_follower_of)
    where_sql, args = _add_contact_tag_filters_clause(where_sql, args, contact_tag_filters)
    if tenant_id is not None:
        where_sql += " AND tenant_id = %s"
        args.append(tenant_id)
    sql = "SELECT COUNT(*) FROM contacts" + where_sql
    with connect() as con:
        row = con.execute(sql, args).fetchone()
        return int(row["count"] if isinstance(row, dict) else row[0])


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
    tenant_id: Any = _UNSET,
) -> list[dict[str, Any]]:
    tenant_id = _resolve_tenant(tenant_id)
    where_sql, args = _build_contacts_filters(
        status, source_task_id, source_domain, search, channel, score_min
    )
    where_sql, args = _add_source_follower_of_filter(where_sql, args, source_follower_of)
    where_sql, args = _add_contact_tag_filters_clause(where_sql, args, contact_tag_filters)
    if tenant_id is not None:
        where_sql += " AND tenant_id = %s"
        args.append(tenant_id)
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
    # Postgres richiede TUTTE le colonne non aggregate in GROUP BY (a differenza di SQLite).
    with connect() as con:
        rows = con.execute(
            "SELECT c.source_task_id AS tid, t.name AS tname, t.agent_mode AS amode, "
            "       COUNT(*) AS n "
            "FROM contacts c LEFT JOIN tasks t ON t.id = c.source_task_id "
            "WHERE c.source_task_id IS NOT NULL "
            "GROUP BY c.source_task_id, t.name, t.agent_mode ORDER BY n DESC"
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


def list_distinct_asset_source_tasks(only_qualified: bool = False) -> list[dict[str, Any]]:
    """Task che hanno generato asset (distinct source_task_id), con count.
    Variante asset-centric di list_distinct_contact_source_tasks.

    Se `only_qualified=True`, conta solo asset qualified per tutti i task
    (utile per popolare il filtro 'Task generatore' nel tab /qualified).
    """
    sql = (
        "SELECT a.source_task_id AS tid, t.name AS tname, t.agent_mode AS amode, "
        "       COUNT(*) AS n "
        "FROM assets a LEFT JOIN tasks t ON t.id = a.source_task_id "
        "WHERE a.source_task_id IS NOT NULL "
    )
    if only_qualified:
        sql += "AND a.status = 'qualified' "
    sql += "GROUP BY a.source_task_id, t.name, t.agent_mode ORDER BY n DESC"
    with connect() as con:
        rows = con.execute(sql).fetchall()
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
        "asset_id",  # link a un asset 'contact' (promote da legacy)
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


def delete_contact(contact_id: int, tenant_id: Any = _UNSET) -> int:
    """Cancella un contatto. Threads e messages cascade-deletono per FK ON DELETE CASCADE."""
    tenant_id = _resolve_tenant(tenant_id)
    with connect() as con:
        if tenant_id is None:
            cur = con.execute("DELETE FROM contacts WHERE id = %s", (contact_id,))
        else:
            cur = con.execute(
                "DELETE FROM contacts WHERE id = %s AND tenant_id = %s",
                (contact_id, tenant_id),
            )
        return cur.rowcount


def delete_contacts_bulk(contact_ids: list[int], tenant_id: Any = _UNSET) -> int:
    tenant_id = _resolve_tenant(tenant_id)
    ids = [int(i) for i in contact_ids if i]
    if not ids:
        return 0
    placeholders = ",".join("%s" for _ in ids)
    with connect() as con:
        if tenant_id is None:
            cur = con.execute(f"DELETE FROM contacts WHERE id IN ({placeholders})", ids)
        else:
            cur = con.execute(
                f"DELETE FROM contacts WHERE id IN ({placeholders}) AND tenant_id = %s",
                ids + [tenant_id],
            )
        return cur.rowcount


# ===========================================================================
# Assets -modello generale per profili/annunci/prodotti/articoli/eventi/...
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
    *,
    tenant_id: Any = _UNSET,
    created_by_user_id: Any = _UNSET,
) -> int:
    """Inserisce o aggiorna un asset.
    Chiave di dedup: source_url_canonical (cross-lingua/paginazione) + asset_type.
    Fallback: source_url letterale se canonical non calcolabile.
    Ritorna asset_id. I tag sostituiscono quelli precedenti.
    """
    tenant_id = _resolve_tenant(tenant_id)
    created_by_user_id = _resolve_user(created_by_user_id)
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
    status_in = data.get("status")
    # Campi outreach (Fase 2A+2D)
    display_name = data.get("display_name")
    email = data.get("email")
    telegram_username = data.get("telegram_username")
    telegram_chat_id = data.get("telegram_chat_id")
    whatsapp = data.get("whatsapp")
    whatsapp_consent = data.get("whatsapp_consent")
    whatsapp_last_inbound_at = data.get("whatsapp_last_inbound_at")
    social_json = data.get("social_json")
    if social_json is None and data.get("social") is not None:
        # Tollera l'alias 'social' con lista/dict -> serializzo a JSON
        sj = data.get("social")
        social_json = sj if isinstance(sj, str) else json.dumps(sj, ensure_ascii=False)
    sitoweb = data.get("sitoweb")
    outreach_status = data.get("outreach_status")
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
            # Update con COALESCE per non sovrascrivere valori esistenti
            # (idempotenza dell'upsert).
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
                  display_name         = COALESCE(%s, display_name),
                  email                = COALESCE(%s, email),
                  telegram_username    = COALESCE(%s, telegram_username),
                  telegram_chat_id     = COALESCE(%s, telegram_chat_id),
                  whatsapp             = COALESCE(%s, whatsapp),
                  whatsapp_consent     = COALESCE(%s, whatsapp_consent),
                  whatsapp_last_inbound_at = COALESCE(%s, whatsapp_last_inbound_at),
                  social_json          = COALESCE(%s, social_json),
                  sitoweb              = COALESCE(%s, sitoweb),
                  outreach_status      = COALESCE(%s, outreach_status),
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
                    display_name,
                    email,
                    telegram_username,
                    telegram_chat_id,
                    whatsapp,
                    whatsapp_consent,
                    whatsapp_last_inbound_at,
                    social_json,
                    sitoweb,
                    outreach_status,
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
                  display_name, email, telegram_username, telegram_chat_id,
                  whatsapp, whatsapp_consent, whatsapp_last_inbound_at,
                  social_json, sitoweb, outreach_status,
                  tenant_id, created_by_user_id, created_at, updated_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, NULL, %s,
                          %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                          %s, %s, %s, %s)
                RETURNING id
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
                    status_in or "new",
                    notes,
                    display_name,
                    email,
                    telegram_username,
                    telegram_chat_id,
                    whatsapp,
                    whatsapp_consent,
                    whatsapp_last_inbound_at,
                    social_json,
                    sitoweb,
                    outreach_status,
                    tenant_id,
                    created_by_user_id,
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
                        "INSERT INTO asset_tags (asset_id, tag_key, tag_value) "
                        "VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
                        (asset_id, tk, tv),
                    )

    # B-016: detection dedup post-insert. Best-effort, errori non bloccano.
    # Import locale per evitare circolarita' (asset_dedup importa db).
    try:
        from .agent.asset_dedup import find_dedup_candidates
        find_dedup_candidates(asset_id, tenant_id=tenant_id)
    except Exception as _e:
        # Logged ma non solleva: dedup e' opt-in, l'upsert riesce comunque.
        import logging as _log
        _log.getLogger(__name__).debug("dedup detection skipped for %s: %s", asset_id, _e)

    return asset_id


def get_asset(asset_id: int, tenant_id: Any = _UNSET) -> dict[str, Any] | None:
    tenant_id = _resolve_tenant(tenant_id)
    with connect() as con:
        if tenant_id is None:
            row = con.execute("SELECT * FROM assets WHERE id = %s", (asset_id,)).fetchone()
        else:
            row = con.execute(
                "SELECT * FROM assets WHERE id = %s AND tenant_id = %s",
                (asset_id, tenant_id),
            ).fetchone()
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
    search: str | None = None,
    tenant_id: Any = _UNSET,
    tag_mode: str = "and",
    tag_expr: str | None = None,
    has_contacts: bool = False,
    has_social: bool = False,
) -> int:
    """Conta gli asset matchando gli stessi filtri di `list_assets`.

    Riusa `_qualified_assets_where_clause` con qualifier_slugs vuoto, cosi'
    /assets e /qualified hanno la stessa grammatica di tag (and/or/custom),
    la stessa search scope, e gli stessi flag has_contacts/has_social.
    """
    tenant_id = _resolve_tenant(tenant_id)
    where, args = _qualified_assets_where_clause(
        qualifier_slugs=[],
        status_filter="qualified",  # no-op senza slug
        score_min=None,
        asset_type=asset_type,
        source_task_id=source_task_id,
        search=search,
        extra_tag_filters=tag_filters,
        tenant_id=tenant_id,
        tag_mode=tag_mode,
        tag_expr=tag_expr,
        has_contacts=has_contacts,
        has_social=has_social,
        asset_status_col=status,
    )
    sql = "SELECT COUNT(*) FROM assets a" + where
    with connect() as con:
        row = con.execute(sql, args).fetchone()
        return int(row["count"] if isinstance(row, dict) else row[0])


def list_assets(
    asset_type: str | None = None,
    status: str | None = None,
    source_task_id: int | None = None,
    tag_filters: list[tuple[str, str]] | None = None,
    search: str | None = None,
    limit: int = 200,
    offset: int = 0,
    tenant_id: Any = _UNSET,
    tag_mode: str = "and",
    tag_expr: str | None = None,
    has_contacts: bool = False,
    has_social: bool = False,
) -> list[dict[str, Any]]:
    """Lista asset (con tag combine and/or/custom, has_*). Helper condiviso
    con list_qualified_assets via `_qualified_assets_where_clause`."""
    tenant_id = _resolve_tenant(tenant_id)
    where, args = _qualified_assets_where_clause(
        qualifier_slugs=[],
        status_filter="qualified",
        score_min=None,
        asset_type=asset_type,
        source_task_id=source_task_id,
        search=search,
        extra_tag_filters=tag_filters,
        tenant_id=tenant_id,
        tag_mode=tag_mode,
        tag_expr=tag_expr,
        has_contacts=has_contacts,
        has_social=has_social,
        asset_status_col=status,
    )
    sql = "SELECT a.* FROM assets a" + where + " ORDER BY a.id DESC LIMIT %s OFFSET %s"
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
    # Filtra solo campi modificabili in edit manuale + canali outreach
    ALLOWED = {
        "asset_type", "source_url", "source_domain", "title", "raw_json", "notes", "status",
        # Canali contatto (Fase 2A) -ora extracted da contacts
        "display_name", "email",
        "telegram_username", "telegram_chat_id",
        "whatsapp", "whatsapp_consent", "whatsapp_last_inbound_at",
        "social_json", "sitoweb", "outreach_status",
    }
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
                "INSERT INTO asset_tags (asset_id, tag_key, tag_value) "
                "VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
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
                "INSERT INTO asset_tags (asset_id, tag_key, tag_value) "
                "VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
                (asset_id, tag_key, tag_value),
            )
            return True
    except Exception:
        return False


def delete_asset(asset_id: int, tenant_id: Any = _UNSET) -> int:
    tenant_id = _resolve_tenant(tenant_id)
    with connect() as con:
        if tenant_id is None:
            cur = con.execute("DELETE FROM assets WHERE id = %s", (asset_id,))
        else:
            cur = con.execute(
                "DELETE FROM assets WHERE id = %s AND tenant_id = %s",
                (asset_id, tenant_id),
            )
        return cur.rowcount


def delete_assets_bulk(asset_ids: list[int], tenant_id: Any = _UNSET) -> int:
    """Cancella in massa gli asset indicati. Le `asset_tags` cascade-deletono per FK."""
    tenant_id = _resolve_tenant(tenant_id)
    ids = [int(i) for i in asset_ids if i]
    if not ids:
        return 0
    placeholders = ",".join("%s" for _ in ids)
    with connect() as con:
        if tenant_id is None:
            cur = con.execute(f"DELETE FROM assets WHERE id IN ({placeholders})", ids)
        else:
            cur = con.execute(
                f"DELETE FROM assets WHERE id IN ({placeholders}) AND tenant_id = %s",
                ids + [tenant_id],
            )
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


# ===========================================================================
# Asset outreach API (Fase 2D — asset-centric, sostituisce contact-centric)
# ===========================================================================

def update_asset_outreach_status(asset_id: int, status: str, notes: str | None = None) -> None:
    """Aggiorna `assets.outreach_status`. Valori validi: pending/contacted/replied/optedout.
    Equivalente di update_contact_status ma sul campo dedicato outreach (non sovrascrive
    `assets.status` che resta scrape/qualifier-status)."""
    if status not in ("pending", "contacted", "replied", "optedout"):
        raise ValueError(f"outreach_status invalido: {status!r}")
    ts = now_iso()
    with connect() as con:
        if notes is not None:
            con.execute(
                "UPDATE assets SET outreach_status = %s, notes = %s, updated_at = %s WHERE id = %s",
                (status, notes, ts, asset_id),
            )
        else:
            con.execute(
                "UPDATE assets SET outreach_status = %s, updated_at = %s WHERE id = %s",
                (status, ts, asset_id),
            )


def update_asset_whatsapp_consent(asset_id: int, consent: str) -> None:
    """Aggiorna `assets.whatsapp_consent`. Valori validi: cold|opt_in|optedout."""
    if consent not in ("cold", "opt_in", "optedout"):
        raise ValueError(f"whatsapp_consent invalido: {consent!r}")
    with connect() as con:
        con.execute(
            "UPDATE assets SET whatsapp_consent = %s, updated_at = %s WHERE id = %s",
            (consent, now_iso(), asset_id),
        )


def touch_asset_whatsapp_inbound(asset_id: int) -> None:
    """Marca che il destinatario ha appena scritto al business number — abilita
    la 24h-window per messaggi free-form (Motore B WhatsApp)."""
    ts = now_iso()
    with connect() as con:
        con.execute(
            "UPDATE assets SET whatsapp_last_inbound_at = %s, updated_at = %s WHERE id = %s",
            (ts, ts, asset_id),
        )


def find_asset_by_email(email: str, tenant_id: Any = _UNSET) -> dict[str, Any] | None:
    tenant_id = _resolve_tenant(tenant_id)
    sql = "SELECT * FROM assets WHERE LOWER(email) = %s"
    args: list[Any] = [email.strip().lower()]
    if tenant_id is not None:
        sql += " AND tenant_id = %s"
        args.append(tenant_id)
    sql += " ORDER BY id DESC LIMIT 1"
    with connect() as con:
        row = con.execute(sql, args).fetchone()
    return dict(row) if row else None


def find_asset_by_telegram_chat(chat_id: str, tenant_id: Any = _UNSET) -> dict[str, Any] | None:
    tenant_id = _resolve_tenant(tenant_id)
    sql = "SELECT * FROM assets WHERE telegram_chat_id = %s"
    args: list[Any] = [str(chat_id)]
    if tenant_id is not None:
        sql += " AND tenant_id = %s"
        args.append(tenant_id)
    sql += " ORDER BY id DESC LIMIT 1"
    with connect() as con:
        row = con.execute(sql, args).fetchone()
    return dict(row) if row else None


def set_asset_telegram_chat(asset_id: int, chat_id: str) -> None:
    with connect() as con:
        con.execute(
            "UPDATE assets SET telegram_chat_id = %s, updated_at = %s WHERE id = %s",
            (str(chat_id), now_iso(), asset_id),
        )


def get_assets_by_ids(ids: list[int], tenant_id: Any = _UNSET) -> list[dict[str, Any]]:
    """Recupera asset per lista di ID. Filtra per tenant se settato. No filtro su status."""
    if not ids:
        return []
    tenant_id = _resolve_tenant(tenant_id)
    norm = [int(i) for i in ids if i]
    if not norm:
        return []
    placeholders = ",".join("%s" for _ in norm)
    sql = f"SELECT * FROM assets WHERE id IN ({placeholders})"
    args: list[Any] = list(norm)
    if tenant_id is not None:
        sql += " AND tenant_id = %s"
        args.append(tenant_id)
    sql += " ORDER BY id DESC"
    with connect() as con:
        rows = con.execute(sql, args).fetchall()
    return [dict(r) for r in rows]


def list_assets_with_social_platform(
    platform: str, limit: int = 500, tenant_id: Any = _UNSET,
) -> list[dict[str, Any]]:
    """Asset con `social_json` contenente almeno un entry per `platform`.

    Esclude asset con outreach_status='optedout'. Filtro fatto in Python perché
    social_json è opaco a SQL (vedi list_contacts_with_social_platform legacy)."""
    plat = (platform or "").strip().lower()
    if plat not in ("instagram", "tiktok", "facebook"):
        return []
    tenant_id = _resolve_tenant(tenant_id)
    sql = (
        "SELECT * FROM assets "
        "WHERE social_json IS NOT NULL AND social_json != '' "
        "AND (outreach_status IS NULL OR outreach_status != 'optedout')"
    )
    args: list[Any] = []
    if tenant_id is not None:
        sql += " AND tenant_id = %s"
        args.append(tenant_id)
    sql += " ORDER BY id DESC LIMIT %s"
    args.append(max(1, int(limit)))
    with connect() as con:
        rows = con.execute(sql, args).fetchall()
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


def list_assets_with_whatsapp(limit: int = 500, tenant_id: Any = _UNSET) -> list[dict[str, Any]]:
    """Asset con campo `whatsapp` popolato (esclusi optedout)."""
    tenant_id = _resolve_tenant(tenant_id)
    sql = (
        "SELECT * FROM assets "
        "WHERE whatsapp IS NOT NULL AND whatsapp != '' "
        "AND (whatsapp_consent IS NULL OR whatsapp_consent != 'optedout') "
        "AND (outreach_status IS NULL OR outreach_status != 'optedout')"
    )
    args: list[Any] = []
    if tenant_id is not None:
        sql += " AND tenant_id = %s"
        args.append(tenant_id)
    sql += " ORDER BY id DESC LIMIT %s"
    args.append(max(1, int(limit)))
    with connect() as con:
        rows = con.execute(sql, args).fetchall()
    return [dict(r) for r in rows]


def _add_asset_tag_filters_clause(
    sql: str, args: list, tag_filters: list | None
) -> tuple[str, list]:
    """EXISTS-based tag filter su asset_tags. Multipli filtri in AND.
    `tag_filters` accetta list[tuple|dict] con (key, value)."""
    if not tag_filters:
        return sql, args
    for tf in tag_filters:
        if isinstance(tf, dict):
            k, v = tf.get("key"), tf.get("value")
        else:
            k, v = tf[0], tf[1]
        if not k or not v:
            continue
        sql += (
            " AND EXISTS (SELECT 1 FROM asset_tags t "
            "WHERE t.asset_id = assets.id AND t.tag_key = %s AND t.tag_value = %s)"
        )
        args.extend([str(k).lower(), str(v)])
    return sql, args


def list_assets_for_email_outreach(
    status: str | None = None,
    source_task_id: int | None = None,
    source_domain: str | None = None,
    search: str | None = None,
    score_min: int | None = None,
    only_qualified: bool = True,
    exclude_optedout: bool = True,
    exclude_contacted: bool = True,
    limit: int = 500,
    offset: int = 0,
    asset_tag_filters: list | None = None,
    tenant_id: Any = _UNSET,
) -> list[dict[str, Any]]:
    """Asset idonei a outreach_email/telegram. Filtra su `assets.email` o
    `assets.telegram_username` valorizzati; outreach_status != 'optedout';
    se only_qualified=True richiede assets.status='qualified'.

    Equivalente moderno di list_contacts(channel=...) ma legge da assets.
    Per filtrare per canale specifico passa search o asset_tag_filters."""
    tenant_id = _resolve_tenant(tenant_id)
    sql = "SELECT * FROM assets WHERE 1=1"
    args: list[Any] = []
    # Almeno un canale email/telegram presente (default per email outreach)
    sql += (
        " AND ((email IS NOT NULL AND email != '') "
        "OR (telegram_username IS NOT NULL AND telegram_username != ''))"
    )
    if only_qualified:
        sql += " AND status = 'qualified'"
    if exclude_optedout:
        sql += " AND (outreach_status IS NULL OR outreach_status != 'optedout')"
    if exclude_contacted:
        sql += " AND (outreach_status IS NULL OR outreach_status != 'contacted')"
    if status:
        sql += " AND outreach_status = %s"
        args.append(status)
    if source_task_id is not None:
        sql += " AND source_task_id = %s"
        args.append(source_task_id)
    if source_domain:
        sql += " AND source_domain = %s"
        args.append(source_domain)
    if search:
        like = f"%{search.lower()}%"
        sql += " AND (LOWER(title) LIKE %s OR LOWER(display_name) LIKE %s OR LOWER(email) LIKE %s)"
        args.extend([like, like, like])
    if score_min is not None:
        sql += " AND qualifier_score >= %s"
        args.append(int(score_min))
    sql, args = _add_asset_tag_filters_clause(sql, args, asset_tag_filters)
    if tenant_id is not None:
        sql += " AND tenant_id = %s"
        args.append(tenant_id)
    sql += " ORDER BY id DESC LIMIT %s OFFSET %s"
    args.extend([limit, max(0, int(offset))])
    with connect() as con:
        rows = con.execute(sql, args).fetchall()
    return [dict(r) for r in rows]


def list_assets_for_whatsapp_outreach(
    only_qualified: bool = True,
    exclude_optedout: bool = True,
    exclude_contacted: bool = True,
    limit: int = 1000,
    asset_tag_filters: list | None = None,
    tenant_id: Any = _UNSET,
) -> list[dict[str, Any]]:
    """Asset idonei a outreach_whatsapp. Equivalente moderno di
    list_contacts_for_whatsapp_outreach."""
    tenant_id = _resolve_tenant(tenant_id)
    sql = "SELECT * FROM assets WHERE whatsapp IS NOT NULL AND whatsapp != ''"
    args: list[Any] = []
    if only_qualified:
        sql += " AND status = 'qualified'"
    if exclude_optedout:
        sql += (
            " AND (whatsapp_consent IS NULL OR whatsapp_consent != 'optedout')"
            " AND (outreach_status IS NULL OR outreach_status != 'optedout')"
        )
    if exclude_contacted:
        sql += " AND (outreach_status IS NULL OR outreach_status != 'contacted')"
    sql, args = _add_asset_tag_filters_clause(sql, args, asset_tag_filters)
    if tenant_id is not None:
        sql += " AND tenant_id = %s"
        args.append(tenant_id)
    sql += " ORDER BY id DESC LIMIT %s"
    args.append(max(1, int(limit)))
    with connect() as con:
        rows = con.execute(sql, args).fetchall()
    return [dict(r) for r in rows]


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
# Qualifier view helpers (Fase 1 /qualified tab)
# ===========================================================================
# Mostrare asset filtrati per qualifier (vista derivata da asset_tags).
# I tag rilevanti hanno la forma:
#   tag_key = 'qualifier_<slug>'        tag_value = 'qualified' | 'rejected'
#   tag_key = 'qualifier_score_<slug>'  tag_value = '0'..'10'  (cast SQL → int)
# Lo slug è derivato dal nome del task qualifier (runner_qualifier._qualifier_slug).

def list_distinct_qualifier_slugs(tenant_id: Any = _UNSET) -> list[dict[str, Any]]:
    """Ritorna gli slug dei qualifier eseguiti, con friendly name + count.

    Per ogni `tag_key` del tipo `qualifier_<slug>` (esclusi i `qualifier_score_<slug>`):
    - `slug`: lo slug "tecnico" (es. `qualifica_appassionati_palestra`)
    - `friendly_name`: il task.name corrispondente (best-effort: cerchiamo un
      task il cui `_qualifier_slug` matcha questo slug). Se non trovato → derivato
      dallo slug stesso (es. "Qualifica Appassionati Palestra").
    - `count_qualified`, `count_rejected`: conteggio asset taggati per ciascuno
      status, filtrato per il tenant corrente.

    Ordinato per count_qualified DESC.
    """
    tenant_id = _resolve_tenant(tenant_id)

    sql = (
        "SELECT t.tag_key, t.tag_value, COUNT(DISTINCT a.id) AS n "
        "FROM asset_tags t JOIN assets a ON a.id = t.asset_id "
        "WHERE t.tag_key LIKE 'qualifier_%%' "
        "  AND t.tag_key NOT LIKE 'qualifier_score_%%' "
    )
    args: list[Any] = []
    if tenant_id is not None:
        sql += " AND a.tenant_id = %s"
        args.append(tenant_id)
    sql += " GROUP BY t.tag_key, t.tag_value"

    counts: dict[str, dict[str, int]] = {}
    with connect() as con:
        for r in con.execute(sql, args).fetchall():
            slug = r["tag_key"][len("qualifier_"):]
            val = r["tag_value"]
            counts.setdefault(slug, {"qualified": 0, "rejected": 0})
            if val in ("qualified", "rejected"):
                counts[slug][val] = int(r["n"])

        # Friendly name: prova a recuperare il task.name di un task qualifier
        # il cui slug derivato matcha. Heuristica: confronta lo slug calcolato
        # da `_qualifier_slug` se importabile, altrimenti fallback su normalizzazione
        # snake_case di task.name LIKE pattern.
        # Per evitare import circolari + complicazioni, faccio query semplice:
        # task.name normalizzata == slug del tag.
        # Fallback: titlecase dello slug.
        friendly: dict[str, str] = {}
        objective: dict[str, str] = {}
        task_ids: dict[str, int] = {}
        slugs = list(counts.keys())
        if slugs:
            # Recupero tutti i task qualifier-like (agent_mode='qualifier')
            # con anche l'objective per le card descrittive.
            tasks_rows = con.execute(
                "SELECT id, name, objective FROM tasks WHERE agent_mode = 'qualifier'"
                + (" AND tenant_id = %s" if tenant_id is not None else ""),
                ([tenant_id] if tenant_id is not None else []),
            ).fetchall()
        else:
            tasks_rows = []

    # Replica leggera di _qualifier_slug (runner_qualifier.py):
    # snake_case di task.name, max 40 char.
    import re as _re
    def _slug_of(name: str) -> str:
        s = _re.sub(r"[^a-z0-9]+", "_", (name or "").strip().lower()).strip("_")
        return s[:40]

    for t in tasks_rows:
        s = _slug_of(t["name"])
        if s and s not in friendly:
            friendly[s] = t["name"]
            objective[s] = (t.get("objective") or "").strip()
            task_ids[s] = int(t["id"])

    out: list[dict[str, Any]] = []
    for slug, c in counts.items():
        out.append({
            "slug": slug,
            "friendly_name": friendly.get(slug, slug.replace("_", " ").title()),
            "task_objective": objective.get(slug, ""),
            "task_id": task_ids.get(slug),
            "count_qualified": c.get("qualified", 0),
            "count_rejected": c.get("rejected", 0),
        })
    out.sort(key=lambda x: (-x["count_qualified"], x["slug"]))
    return out


def _qualified_assets_where_clause(
    qualifier_slugs: list[str],
    status_filter: str,
    score_min: int | None,
    asset_type: str | None,
    source_task_id: int | None,
    search: str | None,
    extra_tag_filters: list[tuple[str, str]] | None,
    tenant_id: int | None,
    tag_mode: str = "and",
    tag_expr: str | None = None,
    has_contacts: bool = False,
    has_social: bool = False,
    asset_status_col: str | None = None,
) -> tuple[str, list[Any]]:
    """Costruisce WHERE+args condivisi da list_assets / list_qualified_assets
    (e relative count_*). Tabella aliasata come `a`.

    Parametri qualifier-specific (NO-OP per /assets generic):
      - `qualifier_slugs`: se vuota/None, skip del filtro qualifier_<slug>.
      - `score_min`: applicato a TUTTI i qualifier in lista; ignorato senza slug.
      - `status_filter`: 'qualified'|'rejected'|'both' — solo per qualifier tag.

    `asset_status_col`: filtra `assets.status` (colonna SQL, non tag). Usato
    da /assets per filtrare per stato del record asset (new/processed/...).

    `tag_mode` (and|or|custom): combinazione di `extra_tag_filters`:
      - and: tutti AND (un EXISTS per ciascuno) — default retrocompat
      - or: legati con OR
      - custom: usa `tag_expr` (es. '(F1 AND F2) OR F3') via
        app.agent.tag_expr.build_where_clause.

    `search`: LIKE %q% su tutti gli identificatori comuni: title, source_url,
    source_domain, notes, display_name, email, whatsapp, telegram_username,
    social_json + tag_value (via EXISTS). Cosi' /assets e /qualified hanno la
    stessa search scope.
    """
    where = " WHERE 1=1"
    args: list[Any] = []

    # Multi-qualifier AND: ogni slug deve avere il tag con status richiesto
    for slug in qualifier_slugs:
        where += (
            " AND EXISTS (SELECT 1 FROM asset_tags qt WHERE qt.asset_id = a.id "
            " AND qt.tag_key = %s AND qt.tag_value = %s)"
        )
        args.extend([f"qualifier_{slug}", status_filter])
        if score_min is not None:
            where += (
                " AND EXISTS (SELECT 1 FROM asset_tags qs WHERE qs.asset_id = a.id "
                " AND qs.tag_key = %s "
                " AND qs.tag_value ~ '^[0-9]+$' "
                " AND CAST(qs.tag_value AS INTEGER) >= %s)"
            )
            args.extend([f"qualifier_score_{slug}", int(score_min)])

    if asset_type:
        where += " AND a.asset_type = %s"
        args.append(asset_type)
    if asset_status_col:
        where += " AND a.status = %s"
        args.append(asset_status_col)
    if source_task_id is not None:
        where += " AND a.source_task_id = %s"
        args.append(source_task_id)
    if search:
        # Unified search scope: identificatori asset + contatti + tag value.
        # Allinea /assets e /qualified — un termine cercato matcha gli stessi
        # campi in entrambe le pagine.
        like = f"%{search.strip().lower()}%"
        where += (
            " AND ("
            "LOWER(COALESCE(a.title, '')) LIKE %s"
            " OR LOWER(COALESCE(a.source_url, '')) LIKE %s"
            " OR LOWER(COALESCE(a.source_domain, '')) LIKE %s"
            " OR LOWER(COALESCE(a.notes, '')) LIKE %s"
            " OR LOWER(COALESCE(a.display_name, '')) LIKE %s"
            " OR LOWER(COALESCE(a.email, '')) LIKE %s"
            " OR LOWER(COALESCE(a.whatsapp, '')) LIKE %s"
            " OR LOWER(COALESCE(a.telegram_username, '')) LIKE %s"
            " OR LOWER(COALESCE(a.social_json, '')) LIKE %s"
            " OR EXISTS (SELECT 1 FROM asset_tags st WHERE st.asset_id = a.id"
            "            AND LOWER(st.tag_value) LIKE %s))"
        )
        args.extend([like] * 10)
    if extra_tag_filters:
        from .agent.tag_expr import build_where_clause as _tag_build
        norm = [(str(k).lower(), str(v)) for (k, v) in extra_tag_filters]
        frag, frag_args = _tag_build(
            norm, mode=(tag_mode or "and"), expr=tag_expr,
        )
        if frag:
            where += " AND " + frag
            args.extend(frag_args)
    if tenant_id is not None:
        where += " AND a.tenant_id = %s"
        args.append(tenant_id)
    # has_contacts: almeno uno fra email, whatsapp, telegram_username|telegram_chat_id valorizzato
    if has_contacts:
        where += (
            " AND ("
            "(a.email IS NOT NULL AND a.email <> '') "
            "OR (a.whatsapp IS NOT NULL AND a.whatsapp <> '') "
            "OR (a.telegram_username IS NOT NULL AND a.telegram_username <> '') "
            "OR (a.telegram_chat_id IS NOT NULL AND a.telegram_chat_id <> '')"
            ")"
        )
    # has_social: social_json valorizzato con almeno una entry
    if has_social:
        where += " AND a.social_json IS NOT NULL AND a.social_json <> '' AND a.social_json <> '[]'"
    return where, args


def list_qualified_assets(
    qualifier_slugs: list[str] | None = None,
    status_filter: str = "qualified",
    score_min: int | None = None,
    asset_type: str | None = None,
    source_task_id: int | None = None,
    search: str | None = None,
    extra_tag_filters: list[tuple[str, str]] | None = None,
    limit: int = 100,
    offset: int = 0,
    tenant_id: Any = _UNSET,
    tag_mode: str = "and",
    tag_expr: str | None = None,
    has_contacts: bool = False,
    has_social: bool = False,
) -> list[dict[str, Any]]:
    """Lista asset filtrati per qualifier (AND multipli) + score min + filtri extra.

    `qualifier_slugs`: lista di slug `qualifier_<slug>` (senza prefisso 'qualifier_').
      Se vuota o None → no filtro qualifier (mostra tutti gli asset).
    `status_filter`: 'qualified' (default), 'rejected', o 'both' per OR.
    `score_min`: applicato a TUTTI i qualifier selezionati (semantica AND).
    `tag_mode`/`tag_expr`: vedi _qualified_assets_where_clause.
    """
    tenant_id = _resolve_tenant(tenant_id)
    slugs = [s for s in (qualifier_slugs or []) if s]
    if status_filter not in ("qualified", "rejected", "both"):
        status_filter = "qualified"

    # Se 'both', genero 2 chiamate separate (qualified + rejected) e UNION
    if slugs and status_filter == "both":
        rows_q = list_qualified_assets(
            slugs, "qualified", score_min, asset_type, source_task_id, search,
            extra_tag_filters, limit, offset, tenant_id=tenant_id,
            tag_mode=tag_mode, tag_expr=tag_expr,
            has_contacts=has_contacts, has_social=has_social,
        )
        rows_r = list_qualified_assets(
            slugs, "rejected", score_min, asset_type, source_task_id, search,
            extra_tag_filters, limit, offset, tenant_id=tenant_id,
            tag_mode=tag_mode, tag_expr=tag_expr,
            has_contacts=has_contacts, has_social=has_social,
        )
        # merge + dedup per id, limit
        seen: set[int] = set()
        merged: list[dict[str, Any]] = []
        for r in rows_q + rows_r:
            if r["id"] not in seen:
                seen.add(r["id"])
                merged.append(r)
        return merged[:limit]

    where, args = _qualified_assets_where_clause(
        slugs, status_filter, score_min, asset_type, source_task_id, search,
        extra_tag_filters, tenant_id, tag_mode=tag_mode, tag_expr=tag_expr,
        has_contacts=has_contacts, has_social=has_social,
    )
    sql = (
        "SELECT a.* FROM assets a"
        + where
        + " ORDER BY a.id DESC LIMIT %s OFFSET %s"
    )
    args.extend([limit, max(0, int(offset))])
    with connect() as con:
        rows = con.execute(sql, args).fetchall()
        results = [dict(r) for r in rows]
        if results:
            ids = [r["id"] for r in results]
            placeholders = ",".join(["%s"] * len(ids))
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


def count_qualified_assets(
    qualifier_slugs: list[str] | None = None,
    status_filter: str = "qualified",
    score_min: int | None = None,
    asset_type: str | None = None,
    source_task_id: int | None = None,
    search: str | None = None,
    extra_tag_filters: list[tuple[str, str]] | None = None,
    tenant_id: Any = _UNSET,
    tag_mode: str = "and",
    tag_expr: str | None = None,
    has_contacts: bool = False,
    has_social: bool = False,
) -> int:
    """Count con gli stessi filtri di list_qualified_assets. Per paginazione."""
    tenant_id = _resolve_tenant(tenant_id)
    slugs = [s for s in (qualifier_slugs or []) if s]
    if status_filter not in ("qualified", "rejected", "both"):
        status_filter = "qualified"
    if slugs and status_filter == "both":
        # Approssimazione: per il count basta unire qualified + rejected
        # cardinalità è "distinct asset". Usiamo UNION via subquery.
        where_q, args_q = _qualified_assets_where_clause(
            slugs, "qualified", score_min, asset_type, source_task_id, search,
            extra_tag_filters, tenant_id, tag_mode=tag_mode, tag_expr=tag_expr,
        )
        where_r, args_r = _qualified_assets_where_clause(
            slugs, "rejected", score_min, asset_type, source_task_id, search,
            extra_tag_filters, tenant_id, tag_mode=tag_mode, tag_expr=tag_expr,
        )
        sql = (
            f"SELECT COUNT(*) FROM ("
            f"  SELECT a.id FROM assets a {where_q} "
            f"  UNION "
            f"  SELECT a.id FROM assets a {where_r}"
            f") sub"
        )
        with connect() as con:
            row = con.execute(sql, args_q + args_r).fetchone()
            return int(row["count"] if isinstance(row, dict) else row[0])

    where, args = _qualified_assets_where_clause(
        slugs, status_filter, score_min, asset_type, source_task_id, search,
        extra_tag_filters, tenant_id, tag_mode=tag_mode, tag_expr=tag_expr,
        has_contacts=has_contacts, has_social=has_social,
    )
    sql = "SELECT COUNT(*) FROM assets a" + where
    with connect() as con:
        row = con.execute(sql, args).fetchone()
        return int(row["count"] if isinstance(row, dict) else row[0])


# ===========================================================================
# Site patterns -memoria pattern URL "target" per dominio
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
# Site playbooks (Stage 2 -knowledge transfer cross-runner)
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
                transferable, status, hits, successes, failures, created_at, updated_at)
               VALUES (%s, %s, %s, %s, %s, %s, 'active', 0, 0, 0, %s, %s) RETURNING id""",
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
    contact_id: int | None = None,
    channel: str = "",
    external_id: str | None = None,
    subject: str | None = None,
    task_id: int | None = None,
    *,
    asset_id: int | None = None,
    tenant_id: Any = _UNSET,
) -> int:
    """Crea (o ritrova) un thread di conversazione. Asset-centric.

    Almeno uno fra `asset_id` e `contact_id` deve essere valorizzato. Durante
    la transizione Fase 2D entrambi i campi vengono scritti su `threads` se
    risolvibili (asset_id derivato da contacts.asset_id se serve).
    """
    if asset_id is None and contact_id is None:
        raise ValueError("get_or_create_thread richiede asset_id o contact_id")
    tenant_id = _resolve_tenant(tenant_id)
    with connect() as con:
        # 1) Match per external_id (idempotenza inbound)
        if external_id:
            row = con.execute(
                "SELECT id FROM threads WHERE channel = %s AND external_id = %s LIMIT 1",
                (channel, external_id),
            ).fetchone()
            if row:
                return int(row["id"])
        # 2) Fallback su thread aperto stesso destinatario/canale.
        #    Preferenza asset_id se disponibile.
        if asset_id is not None:
            row = con.execute(
                "SELECT id FROM threads WHERE asset_id = %s AND channel = %s AND status='open' "
                "ORDER BY id DESC LIMIT 1",
                (asset_id, channel),
            ).fetchone()
        else:
            row = con.execute(
                "SELECT id FROM threads WHERE contact_id = %s AND channel = %s AND status='open' "
                "ORDER BY id DESC LIMIT 1",
                (contact_id, channel),
            ).fetchone()
        if row:
            return int(row["id"])
        # 3) Backfill mancanti: se ho solo contact_id, deduco asset_id da contacts
        if asset_id is None and contact_id is not None:
            r = con.execute(
                "SELECT asset_id FROM contacts WHERE id = %s", (contact_id,)
            ).fetchone()
            if r and r["asset_id"]:
                asset_id = int(r["asset_id"])
        # NOT NULL su channel (rimasto NOT NULL nello schema)
        if not channel:
            raise ValueError("channel obbligatorio")
        cur = con.execute(
            """
            INSERT INTO threads (contact_id, asset_id, channel, external_id, subject, status,
                                 task_id, tenant_id, created_at)
            VALUES (%s, %s, %s, %s, %s, 'open', %s, %s, %s) RETURNING id
            """,
            (contact_id, asset_id, channel, external_id, subject, task_id, tenant_id, now_iso()),
        )
        return int(cur.fetchone()['id'])


def get_thread(thread_id: int, tenant_id: Any = _UNSET) -> dict[str, Any] | None:
    tenant_id = _resolve_tenant(tenant_id)
    with connect() as con:
        if tenant_id is None:
            row = con.execute(
                "SELECT * FROM threads WHERE id = %s", (thread_id,)
            ).fetchone()
        else:
            row = con.execute(
                "SELECT * FROM threads WHERE id = %s AND tenant_id = %s",
                (thread_id, tenant_id),
            ).fetchone()
    return dict(row) if row else None


def list_threads(
    channel: str | None = None,
    status: str | None = None,
    task_id: int | None = None,
    limit: int = 200,
    tenant_id: Any = _UNSET,
) -> list[dict[str, Any]]:
    tenant_id = _resolve_tenant(tenant_id)
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
    if tenant_id is not None:
        sql += " AND t.tenant_id = %s"
        args.append(tenant_id)
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
    *,
    tenant_id: Any = _UNSET,
) -> int:
    tenant_id = _resolve_tenant(tenant_id)
    with connect() as con:
        cur = con.execute(
            """
            INSERT INTO messages (thread_id, direction, body, llm_generated, external_id,
                                  status, error, sent_at, tenant_id, created_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id
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
                tenant_id,
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


def list_messages(thread_id: int, tenant_id: Any = _UNSET) -> list[dict[str, Any]]:
    tenant_id = _resolve_tenant(tenant_id)
    with connect() as con:
        if tenant_id is None:
            rows = con.execute(
                "SELECT * FROM messages WHERE thread_id = %s ORDER BY id",
                (thread_id,),
            ).fetchall()
        else:
            rows = con.execute(
                "SELECT * FROM messages WHERE thread_id = %s AND tenant_id = %s ORDER BY id",
                (thread_id, tenant_id),
            ).fetchall()
    return [dict(r) for r in rows]


def find_unprocessed_inbound(tenant_id: Any = _UNSET) -> list[dict[str, Any]]:
    """Messaggi inbound senza una reply outbound successiva nello stesso thread."""
    tenant_id = _resolve_tenant(tenant_id)
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
    """
    args: list = []
    if tenant_id is not None:
        sql += " AND m.tenant_id = %s"
        args.append(tenant_id)
    sql += " ORDER BY m.created_at"
    with connect() as con:
        rows = con.execute(sql, args).fetchall()
    return [dict(r) for r in rows]


# ===========================================================================
# Channel config (singleton per channel)
# ===========================================================================

def get_channel_config(channel: str, tenant_id: Any = _UNSET) -> dict[str, Any] | None:
    tenant_id = _resolve_tenant(tenant_id)
    with connect() as con:
        if tenant_id is None:
            row = con.execute(
                "SELECT * FROM channel_config WHERE channel = %s", (channel,)
            ).fetchone()
        else:
            row = con.execute(
                "SELECT * FROM channel_config WHERE channel = %s AND tenant_id = %s",
                (channel, tenant_id),
            ).fetchone()
    if not row:
        return None
    d = dict(row)
    try:
        d["config"] = json.loads(d.get("config_json") or "{}")
    except json.JSONDecodeError:
        d["config"] = {}
    return d


def save_channel_config(
    channel: str, config: dict[str, Any], enabled: bool, tenant_id: Any = _UNSET
) -> None:
    tenant_id = _resolve_tenant(tenant_id)
    payload = json.dumps(config)
    with connect() as con:
        # NOTA: channel è PRIMARY KEY → per scoping multi-tenant servirebbe una
        # PK composta (channel, tenant_id). Per ora il channel_config è semi-globale
        # con tenant_id come metadato. Refactor PK in step successivo se serve.
        con.execute(
            """
            INSERT INTO channel_config (channel, config_json, enabled, tenant_id, updated_at)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT(channel) DO UPDATE SET
              config_json = excluded.config_json,
              enabled     = excluded.enabled,
              tenant_id   = excluded.tenant_id,
              updated_at  = excluded.updated_at
            """,
            (channel, payload, 1 if enabled else 0, tenant_id, now_iso()),
        )


# ===========================================================================
# Orchestrator persistent chat
# ===========================================================================

def add_orchestrator_message(
    role: str,
    body: str,
    metadata: dict[str, Any] | None = None,
    *,
    tenant_id: Any = _UNSET,
) -> int:
    tenant_id = _resolve_tenant(tenant_id)
    payload = json.dumps(metadata or {})
    with connect() as con:
        cur = con.execute(
            """
            INSERT INTO orchestrator_messages (role, body, metadata_json, tenant_id, created_at)
            VALUES (%s, %s, %s, %s, %s) RETURNING id
            """,
            (role, body, payload, tenant_id, now_iso()),
        )
        return int(cur.fetchone()['id'])


def list_orchestrator_messages(
    limit: int = 100, tenant_id: Any = _UNSET
) -> list[dict[str, Any]]:
    tenant_id = _resolve_tenant(tenant_id)
    with connect() as con:
        if tenant_id is None:
            rows = con.execute(
                "SELECT * FROM orchestrator_messages ORDER BY id DESC LIMIT %s",
                (limit,),
            ).fetchall()
        else:
            rows = con.execute(
                "SELECT * FROM orchestrator_messages WHERE tenant_id = %s "
                "ORDER BY id DESC LIMIT %s",
                (tenant_id, limit),
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


def clear_orchestrator_messages(tenant_id: Any = _UNSET) -> None:
    tenant_id = _resolve_tenant(tenant_id)
    with connect() as con:
        if tenant_id is None:
            con.execute("DELETE FROM orchestrator_messages")
        else:
            con.execute(
                "DELETE FROM orchestrator_messages WHERE tenant_id = %s", (tenant_id,)
            )


# ===========================================================================
# Social accounts + DM log (outreach social runner)
# ===========================================================================

def create_social_account(
    data: dict,
    *,
    tenant_id: Any = _UNSET,
    created_by_user_id: Any = _UNSET,
) -> int:
    """Insert social_account. encrypted_password deve essere bytes Fernet.

    Required keys: uuid, platform, username, encrypted_password.
    Optional: proxy_label, daily_dm_cap, status, notes.
    """
    tenant_id = _resolve_tenant(tenant_id)
    created_by_user_id = _resolve_user(created_by_user_id)
    ts = now_iso()
    with connect() as con:
        cur = con.execute(
            """INSERT INTO social_accounts
            (uuid, platform, username, encrypted_password, proxy_label,
             daily_dm_cap, status, warmup_started_at, warmup_days_target,
             notes, tenant_id, created_by_user_id, created_at, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id""",
            (
                data["uuid"], data["platform"], data["username"],
                data["encrypted_password"], data.get("proxy_label"),
                int(data.get("daily_dm_cap", 10)),
                data.get("status", "warming_up"),
                data.get("warmup_started_at"),
                int(data.get("warmup_days_target", 30)),
                data.get("notes"),
                tenant_id, created_by_user_id,
                ts, ts,
            ),
        )
        return int(cur.fetchone()['id'])


def list_social_accounts(
    platform: str | None = None,
    status: str | None = None,
    tenant_id: Any = _UNSET,
    created_by_user_id: Any = None,
) -> list[dict]:
    """Lista social_accounts. JOIN users per arricchire con `owner_email`
    (utile per la UI). Se `created_by_user_id` valorizzato (int), filtra
    per autore (uso 'I miei account')."""
    tenant_id = _resolve_tenant(tenant_id)
    sql = (
        "SELECT s.*, u.email AS owner_email, "
        "       u.first_name AS owner_first_name, u.last_name AS owner_last_name "
        "FROM social_accounts s "
        "LEFT JOIN users u ON u.id = s.created_by_user_id "
        "WHERE 1=1"
    )
    args: list = []
    if platform:
        sql += " AND s.platform = %s"; args.append(platform)
    if status:
        sql += " AND s.status = %s"; args.append(status)
    if tenant_id is not None:
        sql += " AND s.tenant_id = %s"; args.append(tenant_id)
    if created_by_user_id is not None:
        sql += " AND s.created_by_user_id = %s"; args.append(int(created_by_user_id))
    sql += " ORDER BY s.id DESC"
    with connect() as con:
        rows = con.execute(sql, args).fetchall()
    return [dict(r) for r in rows]


def get_social_account(account_id: int, tenant_id: Any = _UNSET) -> dict | None:
    tenant_id = _resolve_tenant(tenant_id)
    with connect() as con:
        if tenant_id is None:
            r = con.execute(
                "SELECT * FROM social_accounts WHERE id = %s", (account_id,)
            ).fetchone()
        else:
            r = con.execute(
                "SELECT * FROM social_accounts WHERE id = %s AND tenant_id = %s",
                (account_id, tenant_id),
            ).fetchone()
    return dict(r) if r else None


def update_social_account(account_id: int, *, tenant_id: Any = _UNSET, **fields) -> None:
    if not fields: return
    tenant_id = _resolve_tenant(tenant_id)
    sets = [f"{k} = %s" for k in fields]
    if tenant_id is None:
        sql = f"UPDATE social_accounts SET {', '.join(sets)}, updated_at = %s WHERE id = %s"
        params = (*fields.values(), now_iso(), account_id)
    else:
        sql = (
            f"UPDATE social_accounts SET {', '.join(sets)}, updated_at = %s "
            f"WHERE id = %s AND tenant_id = %s"
        )
        params = (*fields.values(), now_iso(), account_id, tenant_id)
    with connect() as con:
        con.execute(sql, params)


def delete_social_account(account_id: int, tenant_id: Any = _UNSET) -> None:
    tenant_id = _resolve_tenant(tenant_id)
    with connect() as con:
        if tenant_id is None:
            con.execute("DELETE FROM social_accounts WHERE id = %s", (account_id,))
        else:
            con.execute(
                "DELETE FROM social_accounts WHERE id = %s AND tenant_id = %s",
                (account_id, tenant_id),
            )


def insert_social_dm_log(data: dict, *, tenant_id: Any = _UNSET) -> int:
    """Log di un singolo DM inviato. Accetta sia `target_asset_id` (preferito)
    che `target_contact_id` legacy; entrambi vengono scritti se forniti."""
    tenant_id = _resolve_tenant(tenant_id)
    account_id = data.get("account_id")
    with connect() as con:
        cur = con.execute(
            """INSERT INTO social_dm_log
            (account_id, job_id, target_contact_id, target_asset_id, target_platform,
             target_username, message, sent_at, ok, reason, health_post,
             engine, api_config_id, tenant_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id""",
            (
                int(account_id) if account_id is not None else None,
                data.get("job_id"),
                data.get("target_contact_id"),
                data.get("target_asset_id"),
                data["target_platform"],
                data["target_username"],
                data["message"],
                data.get("sent_at") or now_iso(),
                1 if data.get("ok") else 0,
                data.get("reason"),
                data.get("health_post"),
                data.get("engine"),
                data.get("api_config_id"),
                tenant_id,
            ),
        )
        return int(cur.fetchone()['id'])


def list_social_dm_log(
    account_id: int | None = None, limit: int = 100, tenant_id: Any = _UNSET
) -> list[dict]:
    tenant_id = _resolve_tenant(tenant_id)
    sql = "SELECT * FROM social_dm_log WHERE 1=1"
    args: list = []
    if account_id is not None:
        sql += " AND account_id = %s"; args.append(account_id)
    if tenant_id is not None:
        sql += " AND tenant_id = %s"; args.append(tenant_id)
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
    if not r:
        return 0
    return int(r["count"] if isinstance(r, dict) else r[0])


# ===========================================================================
# WhatsApp API config (Motore B -Meta Cloud API)
# ===========================================================================

def insert_whatsapp_api_config(
    data: dict, *, tenant_id: Any = _UNSET, created_by_user_id: Any = _UNSET
) -> int:
    """Crea una nuova configurazione Meta Cloud API."""
    tenant_id = _resolve_tenant(tenant_id)
    created_by_user_id = _resolve_user(created_by_user_id)
    ts = now_iso()
    with connect() as con:
        cur = con.execute(
            """INSERT INTO whatsapp_api_config
            (label, phone_number_id, business_account_id, app_id,
             encrypted_access_token, default_template_name, default_template_language,
             status, daily_msg_cap, notes,
             tenant_id, created_by_user_id, created_at, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id""",
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
                tenant_id, created_by_user_id,
                ts, ts,
            ),
        )
        return int(cur.fetchone()['id'])


def list_whatsapp_api_config(
    status: str | None = None,
    tenant_id: Any = _UNSET,
    created_by_user_id: Any = None,
) -> list[dict]:
    """Lista whatsapp_api_config + owner_email via LEFT JOIN users.
    Se `created_by_user_id` valorizzato, filtra per autore."""
    tenant_id = _resolve_tenant(tenant_id)
    sql = (
        "SELECT w.*, u.email AS owner_email, "
        "       u.first_name AS owner_first_name, u.last_name AS owner_last_name "
        "FROM whatsapp_api_config w "
        "LEFT JOIN users u ON u.id = w.created_by_user_id "
        "WHERE 1=1"
    )
    args: list = []
    if status:
        sql += " AND w.status = %s"; args.append(status)
    if tenant_id is not None:
        sql += " AND w.tenant_id = %s"; args.append(tenant_id)
    if created_by_user_id is not None:
        sql += " AND w.created_by_user_id = %s"; args.append(int(created_by_user_id))
    sql += " ORDER BY w.id DESC"
    with connect() as con:
        rows = con.execute(sql, args).fetchall()
    return [dict(r) for r in rows]


def get_whatsapp_api_config(config_id: int, tenant_id: Any = _UNSET) -> dict | None:
    tenant_id = _resolve_tenant(tenant_id)
    with connect() as con:
        if tenant_id is None:
            r = con.execute(
                "SELECT * FROM whatsapp_api_config WHERE id = %s", (config_id,)
            ).fetchone()
        else:
            r = con.execute(
                "SELECT * FROM whatsapp_api_config WHERE id = %s AND tenant_id = %s",
                (config_id, tenant_id),
            ).fetchone()
    return dict(r) if r else None


def update_whatsapp_api_config(config_id: int, *, tenant_id: Any = _UNSET, **fields) -> None:
    if not fields:
        return
    tenant_id = _resolve_tenant(tenant_id)
    sets = [f"{k} = %s" for k in fields]
    if tenant_id is None:
        sql = f"UPDATE whatsapp_api_config SET {', '.join(sets)}, updated_at = %s WHERE id = %s"
        params = (*fields.values(), now_iso(), config_id)
    else:
        sql = (
            f"UPDATE whatsapp_api_config SET {', '.join(sets)}, updated_at = %s "
            f"WHERE id = %s AND tenant_id = %s"
        )
        params = (*fields.values(), now_iso(), config_id, tenant_id)
    with connect() as con:
        con.execute(sql, params)


def delete_whatsapp_api_config(config_id: int, tenant_id: Any = _UNSET) -> None:
    tenant_id = _resolve_tenant(tenant_id)
    with connect() as con:
        if tenant_id is None:
            con.execute("DELETE FROM whatsapp_api_config WHERE id = %s", (config_id,))
        else:
            con.execute(
                "DELETE FROM whatsapp_api_config WHERE id = %s AND tenant_id = %s",
                (config_id, tenant_id),
            )


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
    if not r:
        return 0
    return int(r["count"] if isinstance(r, dict) else r[0])


# ===========================================================================
# Email accounts (SMTP/IMAP multi-account)
# ===========================================================================

def insert_email_account(
    data: dict, *, tenant_id: Any = _UNSET, created_by_user_id: Any = _UNSET
) -> int:
    """Crea un nuovo email account. `encrypted_smtp_password` e
    `encrypted_imap_password` devono essere già cifrati via crypto_creds.encrypt.
    """
    tenant_id = _resolve_tenant(tenant_id)
    created_by_user_id = _resolve_user(created_by_user_id)
    ts = now_iso()
    with connect() as con:
        cur = con.execute(
            """INSERT INTO email_accounts
            (uuid, label, from_address, reply_to,
             smtp_host, smtp_port, smtp_user, encrypted_smtp_password, smtp_use_tls,
             imap_host, imap_port, imap_user, encrypted_imap_password, imap_folder,
             status, daily_send_cap, rate_limit_per_minute, notes,
             tenant_id, created_by_user_id, created_at, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id""",
            (
                data["uuid"],
                data["label"],
                data["from_address"],
                data.get("reply_to"),
                data["smtp_host"],
                int(data.get("smtp_port", 587)),
                data["smtp_user"],
                data["encrypted_smtp_password"],
                1 if data.get("smtp_use_tls", True) else 0,
                data.get("imap_host"),
                int(data.get("imap_port", 993)) if data.get("imap_port") else None,
                data.get("imap_user"),
                data.get("encrypted_imap_password"),
                data.get("imap_folder", "INBOX"),
                data.get("status", "active"),
                int(data.get("daily_send_cap", 200)),
                int(data.get("rate_limit_per_minute", 10)),
                data.get("notes"),
                tenant_id, created_by_user_id,
                ts, ts,
            ),
        )
        return int(cur.fetchone()['id'])


def list_email_accounts(
    status: str | None = None,
    tenant_id: Any = _UNSET,
    created_by_user_id: Any = None,
) -> list[dict]:
    """Lista email_accounts + owner_email via LEFT JOIN users."""
    tenant_id = _resolve_tenant(tenant_id)
    sql = (
        "SELECT e.*, u.email AS owner_email, "
        "       u.first_name AS owner_first_name, u.last_name AS owner_last_name "
        "FROM email_accounts e "
        "LEFT JOIN users u ON u.id = e.created_by_user_id "
        "WHERE 1=1"
    )
    args: list = []
    if status:
        sql += " AND e.status = %s"; args.append(status)
    if tenant_id is not None:
        sql += " AND e.tenant_id = %s"; args.append(tenant_id)
    if created_by_user_id is not None:
        sql += " AND e.created_by_user_id = %s"; args.append(int(created_by_user_id))
    sql += " ORDER BY e.id DESC"
    with connect() as con:
        rows = con.execute(sql, args).fetchall()
    return [dict(r) for r in rows]


def get_email_account(account_id: int, tenant_id: Any = _UNSET) -> dict | None:
    tenant_id = _resolve_tenant(tenant_id)
    with connect() as con:
        if tenant_id is None:
            r = con.execute(
                "SELECT * FROM email_accounts WHERE id = %s", (account_id,)
            ).fetchone()
        else:
            r = con.execute(
                "SELECT * FROM email_accounts WHERE id = %s AND tenant_id = %s",
                (account_id, tenant_id),
            ).fetchone()
    return dict(r) if r else None


def get_default_email_account(tenant_id: Any = _UNSET) -> dict | None:
    """Primo email account con status='active' nel tenant. Usato come fallback
    quando un task outreach NON specifica `email_account_id`."""
    tenant_id = _resolve_tenant(tenant_id)
    with connect() as con:
        if tenant_id is None:
            r = con.execute(
                "SELECT * FROM email_accounts WHERE status = 'active' "
                "ORDER BY id ASC LIMIT 1"
            ).fetchone()
        else:
            r = con.execute(
                "SELECT * FROM email_accounts WHERE status = 'active' AND tenant_id = %s "
                "ORDER BY id ASC LIMIT 1",
                (tenant_id,),
            ).fetchone()
    return dict(r) if r else None


def update_email_account(account_id: int, *, tenant_id: Any = _UNSET, **fields) -> None:
    if not fields:
        return
    tenant_id = _resolve_tenant(tenant_id)
    sets = [f"{k} = %s" for k in fields]
    if tenant_id is None:
        sql = f"UPDATE email_accounts SET {', '.join(sets)}, updated_at = %s WHERE id = %s"
        params = (*fields.values(), now_iso(), account_id)
    else:
        sql = (
            f"UPDATE email_accounts SET {', '.join(sets)}, updated_at = %s "
            f"WHERE id = %s AND tenant_id = %s"
        )
        params = (*fields.values(), now_iso(), account_id, tenant_id)
    with connect() as con:
        con.execute(sql, params)


def delete_email_account(account_id: int, tenant_id: Any = _UNSET) -> None:
    """Elimina email account. Setta a NULL `tasks.email_account_id` orfani prima
    della DELETE (no FK CASCADE dichiarato per audit/lifecycle).
    """
    tenant_id = _resolve_tenant(tenant_id)
    with connect() as con:
        con.execute(
            "UPDATE tasks SET email_account_id = NULL WHERE email_account_id = %s",
            (account_id,),
        )
        if tenant_id is None:
            con.execute("DELETE FROM email_accounts WHERE id = %s", (account_id,))
        else:
            con.execute(
                "DELETE FROM email_accounts WHERE id = %s AND tenant_id = %s",
                (account_id, tenant_id),
            )


# ===========================================================================
# Telegram bots (Bot API multi-bot)
# ===========================================================================

def insert_telegram_bot(
    data: dict, *, tenant_id: Any = _UNSET, created_by_user_id: Any = _UNSET
) -> int:
    """Crea un nuovo bot Telegram. `encrypted_bot_token` deve essere già cifrato
    via crypto_creds.encrypt."""
    tenant_id = _resolve_tenant(tenant_id)
    created_by_user_id = _resolve_user(created_by_user_id)
    ts = now_iso()
    with connect() as con:
        cur = con.execute(
            """INSERT INTO telegram_bots
            (uuid, label, bot_username, encrypted_bot_token,
             status, daily_msg_cap, poll_interval_seconds, last_update_id, notes,
             tenant_id, created_by_user_id, created_at, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id""",
            (
                data["uuid"],
                data["label"],
                data.get("bot_username"),
                data["encrypted_bot_token"],
                data.get("status", "active"),
                int(data.get("daily_msg_cap", 500)),
                int(data.get("poll_interval_seconds", 30)),
                data.get("last_update_id"),
                data.get("notes"),
                tenant_id, created_by_user_id,
                ts, ts,
            ),
        )
        return int(cur.fetchone()['id'])


def list_telegram_bots(
    status: str | None = None,
    tenant_id: Any = _UNSET,
    created_by_user_id: Any = None,
) -> list[dict]:
    """Lista telegram_bots + owner_email via LEFT JOIN users."""
    tenant_id = _resolve_tenant(tenant_id)
    sql = (
        "SELECT b.*, u.email AS owner_email, "
        "       u.first_name AS owner_first_name, u.last_name AS owner_last_name "
        "FROM telegram_bots b "
        "LEFT JOIN users u ON u.id = b.created_by_user_id "
        "WHERE 1=1"
    )
    args: list = []
    if status:
        sql += " AND b.status = %s"; args.append(status)
    if tenant_id is not None:
        sql += " AND b.tenant_id = %s"; args.append(tenant_id)
    if created_by_user_id is not None:
        sql += " AND b.created_by_user_id = %s"; args.append(int(created_by_user_id))
    sql += " ORDER BY b.id DESC"
    with connect() as con:
        rows = con.execute(sql, args).fetchall()
    return [dict(r) for r in rows]


def get_telegram_bot(bot_id: int, tenant_id: Any = _UNSET) -> dict | None:
    tenant_id = _resolve_tenant(tenant_id)
    with connect() as con:
        if tenant_id is None:
            r = con.execute(
                "SELECT * FROM telegram_bots WHERE id = %s", (bot_id,)
            ).fetchone()
        else:
            r = con.execute(
                "SELECT * FROM telegram_bots WHERE id = %s AND tenant_id = %s",
                (bot_id, tenant_id),
            ).fetchone()
    return dict(r) if r else None


def get_default_telegram_bot(tenant_id: Any = _UNSET) -> dict | None:
    """Primo bot Telegram con status='active' nel tenant. Usato come fallback
    quando un task outreach NON specifica `telegram_bot_id`."""
    tenant_id = _resolve_tenant(tenant_id)
    with connect() as con:
        if tenant_id is None:
            r = con.execute(
                "SELECT * FROM telegram_bots WHERE status = 'active' "
                "ORDER BY id ASC LIMIT 1"
            ).fetchone()
        else:
            r = con.execute(
                "SELECT * FROM telegram_bots WHERE status = 'active' AND tenant_id = %s "
                "ORDER BY id ASC LIMIT 1",
                (tenant_id,),
            ).fetchone()
    return dict(r) if r else None


def update_telegram_bot(bot_id: int, *, tenant_id: Any = _UNSET, **fields) -> None:
    if not fields:
        return
    tenant_id = _resolve_tenant(tenant_id)
    sets = [f"{k} = %s" for k in fields]
    if tenant_id is None:
        sql = f"UPDATE telegram_bots SET {', '.join(sets)}, updated_at = %s WHERE id = %s"
        params = (*fields.values(), now_iso(), bot_id)
    else:
        sql = (
            f"UPDATE telegram_bots SET {', '.join(sets)}, updated_at = %s "
            f"WHERE id = %s AND tenant_id = %s"
        )
        params = (*fields.values(), now_iso(), bot_id, tenant_id)
    with connect() as con:
        con.execute(sql, params)


def delete_telegram_bot(bot_id: int, tenant_id: Any = _UNSET) -> None:
    """Elimina bot Telegram. Setta a NULL `tasks.telegram_bot_id` orfani prima
    della DELETE (no FK CASCADE)."""
    tenant_id = _resolve_tenant(tenant_id)
    with connect() as con:
        con.execute(
            "UPDATE tasks SET telegram_bot_id = NULL WHERE telegram_bot_id = %s",
            (bot_id,),
        )
        if tenant_id is None:
            con.execute("DELETE FROM telegram_bots WHERE id = %s", (bot_id,))
        else:
            con.execute(
                "DELETE FROM telegram_bots WHERE id = %s AND tenant_id = %s",
                (bot_id, tenant_id),
            )


# ===========================================================================
# Contacts -helpers WhatsApp (consent + inbound tracking)
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
    """Segna che il contatto ha appena scritto al business number -abilita la
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
