from __future__ import annotations

import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from .config import RESULTS_DIR


def _is_inside(child: Path, parent: Path) -> bool:
    """Path containment check case-insensitive su Windows (NTFS) e sensible
    altrove. Usato per la safety net contro path traversal: previene che
    `resolve()` punti fuori da RESULTS_DIR/{project_id}.
    """
    child_n = os.path.normcase(str(child))
    parent_n = os.path.normcase(str(parent))
    if not parent_n.endswith(os.sep):
        parent_n += os.sep
    return child_n == os.path.normcase(str(parent)) or child_n.startswith(parent_n)


def _safe_timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def write_result(project_id: int, content: str, fmt: str = "txt") -> str:
    """Salva il contenuto come file/i flat (modalità ReAct) e ritorna il path del primo."""
    folder = RESULTS_DIR / str(project_id)
    folder.mkdir(parents=True, exist_ok=True)
    ts = _safe_timestamp()

    paths: list[Path] = []
    if fmt in ("txt", "both"):
        p = folder / f"{ts}.txt"
        p.write_text(content, encoding="utf-8")
        paths.append(p)
    if fmt in ("md", "both"):
        p = folder / f"{ts}.md"
        p.write_text(content, encoding="utf-8")
        paths.append(p)

    if not paths:
        p = folder / f"{ts}.txt"
        p.write_text(content, encoding="utf-8")
        paths.append(p)

    return str(paths[0])


def list_results(project_id: int) -> list[dict]:
    """Top-level: ritorna sia file flat sia cartelle di run (browser-use)."""
    folder = RESULTS_DIR / str(project_id)
    if not folder.exists():
        return []
    items: list[dict] = []
    for entry in sorted(folder.iterdir(), key=lambda p: p.name, reverse=True):
        if entry.is_file():
            items.append(
                {
                    "type": "file",
                    "name": entry.name,
                    "size": entry.stat().st_size,
                }
            )
        elif entry.is_dir():
            files = [f for f in entry.rglob("*") if f.is_file()]
            total = sum(f.stat().st_size for f in files)
            items.append(
                {
                    "type": "dir",
                    "name": entry.name,
                    "files_count": len(files),
                    "size": total,
                }
            )
    return items


def list_run_files(project_id: int, run_name: str) -> list[dict] | None:
    """Lista ricorsiva dei file dentro una cartella di run."""
    project_root = (RESULTS_DIR / str(project_id)).resolve()
    run_dir = (project_root / run_name).resolve()
    if not _is_inside(run_dir, project_root):
        return None
    if not run_dir.is_dir():
        return None
    out: list[dict] = []
    for f in sorted(run_dir.rglob("*"), key=lambda p: str(p)):
        if f.is_file():
            rel = f.relative_to(run_dir).as_posix()
            out.append({"path": rel, "name": f.name, "size": f.stat().st_size})
    return out


def read_result_file(project_id: int, name: str) -> Path | None:
    """Resolve sicuro contro path traversal — file flat al top-level."""
    folder = (RESULTS_DIR / str(project_id)).resolve()
    target = (folder / name).resolve()
    if not _is_inside(target, folder):
        return None
    if not target.is_file():
        return None
    return target


def read_run_file(project_id: int, run_name: str, file_path: str) -> Path | None:
    """Resolve sicuro per file dentro una cartella di run (anche annidati)."""
    project_root = (RESULTS_DIR / str(project_id)).resolve()
    target = (project_root / run_name / file_path).resolve()
    if not _is_inside(target, project_root):
        return None
    if not target.is_file():
        return None
    return target


# ---- File viewer / ZIP helpers ---------------------------------------------

# Nomi di file considerati "report" per la scorciatoia "Ultimi N report" sul
# task_detail. Ordine = priorita' di visualizzazione (a parita' di mtime).
RECENT_REPORT_NAMES = ("report.md", "master_summary.md", "todo.md")


def read_text_safe(
    project_id: int,
    parts: list[str],
    max_bytes: int = 5_000_000,
) -> tuple[Path, str] | None:
    """Risolve `parts` come path relativo sotto RESULTS_DIR/{project_id}, legge
    il file come UTF-8 con `errors="replace"`. Rifiuta:
    - path che escono dal project root (traversal)
    - file > max_bytes (default 5 MB) → ritorna None, il caller proporra'
      solo download per i giganti.
    """
    if not parts:
        return None
    project_root = (RESULTS_DIR / str(project_id)).resolve()
    target = project_root.joinpath(*parts).resolve()
    if not _is_inside(target, project_root):
        return None
    if not target.is_file():
        return None
    try:
        size = target.stat().st_size
    except OSError:
        return None
    if size > max_bytes:
        return None
    try:
        return target, target.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def iter_files_for_zip(
    project_id: int,
    sub: str | None = None,
) -> Iterator[tuple[Path, str]]:
    """Itera (path_assoluto, arcname_relativo) per la creazione di uno ZIP.
    - `sub=None` → tutta la dir del task.
    - `sub="20260523T071045Z"` → solo quella run.
    Skippa symlink (difesa in profondita' contro escape).
    """
    project_root = (RESULTS_DIR / str(project_id)).resolve()
    if not project_root.is_dir():
        return
    if sub:
        base = (project_root / sub).resolve()
        if not _is_inside(base, project_root) or not base.is_dir():
            return
        arc_root = base
    else:
        base = project_root
        arc_root = project_root
    for f in sorted(base.rglob("*"), key=lambda p: str(p)):
        if not f.is_file():
            continue
        if f.is_symlink():
            continue
        try:
            rel = f.relative_to(arc_root).as_posix()
        except ValueError:
            continue
        yield f, rel


def total_size_safe(
    project_id: int,
    sub: str | None = None,
    hard_cap: int = 500_000_000,
    max_files: int = 10_000,
) -> int | None:
    """Pre-check per ZIP: somma dimensioni dei file da archiviare.
    Ritorna None se eccede hard_cap o max_files (route → 413).
    """
    total = 0
    count = 0
    for path, _arcname in iter_files_for_zip(project_id, sub):
        count += 1
        if count > max_files:
            return None
        try:
            total += path.stat().st_size
        except OSError:
            continue
        if total > hard_cap:
            return None
    return total


def delete_job_artifact(task_id: int, result_path: str) -> bool:
    """Hard delete dell'artifact (file flat o run-dir) di un job dal filesystem.

    `result_path` e' il valore della colonna `jobs.result_path`, che puo' essere:
      - un file flat:       `.../data/results/{task_id}/20260523T044741Z.txt`
      - un file dentro dir: `.../data/results/{task_id}/20260523T044741Z/report.md`
      - la run-dir stessa:  `.../data/results/{task_id}/20260523T044741Z`

    In tutti i casi cancella il "primo segmento sotto la cartella del task": il
    file flat oppure l'intera run-dir, mai file fratelli o la cartella del task.

    Safety:
      - blocca se `result_path` punta fuori da RESULTS_DIR/{task_id} (traversal)
      - blocca se il segmento risolto coincide con la project root
      - skippa silenziosamente se l'artifact non esiste

    Ritorna True se qualcosa e' stato effettivamente cancellato.
    """
    if not result_path:
        return False
    project_root = (RESULTS_DIR / str(task_id)).resolve()
    try:
        target = Path(result_path).resolve()
    except (OSError, RuntimeError):
        return False
    if not _is_inside(target, project_root):
        return False
    try:
        rel = target.relative_to(project_root)
    except ValueError:
        return False
    if not rel.parts:
        return False
    artifact = project_root / rel.parts[0]
    if not artifact.exists():
        return False
    if artifact.is_dir():
        shutil.rmtree(artifact, ignore_errors=True)
        return not artifact.exists()
    try:
        artifact.unlink()
        return True
    except OSError:
        return False


def list_recent_reports(project_id: int, limit: int = 3) -> list[dict]:
    """Scorciatoia per il task_detail: ultimi N file di nome report.md /
    master_summary.md / todo.md, ordinati per st_mtime desc.
    Cerca sia in flat top-level che dentro le run dir.
    """
    project_root = (RESULTS_DIR / str(project_id)).resolve()
    if not project_root.is_dir():
        return []
    candidates: list[dict] = []
    for f in project_root.rglob("*"):
        if not f.is_file():
            continue
        if f.name not in RECENT_REPORT_NAMES:
            continue
        try:
            st = f.stat()
        except OSError:
            continue
        # path relativo dalla project_root, usato per costruire URL /file-view/...
        try:
            rel = f.relative_to(project_root).as_posix()
        except ValueError:
            continue
        # nome run = primo segmento se annidato, "" se flat top-level
        parts = rel.split("/")
        run_name = parts[0] if len(parts) > 1 else ""
        file_path = "/".join(parts[1:]) if len(parts) > 1 else parts[0]
        mtime_iso = datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).isoformat(
            timespec="seconds"
        )
        candidates.append(
            {
                "rel": rel,
                "run_name": run_name,
                "file_path": file_path,
                "name": f.name,
                "size": st.st_size,
                "mtime": st.st_mtime,
                "mtime_iso": mtime_iso,
            }
        )
    candidates.sort(key=lambda d: d["mtime"], reverse=True)
    return candidates[:limit]
