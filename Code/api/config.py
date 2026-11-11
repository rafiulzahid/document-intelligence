"""Settings, read from environment variables or the project .env file.

DATABASE_URL     postgresql+psycopg://docintel:docintel@localhost:5432/docintel   (Postgres)
                 sqlite:///data/app.db                                             (no setup, default)
QUEUE_MODE       rq (Redis + worker process) | thread (background threads in the API) | sync (tests)
REDIS_URL        redis://localhost:6379/0
API_KEY          required in the X-API-Key header when set (leave empty to disable auth locally)
STORAGE_DIR      where uploaded files are kept (default data/uploads)
MAX_UPLOAD_MB    per-file limit (default 50)
CORS_ORIGINS     comma-separated, default http://localhost:3000
EMBED_PROVIDER   openai (OpenAI-compatible /embeddings, e.g. Ollama) | sentence_transformers | hash | none
EMBED_MODEL      e.g. nomic-embed-text (Ollama) or all-MiniLM-L6-v2 (sentence-transformers)
EMBED_BASE_URL   default: DOCINTEL_LLM_BASE_URL
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]          # project folder (contains Code/)


def _load_env():
    p = ROOT / ".env"
    if p.is_file():
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_env()


@dataclass
class Settings:
    database_url: str = field(default_factory=lambda: os.environ.get("DATABASE_URL", f"sqlite:///{ROOT / 'data' / 'app.db'}"))
    queue_mode: str = field(default_factory=lambda: os.environ.get("QUEUE_MODE", "thread"))
    redis_url: str = field(default_factory=lambda: os.environ.get("REDIS_URL", "redis://localhost:6379/0"))
    api_key: str = field(default_factory=lambda: os.environ.get("API_KEY", ""))
    storage_dir: Path = field(default_factory=lambda: Path(os.environ.get("STORAGE_DIR", str(ROOT / "data" / "uploads"))))
    max_upload_mb: float = field(default_factory=lambda: float(os.environ.get("MAX_UPLOAD_MB", "50")))
    cors_origins: list = field(default_factory=lambda: [o.strip() for o in os.environ.get(
        "CORS_ORIGINS", "http://localhost:3000,http://127.0.0.1:3000").split(",") if o.strip()])
    embed_provider: str = field(default_factory=lambda: os.environ.get("EMBED_PROVIDER", "hash"))
    embed_model: str = field(default_factory=lambda: os.environ.get("EMBED_MODEL", "nomic-embed-text"))
    embed_base_url: str = field(default_factory=lambda: os.environ.get("EMBED_BASE_URL", os.environ.get(
        "DOCINTEL_LLM_BASE_URL", "http://localhost:11434/v1")))
    embed_api_key: str = field(default_factory=lambda: os.environ.get("EMBED_API_KEY", os.environ.get("DOCINTEL_LLM_API_KEY", "")))
    ocr: str = field(default_factory=lambda: os.environ.get("DOCINTEL_OCR", "auto"))
    thread_workers: int = field(default_factory=lambda: int(os.environ.get("THREAD_WORKERS", "2")))

    @property
    def is_postgres(self) -> bool:
        return self.database_url.startswith("postgresql")


settings = Settings()


def reload_settings(**overrides) -> Settings:
    """Used by tests: rebuild settings from the environment plus overrides."""
    global settings
    settings = Settings(**overrides)
    return settings
