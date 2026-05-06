from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from .config import RESULTS_DIR


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
    if not str(run_dir).startswith(str(project_root)):
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
    if not str(target).startswith(str(folder)):
        return None
    if not target.is_file():
        return None
    return target


def read_run_file(project_id: int, run_name: str, file_path: str) -> Path | None:
    """Resolve sicuro per file dentro una cartella di run (anche annidati)."""
    project_root = (RESULTS_DIR / str(project_id)).resolve()
    target = (project_root / run_name / file_path).resolve()
    if not str(target).startswith(str(project_root)):
        return None
    if not target.is_file():
        return None
    return target
