"""SQLite layer (no ORM). Connessione per-thread, schema migration al boot."""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator

from .config import DATA_DIR, DB_PATH


SCHEMA = """
-- Tasks (era 'projects'): unità di lavoro autonoma, lanciabile da sola
-- o orchestrata in un workflow.
CREATE TABLE IF NOT EXISTS tasks (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
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
  target_cap_per_site INTEGER NOT NULL DEFAULT 30,
  bulk_rate_limit_per_sec REAL NOT NULL DEFAULT 2.0,
  bulk_extraction_method TEXT NOT NULL DEFAULT 'llm_per_page',
  bulk_css_selectors TEXT,
  crawler_enabled INTEGER NOT NULL DEFAULT 0,
  crawler_url_pattern TEXT,
  crawler_max_depth INTEGER NOT NULL DEFAULT 3,
  discovery_llm_provider TEXT,
  discovery_llm_model TEXT,
  discovery_llm_api_key TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS jobs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
  status TEXT NOT NULL,
  started_at TEXT,
  finished_at TEXT,
  log TEXT NOT NULL DEFAULT '',
  result_path TEXT,
  error TEXT,
  control_signal TEXT,
  triggered_by_job_id INTEGER REFERENCES jobs(id) ON DELETE SET NULL,
  workflow_run_id INTEGER REFERENCES workflow_runs(id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_jobs_task ON jobs(task_id, id DESC);

-- Workflow: contenitore nominato di task collegati in DAG
CREATE TABLE IF NOT EXISTS workflows (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  description TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

-- Esecuzioni di workflow (ogni "▶ Esegui workflow" crea una riga)
CREATE TABLE IF NOT EXISTS workflow_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  workflow_id INTEGER NOT NULL REFERENCES workflows(id) ON DELETE CASCADE,
  status TEXT NOT NULL DEFAULT 'running',
  started_at TEXT NOT NULL,
  finished_at TEXT
);

-- Edges: collegano due task DENTRO un workflow specifico
CREATE TABLE IF NOT EXISTS workflow_edges (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  workflow_id   INTEGER REFERENCES workflows(id) ON DELETE CASCADE,
  from_task_id  INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
  to_task_id    INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
  trigger_event TEXT NOT NULL DEFAULT 'on_done',
  pass_artifact TEXT,
  enabled       INTEGER NOT NULL DEFAULT 1,
  created_at    TEXT NOT NULL,
  UNIQUE(workflow_id, from_task_id, to_task_id)
);
-- NOTA: gli indici su workflow_edges (incluso quello su workflow_id) vengono
-- creati DOPO la migrazione colonne in init_db(), per supportare DB pre-esistenti
-- che non avevano workflow_id quando la tabella è stata creata la prima volta.

-- Contatti materializzati dai profiles.jsonl
CREATE TABLE IF NOT EXISTS contacts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  source_task_id    INTEGER REFERENCES tasks(id) ON DELETE SET NULL,
  source_job_id     INTEGER REFERENCES jobs(id) ON DELETE SET NULL,
  source_url        TEXT,
  source_domain     TEXT,
  display_name      TEXT,
  email             TEXT,
  telegram_username TEXT,
  telegram_chat_id  TEXT,
  raw_json          TEXT,
  status            TEXT NOT NULL DEFAULT 'new',
  qualifier_score   INTEGER,
  notes             TEXT,
  created_at        TEXT NOT NULL,
  updated_at        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_contacts_email ON contacts(email);
CREATE INDEX IF NOT EXISTS idx_contacts_telegram_chat ON contacts(telegram_chat_id);
CREATE INDEX IF NOT EXISTS idx_contacts_status ON contacts(status);
CREATE INDEX IF NOT EXISTS idx_contacts_source ON contacts(source_task_id);

-- Thread di conversazione su un canale
CREATE TABLE IF NOT EXISTS threads (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  contact_id   INTEGER NOT NULL REFERENCES contacts(id) ON DELETE CASCADE,
  channel      TEXT NOT NULL,
  external_id  TEXT,
  subject      TEXT,
  status       TEXT NOT NULL DEFAULT 'open',
  task_id      INTEGER REFERENCES tasks(id) ON DELETE SET NULL,
  last_msg_at  TEXT,
  created_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_threads_contact ON threads(contact_id);
CREATE INDEX IF NOT EXISTS idx_threads_status ON threads(status);
CREATE INDEX IF NOT EXISTS idx_threads_external ON threads(channel, external_id);

-- Messaggi singoli
CREATE TABLE IF NOT EXISTS messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  thread_id     INTEGER NOT NULL REFERENCES threads(id) ON DELETE CASCADE,
  direction     TEXT NOT NULL,
  body          TEXT NOT NULL,
  llm_generated INTEGER NOT NULL DEFAULT 0,
  external_id   TEXT,
  status        TEXT NOT NULL DEFAULT 'pending',
  error         TEXT,
  sent_at       TEXT,
  created_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_thread ON messages(thread_id, id);
CREATE INDEX IF NOT EXISTS idx_messages_status ON messages(status, direction);

-- Configurazione canali singleton
CREATE TABLE IF NOT EXISTS channel_config (
  channel     TEXT PRIMARY KEY,
  config_json TEXT NOT NULL,
  enabled     INTEGER NOT NULL DEFAULT 0,
  updated_at  TEXT NOT NULL
);

-- Chat persistente dell'Orchestrator
CREATE TABLE IF NOT EXISTS orchestrator_messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  role TEXT NOT NULL,
  body TEXT NOT NULL,
  metadata_json TEXT,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_orchestrator_messages_id ON orchestrator_messages(id);

-- Asset generalizzati: ogni profilo/annuncio/prodotto/articolo estratto dai runner
-- diventa una riga qui, con tag derivati dichiarativamente dai campi del template.
-- Sostituisce/affianca `contacts` come inbox principale dei dati estratti.
CREATE TABLE IF NOT EXISTS assets (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  asset_type           TEXT NOT NULL,
  source_task_id       INTEGER REFERENCES tasks(id) ON DELETE SET NULL,
  source_job_id        INTEGER REFERENCES jobs(id) ON DELETE SET NULL,
  source_url           TEXT,
  source_url_canonical TEXT,         -- canonicalized form per dedup cross-lingua/paginazione
  source_domain        TEXT,
  title                TEXT,
  raw_json             TEXT NOT NULL,
  status               TEXT NOT NULL DEFAULT 'new',
  qualifier_score      INTEGER,
  notes                TEXT,
  created_at           TEXT NOT NULL,
  updated_at           TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_assets_type_status ON assets(asset_type, status);
CREATE INDEX IF NOT EXISTS idx_assets_url ON assets(source_url);
CREATE INDEX IF NOT EXISTS idx_assets_source ON assets(source_task_id);
-- idx_assets_url_canonical creato dopo la migrazione idempotente (vedi init_db)

CREATE TABLE IF NOT EXISTS asset_tags (
  asset_id  INTEGER NOT NULL REFERENCES assets(id) ON DELETE CASCADE,
  tag_key   TEXT NOT NULL,
  tag_value TEXT NOT NULL,
  PRIMARY KEY (asset_id, tag_key, tag_value)
);
CREATE INDEX IF NOT EXISTS idx_asset_tags_lookup ON asset_tags(tag_key, tag_value);

-- Memoria pattern per dominio: i pattern URL "target" scoperti dai task in passato.
-- Permette ai task futuri di saltare la discovery LLM se gia' confermato.
CREATE TABLE IF NOT EXISTS site_patterns (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  registrable_domain TEXT NOT NULL,
  pattern            TEXT NOT NULL,
  regex              TEXT NOT NULL,
  asset_type         TEXT,
  status             TEXT NOT NULL DEFAULT 'candidate',
  hits               INTEGER NOT NULL DEFAULT 0,
  successes          INTEGER NOT NULL DEFAULT 0,
  failures           INTEGER NOT NULL DEFAULT 0,
  source_task_id     INTEGER REFERENCES tasks(id) ON DELETE SET NULL,
  source_job_id      INTEGER REFERENCES jobs(id) ON DELETE SET NULL,
  notes              TEXT,
  created_at         TEXT NOT NULL,
  updated_at         TEXT NOT NULL,
  UNIQUE (registrable_domain, pattern)
);
CREATE INDEX IF NOT EXISTS idx_site_patterns_domain_status ON site_patterns(registrable_domain, status);
CREATE INDEX IF NOT EXISTS idx_site_patterns_asset_type ON site_patterns(asset_type);

-- Playbook cross-runner per dominio: l'agente potente (browser_use) salva
-- istruzioni operative free-form da iniettare nel system prompt dell'agente
-- debole (site_explorer) sui run successivi sullo stesso dominio.
-- Stage 2 del knowledge transfer: vedi GUIDA §9.4.x.
CREATE TABLE IF NOT EXISTS site_playbooks (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  registrable_domain TEXT NOT NULL,
  asset_type         TEXT NOT NULL,
  playbook           TEXT NOT NULL,         -- istruzioni operative free-form (LLM-generated)
  source_runner      TEXT NOT NULL,         -- 'browser_use' / 'site_explorer' / 'manual'
  source_job_id      INTEGER REFERENCES jobs(id) ON DELETE SET NULL,
  transferable       INTEGER NOT NULL DEFAULT 1,   -- 0 se NO (es. richiede browser/login)
  status             TEXT NOT NULL DEFAULT 'active', -- 'active' / 'stale' / 'archived'
  hits               INTEGER NOT NULL DEFAULT 0,   -- quante volte un runner l'ha usato
  successes          INTEGER NOT NULL DEFAULT 0,   -- quante volte ha portato a estrazioni
  failures           INTEGER NOT NULL DEFAULT 0,   -- quante volte ha portato a 0 estrazioni
  created_at         TEXT NOT NULL,
  updated_at         TEXT NOT NULL,
  UNIQUE (registrable_domain, asset_type)
);
CREATE INDEX IF NOT EXISTS idx_site_playbooks_domain ON site_playbooks(registrable_domain);
"""


def init_db() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with connect() as con:
        # ---------- migrazione: projects → tasks (se DB pre-esistente) ----------
        existing_tables = {
            r["name"]
            for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        if "projects" in existing_tables and "tasks" not in existing_tables:
            con.execute("ALTER TABLE projects RENAME TO tasks")
        # rinomina colonne project_id → task_id se ancora vecchie
        if "jobs" in existing_tables:
            jcols = {r["name"] for r in con.execute("PRAGMA table_info(jobs)").fetchall()}
            if "project_id" in jcols and "task_id" not in jcols:
                con.execute("ALTER TABLE jobs RENAME COLUMN project_id TO task_id")
        if "workflow_edges" in existing_tables:
            ecols = {r["name"] for r in con.execute("PRAGMA table_info(workflow_edges)").fetchall()}
            if "from_project_id" in ecols and "from_task_id" not in ecols:
                con.execute("ALTER TABLE workflow_edges RENAME COLUMN from_project_id TO from_task_id")
            if "to_project_id" in ecols and "to_task_id" not in ecols:
                con.execute("ALTER TABLE workflow_edges RENAME COLUMN to_project_id TO to_task_id")
        if "contacts" in existing_tables:
            ccols = {r["name"] for r in con.execute("PRAGMA table_info(contacts)").fetchall()}
            if "source_project_id" in ccols and "source_task_id" not in ccols:
                con.execute("ALTER TABLE contacts RENAME COLUMN source_project_id TO source_task_id")
        if "threads" in existing_tables:
            tcols = {r["name"] for r in con.execute("PRAGMA table_info(threads)").fetchall()}
            if "project_id" in tcols and "task_id" not in tcols:
                con.execute("ALTER TABLE threads RENAME COLUMN project_id TO task_id")

        # ---------- crea schema (idempotente) ----------
        con.executescript(SCHEMA)

        # ---------- migrazioni colonne idempotenti ----------
        cols = {r["name"] for r in con.execute("PRAGMA table_info(tasks)").fetchall()}
        if "agent_mode" not in cols:
            con.execute("ALTER TABLE tasks ADD COLUMN agent_mode TEXT NOT NULL DEFAULT 'react'")
        if "extraction_template" not in cols:
            con.execute("ALTER TABLE tasks ADD COLUMN extraction_template TEXT")
        if "extraction_schema" not in cols:
            con.execute("ALTER TABLE tasks ADD COLUMN extraction_schema TEXT")
        if "llm_provider" not in cols:
            con.execute(
                "ALTER TABLE tasks ADD COLUMN llm_provider TEXT NOT NULL DEFAULT 'ollama'"
            )
        if "llm_base_url" not in cols:
            con.execute("ALTER TABLE tasks ADD COLUMN llm_base_url TEXT")
        if "llm_api_key" not in cols:
            con.execute("ALTER TABLE tasks ADD COLUMN llm_api_key TEXT")
        if "input_artifact_path" not in cols:
            con.execute("ALTER TABLE tasks ADD COLUMN input_artifact_path TEXT")
        if "message_template" not in cols:
            con.execute("ALTER TABLE tasks ADD COLUMN message_template TEXT")
        if "message_subject" not in cols:
            con.execute("ALTER TABLE tasks ADD COLUMN message_subject TEXT")
        if "message_channels" not in cols:
            con.execute("ALTER TABLE tasks ADD COLUMN message_channels TEXT")
        if "responder_system_prompt" not in cols:
            con.execute("ALTER TABLE tasks ADD COLUMN responder_system_prompt TEXT")
        if "bulk_concurrency" not in cols:
            con.execute("ALTER TABLE tasks ADD COLUMN bulk_concurrency INTEGER NOT NULL DEFAULT 5")
        if "target_cap_per_site" not in cols:
            con.execute("ALTER TABLE tasks ADD COLUMN target_cap_per_site INTEGER NOT NULL DEFAULT 30")
        if "bulk_rate_limit_per_sec" not in cols:
            con.execute("ALTER TABLE tasks ADD COLUMN bulk_rate_limit_per_sec REAL NOT NULL DEFAULT 2.0")
        if "bulk_extraction_method" not in cols:
            con.execute("ALTER TABLE tasks ADD COLUMN bulk_extraction_method TEXT NOT NULL DEFAULT 'llm_per_page'")
        if "bulk_css_selectors" not in cols:
            con.execute("ALTER TABLE tasks ADD COLUMN bulk_css_selectors TEXT")
        if "crawler_enabled" not in cols:
            con.execute("ALTER TABLE tasks ADD COLUMN crawler_enabled INTEGER NOT NULL DEFAULT 0")
        if "crawler_url_pattern" not in cols:
            con.execute("ALTER TABLE tasks ADD COLUMN crawler_url_pattern TEXT")
        if "crawler_max_depth" not in cols:
            con.execute("ALTER TABLE tasks ADD COLUMN crawler_max_depth INTEGER NOT NULL DEFAULT 3")
        if "discovery_llm_provider" not in cols:
            con.execute("ALTER TABLE tasks ADD COLUMN discovery_llm_provider TEXT")
        if "discovery_llm_model" not in cols:
            con.execute("ALTER TABLE tasks ADD COLUMN discovery_llm_model TEXT")
        if "discovery_llm_api_key" not in cols:
            con.execute("ALTER TABLE tasks ADD COLUMN discovery_llm_api_key TEXT")
        if "max_discovery_retries" not in cols:
            con.execute("ALTER TABLE tasks ADD COLUMN max_discovery_retries INTEGER NOT NULL DEFAULT 3")
        # LLM dedicato per browser_use (visione + tool-calling complesso): può
        # essere diverso dal main extraction (che spesso è locale economico).
        if "browser_llm_provider" not in cols:
            con.execute("ALTER TABLE tasks ADD COLUMN browser_llm_provider TEXT")
        if "browser_llm_model" not in cols:
            con.execute("ALTER TABLE tasks ADD COLUMN browser_llm_model TEXT")
        if "browser_llm_api_key" not in cols:
            con.execute("ALTER TABLE tasks ADD COLUMN browser_llm_api_key TEXT")
        # Valutazione personale dell'utente sul task
        if "rating" not in cols:
            con.execute("ALTER TABLE tasks ADD COLUMN rating INTEGER")
        if "notes" not in cols:
            con.execute("ALTER TABLE tasks ADD COLUMN notes TEXT")
        if "status_tag" not in cols:
            con.execute("ALTER TABLE tasks ADD COLUMN status_tag TEXT")
        # Refresh policy: ogni quanti giorni ri-extract di un asset esistente.
        # 0 = mai (skip se esiste in DB). N>0 = ri-extract se updated_at piu' vecchio
        # di N giorni. -1 = sempre (mai skip). Default 7 (ragionevole per dati che
        # cambiano poco). Vale per site_explorer e bulk_extract; non ha senso per browser_use.
        if "refresh_policy_days" not in cols:
            con.execute("ALTER TABLE tasks ADD COLUMN refresh_policy_days INTEGER NOT NULL DEFAULT 7")
        # assets — Fix 2: canonical URL per dedup cross-lingua
        acols = {r["name"] for r in con.execute("PRAGMA table_info(assets)").fetchall()}
        if "source_url_canonical" not in acols:
            con.execute("ALTER TABLE assets ADD COLUMN source_url_canonical TEXT")
            # backfill: popola la canonical sui record esistenti
            try:
                from .agent.url_canonical import canonical_url as _canon_url
                rows = con.execute("SELECT id, source_url FROM assets WHERE source_url IS NOT NULL").fetchall()
                for r in rows:
                    con.execute(
                        "UPDATE assets SET source_url_canonical = ? WHERE id = ?",
                        (_canon_url(r["source_url"]), r["id"]),
                    )
            except Exception:
                pass
        # Indice (creato sempre, idempotente, dopo che la colonna esiste)
        con.execute("CREATE INDEX IF NOT EXISTS idx_assets_url_canonical ON assets(source_url_canonical, asset_type)")
        # jobs
        jcols = {r["name"] for r in con.execute("PRAGMA table_info(jobs)").fetchall()}
        if "control_signal" not in jcols:
            con.execute("ALTER TABLE jobs ADD COLUMN control_signal TEXT")
        if "triggered_by_job_id" not in jcols:
            con.execute("ALTER TABLE jobs ADD COLUMN triggered_by_job_id INTEGER")
        if "workflow_run_id" not in jcols:
            con.execute("ALTER TABLE jobs ADD COLUMN workflow_run_id INTEGER")
        # workflow_edges: aggiungi workflow_id se mancante
        ecols = {r["name"] for r in con.execute("PRAGMA table_info(workflow_edges)").fetchall()}
        if "workflow_id" not in ecols:
            con.execute("ALTER TABLE workflow_edges ADD COLUMN workflow_id INTEGER REFERENCES workflows(id) ON DELETE CASCADE")

        # tasks.disabled e workflows.disabled (flag boolean per disabilitare lancio)
        tcols = {r["name"] for r in con.execute("PRAGMA table_info(tasks)").fetchall()}
        if "disabled" not in tcols:
            con.execute("ALTER TABLE tasks ADD COLUMN disabled INTEGER NOT NULL DEFAULT 0")
        wcols = {r["name"] for r in con.execute("PRAGMA table_info(workflows)").fetchall()}
        if "disabled" not in wcols:
            con.execute("ALTER TABLE workflows ADD COLUMN disabled INTEGER NOT NULL DEFAULT 0")

        # contacts: aggiungi canali secondari (whatsapp, sitoweb, social_json).
        # Permette materializzazione di profili con SOLO social/whatsapp anche
        # senza email/telegram. Outreach automatico via questi canali ancora
        # non implementato (vedi backlog: outreach via Playwright per social DM).
        ccols = {r["name"] for r in con.execute("PRAGMA table_info(contacts)").fetchall()}
        if "whatsapp" not in ccols:
            con.execute("ALTER TABLE contacts ADD COLUMN whatsapp TEXT")
        if "sitoweb" not in ccols:
            con.execute("ALTER TABLE contacts ADD COLUMN sitoweb TEXT")
        if "social_json" not in ccols:
            con.execute("ALTER TABLE contacts ADD COLUMN social_json TEXT")
        # FK contacts.asset_id → assets(id): permette di legare un contatto al
        # suo asset (es. palestra). 1 asset può avere N contatti (handle IG,
        # FB, email, tel). Popolato dall'import CSV; legacy contacts restano
        # con NULL.
        if "asset_id" not in ccols:
            con.execute(
                "ALTER TABLE contacts ADD COLUMN asset_id INTEGER "
                "REFERENCES assets(id) ON DELETE CASCADE"
            )
        con.execute(
            "CREATE INDEX IF NOT EXISTS idx_contacts_asset ON contacts(asset_id)"
        )

        # tasks: campi per outreach_social
        if "social_platform" not in tcols:
            con.execute("ALTER TABLE tasks ADD COLUMN social_platform TEXT")
        if "outreach_intent" not in tcols:
            con.execute("ALTER TABLE tasks ADD COLUMN outreach_intent TEXT")
        if "message_template_variants" not in tcols:
            con.execute("ALTER TABLE tasks ADD COLUMN message_template_variants TEXT")
        if "max_dms_per_run" not in tcols:
            con.execute("ALTER TABLE tasks ADD COLUMN max_dms_per_run INTEGER NOT NULL DEFAULT 30")
        if "max_dms_per_session" not in tcols:
            con.execute("ALTER TABLE tasks ADD COLUMN max_dms_per_session INTEGER NOT NULL DEFAULT 5")
        if "headed" not in tcols:
            con.execute("ALTER TABLE tasks ADD COLUMN headed INTEGER NOT NULL DEFAULT 1")
        # target_contact_ids: JSON array di contact.id selezionati esplicitamente
        # come target per outreach_social. Se NULL/vuoto, il runner usa tutti i
        # contacts qualified con social[platform] popolato.
        if "target_contact_ids" not in tcols:
            con.execute("ALTER TABLE tasks ADD COLUMN target_contact_ids TEXT")

        # social_accounts: account social per outreach DM
        con.execute("""
            CREATE TABLE IF NOT EXISTS social_accounts (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              uuid TEXT UNIQUE NOT NULL,
              platform TEXT NOT NULL,
              username TEXT NOT NULL,
              encrypted_password BLOB NOT NULL,
              proxy_label TEXT,
              daily_dm_cap INTEGER NOT NULL DEFAULT 10,
              status TEXT NOT NULL DEFAULT 'active',
              warmup_started_at TEXT,
              warmup_days_target INTEGER DEFAULT 30,
              notes TEXT,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              UNIQUE(platform, username)
            )
        """)
        con.execute(
            "CREATE INDEX IF NOT EXISTS idx_social_accounts_platform_status "
            "ON social_accounts(platform, status)"
        )

        # social_dm_log: log dettagliato di ogni DM inviato (audit + analytics)
        con.execute("""
            CREATE TABLE IF NOT EXISTS social_dm_log (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              account_id INTEGER NOT NULL REFERENCES social_accounts(id) ON DELETE CASCADE,
              job_id INTEGER REFERENCES jobs(id) ON DELETE SET NULL,
              target_contact_id INTEGER REFERENCES contacts(id) ON DELETE SET NULL,
              target_platform TEXT NOT NULL,
              target_username TEXT NOT NULL,
              message TEXT NOT NULL,
              sent_at TEXT NOT NULL,
              ok INTEGER NOT NULL,
              reason TEXT,
              health_post TEXT
            )
        """)
        con.execute(
            "CREATE INDEX IF NOT EXISTS idx_social_dm_log_account "
            "ON social_dm_log(account_id, sent_at)"
        )
        con.execute(
            "CREATE INDEX IF NOT EXISTS idx_social_dm_log_target "
            "ON social_dm_log(target_contact_id)"
        )

        # ===== WhatsApp (Fase 1) =====
        # Estensione di social_accounts per ospitare account WhatsApp browser:
        # - phone_number: numero E.164 (es. "+393331234567")
        # - auth_method: 'password' (default IG/TikTok) | 'qr_session' (WA) | 'api_token'
        # - session_dir: path Playwright user_data_dir per persistenza WA Web
        sa_cols = {r["name"] for r in con.execute("PRAGMA table_info(social_accounts)").fetchall()}
        if "phone_number" not in sa_cols:
            con.execute("ALTER TABLE social_accounts ADD COLUMN phone_number TEXT")
        if "auth_method" not in sa_cols:
            con.execute(
                "ALTER TABLE social_accounts ADD COLUMN auth_method TEXT NOT NULL DEFAULT 'password'"
            )
        if "session_dir" not in sa_cols:
            con.execute("ALTER TABLE social_accounts ADD COLUMN session_dir TEXT")

        # Estensione di social_dm_log per supportare due engine:
        # - engine: 'A_browser' | 'B_api' | NULL (record legacy IG/TikTok)
        # - api_config_id: FK a whatsapp_api_config (popolata quando engine='B_api')
        # Inoltre account_id va reso NULLABLE: record Motore B non hanno un
        # social_accounts collegato, hanno solo api_config_id.
        sdl_cols = {r["name"] for r in con.execute("PRAGMA table_info(social_dm_log)").fetchall()}
        if "engine" not in sdl_cols:
            con.execute("ALTER TABLE social_dm_log ADD COLUMN engine TEXT")
        if "api_config_id" not in sdl_cols:
            con.execute(
                "ALTER TABLE social_dm_log ADD COLUMN api_config_id INTEGER "
                "REFERENCES whatsapp_api_config(id) ON DELETE SET NULL"
            )
        # Rendi account_id NULLABLE se non lo è già (SQLite non supporta
        # ALTER COLUMN: ricreazione tabella preservando i dati).
        _make_social_dm_log_account_nullable(con)

        # Estensione di contacts per consent management WhatsApp:
        # - whatsapp_consent: 'cold' (default) | 'opt_in' | 'optedout'
        # - whatsapp_last_inbound_at: ultima volta che il contatto ha scritto al
        #   business number (per la 24h-window di Meta Cloud API free-form)
        if "whatsapp_consent" not in ccols:
            con.execute(
                "ALTER TABLE contacts ADD COLUMN whatsapp_consent TEXT NOT NULL DEFAULT 'cold'"
            )
        if "whatsapp_last_inbound_at" not in ccols:
            con.execute("ALTER TABLE contacts ADD COLUMN whatsapp_last_inbound_at TEXT")

        # tasks: campi specifici outreach_whatsapp
        if "whatsapp_engine_preference" not in tcols:
            con.execute(
                "ALTER TABLE tasks ADD COLUMN whatsapp_engine_preference "
                "TEXT NOT NULL DEFAULT 'auto'"
            )
        if "whatsapp_dry_run" not in tcols:
            con.execute(
                "ALTER TABLE tasks ADD COLUMN whatsapp_dry_run "
                "INTEGER NOT NULL DEFAULT 0"
            )
        # Sender single-select per outreach_whatsapp:
        # - whatsapp_account_id: FK opzionale a social_accounts.id (Motore A)
        # - whatsapp_api_config_id: FK opzionale a whatsapp_api_config.id (Motore B)
        # NULL = comportamento legacy (pool default tutti attivi)
        # Valorizzato = SOLO quel sender, fail-fast se è banned/disabled.
        if "whatsapp_account_id" not in tcols:
            con.execute(
                "ALTER TABLE tasks ADD COLUMN whatsapp_account_id INTEGER "
                "REFERENCES social_accounts(id) ON DELETE SET NULL"
            )
        if "whatsapp_api_config_id" not in tcols:
            con.execute(
                "ALTER TABLE tasks ADD COLUMN whatsapp_api_config_id INTEGER "
                "REFERENCES whatsapp_api_config(id) ON DELETE SET NULL"
            )

        # Recon social (R1+R2+R3). Vedi PIANO_RECON_SOCIAL.md
        if "recon_mode" not in tcols:
            con.execute("ALTER TABLE tasks ADD COLUMN recon_mode TEXT")
            # 'url_driven' (R1) | 'exploration' (R2)
        if "recon_social_account_id" not in tcols:
            con.execute(
                "ALTER TABLE tasks ADD COLUMN recon_social_account_id INTEGER "
                "REFERENCES social_accounts(id) ON DELETE SET NULL"
            )
        if "recon_hypothesis" not in tcols:
            con.execute("ALTER TABLE tasks ADD COLUMN recon_hypothesis TEXT")
            # Per R2: l'ipotesi NL ("trova chi ama il sushi")
        if "recon_max_targets_per_day" not in tcols:
            con.execute(
                "ALTER TABLE tasks ADD COLUMN recon_max_targets_per_day "
                "INTEGER NOT NULL DEFAULT 50"
            )
        if "recon_score_threshold" not in tcols:
            con.execute(
                "ALTER TABLE tasks ADD COLUMN recon_score_threshold "
                "INTEGER NOT NULL DEFAULT 6"
            )
        if "seed_queries_friends" not in tcols:
            # JSON-encoded list of names da risolvere contro la friend list /
            # following list dell'account loggato (recon_social). Differente da
            # `seed_queries` che usa search globale + slug match (con possibili
            # falsi positivi su omonimi).
            con.execute(
                "ALTER TABLE tasks ADD COLUMN seed_queries_friends TEXT"
            )
        if "input_asset_filter" not in tcols:
            # JSON-encoded filter per leggere asset esistenti dalla tabella
            # `assets` come input al task (qualifier/outreach_social/ecc.).
            # Schema (v1): `{"asset_type": "palestra"}`. Estendibile a `status`,
            # `tags` in v2. Quando valorizzato ha PRIORITÀ su upstream da
            # workflow_edges e su input_artifact_path nel runner_qualifier.
            con.execute(
                "ALTER TABLE tasks ADD COLUMN input_asset_filter TEXT"
            )

        # Tabelle dedicate recon (R3 checkpoint/resume + dedup visited)
        con.execute("""
            CREATE TABLE IF NOT EXISTS recon_runs (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
              job_id INTEGER REFERENCES jobs(id) ON DELETE SET NULL,
              social_account_id INTEGER REFERENCES social_accounts(id) ON DELETE SET NULL,
              status TEXT NOT NULL DEFAULT 'running',
              started_at TEXT NOT NULL,
              last_active_at TEXT,
              finished_at TEXT,
              target_count INTEGER NOT NULL DEFAULT 0,
              notes TEXT
            )
        """)
        con.execute("""
            CREATE TABLE IF NOT EXISTS recon_checkpoints (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              run_id INTEGER NOT NULL REFERENCES recon_runs(id) ON DELETE CASCADE,
              snapshot_json TEXT NOT NULL,
              created_at TEXT NOT NULL
            )
        """)
        con.execute("""
            CREATE TABLE IF NOT EXISTS recon_visited (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              run_id INTEGER NOT NULL REFERENCES recon_runs(id) ON DELETE CASCADE,
              target_url TEXT NOT NULL,
              target_platform TEXT NOT NULL,
              visited_at TEXT NOT NULL,
              classified INTEGER NOT NULL DEFAULT 0,
              score INTEGER,
              reason TEXT,
              UNIQUE(run_id, target_url)
            )
        """)
        con.execute(
            "CREATE INDEX IF NOT EXISTS idx_recon_visited_run "
            "ON recon_visited(run_id, visited_at)"
        )
        con.execute(
            "CREATE INDEX IF NOT EXISTS idx_recon_runs_status "
            "ON recon_runs(status, task_id)"
        )

        # whatsapp_api_config: una riga per ogni numero Business registrato su
        # Meta Cloud API. Cifrato access_token con AGENTSCRAPER_SECRET.
        con.execute("""
            CREATE TABLE IF NOT EXISTS whatsapp_api_config (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              label TEXT NOT NULL,
              phone_number_id TEXT NOT NULL,
              business_account_id TEXT NOT NULL,
              app_id TEXT,
              encrypted_access_token BLOB NOT NULL,
              default_template_name TEXT,
              default_template_language TEXT NOT NULL DEFAULT 'it',
              status TEXT NOT NULL DEFAULT 'active',
              daily_msg_cap INTEGER NOT NULL DEFAULT 250,
              notes TEXT,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            )
        """)
        con.execute(
            "CREATE INDEX IF NOT EXISTS idx_whatsapp_api_config_status "
            "ON whatsapp_api_config(status)"
        )
        # Indici su workflow_edges (creati qui dopo che workflow_id esiste sicuramente)
        con.execute("CREATE INDEX IF NOT EXISTS idx_workflow_edges_from ON workflow_edges(from_task_id, enabled)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_workflow_edges_workflow ON workflow_edges(workflow_id)")

        # SQLite ALTER TABLE RENAME non aggiorna i FK che puntavano alla tabella rinominata.
        # Quando abbiamo rinominato projects→tasks, i FK in jobs/workflow_edges/contacts/threads
        # restano puntati a 'projects' che non esiste → ogni INSERT fallisce con FK constraint.
        # Ricreiamo queste 4 tabelle se i loro FK referenziano ancora 'projects'.
        _fix_obsolete_fks_to_projects(con)


def _fix_obsolete_fks_to_projects(con: sqlite3.Connection) -> None:
    """SQLite ALTER TABLE RENAME non propaga i FK alle tabelle che riferivano la
    tabella rinominata. Se il FK punta ancora a 'projects', ricreiamo la tabella
    con i FK aggiornati a 'tasks(id)', preservando i dati.
    """
    tables_to_check = ["jobs", "workflow_edges", "contacts", "threads"]
    needs_fix = []
    for tbl in tables_to_check:
        try:
            for r in con.execute(f"PRAGMA foreign_key_list({tbl})").fetchall():
                if r["table"] == "projects":
                    needs_fix.append(tbl)
                    break
        except sqlite3.Error:
            continue
    if not needs_fix:
        return

    # Disabilita FK durante la ricreazione (le foreign key check verranno riabilitate
    # alla prossima connect()).
    con.execute("PRAGMA foreign_keys = OFF")
    try:
        for tbl in needs_fix:
            cols_info = con.execute(f"PRAGMA table_info({tbl})").fetchall()
            col_names = [c["name"] for c in cols_info]
            new_table_sql = _build_table_sql(tbl, col_names)
            if not new_table_sql:
                continue
            tmp = f"_{tbl}_new"
            con.execute(f"DROP TABLE IF EXISTS {tmp}")
            con.execute(new_table_sql.replace(f"CREATE TABLE {tbl}", f"CREATE TABLE {tmp}"))
            cols_csv = ", ".join(col_names)
            con.execute(f"INSERT INTO {tmp} ({cols_csv}) SELECT {cols_csv} FROM {tbl}")
            con.execute(f"DROP TABLE {tbl}")
            con.execute(f"ALTER TABLE {tmp} RENAME TO {tbl}")
        # ricrea indici principali
        con.execute("CREATE INDEX IF NOT EXISTS idx_jobs_task ON jobs(task_id, id DESC)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_workflow_edges_from ON workflow_edges(from_task_id, enabled)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_workflow_edges_workflow ON workflow_edges(workflow_id)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_contacts_email ON contacts(email)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_contacts_telegram_chat ON contacts(telegram_chat_id)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_contacts_status ON contacts(status)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_contacts_source ON contacts(source_task_id)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_threads_contact ON threads(contact_id)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_threads_status ON threads(status)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_threads_external ON threads(channel, external_id)")
    finally:
        con.execute("PRAGMA foreign_keys = ON")


def _make_social_dm_log_account_nullable(con: sqlite3.Connection) -> None:
    """Rende `social_dm_log.account_id` NULLABLE per ospitare i record del Motore
    B (Meta Cloud API) che non hanno un social_accounts collegato, solo un
    api_config_id. SQLite non supporta ALTER COLUMN: ricreiamo la tabella
    preservando i dati esistenti. Idempotente: ricrea solo se necessario.
    """
    info = con.execute("PRAGMA table_info(social_dm_log)").fetchall()
    by_name = {r["name"]: r for r in info}
    account_col = by_name.get("account_id")
    if not account_col or int(account_col["notnull"]) == 0:
        return  # già nullable o tabella assente, nulla da fare

    col_names = [r["name"] for r in info]
    cols_csv = ", ".join(col_names)
    con.execute("PRAGMA foreign_keys = OFF")
    try:
        con.execute("DROP TABLE IF EXISTS _social_dm_log_new")
        con.execute("""
            CREATE TABLE _social_dm_log_new (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              account_id INTEGER REFERENCES social_accounts(id) ON DELETE CASCADE,
              job_id INTEGER REFERENCES jobs(id) ON DELETE SET NULL,
              target_contact_id INTEGER REFERENCES contacts(id) ON DELETE SET NULL,
              target_platform TEXT NOT NULL,
              target_username TEXT NOT NULL,
              message TEXT NOT NULL,
              sent_at TEXT NOT NULL,
              ok INTEGER NOT NULL,
              reason TEXT,
              health_post TEXT,
              engine TEXT,
              api_config_id INTEGER REFERENCES whatsapp_api_config(id) ON DELETE SET NULL
            )
        """)
        con.execute(
            f"INSERT INTO _social_dm_log_new ({cols_csv}) SELECT {cols_csv} FROM social_dm_log"
        )
        con.execute("DROP TABLE social_dm_log")
        con.execute("ALTER TABLE _social_dm_log_new RENAME TO social_dm_log")
        con.execute(
            "CREATE INDEX IF NOT EXISTS idx_social_dm_log_account "
            "ON social_dm_log(account_id, sent_at)"
        )
        con.execute(
            "CREATE INDEX IF NOT EXISTS idx_social_dm_log_target "
            "ON social_dm_log(target_contact_id)"
        )
    finally:
        con.execute("PRAGMA foreign_keys = ON")


def _build_table_sql(tbl: str, cols: list[str]) -> str | None:
    """Definizione canonica delle 4 tabelle che potrebbero avere FK obsoleti.

    Ritorna lo SQL di CREATE TABLE con i FK corretti su tasks(id), preservando
    SOLO le colonne effettivamente presenti (per supportare DB di versioni vecchie).
    """
    if tbl == "jobs":
        all_cols = [
            ("id", "INTEGER PRIMARY KEY AUTOINCREMENT"),
            ("task_id", "INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE"),
            ("status", "TEXT NOT NULL"),
            ("started_at", "TEXT"),
            ("finished_at", "TEXT"),
            ("log", "TEXT NOT NULL DEFAULT ''"),
            ("result_path", "TEXT"),
            ("error", "TEXT"),
            ("control_signal", "TEXT"),
            ("triggered_by_job_id", "INTEGER REFERENCES jobs(id) ON DELETE SET NULL"),
            ("workflow_run_id", "INTEGER REFERENCES workflow_runs(id) ON DELETE SET NULL"),
        ]
    elif tbl == "workflow_edges":
        all_cols = [
            ("id", "INTEGER PRIMARY KEY AUTOINCREMENT"),
            ("workflow_id", "INTEGER REFERENCES workflows(id) ON DELETE CASCADE"),
            ("from_task_id", "INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE"),
            ("to_task_id", "INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE"),
            ("trigger_event", "TEXT NOT NULL DEFAULT 'on_done'"),
            ("pass_artifact", "TEXT"),
            ("enabled", "INTEGER NOT NULL DEFAULT 1"),
            ("created_at", "TEXT NOT NULL"),
        ]
    elif tbl == "contacts":
        all_cols = [
            ("id", "INTEGER PRIMARY KEY AUTOINCREMENT"),
            ("source_task_id", "INTEGER REFERENCES tasks(id) ON DELETE SET NULL"),
            ("source_job_id", "INTEGER REFERENCES jobs(id) ON DELETE SET NULL"),
            ("source_url", "TEXT"),
            ("source_domain", "TEXT"),
            ("display_name", "TEXT"),
            ("email", "TEXT"),
            ("telegram_username", "TEXT"),
            ("telegram_chat_id", "TEXT"),
            ("raw_json", "TEXT"),
            ("status", "TEXT NOT NULL DEFAULT 'new'"),
            ("qualifier_score", "INTEGER"),
            ("notes", "TEXT"),
            ("created_at", "TEXT NOT NULL"),
            ("updated_at", "TEXT NOT NULL"),
        ]
    elif tbl == "threads":
        all_cols = [
            ("id", "INTEGER PRIMARY KEY AUTOINCREMENT"),
            ("contact_id", "INTEGER NOT NULL REFERENCES contacts(id) ON DELETE CASCADE"),
            ("channel", "TEXT NOT NULL"),
            ("external_id", "TEXT"),
            ("subject", "TEXT"),
            ("status", "TEXT NOT NULL DEFAULT 'open'"),
            ("task_id", "INTEGER REFERENCES tasks(id) ON DELETE SET NULL"),
            ("last_msg_at", "TEXT"),
            ("created_at", "TEXT NOT NULL"),
        ]
    else:
        return None

    # filtra solo le colonne presenti nel DB attuale
    parts = [f"  {n} {d}" for n, d in all_cols if n in cols]
    if not parts:
        return None
    return f"CREATE TABLE {tbl} (\n" + ",\n".join(parts) + "\n)"


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    con = sqlite3.connect(DB_PATH, detect_types=sqlite3.PARSE_DECLTYPES)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    try:
        yield con
        con.commit()
    finally:
        con.close()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


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


def _row_to_task(row: sqlite3.Row) -> dict[str, Any]:
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

def list_tasks() -> list[dict[str, Any]]:
    with connect() as con:
        rows = con.execute("SELECT * FROM tasks ORDER BY id DESC").fetchall()
    return [_row_to_task(r) for r in rows]


def get_task(task_id: int) -> dict[str, Any] | None:
    with connect() as con:
        row = con.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    return _row_to_task(row) if row else None


def set_task_disabled(task_id: int, disabled: bool) -> None:
    """Imposta il flag `disabled` (0/1) per un task."""
    with connect() as con:
        con.execute(
            "UPDATE tasks SET disabled = ? WHERE id = ?",
            (1 if disabled else 0, task_id),
        )


def set_workflow_disabled(workflow_id: int, disabled: bool) -> None:
    """Imposta il flag `disabled` (0/1) per un workflow."""
    with connect() as con:
        con.execute(
            "UPDATE workflows SET disabled = ? WHERE id = ?",
            (1 if disabled else 0, workflow_id),
        )


def create_task(data: dict[str, Any]) -> int:
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
                                  created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                ts,
                ts,
            ),
        )
        return int(cur.lastrowid)


def update_task(task_id: int, data: dict[str, Any]) -> None:
    with connect() as con:
        con.execute(
            """
            UPDATE tasks
            SET name = ?, description = ?, objective = ?, seed_queries = ?,
                allowed_domains = ?, blocked_domains = ?, max_iterations = ?,
                model = ?, output_format = ?, cron = ?, agent_mode = ?,
                extraction_template = ?, extraction_schema = ?,
                llm_provider = ?, llm_base_url = ?, llm_api_key = ?,
                input_artifact_path = ?, message_template = ?, message_subject = ?,
                message_channels = ?, responder_system_prompt = ?,
                bulk_concurrency = ?, target_cap_per_site = ?, refresh_policy_days = ?,
                bulk_rate_limit_per_sec = ?,
                bulk_extraction_method = ?, bulk_css_selectors = ?,
                crawler_enabled = ?, crawler_url_pattern = ?, crawler_max_depth = ?,
                discovery_llm_provider = ?, discovery_llm_model = ?,
                discovery_llm_api_key = ?, max_discovery_retries = ?,
                browser_llm_provider = ?, browser_llm_model = ?, browser_llm_api_key = ?,
                rating = ?, notes = ?, status_tag = ?,
                social_platform = ?, outreach_intent = ?, message_template_variants = ?,
                max_dms_per_run = ?, max_dms_per_session = ?, headed = ?,
                target_contact_ids = ?,
                whatsapp_engine_preference = ?, whatsapp_dry_run = ?,
                whatsapp_account_id = ?, whatsapp_api_config_id = ?,
                recon_mode = ?, recon_social_account_id = ?, recon_hypothesis = ?,
                recon_max_targets_per_day = ?, recon_score_threshold = ?,
                seed_queries_friends = ?,
                input_asset_filter = ?,
                updated_at = ?
            WHERE id = ?
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
                now_iso(),
                task_id,
            ),
        )


def delete_task(task_id: int) -> None:
    with connect() as con:
        con.execute("DELETE FROM tasks WHERE id = ?", (task_id,))


# ----- Jobs -----

def create_job(
    task_id: int,
    triggered_by_job_id: int | None = None,
    workflow_run_id: int | None = None,
) -> int:
    with connect() as con:
        cur = con.execute(
            "INSERT INTO jobs (task_id, status, log, triggered_by_job_id, workflow_run_id) "
            "VALUES (?, 'queued', '', ?, ?)",
            (task_id, triggered_by_job_id, workflow_run_id),
        )
        return int(cur.lastrowid)


def get_job(job_id: int) -> dict[str, Any] | None:
    with connect() as con:
        row = con.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    return dict(row) if row else None


def list_jobs(task_id: int) -> list[dict[str, Any]]:
    with connect() as con:
        rows = con.execute(
            "SELECT * FROM jobs WHERE task_id = ? ORDER BY id DESC LIMIT 100",
            (task_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def list_jobs_for_workflow_run(workflow_run_id: int) -> list[dict[str, Any]]:
    with connect() as con:
        rows = con.execute(
            "SELECT * FROM jobs WHERE workflow_run_id = ? ORDER BY id ASC",
            (workflow_run_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def update_job(job_id: int, **fields: Any) -> None:
    if not fields:
        return
    cols = ", ".join(f"{k} = ?" for k in fields)
    values = list(fields.values()) + [job_id]
    with connect() as con:
        con.execute(f"UPDATE jobs SET {cols} WHERE id = ?", values)


def append_job_log(job_id: int, line: str) -> None:
    stamp = now_iso()
    entry = f"[{stamp}] {line}\n"
    with connect() as con:
        con.execute(
            "UPDATE jobs SET log = COALESCE(log, '') || ? WHERE id = ?",
            (entry, job_id),
        )


def latest_job(task_id: int) -> dict[str, Any] | None:
    with connect() as con:
        row = con.execute(
            "SELECT * FROM jobs WHERE task_id = ? ORDER BY id DESC LIMIT 1",
            (task_id,),
        ).fetchone()
    return dict(row) if row else None


def set_control_signal(job_id: int, signal: str | None) -> None:
    with connect() as con:
        con.execute("UPDATE jobs SET control_signal = ? WHERE id = ?", (signal, job_id))


def get_control_signal(job_id: int) -> str | None:
    with connect() as con:
        row = con.execute("SELECT control_signal FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if not row:
        return None
    return row["control_signal"]


# ===========================================================================
# Workflows (entità di prima classe)
# ===========================================================================

def list_workflows() -> list[dict[str, Any]]:
    with connect() as con:
        rows = con.execute("SELECT * FROM workflows ORDER BY id DESC").fetchall()
    return [dict(r) for r in rows]


def get_workflow(workflow_id: int) -> dict[str, Any] | None:
    with connect() as con:
        row = con.execute("SELECT * FROM workflows WHERE id = ?", (workflow_id,)).fetchone()
    return dict(row) if row else None


def create_workflow(name: str, description: str | None = None) -> int:
    ts = now_iso()
    with connect() as con:
        cur = con.execute(
            "INSERT INTO workflows (name, description, created_at, updated_at) VALUES (?, ?, ?, ?)",
            (name, description, ts, ts),
        )
        return int(cur.lastrowid)


def update_workflow(workflow_id: int, name: str, description: str | None) -> None:
    with connect() as con:
        con.execute(
            "UPDATE workflows SET name = ?, description = ?, updated_at = ? WHERE id = ?",
            (name, description, now_iso(), workflow_id),
        )


def delete_workflow(workflow_id: int) -> None:
    with connect() as con:
        con.execute("DELETE FROM workflows WHERE id = ?", (workflow_id,))


# ----- Workflow runs (executions of a workflow) -----

def create_workflow_run(workflow_id: int) -> int:
    with connect() as con:
        cur = con.execute(
            "INSERT INTO workflow_runs (workflow_id, status, started_at) "
            "VALUES (?, 'running', ?)",
            (workflow_id, now_iso()),
        )
        return int(cur.lastrowid)


def update_workflow_run_status(run_id: int, status: str) -> None:
    with connect() as con:
        if status in ("done", "error", "cancelled"):
            con.execute(
                "UPDATE workflow_runs SET status = ?, finished_at = ? WHERE id = ?",
                (status, now_iso(), run_id),
            )
        else:
            con.execute(
                "UPDATE workflow_runs SET status = ? WHERE id = ?",
                (status, run_id),
            )


def list_workflow_runs(workflow_id: int, limit: int = 50) -> list[dict[str, Any]]:
    with connect() as con:
        rows = con.execute(
            "SELECT * FROM workflow_runs WHERE workflow_id = ? ORDER BY id DESC LIMIT ?",
            (workflow_id, limit),
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
        sql += " AND workflow_id = ?"
        args.append(workflow_id)
    if from_task_id is not None:
        sql += " AND from_task_id = ?"
        args.append(from_task_id)
    if to_task_id is not None:
        sql += " AND to_task_id = ?"
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
            VALUES (?, ?, ?, ?, ?, ?, ?)
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
        return int(cur.lastrowid)


def delete_edge(edge_id: int) -> None:
    with connect() as con:
        con.execute("DELETE FROM workflow_edges WHERE id = ?", (edge_id,))


def toggle_edge(edge_id: int, enabled: bool) -> None:
    with connect() as con:
        con.execute(
            "UPDATE workflow_edges SET enabled = ? WHERE id = ?",
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
                "WHERE workflow_id = ? AND enabled = 1",
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
                "SELECT id FROM contacts WHERE LOWER(email) = ? LIMIT 1", (email,)
            ).fetchone()
        if not existing and tg_user:
            existing = con.execute(
                "SELECT id FROM contacts WHERE LOWER(telegram_username) = ? LIMIT 1",
                (tg_user,),
            ).fetchone()
        if not existing and source_url:
            # Fallback dedup per source_url: necessario per profili con solo
            # social/whatsapp/sitoweb (no email/tg) che altrimenti verrebbero
            # ri-inseriti ad ogni run del qualifier.
            existing = con.execute(
                "SELECT id FROM contacts WHERE source_url = ? LIMIT 1", (source_url,)
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
                  source_task_id    = COALESCE(?, source_task_id),
                  source_job_id     = COALESCE(?, source_job_id),
                  source_url        = COALESCE(?, source_url),
                  source_domain     = COALESCE(?, source_domain),
                  display_name      = COALESCE(?, display_name),
                  email             = COALESCE(?, email),
                  telegram_username = COALESCE(?, telegram_username),
                  whatsapp          = COALESCE(?, whatsapp),
                  sitoweb           = COALESCE(?, sitoweb),
                  social_json       = COALESCE(?, social_json),
                  raw_json          = COALESCE(?, raw_json),
                  asset_id          = COALESCE(?, asset_id),
                  updated_at        = ?
                WHERE id = ?
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
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
        return int(cur.lastrowid)


def get_contact(contact_id: int) -> dict[str, Any] | None:
    with connect() as con:
        row = con.execute("SELECT * FROM contacts WHERE id = ?", (contact_id,)).fetchone()
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
        sql += " AND status = ?"
        args.append(status)
    if source_task_id is not None:
        sql += " AND source_task_id = ?"
        args.append(source_task_id)
    if source_domain:
        sql += " AND source_domain = ?"
        args.append(source_domain)
    if search:
        like = f"%{search.strip()}%"
        sql += (
            " AND ("
            "  LOWER(COALESCE(display_name,'')) LIKE LOWER(?) OR"
            "  LOWER(COALESCE(email,'')) LIKE LOWER(?) OR"
            "  LOWER(COALESCE(telegram_username,'')) LIKE LOWER(?) OR"
            "  LOWER(COALESCE(whatsapp,'')) LIKE LOWER(?) OR"
            "  LOWER(COALESCE(sitoweb,'')) LIKE LOWER(?) OR"
            "  LOWER(COALESCE(source_url,'')) LIKE LOWER(?) OR"
            "  LOWER(COALESCE(source_domain,'')) LIKE LOWER(?) OR"
            "  LOWER(COALESCE(notes,'')) LIKE LOWER(?) OR"
            "  LOWER(COALESCE(social_json,'')) LIKE LOWER(?)"
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
            sql += " AND social_json LIKE ?"
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
        sql += " AND qualifier_score >= ?"
        args.append(int(score_min))
    return sql, args


def count_contacts(
    status: str | None = None,
    source_task_id: int | None = None,
    source_domain: str | None = None,
    search: str | None = None,
    channel: str | None = None,
    score_min: int | None = None,
) -> int:
    """Conta i contatti che matchano i filtri di list_contacts. Per paginazione UI."""
    where_sql, args = _build_contacts_filters(
        status, source_task_id, source_domain, search, channel, score_min
    )
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
) -> list[dict[str, Any]]:
    where_sql, args = _build_contacts_filters(
        status, source_task_id, source_domain, search, channel, score_min
    )
    sql = "SELECT * FROM contacts" + where_sql + " ORDER BY id DESC LIMIT ? OFFSET ?"
    args.extend([limit, max(0, int(offset))])
    with connect() as con:
        rows = con.execute(sql, args).fetchall()
    return [dict(r) for r in rows]


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
        "ORDER BY id DESC LIMIT ?"
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
        "ORDER BY id DESC LIMIT ?"
    )
    with connect() as con:
        rows = con.execute(sql, (max(1, int(limit)),)).fetchall()
    return [dict(r) for r in rows]


def get_contacts_by_ids(ids: list[int]) -> list[dict[str, Any]]:
    """Recupera contatti per lista di ID. Niente filtri su status."""
    clean = [int(i) for i in ids if str(i).strip().lstrip("-").isdigit()]
    if not clean:
        return []
    placeholders = ",".join("?" for _ in clean)
    sql = f"SELECT * FROM contacts WHERE id IN ({placeholders})"
    with connect() as con:
        rows = con.execute(sql, clean).fetchall()
    return [dict(r) for r in rows]


def list_contact_source_domains(limit: int = 100) -> list[tuple[str, int]]:
    """Lista dei domini di provenienza piu' frequenti (per dropdown filtro UI)."""
    sql = (
        "SELECT source_domain, COUNT(*) AS n FROM contacts "
        "WHERE source_domain IS NOT NULL AND source_domain != '' "
        "GROUP BY source_domain ORDER BY n DESC LIMIT ?"
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
        sets.append(f"{k} = ?")
        vals.append(v)
    if not sets:
        return
    sets.append("updated_at = ?")
    vals.append(now_iso())
    vals.append(contact_id)
    sql = f"UPDATE contacts SET {', '.join(sets)} WHERE id = ?"
    with connect() as con:
        con.execute(sql, vals)


def update_contact_status(contact_id: int, status: str, notes: str | None = None) -> None:
    with connect() as con:
        if notes is not None:
            con.execute(
                "UPDATE contacts SET status = ?, notes = ?, updated_at = ? WHERE id = ?",
                (status, notes, now_iso(), contact_id),
            )
        else:
            con.execute(
                "UPDATE contacts SET status = ?, updated_at = ? WHERE id = ?",
                (status, now_iso(), contact_id),
            )


def update_contact_qualifier(contact_id: int, score: int, status: str) -> None:
    with connect() as con:
        con.execute(
            "UPDATE contacts SET qualifier_score = ?, status = ?, updated_at = ? WHERE id = ?",
            (score, status, now_iso(), contact_id),
        )


def find_contact_by_email(email: str) -> dict[str, Any] | None:
    with connect() as con:
        row = con.execute(
            "SELECT * FROM contacts WHERE LOWER(email) = ? LIMIT 1",
            (email.strip().lower(),),
        ).fetchone()
    return dict(row) if row else None


def find_contact_by_telegram_chat(chat_id: str) -> dict[str, Any] | None:
    with connect() as con:
        row = con.execute(
            "SELECT * FROM contacts WHERE telegram_chat_id = ? LIMIT 1",
            (str(chat_id),),
        ).fetchone()
    return dict(row) if row else None


def set_contact_telegram_chat(contact_id: int, chat_id: str) -> None:
    with connect() as con:
        con.execute(
            "UPDATE contacts SET telegram_chat_id = ?, updated_at = ? WHERE id = ?",
            (str(chat_id), now_iso(), contact_id),
        )


def delete_contact(contact_id: int) -> int:
    """Cancella un contatto. Threads e messages cascade-deletono per FK ON DELETE CASCADE."""
    with connect() as con:
        cur = con.execute("DELETE FROM contacts WHERE id = ?", (contact_id,))
        return cur.rowcount


def delete_contacts_bulk(contact_ids: list[int]) -> int:
    ids = [int(i) for i in contact_ids if i]
    if not ids:
        return 0
    placeholders = ",".join("?" for _ in ids)
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
               WHERE asset_type = ?
                     AND (source_url_canonical = ? OR source_url = ?)
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
                "SELECT id FROM assets WHERE source_url_canonical = ? AND asset_type = ? LIMIT 1",
                (source_url_canonical, asset_type),
            ).fetchone()
            existing = int(row["id"]) if row else None
        if existing is None and source_url:
            row = con.execute(
                "SELECT id FROM assets WHERE source_url = ? AND asset_type = ? LIMIT 1",
                (source_url, asset_type),
            ).fetchone()
            existing = int(row["id"]) if row else None

        if existing:
            con.execute(
                """
                UPDATE assets SET
                  source_task_id       = COALESCE(?, source_task_id),
                  source_job_id        = COALESCE(?, source_job_id),
                  source_url_canonical = COALESCE(?, source_url_canonical),
                  source_domain        = COALESCE(?, source_domain),
                  title                = COALESCE(?, title),
                  raw_json             = ?,
                  notes                = COALESCE(?, notes),
                  updated_at           = ?
                WHERE id = ?
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
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'new', NULL, ?, ?, ?)
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
            asset_id = int(cur.lastrowid)

        # Tag: sostituiamo l'intero set per asset (semantica idempotente)
        if tags is not None:
            con.execute("DELETE FROM asset_tags WHERE asset_id = ?", (asset_id,))
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
                        "INSERT OR IGNORE INTO asset_tags (asset_id, tag_key, tag_value) VALUES (?, ?, ?)",
                        (asset_id, tk, tv),
                    )

    return asset_id


def get_asset(asset_id: int) -> dict[str, Any] | None:
    with connect() as con:
        row = con.execute("SELECT * FROM assets WHERE id = ?", (asset_id,)).fetchone()
        if not row:
            return None
        asset = dict(row)
        tag_rows = con.execute(
            "SELECT tag_key, tag_value FROM asset_tags WHERE asset_id = ? ORDER BY tag_key, tag_value",
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
                f"AND {alias}.tag_key = ? AND {alias}.tag_value = ?"
            )
            args.extend([k.lower(), v])
    sql += " WHERE 1=1"
    if asset_type:
        sql += " AND a.asset_type = ?"
        args.append(asset_type)
    if status:
        sql += " AND a.status = ?"
        args.append(status)
    if source_task_id is not None:
        sql += " AND a.source_task_id = ?"
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
                f"AND {alias}.tag_key = ? AND {alias}.tag_value = ?"
            )
            args.extend([k.lower(), v])
    sql += " WHERE 1=1"
    if asset_type:
        sql += " AND a.asset_type = ?"
        args.append(asset_type)
    if status:
        sql += " AND a.status = ?"
        args.append(status)
    if source_task_id is not None:
        sql += " AND a.source_task_id = ?"
        args.append(source_task_id)
    sql += " ORDER BY a.id DESC LIMIT ? OFFSET ?"
    args.append(limit)
    args.append(max(0, int(offset)))
    with connect() as con:
        rows = con.execute(sql, args).fetchall()
        results = [dict(r) for r in rows]
        if results:
            ids = [r["id"] for r in results]
            placeholders = ",".join("?" for _ in ids)
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
                "UPDATE assets SET status = ?, notes = ?, updated_at = ? WHERE id = ?",
                (status, notes, now_iso(), asset_id),
            )
        else:
            con.execute(
                "UPDATE assets SET status = ?, updated_at = ? WHERE id = ?",
                (status, now_iso(), asset_id),
            )


def delete_asset(asset_id: int) -> int:
    with connect() as con:
        cur = con.execute("DELETE FROM assets WHERE id = ?", (asset_id,))
        return cur.rowcount


def delete_assets_bulk(asset_ids: list[int]) -> int:
    """Cancella in massa gli asset indicati. Le `asset_tags` cascade-deletono per FK.
    Ritorna il numero di righe rimosse.
    """
    ids = [int(i) for i in asset_ids if i]
    if not ids:
        return 0
    placeholders = ",".join("?" for _ in ids)
    with connect() as con:
        cur = con.execute(f"DELETE FROM assets WHERE id IN ({placeholders})", ids)
        return cur.rowcount


def update_asset_qualifier(asset_id: int, score: int, status: str, notes: str | None = None) -> None:
    """Aggiorna asset.qualifier_score + status (qualified/rejected) in un colpo.

    CASCATA: propaga `status` e `qualifier_score` ANCHE ai `contacts` collegati
    via `asset_id`. Cosi' i runner downstream (outreach, ecc.) che leggono dalla
    tabella contacts trovano subito il nuovo stato. Pattern: status='qualified'
    sull'asset → status='qualified' sui contacts linkati.
    """
    ts = now_iso()
    with connect() as con:
        if notes is not None:
            con.execute(
                "UPDATE assets SET qualifier_score = ?, status = ?, notes = ?, updated_at = ? WHERE id = ?",
                (int(score), status, notes, ts, asset_id),
            )
        else:
            con.execute(
                "UPDATE assets SET qualifier_score = ?, status = ?, updated_at = ? WHERE id = ?",
                (int(score), status, ts, asset_id),
            )
        # Cascata sui contacts. Update solo se lo status del contact e' ancora 'new'
        # o uguale a quello dell'asset (preserva 'optedout' e altri stati terminali).
        con.execute(
            """
            UPDATE contacts SET
              status = ?,
              qualifier_score = ?,
              updated_at = ?
            WHERE asset_id = ?
              AND status IN ('new', 'qualified', 'rejected')
            """,
            (status, int(score), ts, asset_id),
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
        sql += " WHERE a.asset_type = ?"
        args.append(asset_type)
    sql += " ORDER BY t.tag_key"
    with connect() as con:
        rows = con.execute(sql, args).fetchall()
    return [r["tag_key"] for r in rows]


def list_asset_tag_values(tag_key: str, asset_type: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
    sql = (
        "SELECT t.tag_value AS v, COUNT(*) AS n FROM asset_tags t "
        "JOIN assets a ON a.id = t.asset_id WHERE t.tag_key = ?"
    )
    args: list[Any] = [tag_key.lower()]
    if asset_type:
        sql += " AND a.asset_type = ?"
        args.append(asset_type)
    sql += " GROUP BY t.tag_value ORDER BY n DESC, t.tag_value LIMIT ?"
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
    sql = "SELECT * FROM site_patterns WHERE registrable_domain = ?"
    args: list[Any] = [registrable_domain.lower()]
    if asset_type:
        sql += " AND (asset_type = ? OR asset_type IS NULL)"
        args.append(asset_type)
    if status:
        sql += " AND status = ?"
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
            "WHERE registrable_domain = ? AND pattern = ?",
            (rd, pattern),
        ).fetchone()
        if row:
            pid = int(row["id"])
            updates: list[str] = []
            args: list[Any] = []
            if not row["asset_type"] and asset_type:
                updates.append("asset_type = ?")
                args.append(asset_type)
            if (not row["regex"]) and regex:
                updates.append("regex = ?")
                args.append(regex)
            if updates:
                args.extend([ts, pid])
                con.execute(
                    f"UPDATE site_patterns SET {', '.join(updates)}, updated_at = ? WHERE id = ?",
                    args,
                )
            return pid
        cur = con.execute(
            """
            INSERT INTO site_patterns (
              registrable_domain, pattern, regex, asset_type, status,
              hits, successes, failures,
              source_task_id, source_job_id, notes, created_at, updated_at
            ) VALUES (?, ?, ?, ?, 'candidate', 0, 0, 0, ?, ?, ?, ?, ?)
            """,
            (rd, pattern, regex, asset_type, source_task_id, source_job_id, notes, ts, ts),
        )
        return int(cur.lastrowid)


def record_pattern_run(pattern_id: int, hits: int = 0, successes: int = 0, failures: int = 0) -> None:
    with connect() as con:
        con.execute(
            """
            UPDATE site_patterns
               SET hits      = hits + ?,
                   successes = successes + ?,
                   failures  = failures + ?,
                   updated_at = ?
             WHERE id = ?
            """,
            (int(hits), int(successes), int(failures), now_iso(), pattern_id),
        )


def set_site_pattern_status(pattern_id: int, status: str, notes: str | None = None) -> None:
    with connect() as con:
        if notes is not None:
            con.execute(
                "UPDATE site_patterns SET status = ?, notes = ?, updated_at = ? WHERE id = ?",
                (status, notes, now_iso(), pattern_id),
            )
        else:
            con.execute(
                "UPDATE site_patterns SET status = ?, updated_at = ? WHERE id = ?",
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
        sql += " AND registrable_domain = ?"
        args.append(registrable_domain.lower())
    if status:
        sql += " AND status = ?"
        args.append(status)
    sql += " ORDER BY registrable_domain, (status='confirmed') DESC, successes DESC, id DESC LIMIT ?"
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
            "SELECT id, status, hits, successes, failures FROM site_patterns WHERE id = ?",
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
               WHERE registrable_domain = ? AND asset_type = ?
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
            "SELECT id FROM site_playbooks WHERE registrable_domain = ? AND asset_type = ?",
            (domain_l, asset_type),
        ).fetchone()
        if existing:
            pb_id = int(existing["id"])
            con.execute(
                """UPDATE site_playbooks
                   SET playbook = ?, source_runner = ?, source_job_id = ?,
                       transferable = ?, status = 'active', failures = 0,
                       updated_at = ?
                   WHERE id = ?""",
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
               VALUES (?, ?, ?, ?, ?, ?, 'active', 0, 0, 0, ?, ?)""",
            (
                domain_l, asset_type, playbook, source_runner, source_job_id,
                1 if transferable else 0, ts, ts,
            ),
        )
        return int(cur.lastrowid)


def bump_playbook_hits(playbook_id: int) -> None:
    with connect() as con:
        con.execute(
            "UPDATE site_playbooks SET hits = hits + 1, updated_at = ? WHERE id = ?",
            (now_iso(), playbook_id),
        )


def bump_playbook_outcome(playbook_id: int, *, success: bool, stale_threshold: int = 3) -> str | None:
    """Bump successes o failures. Auto-stale a `failures >= stale_threshold`.
    Ritorna 'stale' se il playbook e' stato auto-archiviato, None altrimenti."""
    ts = now_iso()
    with connect() as con:
        if success:
            con.execute(
                "UPDATE site_playbooks SET successes = successes + 1, failures = 0, updated_at = ? WHERE id = ?",
                (ts, playbook_id),
            )
            return None
        # failure: bump e check soglia
        con.execute(
            "UPDATE site_playbooks SET failures = failures + 1, updated_at = ? WHERE id = ?",
            (ts, playbook_id),
        )
        row = con.execute(
            "SELECT failures, status FROM site_playbooks WHERE id = ?", (playbook_id,)
        ).fetchone()
        if row and int(row["failures"]) >= stale_threshold and row["status"] == "active":
            con.execute(
                "UPDATE site_playbooks SET status = 'stale', updated_at = ? WHERE id = ?",
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
        sql += " AND registrable_domain = ?"
        params.append(registrable_domain.lower())
    if status:
        sql += " AND status = ?"
        params.append(status)
    sql += " ORDER BY updated_at DESC LIMIT ?"
    params.append(int(limit))
    with connect() as con:
        rows = con.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def delete_site_playbook(playbook_id: int) -> None:
    with connect() as con:
        con.execute("DELETE FROM site_playbooks WHERE id = ?", (playbook_id,))


def delete_site_pattern(pattern_id: int) -> None:
    with connect() as con:
        con.execute("DELETE FROM site_patterns WHERE id = ?", (pattern_id,))


def delete_site_patterns_by_domain(registrable_domain: str) -> int:
    """Cancella tutti i pattern di un dominio. Ritorna il numero di righe cancellate."""
    if not registrable_domain:
        return 0
    with connect() as con:
        cur = con.execute(
            "DELETE FROM site_patterns WHERE registrable_domain = ?",
            (registrable_domain.lower(),),
        )
        return int(cur.rowcount or 0)


def delete_site_playbooks_by_domain(registrable_domain: str) -> int:
    """Cancella tutti i playbook di un dominio. Ritorna n righe cancellate."""
    if not registrable_domain:
        return 0
    with connect() as con:
        cur = con.execute(
            "DELETE FROM site_playbooks WHERE registrable_domain = ?",
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
                "SELECT id FROM threads WHERE channel = ? AND external_id = ? LIMIT 1",
                (channel, external_id),
            ).fetchone()
            if row:
                return int(row["id"])
        # cerca un thread aperto sullo stesso contatto/canale come fallback
        row = con.execute(
            "SELECT id FROM threads WHERE contact_id = ? AND channel = ? AND status='open' "
            "ORDER BY id DESC LIMIT 1",
            (contact_id, channel),
        ).fetchone()
        if row:
            return int(row["id"])
        cur = con.execute(
            """
            INSERT INTO threads (contact_id, channel, external_id, subject, status,
                                 task_id, created_at)
            VALUES (?, ?, ?, ?, 'open', ?, ?)
            """,
            (contact_id, channel, external_id, subject, task_id, now_iso()),
        )
        return int(cur.lastrowid)


def get_thread(thread_id: int) -> dict[str, Any] | None:
    with connect() as con:
        row = con.execute("SELECT * FROM threads WHERE id = ?", (thread_id,)).fetchone()
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
        sql += " AND t.channel = ?"
        args.append(channel)
    if status:
        sql += " AND t.status = ?"
        args.append(status)
    if task_id is not None:
        sql += " AND t.task_id = ?"
        args.append(task_id)
    sql += " ORDER BY COALESCE(t.last_msg_at, t.created_at) DESC LIMIT ?"
    args.append(limit)
    with connect() as con:
        rows = con.execute(sql, args).fetchall()
    return [dict(r) for r in rows]


def update_thread_status(thread_id: int, status: str) -> None:
    with connect() as con:
        con.execute(
            "UPDATE threads SET status = ? WHERE id = ?", (status, thread_id)
        )


def touch_thread(thread_id: int) -> None:
    with connect() as con:
        con.execute(
            "UPDATE threads SET last_msg_at = ? WHERE id = ?", (now_iso(), thread_id)
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
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
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
        return int(cur.lastrowid)


def update_message(message_id: int, **fields: Any) -> None:
    if not fields:
        return
    cols = ", ".join(f"{k} = ?" for k in fields)
    values = list(fields.values()) + [message_id]
    with connect() as con:
        con.execute(f"UPDATE messages SET {cols} WHERE id = ?", values)


def list_messages(thread_id: int) -> list[dict[str, Any]]:
    with connect() as con:
        rows = con.execute(
            "SELECT * FROM messages WHERE thread_id = ? ORDER BY id",
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
            "SELECT * FROM channel_config WHERE channel = ?", (channel,)
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
            VALUES (?, ?, ?, ?)
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
            VALUES (?, ?, ?, ?)
            """,
            (role, body, payload, now_iso()),
        )
        return int(cur.lastrowid)


def list_orchestrator_messages(limit: int = 100) -> list[dict[str, Any]]:
    with connect() as con:
        rows = con.execute(
            """
            SELECT * FROM orchestrator_messages
            ORDER BY id DESC
            LIMIT ?
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
             notes, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
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
        return int(cur.lastrowid)


def list_social_accounts(platform: str | None = None, status: str | None = None) -> list[dict]:
    sql = "SELECT * FROM social_accounts WHERE 1=1"
    args: list = []
    if platform:
        sql += " AND platform = ?"; args.append(platform)
    if status:
        sql += " AND status = ?"; args.append(status)
    sql += " ORDER BY id DESC"
    with connect() as con:
        rows = con.execute(sql, args).fetchall()
    return [dict(r) for r in rows]


def get_social_account(account_id: int) -> dict | None:
    with connect() as con:
        r = con.execute("SELECT * FROM social_accounts WHERE id = ?", (account_id,)).fetchone()
    return dict(r) if r else None


def update_social_account(account_id: int, **fields) -> None:
    if not fields: return
    sets = [f"{k} = ?" for k in fields]
    sql = f"UPDATE social_accounts SET {', '.join(sets)}, updated_at = ? WHERE id = ?"
    with connect() as con:
        con.execute(sql, (*fields.values(), now_iso(), account_id))


def delete_social_account(account_id: int) -> None:
    with connect() as con:
        con.execute("DELETE FROM social_accounts WHERE id = ?", (account_id,))


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
             engine, api_config_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
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
        return int(cur.lastrowid)


def list_social_dm_log(account_id: int | None = None, limit: int = 100) -> list[dict]:
    sql = "SELECT * FROM social_dm_log"
    args: list = []
    if account_id is not None:
        sql += " WHERE account_id = ?"; args.append(account_id)
    sql += " ORDER BY id DESC LIMIT ?"; args.append(limit)
    with connect() as con:
        rows = con.execute(sql, args).fetchall()
    return [dict(r) for r in rows]


def count_social_dms_today(account_id: int) -> int:
    """Numero di DM inviati con ok=1 nelle ultime 24h da questo account."""
    from datetime import datetime, timedelta, timezone
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    with connect() as con:
        r = con.execute(
            "SELECT COUNT(*) FROM social_dm_log WHERE account_id = ? AND ok = 1 AND sent_at >= ?",
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
             status, daily_msg_cap, notes, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
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
        return int(cur.lastrowid)


def list_whatsapp_api_config(status: str | None = None) -> list[dict]:
    sql = "SELECT * FROM whatsapp_api_config"
    args: list = []
    if status:
        sql += " WHERE status = ?"; args.append(status)
    sql += " ORDER BY id DESC"
    with connect() as con:
        rows = con.execute(sql, args).fetchall()
    return [dict(r) for r in rows]


def get_whatsapp_api_config(config_id: int) -> dict | None:
    with connect() as con:
        r = con.execute(
            "SELECT * FROM whatsapp_api_config WHERE id = ?", (config_id,)
        ).fetchone()
    return dict(r) if r else None


def update_whatsapp_api_config(config_id: int, **fields) -> None:
    if not fields:
        return
    sets = [f"{k} = ?" for k in fields]
    sql = f"UPDATE whatsapp_api_config SET {', '.join(sets)}, updated_at = ? WHERE id = ?"
    with connect() as con:
        con.execute(sql, (*fields.values(), now_iso(), config_id))


def delete_whatsapp_api_config(config_id: int) -> None:
    with connect() as con:
        con.execute("DELETE FROM whatsapp_api_config WHERE id = ?", (config_id,))


def count_whatsapp_api_msgs_today(api_config_id: int) -> int:
    """Numero di messaggi Motore B (engine='B_api') inviati con ok=1 oggi (UTC) da
    questa config. Usato per il daily_msg_cap.
    """
    from datetime import datetime, timedelta, timezone
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    with connect() as con:
        r = con.execute(
            "SELECT COUNT(*) FROM social_dm_log "
            "WHERE api_config_id = ? AND engine = 'B_api' AND ok = 1 AND sent_at >= ?",
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
            "UPDATE contacts SET whatsapp_consent = ?, updated_at = ? WHERE id = ?",
            (consent, now_iso(), contact_id),
        )


def touch_contact_whatsapp_inbound(contact_id: int) -> None:
    """Segna che il contatto ha appena scritto al business number — abilita la
    24h-window per messaggi free-form via Motore B.
    """
    ts = now_iso()
    with connect() as con:
        con.execute(
            "UPDATE contacts SET whatsapp_last_inbound_at = ?, updated_at = ? WHERE id = ?",
            (ts, ts, contact_id),
        )


def list_contacts_for_whatsapp_outreach(
    only_qualified: bool = True,
    exclude_optedout: bool = True,
    exclude_contacted: bool = True,
    limit: int = 1000,
) -> list[dict]:
    """Carica i contatti idonei a outreach_whatsapp.

    Filtri di default: status='qualified', whatsapp_consent != 'optedout',
    status != 'contacted', whatsapp IS NOT NULL.
    """
    sql = "SELECT * FROM contacts WHERE whatsapp IS NOT NULL AND whatsapp != ''"
    if only_qualified:
        sql += " AND status = 'qualified'"
    if exclude_optedout:
        sql += " AND (whatsapp_consent IS NULL OR whatsapp_consent != 'optedout')"
    if exclude_contacted:
        sql += " AND status != 'contacted'"
    sql += " ORDER BY id DESC LIMIT ?"
    with connect() as con:
        rows = con.execute(sql, (limit,)).fetchall()
    return [dict(r) for r in rows]
