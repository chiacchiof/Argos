"""baseline (post-Fase 2: schema gestito da init_db)

Revision ID: 0001
Revises: 
Create Date: 2026-05-16 14:52:25.453534

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0001'
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Baseline: SCHEMA esistente (post-Fase 2 multi-tenant Postgres) è applicato
    dall'app via `app.db.init_db()` + `app.db_cloud.init_db()` al boot. Questa
    revision NON crea tabelle; serve solo come marker `alembic_version=0001`.

    Per nuove modifiche di schema (Fase 3+):
      python -m alembic revision -m "descrizione"
      # edita upgrade()/downgrade() con op.add_column / op.create_table / ...
      python -m alembic upgrade head    # apply su DB corrente (.env o /dbconfig)
    """
    pass


def downgrade() -> None:
    """Downgrade dalla baseline NON è supportato — ridurrebbe a DB vuoto."""
    pass
