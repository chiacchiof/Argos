"""chat conversations for operator with user scoping

Introduce una nuova tabella `chat_conversations` per gestire piu' conversazioni
parallele nella chat dell'orchestrator (UI operator drawer). Ogni utente puo'
avere fino a 5 conversazioni attive (limite enforced lato applicazione).

Schema:
  - chat_conversations: id, tenant_id (CASCADE), user_id (CASCADE), title,
    is_active (bool, una sola attiva per utente), created_at, last_message_at

E aggiunge a `orchestrator_messages`:
  - conversation_id (FK nullable): NULL = chat legacy/architect (singolo thread
    per tenant come oggi); NOT NULL = appartiene a una conversation operator
  - user_id (FK nullable): chi ha scritto il messaggio (per messaggi role=user)

Compatibilita': i messaggi gia' presenti restano con conversation_id NULL e
user_id NULL — la chat architect funziona esattamente come prima. La UI
operator filtra solo i messaggi con conversation_id != NULL.

Idempotenza: tutte le CREATE / ADD COLUMN usano `IF NOT EXISTS` in raw SQL
per coesistere con `init_db()` che applica `SCHEMA_SQL` al boot dell'app.

Revision ID: dd9f4fc12f91
Revises: 634164fff206
Create Date: 2026-05-25 07:23:35.963502
"""
from typing import Sequence, Union

from alembic import op


revision: str = 'dd9f4fc12f91'
down_revision: Union[str, Sequence[str], None] = '634164fff206'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS chat_conversations (
          id BIGSERIAL PRIMARY KEY,
          tenant_id BIGINT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
          user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
          title TEXT NOT NULL DEFAULT 'Nuova conversazione',
          is_active INTEGER NOT NULL DEFAULT 0,
          created_at TEXT NOT NULL DEFAULT to_char(now() AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS"Z"'),
          last_message_at TEXT
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS idx_chat_conv_user ON chat_conversations(user_id, last_message_at DESC)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_chat_conv_tenant ON chat_conversations(tenant_id)")

    # Aggiungi conversation_id + user_id su orchestrator_messages.
    op.execute("""
        ALTER TABLE orchestrator_messages
          ADD COLUMN IF NOT EXISTS conversation_id BIGINT
            REFERENCES chat_conversations(id) ON DELETE CASCADE
    """)
    op.execute("""
        ALTER TABLE orchestrator_messages
          ADD COLUMN IF NOT EXISTS user_id BIGINT
            REFERENCES users(id) ON DELETE SET NULL
    """)
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_orchestrator_msg_conv "
        "ON orchestrator_messages(conversation_id, id)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_orchestrator_msg_conv")
    op.execute("ALTER TABLE orchestrator_messages DROP COLUMN IF EXISTS conversation_id")
    op.execute("ALTER TABLE orchestrator_messages DROP COLUMN IF EXISTS user_id")
    op.execute("DROP INDEX IF EXISTS idx_chat_conv_user")
    op.execute("DROP INDEX IF EXISTS idx_chat_conv_tenant")
    op.execute("DROP TABLE IF EXISTS chat_conversations")
