"""add project_sheet_users (ACL per-foglio per condivisione)

Tabella di condivisione esplicita di un foglio verso utenti del tenant
(viewer/editor). Utile soprattutto per i fogli 'user' (privati): concede
accesso/modifica a utenti specifici. Per i fogli 'tenant' resta additiva.

DDL idempotente (CREATE TABLE IF NOT EXISTS): coesiste con app/db.py init_db().

Revision ID: fogli_v2
Revises: fogli_v1
Create Date: 2026-05-29
"""
from typing import Sequence, Union

from alembic import op


revision: str = 'fogli_v2'
down_revision: Union[str, Sequence[str], None] = 'fogli_v1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS project_sheet_users (
          sheet_id   BIGINT NOT NULL REFERENCES project_sheets(id) ON DELETE CASCADE,
          user_id    BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
          role       TEXT NOT NULL CHECK (role IN ('viewer', 'editor')),
          added_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
          PRIMARY KEY (sheet_id, user_id)
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS idx_project_sheet_users_user ON project_sheet_users(user_id)")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_project_sheet_users_user")
    op.execute("DROP TABLE IF EXISTS project_sheet_users")
