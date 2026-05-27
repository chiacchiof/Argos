"""add job_chat_messages table for B-001 in-running chat

Introduce la tabella `job_chat_messages`: coda di messaggi human-in-the-loop per
job attivo (B-001). I messaggi `direction='user'` con `applied=0` sono consumati
dal runner al checkpoint (testo libero = suggerimento iniettato nel prompt LLM,
`/skip` = comando runner). I comandi deterministici (/stop, /pause, /resume,
/note, /set) e gli ack `direction='assistant'` sono salvati con `applied=1`.

La colonna `tenant_id` (nullable, CASCADE) è inclusa qui per i DB esistenti che
applicano solo le migration. Su DB fresh, `init_db()` crea la tabella via
SCHEMA_SQL (senza tenant_id) e `_apply_multitenant_columns()` aggiunge la colonna:
stato finale identico. Tutte le CREATE usano IF NOT EXISTS per coesistere con
`init_db()` al boot.

Revision ID: 0fb817660193
Revises: dd9f4fc12f91
Create Date: 2026-05-27 16:04:15.900277

"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = '0fb817660193'
down_revision: Union[str, Sequence[str], None] = 'dd9f4fc12f91'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS job_chat_messages (
          id BIGSERIAL PRIMARY KEY,
          job_id BIGINT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
          direction TEXT NOT NULL,
          kind TEXT NOT NULL DEFAULT 'suggestion',
          body TEXT NOT NULL,
          applied INTEGER NOT NULL DEFAULT 0,
          created_at TEXT NOT NULL,
          tenant_id BIGINT REFERENCES tenants(id) ON DELETE CASCADE
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS idx_job_chat_job ON job_chat_messages(job_id, id)")
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_job_chat_pending "
        "ON job_chat_messages(job_id) WHERE applied = 0 AND direction = 'user'"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_job_chat_pending")
    op.execute("DROP INDEX IF EXISTS idx_job_chat_job")
    op.execute("DROP TABLE IF EXISTS job_chat_messages")
