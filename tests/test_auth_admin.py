"""Test Fase 1: autenticazione cookie session + admin section.

Strategia: monkeypatch del modulo `db_cloud` con un fake in-memory che simula
Postgres senza richiedere un'istanza vera. Verifica:
- hash/verify password
- middleware redirect anonimo
- login form + login con credenziali OK/KO
- admin gating per super-admin
- CRUD tenants e users via /admin
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient


# ---------------------------------------------------------------------------
# Fake in-memory backend per db_cloud
# ---------------------------------------------------------------------------

class FakeCloudDB:
    """In-memory stand-in di app.db_cloud. Replica le firme delle funzioni usate."""

    def __init__(self) -> None:
        self.tenants: dict[int, dict] = {}
        self.users: dict[int, dict] = {}
        self._tid = 0
        self._uid = 0
        self.configured = True

    def is_configured(self) -> bool:
        return self.configured

    def init_db(self) -> None:
        pass

    def close_pool(self) -> None:
        pass

    # --- tenants ---
    def list_tenants(self):
        return sorted(self.tenants.values(), key=lambda t: t["created_at"], reverse=True)

    def get_tenant(self, tid: int):
        return self.tenants.get(int(tid))

    def get_tenant_by_slug(self, slug: str):
        for t in self.tenants.values():
            if t["slug"] == slug:
                return t
        return None

    def create_tenant(self, name: str, slug: str) -> int:
        self._tid += 1
        self.tenants[self._tid] = {
            "id": self._tid,
            "name": name,
            "slug": slug,
            "is_active": True,
            "created_at": datetime.now(timezone.utc),
        }
        return self._tid

    def update_tenant(self, tid: int, *, name=None, is_active=None):
        t = self.tenants[tid]
        if name is not None:
            t["name"] = name
        if is_active is not None:
            t["is_active"] = is_active

    def delete_tenant(self, tid: int):
        self.tenants.pop(int(tid), None)
        # cascade su users
        for uid, u in list(self.users.items()):
            if u.get("tenant_id") == tid:
                del self.users[uid]

    # --- users ---
    def list_users(self, tenant_id=None):
        rows = list(self.users.values())
        if tenant_id is not None:
            rows = [u for u in rows if u.get("tenant_id") == tenant_id]
        out = []
        for u in rows:
            r = dict(u)
            tid = r.get("tenant_id")
            r["tenant_name"] = self.tenants[tid]["name"] if tid and tid in self.tenants else None
            out.append(r)
        return sorted(out, key=lambda u: u["created_at"], reverse=True)

    def get_user(self, uid: int):
        u = self.users.get(int(uid))
        return dict(u) if u else None

    def get_user_by_email(self, email: str):
        e = email.strip().lower()
        for u in self.users.values():
            if u["email"].lower() == e:
                return dict(u)
        return None

    def create_user(self, *, tenant_id, email, password_hash, role,
                    first_name=None, last_name=None):
        if role == "super_admin" and tenant_id is not None:
            raise ValueError("super_admin senza tenant")
        if role == "tenant_user" and tenant_id is None:
            raise ValueError("tenant_user richiede tenant")
        self._uid += 1
        self.users[self._uid] = {
            "id": self._uid,
            "tenant_id": tenant_id,
            "email": email.strip().lower(),
            "password_hash": password_hash,
            "role": role,
            "is_active": True,
            "first_name": (first_name or "").strip() or None,
            "last_name": (last_name or "").strip() or None,
            "created_at": datetime.now(timezone.utc),
        }
        return self._uid

    def update_user(self, uid: int, *, password_hash=None, is_active=None,
                    first_name=None, last_name=None):
        u = self.users[uid]
        if password_hash is not None:
            u["password_hash"] = password_hash
        if first_name is not None:
            u["first_name"] = first_name.strip() or None
        if last_name is not None:
            u["last_name"] = last_name.strip() or None
        if is_active is not None:
            u["is_active"] = is_active

    def delete_user(self, uid: int):
        self.users.pop(int(uid), None)


@pytest.fixture
def fake_cloud(monkeypatch):
    """Sostituisce db_cloud con FakeCloudDB. Disponibile per i test."""
    from app import db_cloud

    fake = FakeCloudDB()
    # Sostituisce ogni attributo del modulo
    for name in [
        "is_configured", "init_db", "close_pool",
        "list_tenants", "get_tenant", "get_tenant_by_slug", "create_tenant",
        "update_tenant", "delete_tenant",
        "list_users", "get_user", "get_user_by_email", "create_user",
        "update_user", "delete_user",
    ]:
        monkeypatch.setattr(db_cloud, name, getattr(fake, name))
    return fake


@pytest.fixture
def client(fake_cloud, monkeypatch, tmp_path):
    """TestClient con app reale + db_cloud mockato + SQLite isolato."""
    from app import config, db, storage
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "RESULTS_DIR", tmp_path / "results")
    monkeypatch.setattr(storage, "RESULTS_DIR", tmp_path / "results")

    from app.main import app
    with TestClient(app) as c:
        yield c


@pytest.fixture
def super_admin(fake_cloud):
    """Crea un super-admin nel fake DB. Restituisce dict."""
    from app.auth import hash_password
    uid = fake_cloud.create_user(
        tenant_id=None,
        email="edgadmin",
        password_hash=hash_password("Entra123!"),
        role="super_admin",
    )
    return fake_cloud.get_user(uid)


@pytest.fixture
def tenant(fake_cloud):
    tid = fake_cloud.create_tenant("Edg Marketing", "edg-marketing")
    return fake_cloud.get_tenant(tid)


@pytest.fixture
def tenant_user(fake_cloud, tenant):
    from app.auth import hash_password
    uid = fake_cloud.create_user(
        tenant_id=tenant["id"],
        email="mario@edg.it",
        password_hash=hash_password("mario-pwd"),
        role="tenant_user",
    )
    return fake_cloud.get_user(uid)


def _login(client: TestClient, email: str, password: str) -> "TestClient":
    """Effettua login e ritorna il client con cookie settati."""
    r = client.post(
        "/login",
        data={"email": email, "password": password, "next": "/"},
        follow_redirects=False,
    )
    assert r.status_code == 303, f"login failed: {r.status_code} {r.text[:200]}"
    return client


# ---------------------------------------------------------------------------
# Unit: hash/verify password
# ---------------------------------------------------------------------------

def test_hash_verify_password():
    from app.auth import hash_password, verify_password

    h = hash_password("Entra123!")
    assert h.startswith("$2"), "bcrypt hash format"
    assert verify_password("Entra123!", h)
    assert not verify_password("wrong", h)
    assert not verify_password("Entra123", h)  # senza !


# ---------------------------------------------------------------------------
# Middleware: redirect anonimo
# ---------------------------------------------------------------------------

def test_anonymous_redirected_to_login(client):
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"].startswith("/login")


def test_login_form_served(client):
    r = client.get("/login")
    assert r.status_code == 200
    assert "Email o username" in r.text
    assert 'name="password"' in r.text


def test_static_path_passthrough(client):
    r = client.get("/static/style.css")
    assert r.status_code == 200


# ---------------------------------------------------------------------------
# Login flow
# ---------------------------------------------------------------------------

def test_login_wrong_credentials(client, super_admin):
    r = client.post(
        "/login",
        data={"email": "edgadmin", "password": "WRONG", "next": "/"},
        follow_redirects=False,
    )
    assert r.status_code == 401
    assert "Credenziali non valide" in r.text


def test_login_success_sets_session(client, super_admin):
    r = client.post(
        "/login",
        data={"email": "edgAdmin", "password": "Entra123!", "next": "/"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/"
    # Cookie session presente
    assert "argos_session" in r.headers.get("set-cookie", "").lower() or \
           any("argos_session" in str(v).lower() for v in r.cookies)


def test_login_case_insensitive_email(client, super_admin):
    # Login con case diverso: edgAdmin (registrato come edgadmin) deve funzionare
    r = client.post(
        "/login",
        data={"email": "EDGADMIN", "password": "Entra123!", "next": "/"},
        follow_redirects=False,
    )
    assert r.status_code == 303


def test_login_inactive_user_rejected(client, fake_cloud, super_admin):
    fake_cloud.update_user(super_admin["id"], is_active=False)
    r = client.post(
        "/login",
        data={"email": "edgadmin", "password": "Entra123!", "next": "/"},
        follow_redirects=False,
    )
    assert r.status_code == 401


def test_logout_clears_session(client, super_admin):
    _login(client, "edgadmin", "Entra123!")
    r = client.get("/logout", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/login"
    # Dopo logout, GET / deve redirigere a login
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 302


def test_open_redirect_protection(client, super_admin):
    """`next` non deve permettere redirect a domini esterni."""
    r = client.post(
        "/login",
        data={
            "email": "edgadmin",
            "password": "Entra123!",
            "next": "https://evil.com/phish",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/"  # fallback safe

    r2 = client.post(
        "/login",
        data={"email": "edgadmin", "password": "Entra123!", "next": "//evil.com"},
        follow_redirects=False,
    )
    assert r2.status_code == 303
    assert r2.headers["location"] == "/"


# ---------------------------------------------------------------------------
# Admin gating
# ---------------------------------------------------------------------------

def test_admin_requires_login(client):
    r = client.get("/admin", follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"].startswith("/login")


def test_admin_forbidden_for_tenant_user(client, tenant_user):
    _login(client, "mario@edg.it", "mario-pwd")
    r = client.get("/admin", follow_redirects=False)
    assert r.status_code == 403


def test_admin_dashboard_for_super_admin(client, super_admin):
    _login(client, "edgadmin", "Entra123!")
    r = client.get("/admin")
    assert r.status_code == 200
    assert "Amministrazione" in r.text
    assert "Aziende" in r.text or "tenant" in r.text.lower()


# ---------------------------------------------------------------------------
# Admin: tenants CRUD
# ---------------------------------------------------------------------------

def test_create_tenant_via_admin(client, fake_cloud, super_admin):
    _login(client, "edgadmin", "Entra123!")
    r = client.post(
        "/admin/tenants",
        data={"name": "Acme", "slug": ""},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert any(t["name"] == "Acme" for t in fake_cloud.list_tenants())
    # Lo slug è stato auto-derivato
    t = fake_cloud.get_tenant_by_slug("acme")
    assert t and t["name"] == "Acme"


def test_create_tenant_explicit_slug(client, fake_cloud, super_admin):
    _login(client, "edgadmin", "Entra123!")
    r = client.post(
        "/admin/tenants",
        data={"name": "Big Co", "slug": "BIG CO Inc"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    t = fake_cloud.get_tenant_by_slug("big-co-inc")
    assert t and t["name"] == "Big Co"


def test_duplicate_slug_rejected(client, fake_cloud, super_admin):
    _login(client, "edgadmin", "Entra123!")
    fake_cloud.create_tenant("Acme", "acme")
    r = client.post(
        "/admin/tenants",
        data={"name": "Acme 2", "slug": "acme"},
        follow_redirects=False,
    )
    # redirect ma con flash error
    assert r.status_code == 303
    # Conferma che NON è stato creato un duplicato
    acmes = [t for t in fake_cloud.list_tenants() if t["slug"] == "acme"]
    assert len(acmes) == 1


def test_toggle_tenant(client, fake_cloud, super_admin, tenant):
    _login(client, "edgadmin", "Entra123!")
    r = client.post(f"/admin/tenants/{tenant['id']}/toggle", follow_redirects=False)
    assert r.status_code == 303
    assert fake_cloud.get_tenant(tenant["id"])["is_active"] is False
    # Riattivo
    client.post(f"/admin/tenants/{tenant['id']}/toggle", follow_redirects=False)
    assert fake_cloud.get_tenant(tenant["id"])["is_active"] is True


def test_delete_tenant_cascades_users(client, fake_cloud, super_admin, tenant, tenant_user):
    _login(client, "edgadmin", "Entra123!")
    assert fake_cloud.get_user(tenant_user["id"]) is not None
    r = client.post(f"/admin/tenants/{tenant['id']}/delete", follow_redirects=False)
    assert r.status_code == 303
    assert fake_cloud.get_tenant(tenant["id"]) is None
    assert fake_cloud.get_user(tenant_user["id"]) is None  # cascade


# ---------------------------------------------------------------------------
# Admin: users CRUD
# ---------------------------------------------------------------------------

def test_create_tenant_user_via_admin(client, fake_cloud, super_admin, tenant):
    _login(client, "edgadmin", "Entra123!")
    r = client.post(
        "/admin/users",
        data={
            "email": "alice@edg.it",
            "password": "alice-pwd",
            "role": "tenant_user",
            "tenant_id": str(tenant["id"]),
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    u = fake_cloud.get_user_by_email("alice@edg.it")
    assert u and u["role"] == "tenant_user"
    assert u["tenant_id"] == tenant["id"]


def test_create_tenant_user_without_tenant_rejected(client, fake_cloud, super_admin):
    _login(client, "edgadmin", "Entra123!")
    r = client.post(
        "/admin/users",
        data={
            "email": "ghost@x.it",
            "password": "ghost-pwd",
            "role": "tenant_user",
            "tenant_id": "",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert fake_cloud.get_user_by_email("ghost@x.it") is None


def test_create_super_admin_via_admin(client, fake_cloud, super_admin):
    _login(client, "edgadmin", "Entra123!")
    r = client.post(
        "/admin/users",
        data={
            "email": "second-admin",
            "password": "another-pwd",
            "role": "super_admin",
            "tenant_id": "",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    u = fake_cloud.get_user_by_email("second-admin")
    assert u and u["role"] == "super_admin"
    assert u["tenant_id"] is None


def test_create_user_password_too_short(client, fake_cloud, super_admin, tenant):
    _login(client, "edgadmin", "Entra123!")
    r = client.post(
        "/admin/users",
        data={
            "email": "short@x.it",
            "password": "123",
            "role": "tenant_user",
            "tenant_id": str(tenant["id"]),
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert fake_cloud.get_user_by_email("short@x.it") is None


def test_duplicate_user_email_rejected(client, fake_cloud, super_admin, tenant_user):
    _login(client, "edgadmin", "Entra123!")
    r = client.post(
        "/admin/users",
        data={
            "email": tenant_user["email"],  # già esistente
            "password": "another-pwd",
            "role": "tenant_user",
            "tenant_id": str(tenant_user["tenant_id"]),
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    # Conta utenti con quella email: deve restare 1
    rows = [u for u in fake_cloud.list_users() if u["email"] == tenant_user["email"]]
    assert len(rows) == 1


def test_toggle_user(client, fake_cloud, super_admin, tenant_user):
    _login(client, "edgadmin", "Entra123!")
    r = client.post(f"/admin/users/{tenant_user['id']}/toggle", follow_redirects=False)
    assert r.status_code == 303
    assert fake_cloud.get_user(tenant_user["id"])["is_active"] is False


def test_cannot_toggle_self(client, fake_cloud, super_admin):
    _login(client, "edgadmin", "Entra123!")
    r = client.post(f"/admin/users/{super_admin['id']}/toggle", follow_redirects=False)
    assert r.status_code == 303
    # Non disattivato
    assert fake_cloud.get_user(super_admin["id"])["is_active"] is True


def test_reset_password(client, fake_cloud, super_admin, tenant_user):
    from app.auth import verify_password

    _login(client, "edgadmin", "Entra123!")
    r = client.post(
        f"/admin/users/{tenant_user['id']}/reset-password",
        data={"new_password": "nuova-password-123"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    u = fake_cloud.get_user(tenant_user["id"])
    assert verify_password("nuova-password-123", u["password_hash"])
    assert not verify_password("mario-pwd", u["password_hash"])


def test_delete_user(client, fake_cloud, super_admin, tenant_user):
    _login(client, "edgadmin", "Entra123!")
    r = client.post(f"/admin/users/{tenant_user['id']}/delete", follow_redirects=False)
    assert r.status_code == 303
    assert fake_cloud.get_user(tenant_user["id"]) is None


def test_cannot_delete_self(client, fake_cloud, super_admin):
    _login(client, "edgadmin", "Entra123!")
    r = client.post(f"/admin/users/{super_admin['id']}/delete", follow_redirects=False)
    assert r.status_code == 303
    # Self-delete rejected
    assert fake_cloud.get_user(super_admin["id"]) is not None
