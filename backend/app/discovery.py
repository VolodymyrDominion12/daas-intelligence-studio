"""Optional discovery providers (start URLs, not full crawling)."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

import httpx

from .config import Settings
from .utils import normalize_domain

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class SearchHit:
    title: str
    url: str
    snippet: str = ""


class SearchProvider:
    name = "none"

    async def search(self, query: str, limit: int = 10) -> list[SearchHit]:  # pragma: no cover
        raise NotImplementedError

    async def aclose(self) -> None:  # pragma: no cover
        return None


class SearxngSearch(SearchProvider):
    """Self-hosted SearXNG (JSON API). No API keys, no scraping of search engines."""

    name = "searxng"

    def __init__(self, base_url: str, *, timeout_s: float = 20.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_s = timeout_s

    async def search(self, query: str, limit: int = 10) -> list[SearchHit]:
        params = {"q": query, "format": "json", "language": "uk-UA", "safesearch": "1"}
        async with httpx.AsyncClient(timeout=self.timeout_s) as client:
            try:
                response = await client.get(f"{self.base_url}/search", params=params)
                response.raise_for_status()
                payload = response.json()
            except (httpx.HTTPError, ValueError) as exc:
                logger.warning("searxng search failed: %s", exc)
                return []
        hits: list[SearchHit] = []
        for item in payload.get("results", [])[:limit]:
            url = str(item.get("url") or "")
            if not url.startswith(("http://", "https://")):
                continue
            hits.append(
                SearchHit(
                    title=str(item.get("title") or ""),
                    url=url,
                    snippet=str(item.get("content") or ""),
                )
            )
        return hits


def build_search(settings: Settings) -> SearchProvider | None:
    if settings.offline:
        return None
    if settings.searxng_url:
        return SearxngSearch(settings.searxng_url)
    return None


_STOPWORDS = {
    "знайди", "збери", "зібери", "спарси", "витягни", "потрібні", "мені", "всі", "усі",
    "та", "і", "з", "у", "в", "на", "для", "про", "що", "які", "який", "їх", "їхні",
    "контакти", "сайти", "сайт", "дані", "інформацію", "список",
}


def query_from_prompt(prompt: str) -> str:
    words = [word for word in re.findall(r"[\w'’\-]+", prompt.lower()) if len(word) > 2]
    kept = [word for word in words if word not in _STOPWORDS][:10]
    return " ".join(kept) or prompt[:120]


def dedupe_domains(hits: list[SearchHit]) -> list[SearchHit]:
    seen: set[str] = set()
    result: list[SearchHit] = []
    for hit in hits:
        domain = normalize_domain(hit.url)
        if not domain or domain in seen:
            continue
        seen.add(domain)
        result.append(hit)
    return result
