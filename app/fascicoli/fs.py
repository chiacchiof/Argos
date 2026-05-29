"""Helpers filesystem per Argos Fascicoli.

Funzioni pure su path: lettura/scrittura manifest, hash file, scansione cartella
progetto (escludendo `.argos/`), sanitize nomi cartella cross-platform.

Nessuna dipendenza da DB / FastAPI: facilmente testabili.
"""
from __future__ import annotations

import hashlib
import json
import mimetypes
import re
import shutil
import uuid as uuid_lib
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Optional


# Nome cartella nascosta dentro ogni progetto con materiale derivato (manifest,
# indici, embeddings, riassunti, log conversazione). Tutto qui dentro e'
# RIGENERABILE dai file sorgente: cancellarla non perde nulla di originale.
ARGOS_FOLDER = ".argos"
MANIFEST_FILE = "manifest.json"
SCHEMA_VERSION = 1


# ---------------------------------------------------------------------------
# Manifest (.argos/manifest.json) -> bind cartella <-> record DB via UUID
# ---------------------------------------------------------------------------

def manifest_path(project_folder: Path) -> Path:
    return project_folder / ARGOS_FOLDER / MANIFEST_FILE


def read_manifest(project_folder: Path) -> Optional[dict]:
    """Legge il manifest. None se manca o non parseabile."""
    p = manifest_path(project_folder)
    if not p.is_file():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def write_manifest(
    project_folder: Path,
    *,
    project_uuid: str,
    tenant_slug: str | None = None,
) -> None:
    """Crea/sovrascrive `.argos/manifest.json` con UUID + metadati base."""
    argos_dir = project_folder / ARGOS_FOLDER
    argos_dir.mkdir(parents=True, exist_ok=True)
    manifest = {
        "argos_project_uuid": project_uuid,
        "tenant_slug": tenant_slug,
        "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "schema_version": SCHEMA_VERSION,
    }
    manifest_path(project_folder).write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def new_project_uuid() -> str:
    return str(uuid_lib.uuid4())


# ---------------------------------------------------------------------------
# Hashing
# ---------------------------------------------------------------------------

def compute_file_hash(file_path: Path, chunk_size: int = 65536) -> str:
    """SHA-256 streaming. Su file molto grandi (>500MB) consider downstream
    soft-cap nel sync (vedi sync.py)."""
    h = hashlib.sha256()
    with file_path.open("rb") as f:
        while True:
            chunk = f.read(chunk_size)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------------------
# Folder name sanitize (Windows + macOS + Linux safe)
# ---------------------------------------------------------------------------

# Caratteri NON ammessi su Windows. Includiamo control chars (\x00-\x1f).
_INVALID_FOLDER_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

# Nomi riservati su Windows (case-insensitive).
_WINDOWS_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


def sanitize_folder_name(name: str) -> str:
    """Pulisce un nome utente per usarlo come nome cartella su qualsiasi OS.

    - Rimpiazza caratteri vietati con spazio.
    - Collassa whitespace ridondante.
    - Trim trailing `. ` (Windows non li ammette).
    - Riserva Windows: aggiunge `_` come suffisso.
    - Tronca a 120 caratteri.
    Ritorna stringa vuota se l'input sanitizzato e' vuoto.
    """
    s = (name or "").strip()
    s = _INVALID_FOLDER_CHARS.sub(" ", s)
    s = re.sub(r"\s+", " ", s).strip()
    s = s.rstrip(". ")
    if s.upper() in _WINDOWS_RESERVED:
        s = s + "_"
    return s[:120]


# ---------------------------------------------------------------------------
# Scansione progetto
# ---------------------------------------------------------------------------

def iter_project_files(project_folder: Path) -> Iterator[dict]:
    """Yield dict per ogni file dentro `project_folder`, ricorsivamente,
    SALTANDO la cartella `.argos/`.

    Dict prodotto:
      relative_path : str (posix style, es. "contratti/2026/Acme.pdf")
      name          : str (basename)
      size_bytes    : int
      mtime         : datetime UTC
      mime_type     : str | None (guess da nome)
      _abs_path     : Path assoluto (per usi interni come hashing)
    """
    if not project_folder.is_dir():
        return
    for path in project_folder.rglob("*"):
        if not path.is_file():
            continue
        try:
            rel = path.relative_to(project_folder)
        except ValueError:
            continue
        parts = rel.parts
        if not parts or parts[0] == ARGOS_FOLDER:
            continue
        try:
            stat = path.stat()
        except OSError:
            continue
        mime, _ = mimetypes.guess_type(str(path))
        mtime = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)
        yield {
            "relative_path": rel.as_posix(),
            "name": path.name,
            "size_bytes": stat.st_size,
            "mtime": mtime,
            "mime_type": mime,
            "_abs_path": path,
        }


def find_project_folder_by_uuid(root: Path, project_uuid) -> Optional[Path]:
    """Cerca tra le sottocartelle dirette di `root` quella con manifest UUID == `project_uuid`.
    Ritorna None se non trovata o root inesistente.

    Accetta `project_uuid` sia come `str` sia come `uuid.UUID` (i record DB postgres
    arrivano come UUID e `UUID == str` darebbe False — bug subdolo).
    """
    if not root or not root.is_dir():
        return None
    target = str(project_uuid)
    for child in root.iterdir():
        if not child.is_dir():
            continue
        m = read_manifest(child)
        if m and str(m.get("argos_project_uuid") or "") == target:
            return child
    return None


def discover_argos_folders(root: Path) -> Iterator[tuple[str, Path]]:
    """Itera (project_uuid, folder_path) sulle sottocartelle dirette di `root`
    che hanno un manifest valido.

    Usato dal rilevamento di "orfani": cartelle con UUID che pero' non hanno
    record DB visibili all'utente corrente.
    """
    if not root or not root.is_dir():
        return
    for child in root.iterdir():
        if not child.is_dir():
            continue
        m = read_manifest(child)
        if not m:
            continue
        uuid_val = m.get("argos_project_uuid")
        if uuid_val:
            yield (str(uuid_val), child)


# ---------------------------------------------------------------------------
# Creazione cartella nuovo progetto
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Eliminazione
# ---------------------------------------------------------------------------

def delete_project_folder(folder: Path) -> bool:
    """Cancella ricorsivamente la cartella del progetto (file utente + `.argos/`).
    Ritorna True se ha cancellato qualcosa, False se la cartella non esisteva.
    """
    if not folder or not folder.exists():
        return False
    if not folder.is_dir():
        return False
    shutil.rmtree(folder)
    return True


def resolve_file_in_project(folder: Path, relative_path: str) -> Path | None:
    """Path assoluto sicuro di un file dentro `folder` (no path-traversal).
    None se il path esce dalla cartella o il file non esiste."""
    if not folder or not folder.is_dir() or not relative_path:
        return None
    folder_resolved = folder.resolve()
    target = (folder / relative_path).resolve()
    try:
        target.relative_to(folder_resolved)
    except ValueError:
        return None
    return target if target.is_file() else None


def open_file_in_os(path: Path) -> bool:
    """Apre un file con l'app predefinita dell'OS. Funziona perche' Argos gira
    sul PC locale dell'utente (i file restano locali). True se avviato."""
    import subprocess
    import sys
    try:
        if sys.platform.startswith("win"):
            import os as _os
            _os.startfile(str(path))  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(path)])
        else:
            subprocess.Popen(["xdg-open", str(path)])
        return True
    except Exception:
        return False


def delete_file_in_project(folder: Path, relative_path: str) -> bool:
    """Cancella un singolo file dentro `folder` (path-traversal safe).
    Ritorna True se cancellato, False se file non trovato o path non valido.
    Rifiuta path che escono dalla cartella tramite `..` (resolve + relative_to).
    """
    if not folder or not folder.is_dir() or not relative_path:
        return False
    folder_resolved = folder.resolve()
    target = (folder / relative_path).resolve()
    try:
        target.relative_to(folder_resolved)
    except ValueError:
        # Tentativo di path traversal (es. "../foo" o path assoluto)
        return False
    if not target.is_file():
        return False
    try:
        target.unlink()
    except OSError:
        return False
    # Pulisci cartelle vuote risalendo verso la root, ma SENZA mai toccare
    # `folder` stessa o `.argos/`.
    parent = target.parent
    while parent != folder_resolved and parent.is_relative_to(folder_resolved):
        try:
            if any(parent.iterdir()):
                break
            if parent.name == ARGOS_FOLDER:
                break
            parent.rmdir()
        except OSError:
            break
        parent = parent.parent
    return True


def create_project_folder(
    root_project_path: Path,
    title: str,
    *,
    project_uuid: str,
    tenant_slug: str | None = None,
) -> Path:
    """Crea la cartella del nuovo progetto sotto la root.

    - Calcola un nome cartella sanitizzato dal titolo. Se collide con una cartella
      esistente, aggiunge `-2`, `-3`, ... fino a trovare un nome libero.
    - Crea la cartella + `.argos/manifest.json` con l'UUID dato.
    - Ritorna il path della cartella creata.

    Solleva `ValueError` se il titolo non e' sanitizzabile, `FileNotFoundError`
    se la root non esiste.
    """
    if not root_project_path.is_dir():
        raise FileNotFoundError(f"RootProject non esiste: {root_project_path}")
    base = sanitize_folder_name(title)
    if not base:
        raise ValueError("Titolo non valido (vuoto dopo sanitize)")
    candidate = root_project_path / base
    n = 2
    while candidate.exists():
        candidate = root_project_path / f"{base}-{n}"
        n += 1
    candidate.mkdir(parents=True)
    write_manifest(candidate, project_uuid=project_uuid, tenant_slug=tenant_slug)
    return candidate
