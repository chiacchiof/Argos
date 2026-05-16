"""threads and social_dm_log point to assets

Fase 2C del piano deprecazione contacts. Aggiunge:
  - threads.asset_id (FK assets.id) con backfill da contacts.asset_id via
    threads.contact_id JOIN. Diventa NOT NULL dopo verifica.
  - social_dm_log.target_asset_id (FK assets.id) con backfill da
    contacts.asset_id via social_dm_log.target_contact_id JOIN.
    Rimane NULLABLE (alcuni log potrebbero non avere target_contact_id).

Le colonne legacy `threads.contact_id` e `social_dm_log.target_contact_id`
NON vengono droppate qui: restano per audit, drop finale in Fase 3
(rename + drop contacts_legacy).

Prerequisito: Fase 2B (backfill contacts->assets) ha portato il count di
contacts orfani a 0. Senza quello, il backfill di threads.asset_id può
lasciare righe NULL e la NOT NULL constraint fallirebbe.

Revision ID: 9fd74ad2acc6
Revises: 1e9265f53339
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '9fd74ad2acc6'
down_revision: Union[str, Sequence[str], None] = '1e9265f53339'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # --- threads.asset_id ---
    op.add_column(
        "threads",
        sa.Column("asset_id", sa.BigInteger(), nullable=True),
    )
    op.create_foreign_key(
        "threads_asset_id_fkey", "threads", "assets",
        ["asset_id"], ["id"], ondelete="CASCADE",
    )
    # Backfill via JOIN su contacts (contacts.asset_id ora popolato per tutti
    # dopo Fase 2B).
    op.execute("""
        UPDATE threads t
        SET asset_id = c.asset_id
        FROM contacts c
        WHERE t.contact_id = c.id
          AND c.asset_id IS NOT NULL
    """)
    # Verifica: count threads con asset_id NULL deve essere 0 (assumendo
    # tutti i threads avevano contact_id valorizzato).
    conn = op.get_bind()
    nulls = conn.execute(
        sa.text(
            "SELECT COUNT(*) FROM threads "
            "WHERE contact_id IS NOT NULL AND asset_id IS NULL"
        )
    ).scalar()
    if nulls and nulls > 0:
        raise RuntimeError(
            f"FAIL: {nulls} threads hanno contact_id ma nessun asset_id post-backfill. "
            "Verifica che la Fase 2B sia stata eseguita (no contacts.asset_id IS NULL)."
        )
    op.create_index("idx_threads_asset", "threads", ["asset_id"])

    # --- social_dm_log.target_asset_id ---
    op.add_column(
        "social_dm_log",
        sa.Column("target_asset_id", sa.BigInteger(), nullable=True),
    )
    op.create_foreign_key(
        "social_dm_log_target_asset_id_fkey", "social_dm_log", "assets",
        ["target_asset_id"], ["id"], ondelete="SET NULL",
    )
    op.execute("""
        UPDATE social_dm_log s
        SET target_asset_id = c.asset_id
        FROM contacts c
        WHERE s.target_contact_id = c.id
          AND c.asset_id IS NOT NULL
    """)
    op.create_index("idx_social_dm_log_target_asset", "social_dm_log", ["target_asset_id"])


def downgrade() -> None:
    op.drop_index("idx_social_dm_log_target_asset", table_name="social_dm_log")
    op.drop_constraint("social_dm_log_target_asset_id_fkey", "social_dm_log", type_="foreignkey")
    op.drop_column("social_dm_log", "target_asset_id")

    op.drop_index("idx_threads_asset", table_name="threads")
    op.drop_constraint("threads_asset_id_fkey", "threads", type_="foreignkey")
    op.drop_column("threads", "asset_id")
