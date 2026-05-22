"""site memory tenant scoping with share flag

Schema change: la memoria del sito (`site_patterns`, `site_playbooks`) diventa
tenant-aware con visibilita' opt-in tramite flag su `tenants`.

1. `tenants.site_memory_shared BOOLEAN NOT NULL DEFAULT FALSE`
   Flag premium: se TRUE il tenant accede al pool condiviso (vede memoria di
   TUTTI i tenant); se FALSE vede solo le proprie righe.

2. `site_patterns.tenant_id BIGINT REFERENCES tenants(id) ON DELETE CASCADE`
   Tag del "primo discoverer" — non cambia mai dopo l'INSERT iniziale.

3. `site_playbooks.tenant_id BIGINT REFERENCES tenants(id) ON DELETE CASCADE`
   Tag dell'"ultimo writer" — il playbook e' una versione e l'owner segue chi
   ha scritto la versione vigente.

4. Indici (tenant_id, registrable_domain) su entrambe le tabelle per accelerare
   le query filtrate per tenant.

5. Backfill: le righe legacy con `tenant_id IS NULL` (pre-multi-tenant) vengono
   riassegnate al "tenant principale" = primo tenant per id (MIN(id)).
   Convenzione del pilot: e' il tenant del super-admin (edgAdmin).

Idempotenza: tutte le ADD COLUMN / CREATE INDEX usano `IF NOT EXISTS` in raw
SQL per coesistere con `init_db()` che applica gli stessi cambi al boot.

Revision ID: 5bca203147e9
Revises: bab96507888e
Create Date: 2026-05-22 18:31:47.748589

"""
from typing import Sequence, Union

from alembic import op


revision: str = '5bca203147e9'
down_revision: Union[str, Sequence[str], None] = 'bab96507888e'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # --- 1) tenants.site_memory_shared -------------------------------------
    op.execute(
        "ALTER TABLE tenants ADD COLUMN IF NOT EXISTS "
        "site_memory_shared BOOLEAN NOT NULL DEFAULT FALSE"
    )

    # --- 2) site_patterns.tenant_id ----------------------------------------
    op.execute(
        "ALTER TABLE site_patterns ADD COLUMN IF NOT EXISTS "
        "tenant_id BIGINT REFERENCES tenants(id) ON DELETE CASCADE"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_site_patterns_tenant "
        "ON site_patterns(tenant_id, registrable_domain)"
    )

    # --- 3) site_playbooks.tenant_id ---------------------------------------
    op.execute(
        "ALTER TABLE site_playbooks ADD COLUMN IF NOT EXISTS "
        "tenant_id BIGINT REFERENCES tenants(id) ON DELETE CASCADE"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_site_playbooks_tenant "
        "ON site_playbooks(tenant_id, registrable_domain)"
    )

    # --- 4) Backfill: righe legacy NULL -> tenant principale ---------------
    # Strategia: il "tenant principale" e' il primo tenant per id (MIN(id)),
    # che nel pilot e' il tenant del super-admin. Se non ci sono tenant
    # ancora (caso degenerato: schema vuoto post-init_db ma pre-creazione
    # tenant), la subquery e' NULL e l'UPDATE non tocca nulla -- l'app a runtime
    # eseguira' `migrate_site_memory_to_super_admin()` quando un tenant esiste.
    op.execute(
        "UPDATE site_patterns SET tenant_id = (SELECT MIN(id) FROM tenants) "
        "WHERE tenant_id IS NULL AND EXISTS (SELECT 1 FROM tenants)"
    )
    op.execute(
        "UPDATE site_playbooks SET tenant_id = (SELECT MIN(id) FROM tenants) "
        "WHERE tenant_id IS NULL AND EXISTS (SELECT 1 FROM tenants)"
    )


def downgrade() -> None:
    # Inverso esatto. DROP COLUMN cancella anche le righe legacy del backfill,
    # ma la knowledge resta nel content (registrable_domain, pattern, playbook
    # text) — solo il tagging tenant viene rimosso.
    op.execute("DROP INDEX IF EXISTS idx_site_playbooks_tenant")
    op.execute("ALTER TABLE site_playbooks DROP COLUMN IF EXISTS tenant_id")
    op.execute("DROP INDEX IF EXISTS idx_site_patterns_tenant")
    op.execute("ALTER TABLE site_patterns DROP COLUMN IF EXISTS tenant_id")
    op.execute("ALTER TABLE tenants DROP COLUMN IF EXISTS site_memory_shared")
