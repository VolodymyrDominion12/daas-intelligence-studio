"""Application configuration and filesystem layout.

Everything is env-driven (prefix ``DAAS_``) so the studio can run fully offline
against local Ollama / LM Studio, or against any OpenAI-compatible cloud API.
"""

from __future__ import annotations

import contextlib
import json
import os
import secrets
from datetime import UTC
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]


class Paths:
    """Resolved local-first storage layout (see docs/01 §2)."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.sqlite = root / "operational.sqlite"
        self.duckdb = root / "analytics.duckdb"
        self.raw_cache = root / "raw_cache"
        self.snapshots = root / "snapshots"
        self.exports = root / "exports"
        self.runtime = root / "runtime.json"
        self.logs = root / "logs"

    def ensure(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        for path in (self.raw_cache, self.snapshots, self.exports, self.logs):
            path.mkdir(parents=True, exist_ok=True)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="DAAS_",
        env_file=(PROJECT_ROOT / ".env", PROJECT_ROOT / "backend" / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- server -----------------------------------------------------------
    host: str = "127.0.0.1"
    port: int = 8000
    storage_dir: str = "storage"
    cors_origins: str = "http://localhost:5173,http://127.0.0.1:5173"
    api_token: str | None = None
    disable_auth: bool = False

    # --- scraping ---------------------------------------------------------
    fetch_engine: Literal["http", "playwright", "auto"] = "http"
    user_agent: str = (
        "DaasStudioBot/0.1 (+https://github.com/daas-intelligence-studio; polite research crawler)"
    )
    respect_robots: bool = True
    request_timeout_s: float = 20.0
    cache_ttl_hours: int = 24
    max_retries: int = 2
    max_concurrency: int = 4
    per_domain_rps: float = 1.0
    per_domain_concurrency: int = 1
    allow_private_network: bool = False

    # --- pipeline limits --------------------------------------------------
    sample_size: int = 4
    max_pages_per_run: int = 50
    max_depth: int = 1
    max_llm_calls_per_run: int = 25
    max_enrich_concurrency: int = 3
    price_drop_threshold_pct: float = 5.0

    # --- LLM --------------------------------------------------------------
    llm_provider: Literal["auto", "openai_compat", "mock", "disabled"] = "auto"
    llm_base_url: str | None = None
    llm_api_key: str | None = None
    llm_model: str = "gpt-4o-mini"
    llm_timeout_s: float = 120.0
    llm_fallback_base_url: str | None = None
    llm_fallback_api_key: str | None = None
    llm_fallback_model: str | None = None
    llm_local_base_url: str | None = "http://127.0.0.1:11434/v1"
    llm_local_model: str | None = "readerlm-v2"
    llm_budget_usd: float = 0.75
    llm_input_char_limit: int = 60_000
    llm_redact_pii: bool = True
    llm_temperature: float = 0.0
    llm_cache: bool = True

    # --- discovery --------------------------------------------------------
    searxng_url: str | None = None
    search_max_results: int = 10

    # --- runtime flags ----------------------------------------------------
    offline: bool = False
    log_level: str = "INFO"

    @field_validator("storage_dir")
    @classmethod
    def _strip_storage_dir(cls, value: str) -> str:
        return value.strip() or "storage"

    @property
    def paths(self) -> Paths:
        raw = Path(self.storage_dir).expanduser()
        root = raw if raw.is_absolute() else PROJECT_ROOT / raw
        return Paths(root.resolve())

    @property
    def cors_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]

    @property
    def llm_configured(self) -> bool:
        if self.llm_provider == "mock":
            return True
        if self.llm_provider == "disabled":
            return False
        return bool(self.llm_base_url and self.llm_api_key)


@lru_cache
def get_settings() -> Settings:
    return Settings()


def reset_settings_cache() -> None:
    get_settings.cache_clear()


def load_runtime_token(settings: Settings) -> str:
    """Bearer token protecting the local API (CSRF / DNS-rebinding guard, ADR-08)."""
    if settings.api_token:
        return settings.api_token
    paths = settings.paths
    paths.ensure()
    if paths.runtime.exists():
        try:
            data = json.loads(paths.runtime.read_text("utf-8"))
            token = data.get("api_token")
            if isinstance(token, str) and len(token) >= 16:
                return token
        except (json.JSONDecodeError, OSError):
            pass
    token = secrets.token_urlsafe(32)
    paths.runtime.write_text(
        json.dumps({"api_token": token, "created_at": _now_iso()}, indent=2), encoding="utf-8"
    )
    with contextlib.suppress(OSError):
        os.chmod(paths.runtime, 0o600)
    return token


def _now_iso() -> str:
    from datetime import datetime

    return datetime.now(UTC).isoformat()
