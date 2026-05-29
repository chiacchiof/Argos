"""add tenant_role to projects + project_sheets

Ruolo di default per i membri del tenant quando visibility='tenant':
'editor' (tutti modificano, comportamento storico) o 'viewer' (tutti in sola
lettura). Permette l'opzione «Tutti gli utenti del tenant (lettura)».

DDL idempotente: coesiste con app/db.py init_db().

Revision ID: fogli_v4
Revises: fogli_v3
Create Date: 2026-05-29
"""
from typing import Sequence, Union

from alembic import op


revision: str = 'fogli_v4'
down_revision: Union[str, Sequence[str], None] = 'fogli_v3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TABLE projects ADD COLUMN IF NOT EXISTS tenant_role TEXT NOT NULL DEFAULT 'editor'")
    op.execute("ALTER TABLE project_sheets ADD COLUMN IF NOT EXISTS tenant_role TEXT NOT NULL DEFAULT 'editor'")


def downgrade() -> None:
    op.execute("ALTER TABLE project_sheets DROP COLUMN IF EXISTS tenant_role")
    op.execute("ALTER TABLE projects DROP COLUMN IF EXISTS tenant_role")
