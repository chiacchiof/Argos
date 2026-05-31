"""add portal_fill (compilazione assistita portali)

Tabelle per la feature "compilazione assistita di portali web" (agent_mode='portal_fill'):

- portal_macros: una macro di compilazione per-portale, tenant-scoped. `fields_json`
  e' la lista ordinata dei campi del form con il loro binding alla colonna del foglio
  (o un valore costante) + il selettore registrato e la strategia di locate. La
  sessione di login al portale NON contiene password: `login_session_key` punta a uno
  storage_state Playwright salvato su disco (data/portal_sessions/<key>.json), come
  per le sessioni social. `auto_submit` e' opt-in PER-MACRO (default 0 = stop prima
  del submit, l'utente conferma a mano).
- portal_fill_log: esito per-riga di un run del runner portal_fill (audit + report).

Inoltre aggiunge alla tabella `tasks` i 3 campi che parametrizzano un task portal_fill:
portal_macro_id, portal_sheet_id, portal_auto_submit (gli altri parametri LLM
riusano i campi llm_provider/llm_base_url/llm_api_key/model gia' esistenti).

DDL idempotente (CREATE TABLE / ADD COLUMN IF NOT EXISTS): coesiste con
`app/db.py init_db()` che applica lo stesso SCHEMA_SQL ad ogni boot.

Revision ID: portal_v1
Revises: fogli_v4
Create Date: 2026-05-30
"""
from typing import Sequence, Union

from alembic import op


revision: str = 'portal_v1'
down_revision: Union[str, Sequence[str], None] = 'fogli_v4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS portal_macros (
          id                 BIGSERIAL PRIMARY KEY,
          tenant_id          BIGINT REFERENCES tenants(id) ON DELETE CASCADE,
          name               TEXT NOT NULL,
          portal_url         TEXT NOT NULL,
          fields_json        TEXT NOT NULL DEFAULT '[]',
          login_session_key  TEXT,
          auto_submit        INTEGER NOT NULL DEFAULT 0,
          submit_selector    TEXT,
          created_by_user_id BIGINT REFERENCES users(id) ON DELETE SET NULL,
          created_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
          updated_at         TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_portal_macros_tenant "
        "ON portal_macros(tenant_id)"
    )

    op.execute("""
        CREATE TABLE IF NOT EXISTS portal_fill_log (
          id          BIGSERIAL PRIMARY KEY,
          tenant_id   BIGINT REFERENCES tenants(id) ON DELETE CASCADE,
          job_id      BIGINT,
          macro_id    BIGINT REFERENCES portal_macros(id) ON DELETE SET NULL,
          sheet_id    BIGINT,
          row_idx     INTEGER,
          status      TEXT NOT NULL,
          detail      TEXT,
          created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_portal_fill_log_job "
        "ON portal_fill_log(job_id)"
    )

    op.execute("ALTER TABLE tasks ADD COLUMN IF NOT EXISTS portal_macro_id BIGINT")
    op.execute("ALTER TABLE tasks ADD COLUMN IF NOT EXISTS portal_sheet_id BIGINT")
    op.execute(
        "ALTER TABLE tasks ADD COLUMN IF NOT EXISTS portal_auto_submit "
        "INTEGER NOT NULL DEFAULT 0"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE tasks DROP COLUMN IF EXISTS portal_auto_submit")
    op.execute("ALTER TABLE tasks DROP COLUMN IF EXISTS portal_sheet_id")
    op.execute("ALTER TABLE tasks DROP COLUMN IF EXISTS portal_macro_id")
    op.execute("DROP INDEX IF EXISTS idx_portal_fill_log_job")
    op.execute("DROP TABLE IF EXISTS portal_fill_log")
    op.execute("DROP INDEX IF EXISTS idx_portal_macros_tenant")
    op.execute("DROP TABLE IF EXISTS portal_macros")
