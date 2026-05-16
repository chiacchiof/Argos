"""Cloud Postgres backend per tabelle multi-tenant (Fase 1: solo `tenants` e `users`).

Le tabelle business (tasks, jobs, assets, workflows, ecc.) restano su SQLite locale
fino alle Fasi 2-3 del piano in SETUP_CLOUD_DB_TENANT.md. Questo modulo è abilitato
solo se la variabile d'ambiente `DATABASE_URL` è settata; altrimenti l'app gira
in modalità legacy single-user come prima.
"""
from __future__ import annotations

import logging
import os
from contextlib import contextmanager
from typing import Any, Iterator

log = logging.getLogger(__name__)


_pool = None  # type: ignore[var-annotated]


def is_configured() -> bool:
    """True se DATABASE_URL è settato e quindi il backend cloud è attivo."""
    return bool(os.environ.get("DATABASE_URL"))


def _get_pool():
    """Singleton lazy del connection pool psycopg3.

    Import psycopg ritardato: se l'utente non usa il cloud DB non vogliamo
    forzarlo a installare psycopg.
    """
    global _pool
    if _pool is not None:
        return _pool
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        raise RuntimeError(
            "DATABASE_URL non impostata. Setta la connection string Neon/Postgres "
            "in .env per abilitare il backend cloud."
        )
    try:
        from psycopg.rows import dict_row
        from psycopg_pool import ConnectionPool
    except ImportError as exc:
        raise RuntimeError(
            "psycopg[binary,pool] non installato. Esegui: pip install -e ."
        ) from exc

    _pool = ConnectionPool(
        conninfo=dsn,
        min_size=1,
        max_size=10,
        timeout=10,
        kwargs={"row_factory": dict_row},
        open=True,
    )
    return _pool


@contextmanager
def connect() -> Iterator[Any]:
    """Context manager per ottenere una connessione dal pool.

    Uso:
        with connect() as conn:
            row = conn.execute("SELECT 1").fetchone()
    """
    pool = _get_pool()
    with pool.connection() as conn:
        yield conn


SCHEMA_SQL = """
CREATE EXTENSION IF NOT EXISTS citext;

CREATE TABLE IF NOT EXISTS tenants (
  id           BIGSERIAL PRIMARY KEY,
  name         TEXT UNIQUE NOT NULL,
  slug         TEXT UNIQUE NOT NULL,
  is_active    BOOLEAN NOT NULL DEFAULT TRUE,
  created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS users (
  id             BIGSERIAL PRIMARY KEY,
  tenant_id      BIGINT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  email          CITEXT UNIQUE NOT NULL,
  password_hash  TEXT NOT NULL,
  role           TEXT NOT NULL CHECK (role IN ('super_admin', 'tenant_user')),
  is_active      BOOLEAN NOT NULL DEFAULT TRUE,
  created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  CHECK (
    (role = 'super_admin' AND tenant_id IS NULL)
    OR (role = 'tenant_user' AND tenant_id IS NOT NULL)
  )
);

CREATE INDEX IF NOT EXISTS idx_users_tenant ON users (tenant_id) WHERE tenant_id IS NOT NULL;
"""


def init_db() -> None:
    """Idempotente. Crea schema multi-tenant su Postgres + bootstrap super-admin se in env."""
    if not is_configured():
        log.info("DATABASE_URL non configurato — cloud DB disabilitato (modalità legacy).")
        return
    try:
        with connect() as conn:
            conn.execute(SCHEMA_SQL)
            conn.commit()
        _bootstrap_super_admin()
        log.info("Cloud DB inizializzato (tenants/users pronti).")
    except Exception as exc:
        log.error("Errore init cloud DB: %s", exc)
        raise


def _bootstrap_super_admin() -> None:
    """Crea il super_admin al primo boot se BOOTSTRAP_SUPER_ADMIN_EMAIL e _PASSWORD
    sono in env e non esiste già un utente con quella email. No-op altrimenti."""
    email = (os.environ.get("BOOTSTRAP_SUPER_ADMIN_EMAIL") or "").strip().lower()
    password = os.environ.get("BOOTSTRAP_SUPER_ADMIN_PASSWORD") or ""
    if not email or not password:
        log.info("BOOTSTRAP_SUPER_ADMIN_EMAIL/PASSWORD non impostati — skip bootstrap super-admin.")
        return
    from .auth import hash_password

    with connect() as conn:
        row = conn.execute("SELECT id FROM users WHERE email = %s", (email,)).fetchone()
        if row:
            log.info("Super-admin %s già presente — skip bootstrap.", email)
            return
        conn.execute(
            "INSERT INTO users (tenant_id, email, password_hash, role) "
            "VALUES (NULL, %s, %s, 'super_admin')",
            (email, hash_password(password)),
        )
        conn.commit()
        log.warning("Super-admin %s creato. Cambia la password dopo il primo login.", email)


# --- TENANTS ---

def list_tenants() -> list[dict[str, Any]]:
    with connect() as conn:
        return list(
            conn.execute(
                "SELECT id, name, slug, is_active, created_at FROM tenants ORDER BY created_at DESC"
            ).fetchall()
        )


def get_tenant(tenant_id: int) -> dict[str, Any] | None:
    with connect() as conn:
        return conn.execute(
            "SELECT id, name, slug, is_active, created_at FROM tenants WHERE id = %s",
            (tenant_id,),
        ).fetchone()


def get_tenant_by_slug(slug: str) -> dict[str, Any] | None:
    with connect() as conn:
        return conn.execute(
            "SELECT id, name, slug, is_active, created_at FROM tenants WHERE slug = %s",
            (slug,),
        ).fetchone()


def create_tenant(name: str, slug: str) -> int:
    with connect() as conn:
        row = conn.execute(
            "INSERT INTO tenants (name, slug) VALUES (%s, %s) RETURNING id",
            (name, slug),
        ).fetchone()
        conn.commit()
        return int(row["id"])


def update_tenant(tenant_id: int, *, name: str | None = None, is_active: bool | None = None) -> None:
    fields: list[str] = []
    params: list[Any] = []
    if name is not None:
        fields.append("name = %s")
        params.append(name)
    if is_active is not None:
        fields.append("is_active = %s")
        params.append(is_active)
    if not fields:
        return
    params.append(tenant_id)
    with connect() as conn:
        conn.execute(f"UPDATE tenants SET {', '.join(fields)} WHERE id = %s", params)
        conn.commit()


def delete_tenant(tenant_id: int) -> None:
    with connect() as conn:
        conn.execute("DELETE FROM tenants WHERE id = %s", (tenant_id,))
        conn.commit()


# --- USERS ---

def list_users(tenant_id: int | None = None) -> list[dict[str, Any]]:
    """Lista utenti. Se tenant_id è None ritorna tutti (per super-admin); altrimenti filtra."""
    base_sql = (
        "SELECT u.id, u.tenant_id, u.email, u.role, u.is_active, u.created_at, "
        "       t.name AS tenant_name "
        "FROM users u LEFT JOIN tenants t ON u.tenant_id = t.id "
    )
    with connect() as conn:
        if tenant_id is None:
            return list(conn.execute(base_sql + "ORDER BY u.created_at DESC").fetchall())
        return list(
            conn.execute(
                base_sql + "WHERE u.tenant_id = %s ORDER BY u.created_at DESC",
                (tenant_id,),
            ).fetchall()
        )


def get_user(user_id: int) -> dict[str, Any] | None:
    with connect() as conn:
        return conn.execute(
            "SELECT id, tenant_id, email, password_hash, role, is_active, created_at "
            "FROM users WHERE id = %s",
            (user_id,),
        ).fetchone()


def get_user_by_email(email: str) -> dict[str, Any] | None:
    with connect() as conn:
        return conn.execute(
            "SELECT id, tenant_id, email, password_hash, role, is_active, created_at "
            "FROM users WHERE email = %s",
            (email.strip().lower(),),
        ).fetchone()


def create_user(*, tenant_id: int | None, email: str, password_hash: str, role: str) -> int:
    if role not in ("super_admin", "tenant_user"):
        raise ValueError(f"role non valido: {role}")
    if role == "super_admin" and tenant_id is not None:
        raise ValueError("super_admin non deve avere tenant_id")
    if role == "tenant_user" and tenant_id is None:
        raise ValueError("tenant_user richiede tenant_id")
    with connect() as conn:
        row = conn.execute(
            "INSERT INTO users (tenant_id, email, password_hash, role) "
            "VALUES (%s, %s, %s, %s) RETURNING id",
            (tenant_id, email.strip().lower(), password_hash, role),
        ).fetchone()
        conn.commit()
        return int(row["id"])


def update_user(
    user_id: int,
    *,
    password_hash: str | None = None,
    is_active: bool | None = None,
) -> None:
    fields: list[str] = []
    params: list[Any] = []
    if password_hash is not None:
        fields.append("password_hash = %s")
        params.append(password_hash)
    if is_active is not None:
        fields.append("is_active = %s")
        params.append(is_active)
    if not fields:
        return
    params.append(user_id)
    with connect() as conn:
        conn.execute(f"UPDATE users SET {', '.join(fields)} WHERE id = %s", params)
        conn.commit()


def delete_user(user_id: int) -> None:
    with connect() as conn:
        conn.execute("DELETE FROM users WHERE id = %s", (user_id,))
        conn.commit()


def close_pool() -> None:
    global _pool
    if _pool is not None:
        try:
            _pool.close()
        except Exception:
            pass
        _pool = None
