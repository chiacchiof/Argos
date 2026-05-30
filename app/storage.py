from __future__ import annotations

import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from .config import RESULTS_DIR, DATA_DIR


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
    # Marker-based prima: estrae il nome run dal path (robusto a prefissi assoluti
    # stantii, es. repo spostato 'AgentScraper'->'Argos', altra macchina) e cancella
    # la run unit sotto l'ATTUALE RESULTS_DIR via delete_run_unit (traversal-safe).
    run_name = run_name_of(task_id, result_path)
    if run_name and delete_run_unit(task_id, run_name):
        return True
    # Fallback resolve-based: path gia' assoluto/corretto senza marker, oppure run
    # gia' rimossa. Mantiene il vecchio comportamento.
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


# ===========================================================================
# Disk-usage management ("Spazio occupato"): sizing + safe deletion helpers.
# Usati dalla pagina architect /spazio-occupato. Ogni path user-supplied
# (run_name, file) viene risolto con resolve() e verificato con _is_inside
# DENTRO la root prima di toccare il filesystem — stesso pattern dei reader.
# ===========================================================================

def dir_size(path: Path) -> int:
    """Somma (uncapped) delle dimensioni dei file sotto `path`. Skippa symlink.
    Robusto a file lockati/spariti (try/except OSError)."""
    try:
        if path.is_symlink():
            return 0
        if path.is_file():
            return path.stat().st_size
    except OSError:
        return 0
    total = 0
    for f in path.rglob("*"):
        try:
            if f.is_file() and not f.is_symlink():
                total += f.stat().st_size
        except OSError:
            continue
    return total


def _safe_run_unit(task_id: int, run_name: str) -> Path | None:
    """Risolve RESULTS_DIR/{task_id}/{run_name} come 'run unit': figlio DIRETTO
    della cartella del task (una run-dir o un file flat). Rifiuta traversal,
    la root stessa, e qualsiasi path annidato piu' in profondita'."""
    project_root = (RESULTS_DIR / str(task_id)).resolve()
    try:
        target = (project_root / run_name).resolve()
    except (OSError, RuntimeError):
        return None
    if not _is_inside(target, project_root) or target == project_root:
        return None
    if target.parent != project_root:  # solo figli diretti
        return None
    return target


def _run_date_label(name: str) -> str:
    """'20260510T193703Z' (anche con suffisso .ext) → '2026-05-10 19:37:03'.
    Per nomi non-timestamp (es. '_pending_queue.json') ritorna '' (niente label,
    cosi' la UI non mostra slice senza senso tipo '_pen-di-ng')."""
    n = name or ""
    if len(n) >= 15 and n[:8].isdigit() and n[8] == "T" and n[9:15].isdigit():
        return f"{n[0:4]}-{n[4:6]}-{n[6:8]} {n[9:11]}:{n[11:13]}:{n[13:15]}"
    return ""


def run_unit_info(task_id: int, run_name: str) -> dict | None:
    """Metadati (size/type/files_count/mtime) di una run unit. None se assente."""
    p = _safe_run_unit(task_id, run_name)
    if p is None or not p.exists():
        return None
    try:
        mtime = p.stat().st_mtime
    except OSError:
        mtime = 0.0
    if p.is_file():
        try:
            size = p.stat().st_size
        except OSError:
            size = 0
        return {"name": run_name, "type": "file", "size": size,
                "files_count": 1, "mtime": mtime,
                "date_label": _run_date_label(run_name)}
    files = [f for f in p.rglob("*") if f.is_file()]
    total = 0
    for f in files:
        try:
            total += f.stat().st_size
        except OSError:
            continue
    return {"name": run_name, "type": "dir", "size": total,
            "files_count": len(files), "mtime": mtime,
            "date_label": _run_date_label(run_name)}


def list_task_runs(task_id: int) -> list[dict]:
    """Tutte le run unit di un task (run-dir + file flat), piu' recenti prima."""
    folder = RESULTS_DIR / str(task_id)
    if not folder.is_dir():
        return []
    out: list[dict] = []
    for entry in folder.iterdir():
        info = run_unit_info(task_id, entry.name)
        if info:
            out.append(info)
    out.sort(key=lambda d: d["name"], reverse=True)
    return out


def task_disk_summary(task_id: int) -> dict:
    """{size, run_count, last_mtime} aggregati di un task."""
    runs = list_task_runs(task_id)
    return {
        "size": sum(r["size"] for r in runs),
        "run_count": len(runs),
        "last_mtime": max((r["mtime"] for r in runs), default=0.0),
    }


def run_name_of(task_id: int, result_path: str | None) -> str | None:
    """Nome della run unit (primo segmento sotto data/results/{task_id}/) estratto
    dal `jobs.result_path`.

    MARKER-BASED, NON resolve(): il `result_path` salvato in DB puo' avere un
    prefisso assoluto diverso dall'attuale RESULTS_DIR (job vecchi, repo spostato
    es. da 'AgentScraper' a 'Argos', altra macchina). La cartella esiste comunque
    sotto l'attuale RESULTS_DIR — quindi estraiamo il nome dal marker, e la safety
    di path la fanno i consumer (list_run_files / _safe_run_unit / delete_run_unit)
    che risolvono contro l'attuale RESULTS_DIR e fanno _is_inside.
    """
    if not result_path:
        return None
    norm = str(result_path).replace("\\", "/")
    marker = f"/results/{int(task_id)}/"
    idx = norm.rfind(marker)
    if idx == -1:
        return None
    rel = norm[idx + len(marker):]
    first = rel.split("/")[0] if rel else ""
    return first or None


def delete_task_folder(task_id: int) -> bool:
    """Cancella l'INTERA cartella RESULTS_DIR/{task_id} (tutti i run). Usato per
    le cartelle 'orfane' di task cancellati. Verifica che sia un figlio diretto
    di RESULTS_DIR prima del rmtree (difesa, anche se task_id e' un int)."""
    results_root = RESULTS_DIR.resolve()
    folder = (results_root / str(task_id)).resolve()
    if not _is_inside(folder, results_root) or folder == results_root:
        return False
    if folder.parent != results_root:
        return False
    if not folder.is_dir():
        return False
    shutil.rmtree(folder, ignore_errors=True)
    return not folder.exists()


def delete_run_unit(task_id: int, run_name: str) -> bool:
    """Cancella un'intera run unit (run-dir o file flat). Traversal-safe."""
    p = _safe_run_unit(task_id, run_name)
    if p is None or not p.exists():
        return False
    if p.is_dir():
        shutil.rmtree(p, ignore_errors=True)
        return not p.exists()
    try:
        p.unlink()
        return True
    except OSError:
        return False


def delete_run_file(task_id: int, run_name: str, file_path: str) -> bool:
    """Cancella un SINGOLO file dentro una run-dir. Rifiuta dir, symlink,
    traversal e il caso file_path vuoto (che punterebbe alla run-dir)."""
    if not file_path:
        return False
    project_root = (RESULTS_DIR / str(task_id)).resolve()
    run_dir = (project_root / run_name).resolve()
    if not _is_inside(run_dir, project_root) or run_dir == project_root:
        return False
    if not run_dir.is_dir():
        return False
    try:
        target = (run_dir / file_path).resolve()
    except (OSError, RuntimeError):
        return False
    if not _is_inside(target, run_dir):
        return False
    if target.is_symlink() or not target.is_file():
        return False
    try:
        target.unlink()
        return True
    except OSError:
        return False


def delete_task_files_by_ext(
    task_id: int, exts: set[str], run_names: list[str] | None = None,
) -> dict:
    """Cancella i file con suffisso in `exts` (lowercase, col punto, es. {'.md'})
    nelle run unit indicate (o tutte). Le run unit che sono file flat vengono
    cancellate solo se il loro suffisso e' in `exts`. Ritorna {deleted, freed}."""
    folder = RESULTS_DIR / str(task_id)
    if not folder.is_dir():
        return {"deleted": 0, "freed": 0}
    names = run_names if run_names else [e.name for e in folder.iterdir()]
    deleted = 0
    freed = 0
    for name in names:
        p = _safe_run_unit(task_id, name)
        if p is None or not p.exists():
            continue
        if p.is_file():
            if p.suffix.lower() in exts:
                try:
                    sz = p.stat().st_size
                    p.unlink()
                    deleted += 1
                    freed += sz
                except OSError:
                    continue
            continue
        for f in list(p.rglob("*")):
            try:
                if not f.is_file() or f.is_symlink():
                    continue
                if f.suffix.lower() not in exts:
                    continue
                if not _is_inside(f.resolve(), p):
                    continue
                sz = f.stat().st_size
                f.unlink()
                deleted += 1
                freed += sz
            except OSError:
                continue
    return {"deleted": deleted, "freed": freed}


# ---- Sessioni browser persistenti (FUORI da RESULTS_DIR) -------------------
# Dominano lo spazio su disco (cache Chromium per-account). Mostrate nella
# pagina Spazio occupato; cancellabili SOLO da super-admin con conferma forte
# (cancellarle = logout dell'account -> re-login/QR al prossimo run).

_SESSION_ROOTS: dict[str, str] = {
    "social_sessions": "Sessioni social (cache browser per account)",
    "whatsapp_sessions": "Sessioni WhatsApp Web",
    "sessions": "Screenshot login falliti",
}


def session_breakdown() -> list[dict]:
    """Per ogni root di sessione: dimensione totale + figli diretti (per-account)."""
    out: list[dict] = []
    for key, label in _SESSION_ROOTS.items():
        root = DATA_DIR / key
        if not root.is_dir():
            out.append({"key": key, "label": label, "size": 0,
                        "count": 0, "entries": [], "exists": False})
            continue
        entries: list[dict] = []
        total = 0
        for sub in root.iterdir():
            if sub.is_symlink():
                continue
            sz = dir_size(sub)
            total += sz
            entries.append({"name": sub.name, "size": sz, "is_dir": sub.is_dir()})
        entries.sort(key=lambda e: e["size"], reverse=True)
        out.append({"key": key, "label": label, "size": total,
                    "count": len(entries), "entries": entries, "exists": True})
    return out


def delete_session_entry(root_key: str, name: str) -> bool:
    """Cancella un figlio diretto (sottocartella/file) di una root di sessione.
    Allowlist sul root_key + figlio diretto + no symlink. Traversal-safe."""
    if root_key not in _SESSION_ROOTS or not name:
        return False
    root = (DATA_DIR / root_key).resolve()
    try:
        target = (root / name).resolve()
    except (OSError, RuntimeError):
        return False
    if not _is_inside(target, root) or target == root:
        return False
    if target.parent != root:
        return False
    if target.is_symlink():
        return False
    if target.is_dir():
        shutil.rmtree(target, ignore_errors=True)
        return not target.exists()
    try:
        target.unlink()
        return True
    except OSError:
        return False
