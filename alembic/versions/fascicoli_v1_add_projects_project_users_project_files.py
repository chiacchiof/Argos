"""add projects, project_users, project_files for Argos Fascicoli v1

Introduce le 3 tabelle del modulo Fascicoli + la colonna users.root_project_path.
Vedi `docs/argos_fascicoli_design.md` per il design completo.

Schema introdotto:
- projects: registro fascicoli per tenant. Visibility 'tenant' (tutto il tenant)
  o 'user' (solo owner + condivisi via project_users). Il `folder_uuid` e' il
  bind con la cartella fisica (.argos/manifest.json).
- project_users: ACL per progetti User-Use (viewer / editor). Owner implicito
  via projects.owner_user_id (non figura in questa tabella).
- project_files: registro metadati file (nome, dimensione, hash, mtime,
  last_indexed_at). Mai contenuto. Aggiornato dal watcher locale.
- users.root_project_path: cartella root dove l'utente tiene i fascicoli sul PC
  corrente. NULL fino a setup. Per-utente perche' utenti diversi su PC condivisi
  possono volere root diverse.

Privacy: in cloud (Neon) vivono solo i metadati. Contenuti dei file, embeddings,
chunk e log conversazione restano nel filesystem locale (`.argos/`). Vedi
"Manifesto Privacy" in docs/argos_fascicoli_design.md (Appendice B).

Idempotenza: tutte le CREATE / ADD COLUMN usano `IF NOT EXISTS` in raw SQL per
coesistere con `init_db()` che applica `SCHEMA_SQL` al boot dell'app.

Revision ID: fascicoli_v1
Revises: 0fb817660193
Create Date: 2026-05-28
"""
from typing import Sequence, Union

from alembic import op


revision: str = 'fascicoli_v1'
down_revision: Union[str, Sequence[str], None] = '0fb817660193'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # users.root_project_path — path della cartella root sul PC corrente.
    # NULL = utente non ha ancora completato il setup fascicoli.
    op.execute(
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS root_project_path TEXT"
    )

    # projects — registro fascicoli per tenant.
    op.execute("""
        CREATE TABLE IF NOT EXISTS projects (
          id              BIGSERIAL PRIMARY KEY,
          tenant_id       BIGINT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
          owner_user_id   BIGINT NOT NULL REFERENCES users(id)   ON DELETE RESTRICT,
          folder_uuid     UUID NOT NULL UNIQUE,
          title           TEXT NOT NULL,
          description     TEXT,
          visibility      TEXT NOT NULL CHECK (visibility IN ('tenant', 'user')),
          is_archived     BOOLEAN NOT NULL DEFAULT FALSE,
          created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
          updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_projects_tenant_visibility "
        "ON projects(tenant_id, visibility)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_projects_owner "
        "ON projects(owner_user_id)"
    )

    # project_users — ACL per User-Use (sharing esplicito).
    op.execute("""
        CREATE TABLE IF NOT EXISTS project_users (
          project_id    BIGINT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
          user_id       BIGINT NOT NULL REFERENCES users(id)   ON DELETE CASCADE,
          role          TEXT NOT NULL CHECK (role IN ('viewer', 'editor')),
          added_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
          PRIMARY KEY (project_id, user_id)
        )
    """)
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_project_users_user "
        "ON project_users(user_id)"
    )

    # project_files — registro metadati (mai contenuto).
    op.execute("""
        CREATE TABLE IF NOT EXISTS project_files (
          id                BIGSERIAL PRIMARY KEY,
          project_id        BIGINT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
          relative_path     TEXT NOT NULL,
          name              TEXT NOT NULL,
          size_bytes        BIGINT NOT NULL,
          content_hash      TEXT,
          mime_type         TEXT,
          added_by_user_id  BIGINT REFERENCES users(id) ON DELETE SET NULL,
          added_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
          mtime             TIMESTAMPTZ,
          last_indexed_at   TIMESTAMPTZ,
          UNIQUE (project_id, relative_path)
        )
    """)
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_project_files_project "
        "ON project_files(project_id)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_project_files_project")
    op.execute("DROP TABLE IF EXISTS project_files")
    op.execute("DROP INDEX IF EXISTS idx_project_users_user")
    op.execute("DROP TABLE IF EXISTS project_users")
    op.execute("DROP INDEX IF EXISTS idx_projects_owner")
    op.execute("DROP INDEX IF EXISTS idx_projects_tenant_visibility")
    op.execute("DROP TABLE IF EXISTS projects")
    op.execute("ALTER TABLE users DROP COLUMN IF EXISTS root_project_path")
