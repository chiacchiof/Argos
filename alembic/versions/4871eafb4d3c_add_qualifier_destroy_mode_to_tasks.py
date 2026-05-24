"""add qualifier_destroy_mode to tasks

Aggiunge la colonna `qualifier_destroy_mode TEXT NOT NULL DEFAULT 'auto'` a
`tasks`. Controlla il comportamento del runner qualifier quando l'LLM emette
il 3o verdict `destroy` su un asset:
  - 'auto' (default): hard delete immediato (asset_tags cascade via FK).
    Forzato anche in workflow_run dove non c'e' UI nel mezzo.
  - 'confirm' (solo standalone): tag `qualifier_<slug>:pending_destroy` e
    l'utente conferma a fine job dal pannello
    `/tasks/<id>/qualifier-destroy-confirm`.

Idempotenza: `ADD COLUMN IF NOT EXISTS` per coesistere con installazioni
che hanno gia' applicato il cambio via embedded `init_db()` prima del
backporting in Alembic.

Revision ID: 4871eafb4d3c
Revises: 5bca203147e9
Create Date: 2026-05-23 16:03:27.535144

"""
from typing import Sequence, Union

from alembic import op


revision: str = '4871eafb4d3c'
down_revision: Union[str, Sequence[str], None] = '5bca203147e9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE tasks ADD COLUMN IF NOT EXISTS qualifier_destroy_mode TEXT "
        "NOT NULL DEFAULT 'auto'"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE tasks DROP COLUMN IF EXISTS qualifier_destroy_mode")
