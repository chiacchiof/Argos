"""add project_chat_messages for Argos Fascicoli RAG

Tabella per cronologia chat sui fascicoli (Q&A retrieval-augmented).

Schema:
- project_chat_messages: id, project_id (CASCADE), user_id (SET NULL),
  role ('user'|'assistant'|'system'), content TEXT, citations TEXT (JSON con
  lista {file, score}), created_at TIMESTAMPTZ.

In v1 una chat per progetto, senza conversation_id. Se in futuro vorremo
piu' conversazioni parallele per progetto, aggiungeremo `conversation_id`
seguendo il pattern di `chat_conversations` esistente.

Privacy: in DB c'e' solo testo dell'utente e dell'assistente (con riferimento
al nome file). I CHUNK del documento citato vivono solo localmente in
`.argos/embeddings.json`. La citazione DB e' una pura ancora "file X chunk Y".

Revision ID: fascicoli_v2
Revises: fascicoli_v1
Create Date: 2026-05-28
"""
from typing import Sequence, Union

from alembic import op


revision: str = 'fascicoli_v2'
down_revision: Union[str, Sequence[str], None] = 'fascicoli_v1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS project_chat_messages (
          id          BIGSERIAL PRIMARY KEY,
          project_id  BIGINT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
          user_id     BIGINT REFERENCES users(id) ON DELETE SET NULL,
          role        TEXT NOT NULL CHECK (role IN ('user', 'assistant', 'system')),
          content     TEXT NOT NULL,
          citations   TEXT,
          created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_project_chat_messages_project "
        "ON project_chat_messages(project_id, id)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_project_chat_messages_project")
    op.execute("DROP TABLE IF EXISTS project_chat_messages")
