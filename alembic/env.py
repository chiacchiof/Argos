"""Alembic env: usa la stessa DATABASE_URL dell'app (.env + override /dbconfig).

Workflow tipico:
    # 1. modifica schema desiderata + nuova revision:
    python -m alembic revision -m "rename notes column"
    # 2. edita versions/XXXX_rename_notes_column.py (upgrade/downgrade)
    # 3. test in locale:
    python -m alembic upgrade head
    pytest
    # 4. apply su Neon (NON modifica .env, override esplicito):
    $env:DATABASE_URL="<NEON_URL>"; python -m alembic upgrade head

Note:
- target_metadata=None: usiamo migrations MANUALI (op.add_column, op.create_table…)
  perché il nostro app.db.py non usa SQLAlchemy ORM. Autogenerate non funzionerebbe.
- pool NullPool: ogni operazione apre una nuova connessione (Alembic non beneficia
  del pool, e su Neon evita conflitti con pgbouncer).
"""
from __future__ import annotations

import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

# Resolution della DATABASE_URL:
# 1) Se l'utente ha già settato `DATABASE_URL` esplicitamente nell'env shell
#    (es. `$env:DATABASE_URL=... ; python -m alembic upgrade head`), usa quella
#    e basta. Permette di applicare alembic su un DB diverso dal "default"
#    dell'app senza toccare .env o /dbconfig.
# 2) Altrimenti, importa app.config per attivare load_dotenv(.env) +
#    apply_override(/dbconfig) come fa l'app stessa. Cosi' `alembic upgrade head`
#    senza override applica sul DB attivo per l'app.
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

if not os.environ.get("DATABASE_URL", "").strip():
    from app import config as _app_config  # noqa: E402, F401  (side-effect: load_dotenv + apply_override)

config = context.config

# Interpret the config file for Python logging.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Override sqlalchemy.url di alembic.ini con la DATABASE_URL effettiva dell'app.
# alembic.ini ha un placeholder vuoto; non vogliamo committare URL con password.
_dsn = os.environ.get("DATABASE_URL", "").strip()
if _dsn:
    # SQLAlchemy preferisce `postgresql+psycopg://` per psycopg3, ma `postgresql://`
    # funziona usando il driver di default (psycopg2 se installato, altrimenti psycopg3
    # con prefisso esplicito). Forziamo psycopg3 perché è quello già installato.
    if _dsn.startswith("postgresql://"):
        _dsn = "postgresql+psycopg://" + _dsn[len("postgresql://"):]
    elif _dsn.startswith("postgres://"):
        _dsn = "postgresql+psycopg://" + _dsn[len("postgres://"):]
    config.set_main_option("sqlalchemy.url", _dsn)

# target_metadata=None: migrations manuali, no autogenerate dalle ORM models.
target_metadata = None


def run_migrations_offline() -> None:
    """Esegue le migrations in modalità 'offline' (genera SQL senza eseguire).
    Utile per `alembic upgrade head --sql` quando devi rivedere lo SQL prima di
    applicare in produzione.
    """
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Esegue le migrations in modalità 'online' contro il DB reale."""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
