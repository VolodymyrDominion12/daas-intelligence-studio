"""Resolve a company website from its name via the configured search provider."""

from __future__ import annotations

from typing import Any

from rapidfuzz import fuzz

from ..scraping.page import page_metadata
from ..utils import normalize_company_name, normalize_domain
from .base import Enricher, EnrichmentContext, EnrichmentResult

BLOCKED_DOMAINS = {
    "facebook.com", "instagram.com", "linkedin.com", "t.me", "telegram.me", "youtube.com",
    "tiktok.com", "prom.ua", "olx.ua", "rozetka.com.ua", "epicentrk.ua", "google.com",
    "googleusercontent.com", "wikipedia.org", "yelp.com", "trustpilot.com", "dozorro.org",
    "prozorro.gov.ua", "youcontrol.com.ua", "opendatabot.ua", "work.ua", "rabota.ua",
}


class DomainResolverEnricher(Enricher):
    key = "domain_resolver"
    title = "Пошук офіційного сайту за назвою"
    produces = ["website"]
    requires = ["name"]

    def applies(self, payload: dict[str, Any]) -> bool:
        return not payload.get("website") and bool(payload.get("name"))

    async def run(self, payload: dict[str, Any], ctx: EnrichmentContext) -> EnrichmentResult:
        if ctx.search is None:
            return EnrichmentResult(
                skipped=True,
                notes=["domain_resolver needs a search provider (DAAS_SEARXNG_URL)"],
            )
        name = str(payload.get("name") or "").strip()
        city = str(payload.get("city") or "").strip()
        if not name:
            return EnrichmentResult(skipped=True, notes=["no name"])
        query = f"{name} {city} офіційний сайт".strip()
        hits = await ctx.search.search(query, limit=5)
        target = normalize_company_name(name)

        for hit in hits:
            domain = normalize_domain(hit.url)
            if not domain or any(domain == blocked or domain.endswith("." + blocked) for blocked in BLOCKED_DOMAINS):
                continue
            check = await ctx.fetcher.fetch(f"https://{domain}")
            if check.page is None:
                continue
            metadata = page_metadata(check.page.html, check.page.final_url)
            candidate = normalize_company_name(
                f"{metadata.get('title', '')} {metadata.get('h1', '')}"
            )
            score = max(
                fuzz.token_set_ratio(target, candidate),
                fuzz.partial_ratio(target, candidate),
            )
            if score >= 62:
                return EnrichmentResult(
                    values={"website": f"https://{domain}"},
                    notes=[f"resolved via {ctx.search.name}: score={score:.0f}"],
                )
        return EnrichmentResult(skipped=True, notes=["no plausible official website found"])
