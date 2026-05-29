"""Apertura file del fascicolo sul PC locale + sicurezza path-traversal."""
from __future__ import annotations

import uuid as _uuid

import pytest
from fastapi.testclient import TestClient

from app import db_cloud
from app.auth import hash_password
from app.fascicoli import db as fdb, fs as ffs


def test_resolve_file_in_project_security(tmp_path):
    folder = tmp_path / "proj"
    folder.mkdir()
    (folder / "doc.txt").write_text("ciao", encoding="utf-8")
    assert ffs.resolve_file_in_project(folder, "doc.txt") == (folder / "doc.txt").resolve()
    assert ffs.resolve_file_in_project(folder, "manca.txt") is None
    assert ffs.resolve_file_in_project(folder, "../fuori.txt") is None
    assert ffs.resolve_file_in_project(folder, "/etc/passwd") is None


@pytest.fixture
def env(tmp_path, monkeypatch):
    from app import config, storage
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "RESULTS_DIR", tmp_path / "results")
    monkeypatch.setattr(storage, "RESULTS_DIR", tmp_path / "results")
    ta = db_cloud.create_tenant("TA", "ta")
    uid = db_cloud.create_user(tenant_id=ta, email="own@a.it", password_hash=hash_password("pw"), role="tenant_user")
    root = tmp_path / "root"; root.mkdir()
    fuuid = str(_uuid.uuid4())
    folder = ffs.create_project_folder(root, "MyProj", project_uuid=fuuid, tenant_slug="ta")
    (folder / "doc.txt").write_text("ciao", encoding="utf-8")
    db_cloud.update_user(uid, root_project_path=str(root))
    pid = fdb.create_project(title="MyProj", folder_uuid=fuuid, tenant_id=ta, owner_user_id=uid)
    return {"ta": ta, "uid": uid, "pid": pid, "root": root}


def test_open_route(env, monkeypatch):
    opened = {}
    monkeypatch.setattr(ffs, "open_file_in_os", lambda p: opened.update(path=str(p)) or True)
    from app.main import app
    with TestClient(app) as client:
        client.post("/login", data={"email": "own@a.it", "password": "pw", "next": "/"}, follow_redirects=False)
        # apertura ok -> 204 e l'opener riceve il path del file reale
        r = client.post(f"/fascicoli/{env['pid']}/files/open", data={"relative_path": "doc.txt"})
        assert r.status_code == 204
        assert opened.get("path", "").endswith("doc.txt")
        # file inesistente -> 404
        assert client.post(f"/fascicoli/{env['pid']}/files/open", data={"relative_path": "no.txt"}).status_code == 404
        # path traversal -> 404 (resolve_file_in_project lo blocca)
        assert client.post(f"/fascicoli/{env['pid']}/files/open", data={"relative_path": "../../x"}).status_code == 404
