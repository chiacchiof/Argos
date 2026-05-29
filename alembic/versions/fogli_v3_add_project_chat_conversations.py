"""add project_chat_conversations (chat multiple per fascicolo)

Conversazioni salvate per fascicolo (max 20 enforced lato app) + conversation_id
su project_chat_messages (NULL = chat legacy pre-conversazioni).

DDL idempotente: coesiste con app/db.py init_db().

Revision ID: fogli_v3
Revises: fogli_v2
Create Date: 2026-05-29
"""
from typing import Sequence, Union

from alembic import op


revision: str = 'fogli_v3'
down_revision: Union[str, Sequence[str], None] = 'fogli_v2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS project_chat_conversations (
          id                 BIGSERIAL PRIMARY KEY,
          project_id         BIGINT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
          title              TEXT NOT NULL DEFAULT 'Nuova chat',
          created_by_user_id BIGINT REFERENCES users(id) ON DELETE SET NULL,
          created_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
          last_message_at    TIMESTAMPTZ
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS idx_project_chat_conv_project "
               "ON project_chat_conversations(project_id, last_message_at DESC)")
    op.execute("ALTER TABLE project_chat_messages ADD COLUMN IF NOT EXISTS conversation_id "
               "BIGINT REFERENCES project_chat_conversations(id) ON DELETE CASCADE")
    op.execute("CREATE INDEX IF NOT EXISTS idx_project_chat_messages_conv "
               "ON project_chat_messages(conversation_id, id)")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_project_chat_messages_conv")
    op.execute("ALTER TABLE project_chat_messages DROP COLUMN IF EXISTS conversation_id")
    op.execute("DROP INDEX IF EXISTS idx_project_chat_conv_project")
    op.execute("DROP TABLE IF EXISTS project_chat_conversations")
