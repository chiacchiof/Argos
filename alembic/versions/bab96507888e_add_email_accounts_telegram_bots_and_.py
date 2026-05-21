"""add email_accounts telegram_bots and task fks

Crea due nuove tabelle per multi-account messaging:
  - email_accounts: SMTP/IMAP per account (Fernet-encrypted password)
  - telegram_bots: Bot API per bot (Fernet-encrypted token)

Entrambe seguono il pattern multi-tenant di social_accounts /
whatsapp_api_config: tenant_id (CASCADE) + created_by_user_id (SET NULL) +
indice composito (tenant_id, status).

Aggiunge inoltre due colonne FK su `tasks`:
  - email_account_id: sender single-select per outreach email
  - telegram_bot_id: sender single-select per outreach telegram
Entrambe nullable: NULL = pool default (primo account active del tenant).
Pattern fail-fast (no FK CASCADE: il delete dell'account fa UPDATE tasks SET
... = NULL prima della DELETE, vedi db.delete_email_account/_telegram_bot).

Idempotenza: tutte le CREATE / ADD COLUMN usano `IF NOT EXISTS` in raw SQL
per coesistere con `init_db()` che applica `SCHEMA_SQL` al boot dell'app.

Revision ID: bab96507888e
Revises: 9fd74ad2acc6
Create Date: 2026-05-21 10:21:29.816578
"""
from typing import Sequence, Union

from alembic import op


revision: str = 'bab96507888e'
down_revision: Union[str, Sequence[str], None] = '9fd74ad2acc6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # --- email_accounts ----------------------------------------------------
    op.execute("""
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
          tenant_id BIGINT REFERENCES tenants(id) ON DELETE CASCADE,
          created_by_user_id BIGINT REFERENCES users(id) ON DELETE SET NULL,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          UNIQUE (from_address)
        )
    """)
    # Aggiungi tenant_id / created_by_user_id se la tabella esisteva senza
    # (es. creata da una rev precedente di SCHEMA_SQL che non li includeva).
    op.execute(
        "ALTER TABLE email_accounts ADD COLUMN IF NOT EXISTS tenant_id BIGINT "
        "REFERENCES tenants(id) ON DELETE CASCADE"
    )
    op.execute(
        "ALTER TABLE email_accounts ADD COLUMN IF NOT EXISTS created_by_user_id BIGINT "
        "REFERENCES users(id) ON DELETE SET NULL"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_email_accounts_tenant "
        "ON email_accounts(tenant_id, status)"
    )

    # --- telegram_bots -----------------------------------------------------
    op.execute("""
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
          tenant_id BIGINT REFERENCES tenants(id) ON DELETE CASCADE,
          created_by_user_id BIGINT REFERENCES users(id) ON DELETE SET NULL,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          UNIQUE (bot_username)
        )
    """)
    op.execute(
        "ALTER TABLE telegram_bots ADD COLUMN IF NOT EXISTS tenant_id BIGINT "
        "REFERENCES tenants(id) ON DELETE CASCADE"
    )
    op.execute(
        "ALTER TABLE telegram_bots ADD COLUMN IF NOT EXISTS created_by_user_id BIGINT "
        "REFERENCES users(id) ON DELETE SET NULL"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_telegram_bots_tenant "
        "ON telegram_bots(tenant_id, status)"
    )

    # --- tasks: nuove FK ---------------------------------------------------
    op.execute(
        "ALTER TABLE tasks ADD COLUMN IF NOT EXISTS email_account_id BIGINT"
    )
    op.execute(
        "ALTER TABLE tasks ADD COLUMN IF NOT EXISTS telegram_bot_id BIGINT"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE tasks DROP COLUMN IF EXISTS telegram_bot_id")
    op.execute("ALTER TABLE tasks DROP COLUMN IF EXISTS email_account_id")

    op.execute("DROP INDEX IF EXISTS idx_telegram_bots_tenant")
    op.execute("DROP TABLE IF EXISTS telegram_bots")

    op.execute("DROP INDEX IF EXISTS idx_email_accounts_tenant")
    op.execute("DROP TABLE IF EXISTS email_accounts")
