"""extract contact fields into assets

Aggiunge ad `assets` le colonne dei canali di contatto + outreach status,
che oggi vivono in `contacts`. Step 2A del piano di deprecazione di
`contacts` (vedi piano in C:\\Users\\Ferdinando\\.claude\\plans\\studiati-la-codebase-anche-zazzy-elephant.md
sezione "Fase 2A — Schema").

Le colonne sono nullable + indicizzate dove utili (email per dedup, telegram_chat_id
per lookup webhook, outreach_status per le list query). Backfill dei dati
contacts → assets sarà fatto da `scripts/backfill_contacts_to_assets.py` (Fase 2B).

Revision ID: 1e9265f53339
Revises: 0001
Create Date: 2026-05-16 17:00:53.416519
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '1e9265f53339'
down_revision: Union[str, Sequence[str], None] = '0001'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Colonne aggiunte ad `assets`. Tutte nullable per backfill graduale.
# `outreach_status` ha default 'pending' (semantica equivalente a contacts.status
# 'new' per asset non ancora processati da outreach).
_NEW_COLUMNS = [
    ("display_name", sa.Text(), None),
    ("email", sa.Text(), None),
    ("telegram_username", sa.Text(), None),
    ("telegram_chat_id", sa.Text(), None),
    ("whatsapp", sa.Text(), None),
    ("whatsapp_consent", sa.Text(), "cold"),
    ("whatsapp_last_inbound_at", sa.Text(), None),
    ("social_json", sa.Text(), None),
    ("sitoweb", sa.Text(), None),
    ("outreach_status", sa.Text(), "pending"),
]

_NEW_INDICES = [
    ("idx_assets_email", "email"),
    ("idx_assets_telegram_chat", "telegram_chat_id"),
    ("idx_assets_outreach_status", "outreach_status"),
]


def upgrade() -> None:
    for col_name, col_type, default in _NEW_COLUMNS:
        if default is not None:
            op.add_column(
                "assets",
                sa.Column(col_name, col_type, nullable=True, server_default=default),
            )
        else:
            op.add_column("assets", sa.Column(col_name, col_type, nullable=True))

    for idx_name, col_name in _NEW_INDICES:
        op.create_index(idx_name, "assets", [col_name])


def downgrade() -> None:
    for idx_name, _ in reversed(_NEW_INDICES):
        op.drop_index(idx_name, table_name="assets")
    for col_name, _, _ in reversed(_NEW_COLUMNS):
        op.drop_column("assets", col_name)
