from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
RESULTS_DIR = DATA_DIR / "results"
DB_PATH = DATA_DIR / "agentscraper.db"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    ollama_url: str = "http://localhost:11434"
    default_model: str = "qwen3.5:latest"
    host: str = "127.0.0.1"
    port: int = 8000

    http_user_agent: str = "AgentScraper/0.1 (+local research bot)"
    http_timeout: int = 20

    default_max_iterations: int = 10


settings = Settings()
