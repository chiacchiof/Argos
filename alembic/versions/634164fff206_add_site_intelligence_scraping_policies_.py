"""add site_intelligence + scraping_policies tables

Due nuove tabelle per il "cervello scraping" condiviso di Argos:

1. `site_intelligence`: memoria storica per dominio. Una riga per (domain,
   tenant_id). Aggiornata automaticamente post-job dai runner. Contiene
   contatori success/fail, ultima strategia che ha funzionato, note testuali.
   L'orchestrator legge questa tabella via `get_site_intel(domain)` per
   suggerire all'utente la strategia migliore prima di creare un nuovo task.

   Visibility: `visibility = 'private'` (solo tenant owner) o `'shared'`
   (cross-tenant pool, opt-in). Default private.

2. `scraping_policies`: regole policy modificabili dall'utente o dal sistema.
   Ogni riga e' una regola del tipo "match_pattern X (regex su dominio o URL)
   → action Y (skip|browser_use|bulk_extract|warn) con reason Z". Le regole
   sono valutate da match function in ordine di priorita'.

   Source: 'manual' (creata dall'utente), 'auto' (auto-creata da pattern
   ricorrenti nei job falliti), 'community' (promossa da pool cross-tenant).

Idempotenza: tutte le CREATE / ADD COLUMN usano `IF NOT EXISTS` in raw SQL
per coesistere con `init_db()` che applica `SCHEMA_SQL` al boot dell'app.

Revision ID: 634164fff206
Revises: 4871eafb4d3c
Create Date: 2026-05-24

"""
from typing import Sequence, Union

from alembic import op


revision: str = '634164fff206'
down_revision: Union[str, Sequence[str], None] = '4871eafb4d3c'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # --- site_intelligence ---------------------------------------------------
    op.execute("""
        CREATE TABLE IF NOT EXISTS site_intelligence (
          id BIGSERIAL PRIMARY KEY,
          registrable_domain TEXT NOT NULL,
          tenant_id BIGINT REFERENCES tenants(id) ON DELETE CASCADE,
          visibility TEXT NOT NULL DEFAULT 'private',
          last_status TEXT NOT NULL DEFAULT 'unknown',
          last_protection TEXT,
          success_count INTEGER NOT NULL DEFAULT 0,
          fail_count INTEGER NOT NULL DEFAULT 0,
          last_strategy_worked TEXT,
          last_job_id BIGINT,
          last_seen_at TEXT NOT NULL,
          notes TEXT,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          UNIQUE (registrable_domain, tenant_id)
        )
    """)
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_site_intelligence_lookup "
        "ON site_intelligence(registrable_domain, tenant_id)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_site_intelligence_visibility "
        "ON site_intelligence(visibility, registrable_domain) "
        "WHERE visibility = 'shared'"
    )

    # --- scraping_policies ---------------------------------------------------
    op.execute("""
        CREATE TABLE IF NOT EXISTS scraping_policies (
          id BIGSERIAL PRIMARY KEY,
          match_kind TEXT NOT NULL DEFAULT 'domain_regex',
          match_pattern TEXT NOT NULL,
          action TEXT NOT NULL,
          reason TEXT,
          source TEXT NOT NULL DEFAULT 'manual',
          priority INTEGER NOT NULL DEFAULT 100,
          active INTEGER NOT NULL DEFAULT 1,
          tenant_id BIGINT REFERENCES tenants(id) ON DELETE CASCADE,
          visibility TEXT NOT NULL DEFAULT 'private',
          hits INTEGER NOT NULL DEFAULT 0,
          last_hit_at TEXT,
          created_by_user_id BIGINT REFERENCES users(id) ON DELETE SET NULL,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        )
    """)
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_scraping_policies_active "
        "ON scraping_policies(active, priority) WHERE active = 1"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_scraping_policies_tenant "
        "ON scraping_policies(tenant_id, active)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_scraping_policies_tenant")
    op.execute("DROP INDEX IF EXISTS idx_scraping_policies_active")
    op.execute("DROP TABLE IF EXISTS scraping_policies")
    op.execute("DROP INDEX IF EXISTS idx_site_intelligence_visibility")
    op.execute("DROP INDEX IF EXISTS idx_site_intelligence_lookup")
    op.execute("DROP TABLE IF EXISTS site_intelligence")
