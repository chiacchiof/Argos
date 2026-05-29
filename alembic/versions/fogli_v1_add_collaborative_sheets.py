"""add collaborative sheets (Argos Fogli v1)

Tabelle per i Fogli collaborativi realtime (spreadsheet multiutente):

- project_sheets: metadati foglio tenant-scoped. `project_id` e' NULLABLE: un
  foglio puo' essere standalone (asset del tenant) oppure agganciato a un
  fascicolo. `revision` e' la revisione corrente (head) per serializzare le patch.
- project_sheet_cells: stato corrente delle celle (upsert per patch).
- project_sheet_revisions: log append-only delle patch (recupero al reconnect +
  audit). UNIQUE(sheet_id, revision) garantisce sequenza monotona per foglio.

ECCEZIONE consapevole al principio privacy-first dei Fascicoli: il contenuto del
foglio vive ONLINE (Postgres), perche' piu' utenti lo modificano insieme da
macchine diverse. Redis (se attivo) e' solo bus realtime, mai fonte di verita'.

DDL idempotente (CREATE TABLE IF NOT EXISTS): coesiste con `app/db.py init_db()`
che applica lo stesso SCHEMA_SQL ad ogni boot. Vedi
docs/argos_fogli_collaborativi_plan.md e docs/argos_fascicoli_design.md App. A.

Revision ID: fogli_v1
Revises: fascicoli_v2
Create Date: 2026-05-29
"""
from typing import Sequence, Union

from alembic import op


revision: str = 'fogli_v1'
down_revision: Union[str, Sequence[str], None] = 'fascicoli_v2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS project_sheets (
          id                 BIGSERIAL PRIMARY KEY,
          tenant_id          BIGINT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
          project_id         BIGINT REFERENCES projects(id) ON DELETE SET NULL,
          title              TEXT NOT NULL,
          visibility         TEXT NOT NULL DEFAULT 'tenant' CHECK (visibility IN ('tenant', 'user')),
          created_by_user_id BIGINT REFERENCES users(id) ON DELETE SET NULL,
          is_archived        BOOLEAN NOT NULL DEFAULT FALSE,
          revision           BIGINT NOT NULL DEFAULT 0,
          n_rows             INT NOT NULL DEFAULT 100,
          n_cols             INT NOT NULL DEFAULT 26,
          created_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
          updated_at         TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_project_sheets_tenant "
        "ON project_sheets(tenant_id, is_archived)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_project_sheets_project "
        "ON project_sheets(project_id, is_archived)"
    )

    op.execute("""
        CREATE TABLE IF NOT EXISTS project_sheet_cells (
          sheet_id           BIGINT NOT NULL REFERENCES project_sheets(id) ON DELETE CASCADE,
          row_idx            INT NOT NULL,
          col_idx            INT NOT NULL,
          value              TEXT,
          formula            TEXT,
          style_json         JSONB,
          updated_by_user_id BIGINT REFERENCES users(id) ON DELETE SET NULL,
          updated_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
          revision           BIGINT NOT NULL DEFAULT 0,
          PRIMARY KEY (sheet_id, row_idx, col_idx)
        )
    """)
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_project_sheet_cells_sheet_revision "
        "ON project_sheet_cells(sheet_id, revision)"
    )

    op.execute("""
        CREATE TABLE IF NOT EXISTS project_sheet_revisions (
          id            BIGSERIAL PRIMARY KEY,
          sheet_id      BIGINT NOT NULL REFERENCES project_sheets(id) ON DELETE CASCADE,
          actor_user_id BIGINT REFERENCES users(id) ON DELETE SET NULL,
          revision      BIGINT NOT NULL,
          patch_json    JSONB NOT NULL,
          created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
          UNIQUE (sheet_id, revision)
        )
    """)
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_project_sheet_revisions_sheet_revision "
        "ON project_sheet_revisions(sheet_id, revision)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_project_sheet_revisions_sheet_revision")
    op.execute("DROP TABLE IF EXISTS project_sheet_revisions")
    op.execute("DROP INDEX IF EXISTS idx_project_sheet_cells_sheet_revision")
    op.execute("DROP TABLE IF EXISTS project_sheet_cells")
    op.execute("DROP INDEX IF EXISTS idx_project_sheets_project")
    op.execute("DROP INDEX IF EXISTS idx_project_sheets_tenant")
    op.execute("DROP TABLE IF EXISTS project_sheets")
