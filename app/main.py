from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from . import db, jobs
from .config import settings
from .routes import assets as assets_routes
from .routes import inbox as inbox_routes
from .routes import jobs as jobs_routes
from .routes import orchestrator as orchestrator_routes
from .routes import results as results_routes
from .routes import settings as settings_routes
from .routes import settings_whatsapp as settings_whatsapp_routes
from .routes import site_memory as site_memory_routes
from .routes import social_accounts as social_accounts_routes
from .routes import tasks as tasks_routes
from .routes import workflows as workflows_routes


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    # Riconcilia job orfani (server riavviato mentre stavano girando)
    jobs.reconcile_orphan_jobs()
    jobs.start_scheduler()
    try:
        yield
    finally:
        jobs.stop_scheduler()


app = FastAPI(title="AgentScraper", lifespan=lifespan)

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

app.include_router(tasks_routes.router)
app.include_router(jobs_routes.router)
app.include_router(results_routes.router)
app.include_router(settings_routes.router)
app.include_router(settings_whatsapp_routes.router)
app.include_router(inbox_routes.router)
app.include_router(workflows_routes.router)
app.include_router(orchestrator_routes.router)
app.include_router(assets_routes.router)
app.include_router(site_memory_routes.router)
app.include_router(social_accounts_routes.router)


def run() -> None:
    """Entry point per lo script `agentscraper`. Reload attivo su modifiche di app/ e static/.

    Forza line-buffering su stdout/stderr e PYTHONUNBUFFERED nell'env del child di reload,
    così i log uvicorn appaiono in console immediatamente anche su Windows
    (lo script `agentscraper.exe` non passa il flag -u).
    """
    import os
    import sys
    import uvicorn

    # Line-buffering del processo parent
    try:
        sys.stdout.reconfigure(line_buffering=True)  # type: ignore[attr-defined]
        sys.stderr.reconfigure(line_buffering=True)  # type: ignore[attr-defined]
    except (AttributeError, OSError):
        pass
    # Propagato al child del reloader
    os.environ.setdefault("PYTHONUNBUFFERED", "1")

    project_root = Path(__file__).resolve().parent.parent
    uvicorn.run(
        "app.main:app",
        host=settings.host,
        port=settings.port,
        reload=True,
        reload_dirs=[str(project_root / "app"), str(project_root / "static")],
        reload_includes=["*.py", "*.html", "*.css", "*.js"],
    )
