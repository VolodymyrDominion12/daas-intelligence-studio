"""Shared fixtures: fully offline fetcher, temp storage, spec helpers."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import Settings, reset_settings_cache  # noqa: E402
from app.models import PageContent, TaskSpec  # noqa: E402
from app.presets import Limits, build_spec  # noqa: E402
from app.runtime import Studio  # noqa: E402
from app.scraping.fetcher import FetchResult  # noqa: E402
from app.scraping.page import html_to_markdown, html_to_text  # noqa: E402
from app.utils import hash_text  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures"
DEMO_BASE = "http://demo.local"


def read_fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


class FixtureFetcher:
    """Deterministic Fetcher over local HTML files (no network, no robots)."""

    name = "fixture"

    def __init__(self, routes: dict[str, str] | None = None) -> None:
        self.routes = routes or DEFAULTS
        self.calls: list[str] = []

    def _resolve(self, url: str) -> str | None:
        key = url.rstrip("/") or url
        if key in self.routes:
            return self.routes[key]
        if url.rstrip("/") in self.routes:
            return self.routes[url.rstrip("/")]
        if "clinic-" in url:
            return "clinic-alpha.html"
        host = url.split("://", 1)[-1].split("/")[0]
        if host.endswith(".example"):
            return "site_alpha.html"
        return None

    async def fetch(self, url: str, *, force_refresh: bool = False) -> FetchResult:
        self.calls.append(url)
        fixture = self._resolve(url)
        if fixture is None:
            return FetchResult(kind="permanent", error=f"no fixture for {url}", status=404)
        html = read_fixture(fixture)
        page = PageContent(
            url=url,
            final_url=url,
            status_code=200,
            html=html,
            text=html_to_text(html),
            markdown=html_to_markdown(html, url),
            content_hash=hash_text(html),
            engine=self.name,
        )
        return FetchResult(page=page, status=200)

    async def aclose(self) -> None:
        return None


DEFAULTS = {
    f"{DEMO_BASE}/catalog.html": "catalog.html",
    f"{DEMO_BASE}/": "catalog.html",
    f"{DEMO_BASE}": "catalog.html",
    f"{DEMO_BASE}/about": "catalog.html",
    f"{DEMO_BASE}/clinic-alpha.html": "clinic-alpha.html",
    f"{DEMO_BASE}/clinic-beta.html": "clinic-alpha.html",
    f"{DEMO_BASE}/clinic-gamma.html": "clinic-alpha.html",
    "https://alpha.example": "site_alpha.html",
    "https://alpha.example/": "site_alpha.html",
}


def make_settings(tmp_path: Path, **overrides: Any) -> Settings:
    reset_settings_cache()
    values = {
        "storage_dir": str(tmp_path / "storage"),
        "offline": True,
        "disable_auth": True,
        "respect_robots": False,
        "llm_provider": "disabled",
        "llm_local_base_url": None,
        "llm_base_url": None,
        "llm_api_key": None,
        "llm_fallback_base_url": None,
        "llm_fallback_api_key": None,
        "sample_size": 3,
        "max_pages_per_run": 20,
        "log_level": "WARNING",
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def make_spec(prompt: str = "", seeds: list[str] | None = None) -> TaskSpec:
    return build_spec(
        prompt or "Збери стоматологічні клініки Києва: назва, місто, адреса, телефон, сайт, рейтинг",
        seeds if seeds is not None else [f"{DEMO_BASE}/catalog.html"],
        max_pages=20,
        sample_size=3,
        budget_usd=0.5,
        max_depth=1,
    )


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return make_settings(tmp_path)


@pytest.fixture
def fetcher() -> FixtureFetcher:
    return FixtureFetcher()


@pytest.fixture
def studio(tmp_path: Path, fetcher: FixtureFetcher):
    studio = Studio(make_settings(tmp_path), fetcher=fetcher)
    yield studio
    import asyncio

    asyncio.run(studio.aclose())


__all__ = [
    "DEFAULTS",
    "DEMO_BASE",
    "FixtureFetcher",
    "Limits",
    "TaskSpec",
    "make_settings",
    "make_spec",
    "read_fixture",
]
