"""Enricher registry — the "waterfall" columns available to the planner."""

from __future__ import annotations

from .base import Enricher
from .domain_resolver import DomainResolverEnricher
from .site_audit import SiteAuditEnricher


def build_registry() -> dict[str, Enricher]:
    enrichers: list[Enricher] = [SiteAuditEnricher(), DomainResolverEnricher()]
    return {enricher.key: enricher for enricher in enrichers}


def enricher_keys() -> list[str]:
    return list(build_registry())


def enricher_catalog() -> list[dict]:
    return [
        {
            "key": enricher.key,
            "title": enricher.title,
            "produces": enricher.produces,
            "requires": enricher.requires,
        }
        for enricher in build_registry().values()
    ]
