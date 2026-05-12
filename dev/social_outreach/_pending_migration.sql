-- Migration: tabelle per outreach social
-- Da applicare in app/db.py:init_db() quando il framework e' libero da job in corso.
-- Idempotente: usa IF NOT EXISTS.

CREATE TABLE IF NOT EXISTS social_accounts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  uuid TEXT UNIQUE NOT NULL,
  platform TEXT NOT NULL,                  -- 'instagram' | 'tiktok'
  username TEXT NOT NULL,
  encrypted_password BLOB NOT NULL,        -- cifrato con Fernet, chiave da AGENTSCRAPER_SECRET
  proxy_label TEXT,                        -- label del proxy assegnato (da AGENTSCRAPER_PROXIES)
  daily_dm_cap INTEGER NOT NULL DEFAULT 10,
  status TEXT NOT NULL DEFAULT 'active',   -- 'active' | 'quarantine' | 'banned' | 'warming_up'
  warmup_started_at TEXT,
  warmup_days_target INTEGER DEFAULT 30,
  notes TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(platform, username)
);

CREATE INDEX IF NOT EXISTS idx_social_accounts_platform_status
  ON social_accounts(platform, status);

CREATE TABLE IF NOT EXISTS social_dm_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  account_id INTEGER NOT NULL REFERENCES social_accounts(id) ON DELETE CASCADE,
  job_id INTEGER REFERENCES jobs(id) ON DELETE SET NULL,
  target_contact_id INTEGER REFERENCES contacts(id) ON DELETE SET NULL,
  target_platform TEXT NOT NULL,
  target_username TEXT NOT NULL,
  message TEXT NOT NULL,
  sent_at TEXT NOT NULL,
  ok INTEGER NOT NULL,                     -- 1=delivered, 0=failed
  reason TEXT,                             -- es. 'message_button_not_found', 'rate_limited'
  health_post TEXT                         -- HealthStatus.value post-send
);

CREATE INDEX IF NOT EXISTS idx_social_dm_log_account
  ON social_dm_log(account_id, sent_at);
CREATE INDEX IF NOT EXISTS idx_social_dm_log_target
  ON social_dm_log(target_contact_id);
