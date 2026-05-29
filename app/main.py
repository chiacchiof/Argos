from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from . import db, db_cloud, jobs
from .auth import get_optional_user
from .config import settings
from .routes import admin as admin_routes
from .routes import assets as assets_routes
from .routes import auth as auth_routes
from .routes import dbconfig as dbconfig_routes
from .routes import docs as docs_routes
from .routes import fascicoli as fascicoli_routes
from .routes import operator as operator_routes
from .routes import import_csv as import_csv_routes
from .routes import inbox as inbox_routes
from .routes import jobs as jobs_routes
from .routes import orchestrator as orchestrator_routes
from .routes import results as results_routes
from .routes import settings as settings_routes
from .routes import accounts_email as accounts_email_routes
from .routes import accounts_llm_keys as accounts_llm_keys_routes
from .routes import accounts_messaging as accounts_messaging_routes
from .routes import settings_whatsapp as settings_whatsapp_routes
from .routes import site_memory as site_memory_routes
from .routes import social_accounts as social_accounts_routes
from .routes import tasks as tasks_routes
from .routes import update as update_routes
from .routes import workflows as workflows_routes


log = logging.getLogger(__name__)


# Path pubbliche (no auth richiesta) anche quando il cloud DB è configurato.
# `/dbconfig` ha il proprio gate di autenticazione interna (DBadmin), separato dal login utente.
_PUBLIC_PATH_PREFIXES = ("/static", "/login", "/logout", "/favicon.ico", "/dbconfig", "/update")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Log riepilogativo del DB target attivo (LOCALE vs REMOTO + origine).
    # Usiamo uvicorn.error: il logger app.main non ha handler propri e i messaggi
    # vengono scartati silenziosamente; uvicorn.error è sempre configurato da uvicorn.
    import logging as _logging
    _ulog = _logging.getLogger("uvicorn.error")
    _ulog.info(db.describe_active_dsn())

    # Versione corrente + check release (non bloccante, fa fetch in background)
    from . import __version__, release_check
    _ulog.info(f"[VERSION] Argos v{__version__}")
    if release_check.is_enabled():
        import asyncio
        async def _bg_check():
            # Fetch in thread separato per non bloccare il boot
            await asyncio.to_thread(release_check.latest_release, True)
        asyncio.create_task(_bg_check())
    # Ordine cruciale: db_cloud PRIMA (crea tenants/users) di db (che aggiunge
    # FK tenant_id/created_by_user_id alle tabelle business).
    db_cloud.init_db()
    db.init_db()
    # Migration one-shot idempotente: channel_config('email'|'telegram') legacy
    # -> email_accounts / telegram_bots. Da quando le route /settings/email|telegram
    # sono state rimosse (2026-05-22) la fonte canonica e' la tabella multi-account.
    db.migrate_legacy_channels_to_accounts()
    # Site memory: backfilla tenant_id NULL (righe legacy pre-multi-tenant) al
    # tenant principale. Necessaria perche' da 2026-05-22 la memoria del sito
    # e' tenant-scoped opt-in (vedi tenants.site_memory_shared).
    db.migrate_site_memory_to_super_admin()
    # Riconcilia job orfani (server riavviato mentre stavano girando)
    jobs.reconcile_orphan_jobs()
    jobs.start_scheduler()
    try:
        yield
    finally:
        jobs.stop_scheduler()
        db_cloud.close_pool()


app = FastAPI(
    title="Argos",
    lifespan=lifespan,
    docs_url=None,        # disabilita Swagger UI: la app non e' un servizio API pubblico
    redoc_url=None,       # disabilita ReDoc
    openapi_url=None,     # niente schema OpenAPI esposto
)


# NOTA SULL'ORDINE DEI MIDDLEWARE:
# Starlette mette in pipeline il middleware aggiunto PER ULTIMO come outermost.
# Per accedere a `request.session` dentro `auth_middleware`, SessionMiddleware deve
# essere OUTERMOST (eseguito per primo). Quindi:
#   1. Definiamo auth_middleware via decoratore @app.middleware("http") (innermost).
#   2. POI aggiungiamo SessionMiddleware (outermost, eseguito per primo).


@app.middleware("http")
async def update_banner_middleware(request: Request, call_next):
    """Inietta `request.state.update_info` con dict {current,latest,release_url}
    se è disponibile una release più recente su GitHub. None altrimenti.
    Cache 6h gestita da app.release_check.

    Il check NON blocca: legge la cache se fresh, fallback su None se cache vuota.
    Il primo fetch async parte al boot dal lifespan."""
    request.state.update_info = None
    try:
        from . import release_check

        if release_check.is_enabled():
            request.state.update_info = release_check.update_available()
    except Exception:
        pass
    return await call_next(request)


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    """Inietta `request.state.current_user` + tenant_id nel ContextVar.

    - In modalità legacy (no DATABASE_URL) lascia passare tutto.
    - Per super-admin (tenant_id=None) il ContextVar resta a None → no filter.
    - Per tenant_user il ContextVar contiene il loro tenant_id → tutte le
      funzioni `db.*` filtrano automaticamente senza modifiche alle route.
    """
    request.state.current_user = None

    if not db_cloud.is_configured():
        return await call_next(request)

    path = request.url.path
    is_public = any(path == p or path.startswith(p + "/") for p in _PUBLIC_PATH_PREFIXES)

    user = get_optional_user(request)
    request.state.current_user = user

    if is_public:
        tenant_token = db.set_current_tenant(user.tenant_id if user else None)
        user_token = db.set_current_user(user.id if user else None)
        try:
            return await call_next(request)
        finally:
            db.reset_current_user(user_token)
            db.reset_current_tenant(tenant_token)

    if user is None:
        if request.headers.get("HX-Request") == "true":
            resp = Response(status_code=401)
            resp.headers["HX-Redirect"] = f"/login?next={path}"
            return resp
        return RedirectResponse(url=f"/login?next={path}", status_code=302)

    # Setta i ContextVar tenant_id + user_id per la durata della request.
    # tenant_user → tenant_id int (filter); super_admin → tenant_id None (no filter).
    # user_id sempre l'id dell'utente loggato (per `created_by_user_id` auto).
    tenant_token = db.set_current_tenant(user.tenant_id)
    user_token = db.set_current_user(user.id)
    try:
        return await call_next(request)
    finally:
        db.reset_current_user(user_token)
        db.reset_current_tenant(tenant_token)


# SessionMiddleware è necessario per il cookie di login. Se SESSION_SECRET_KEY è
# vuoto generiamo una chiave volatile in memoria — i cookie non sopravvivono al
# restart, ma in modalità legacy (no DATABASE_URL) non ci sono comunque login.
_session_key = settings.session_secret_key
if not _session_key:
    import secrets

    _session_key = secrets.token_urlsafe(32)
    if db_cloud.is_configured():
        log.warning(
            "SESSION_SECRET_KEY non impostata: i login saranno invalidati ad ogni restart. "
            "Setta una chiave persistente in .env."
        )

app.add_middleware(
    SessionMiddleware,
    secret_key=_session_key,
    session_cookie="argos_session",
    same_site="lax",
    https_only=False,  # In dev su http://127.0.0.1 il cookie deve essere inviato anche senza HTTPS.
    max_age=60 * 60 * 24 * 7,  # 7 giorni
)


STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/favicon.ico", include_in_schema=False)
async def favicon() -> FileResponse:
    return FileResponse(STATIC_DIR / "brand" / "favicon.ico", media_type="image/x-icon")


app.include_router(auth_routes.router)
app.include_router(admin_routes.router)
app.include_router(dbconfig_routes.router)
app.include_router(tasks_routes.router)
app.include_router(jobs_routes.router)
app.include_router(results_routes.router)
app.include_router(settings_routes.router)
app.include_router(settings_whatsapp_routes.router)
app.include_router(inbox_routes.router)
app.include_router(workflows_routes.router)
app.include_router(orchestrator_routes.router)
app.include_router(assets_routes.router)
app.include_router(import_csv_routes.router)
app.include_router(site_memory_routes.router)
app.include_router(social_accounts_routes.router)
app.include_router(accounts_email_routes.router)
app.include_router(accounts_messaging_routes.router)
app.include_router(accounts_llm_keys_routes.router)
app.include_router(docs_routes.router)
app.include_router(fascicoli_routes.router)
app.include_router(operator_routes.router)
app.include_router(update_routes.router)


def run() -> None:
    """Entry point per lo script `argos` (alias legacy `agentscraper`). Reload attivo su modifiche di app/ e static/.

    Forza line-buffering su stdout/stderr e PYTHONUNBUFFERED nell'env del child di reload,
    così i log uvicorn appaiono in console immediatamente anche su Windows
    (lo script `argos.exe` non passa il flag -u).
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
