from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from . import db, jobs
from .config import settings
from .routes import inbox as inbox_routes
from .routes import jobs as jobs_routes
from .routes import results as results_routes
from .routes import settings as settings_routes
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
app.include_router(inbox_routes.router)
app.include_router(workflows_routes.router)


def run() -> None:
    """Entry point per lo script `agentscraper`. Reload attivo su modifiche di app/ e static/."""
    import uvicorn

    project_root = Path(__file__).resolve().parent.parent
    uvicorn.run(
        "app.main:app",
        host=settings.host,
        port=settings.port,
        reload=True,
        reload_dirs=[str(project_root / "app"), str(project_root / "static")],
        reload_includes=["*.py", "*.html", "*.css", "*.js"],
    )
