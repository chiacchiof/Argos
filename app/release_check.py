"""Check non-bloccante della release più recente su GitHub.

Logica:
- Configurato via env `GITHUB_REPO=owner/repo` (es. `chiacchiof/Argos`).
  Se vuota → check disabilitato (nessuna chiamata HTTP, nessun banner).
- Opzionale `GITHUB_TOKEN` per repo privati (PAT classic con `repo` scope o
  fine-grained con `Contents: Read`).
- Risultato cached su disco in `data/version_check.json` per 6h (riduce
  chiamate API e attiva il banner anche se sei offline al boot successivo).
- L'app NON blocca mai il boot in attesa del check. La chiamata viene
  triggerata in background al primo accesso, oppure all'avvio del lifespan
  con un task asyncio.

Esposto da `latest_release()` (sync, ritorna dict cached o None).
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .config import DATA_DIR


log = logging.getLogger(__name__)


CACHE_FILE = DATA_DIR / "version_check.json"
CACHE_TTL = timedelta(hours=6)


def _get_repo() -> str | None:
    return (os.environ.get("GITHUB_REPO") or "").strip() or None


def is_enabled() -> bool:
    return _get_repo() is not None


def _read_cache() -> dict[str, Any] | None:
    if not CACHE_FILE.exists():
        return None
    try:
        data = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
        fetched_at = datetime.fromisoformat(data.get("fetched_at", ""))
        if datetime.now(timezone.utc) - fetched_at > CACHE_TTL:
            return None  # cache scaduta
        return data
    except Exception:
        return None


def _write_cache(payload: dict[str, Any]) -> None:
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        payload["fetched_at"] = datetime.now(timezone.utc).isoformat()
        CACHE_FILE.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    except Exception as exc:
        log.warning("release_check: cache write failed: %s", exc)


def _normalize_tag(tag: str) -> str:
    """Rimuove prefisso `v` da `v1.2.3` → `1.2.3` per confronto con __version__."""
    return tag.lstrip("v").strip()


def fetch_latest_remote() -> dict[str, Any] | None:
    """Chiama GitHub API `/repos/{owner}/{repo}/releases/latest`. NON usa cache.
    Ritorna dict con `tag_name`, `name`, `html_url`, `body` (release notes)
    oppure None se errore/disabilitato."""
    repo = _get_repo()
    if not repo:
        return None
    url = f"https://api.github.com/repos/{repo}/releases/latest"
    headers = {"Accept": "application/vnd.github+json"}
    token = (os.environ.get("GITHUB_TOKEN") or "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        import httpx
        r = httpx.get(url, headers=headers, timeout=5.0)
        if r.status_code == 404:
            log.info("release_check: nessuna release pubblicata per %s", repo)
            return None
        r.raise_for_status()
        data = r.json()
        return {
            "tag_name": data.get("tag_name", ""),
            "name": data.get("name") or data.get("tag_name") or "",
            "html_url": data.get("html_url", ""),
            "body": data.get("body") or "",
        }
    except Exception as exc:
        log.warning("release_check: fetch fallito (%s): %s", repo, exc)
        return None


def latest_release(force: bool = False) -> dict[str, Any] | None:
    """Ritorna la release latest: cached se disponibile e fresh, altrimenti fetch.
    `force=True` ignora la cache."""
    if not is_enabled():
        return None
    if not force:
        cached = _read_cache()
        if cached is not None:
            return cached
    fresh = fetch_latest_remote()
    if fresh:
        _write_cache(fresh)
        return fresh
    # fallback su cache anche se scaduta (meglio dato vecchio che niente)
    if CACHE_FILE.exists():
        try:
            return json.loads(CACHE_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return None


def update_available() -> dict[str, Any] | None:
    """Se è disponibile una release più recente, ritorna dict con `current`,
    `latest`, `release_url`. Altrimenti None."""
    from . import __version__

    rel = latest_release()
    if not rel:
        return None
    remote_tag = _normalize_tag(rel.get("tag_name", ""))
    if not remote_tag:
        return None
    if remote_tag == __version__:
        return None
    # Confronto naive: se tag remoto != version locale, c'è update potenziale.
    # Una compare semver proper potrebbe distinguere "remote < local" (downgrade,
    # tipicamente errore di tag) ma per il banner basta "diverso == aggiornabile".
    return {
        "current": __version__,
        "latest": remote_tag,
        "release_url": rel.get("html_url", ""),
        "release_name": rel.get("name", ""),
        "release_body": rel.get("body", ""),
    }
