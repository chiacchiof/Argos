from pathlib import Path

from dotenv import load_dotenv
from pydantic_settings import BaseSettings, SettingsConfigDict


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
RESULTS_DIR = DATA_DIR / "results"
UPLOADS_DIR = DATA_DIR / "uploads"
DB_PATH = DATA_DIR / "agentscraper.db"


# Carica .env in os.environ per le variabili NON coperte da Settings (es.
# AGENTSCRAPER_SECRET, AGENTSCRAPER_PROXIES, OPENAI_API_KEY se serve fuori
# da Settings). pydantic-settings legge .env solo per i suoi campi.
load_dotenv(PROJECT_ROOT / ".env", override=False)

# Override runtime di DATABASE_URL da file cifrato gestito dalla pagina /dbconfig.
# DEVE essere chiamato DOPO load_dotenv (per avere AGENTSCRAPER_SECRET) e PRIMA
# che `settings` sia istanziata (così Settings() legge la DSN overridata).
from ._runtime_db_override import apply_override as _apply_db_override  # noqa: E402

_apply_db_override()


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    ollama_url: str = "http://localhost:11434"
    default_model: str = "qwen3.5:latest"
    host: str = "127.0.0.1"
    port: int = 8000

    http_user_agent: str = "AgentScraper/0.1 (+local research bot)"
    http_timeout: int = 20

    default_max_iterations: int = 10

    # --- Multi-tenant / cloud DB (Fase 1: opzionali, attivano auth solo se settati) ---
    # Connection string Postgres (Neon/Azure). Se vuoto -> modalità legacy single-user (no auth).
    database_url: str = ""
    # Chiave per firmare i cookie di sessione (Starlette SessionMiddleware).
    # Generala con: python -c "import secrets; print(secrets.token_urlsafe(32))"
    session_secret_key: str = ""
    # Bootstrap super-admin al primo boot su DB cloud vuoto. Lette dall'env, usate
    # solo se l'utente con quella email non esiste già. Cambia la password dopo il primo login.
    bootstrap_super_admin_email: str = ""
    bootstrap_super_admin_password: str = ""


settings = Settings()
