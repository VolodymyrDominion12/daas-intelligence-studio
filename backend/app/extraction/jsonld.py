"""JSON-LD / schema.org extraction — highest-precision deterministic strategy."""

from __future__ import annotations

import json
from typing import Any

from ..models import FieldSpec, TaskSpec
from ..scraping.page import extract_json_ld
from ..utils import normalize_domain, parse_number
from .base import ExtractionOutcome
from .synonyms import field_tokens, normalize_token

# column key -> ordered JSON-LD paths (dotted). Falls back to fuzzy path matching.
PATH_MAP: dict[str, tuple[str, ...]] = {
    "name": ("name", "legalName", "headline"),
    "title": ("name", "headline", "title"),
    "job_title": ("title", "name"),
    "website": ("url", "sameAs", "mainEntityOfPage.@id"),
    "phone": ("telephone", "contactPoint.telephone"),
    "email": ("email", "contactPoint.email"),
    "address": ("address.streetAddress", "address", "location.address"),
    "city": ("address.addressLocality", "location.address.addressLocality"),
    "region": ("address.addressRegion",),
    "price": (
        "offers.price",
        "offers.lowPrice",
        "offers.highPrice",
        "priceSpecification.price",
        "price",
    ),
    "currency": ("offers.priceCurrency", "priceCurrency"),
    "rating": ("aggregateRating.ratingValue", "rating.ratingValue", "ratingValue"),
    "reviews": ("aggregateRating.reviewCount", "aggregateRating.ratingCount", "reviewCount"),
    "reviews_count": (
        "aggregateRating.reviewCount",
        "aggregateRating.ratingCount",
        "reviewCount",
    ),
    "brand": ("brand.name", "manufacturer.name"),
    "description": ("description", "abstract"),
    "company": ("hiringOrganization.name", "brand.name", "manufacturer.name", "seller.name"),
    "salary": ("baseSalary.value.value", "baseSalary.value.minValue", "estimatedSalary.value.value"),
    "posted_at": ("datePosted", "datePublished", "dateCreated"),
    "availability": ("offers.availability",),
    "sku": ("sku", "productID", "gtin13"),
    "image": ("image", "photo"),
    "category": ("category", "@type"),
}


def jsonld_items(html: str, *, base_url: str = "") -> list[dict[str, Any]]:
    blocks = extract_json_ld(html)
    items = []
    for block in blocks:
        flattened = _flatten(block)
        kind = str(block.get("@type") or "").lower()
        if not _is_useful(flattened, kind):
            continue
        flattened["_type"] = kind
        if base_url:
            flattened.setdefault("_source", base_url)
        items.append(flattened)
    return items


def _flatten(payload: dict[str, Any], prefix: str = "") -> dict[str, Any]:
    flattened: dict[str, Any] = {}
    for key, value in payload.items():
        name = f"{prefix}.{key}" if prefix else key
        if isinstance(value, dict):
            flattened[name] = value
            flattened.update(_flatten(value, name))
        elif isinstance(value, list):
            flattened[name] = value
            for index, item in enumerate(value):
                if isinstance(item, dict):
                    flattened.update(_flatten(item, name))
                elif index == 0:
                    flattened[f"{name}.0"] = item
        else:
            flattened[name] = value
    return flattened


def _is_useful(flattened: dict[str, Any], kind: str) -> bool:
    interesting = {"name", "headline", "url", "telephone", "email", "offers.price", "price"}
    if any(key in flattened for key in interesting) is False:
        return False
    return bool(kind) or bool(flattened.get("name"))


def map_columns(item: dict[str, Any], columns: list[FieldSpec]) -> dict[str, Any]:
    values: dict[str, Any] = {}
    normalized_keys = {normalize_token(key.split(".")[-1]): key for key in item}
    flat_keys = {normalize_token(key): key for key in item}

    for column in columns:
        if column.is_ai or column.prompt:
            continue
        candidates: list[str] = list(PATH_MAP.get(column.key, ()))
        for alias in column.aliases:
            candidates.append(alias)
        candidates.append(column.key)

        value = _lookup(item, item, candidates, flat_keys)
        if value is None:
            tokens = field_tokens(column)
            for token in tokens:
                if token in normalized_keys:
                    value = item[normalized_keys[token]]
                    break
        if value not in (None, "", []):
            values[column.key] = value
    return values


def _lookup(item: dict[str, Any], root: dict[str, Any], candidates: list[str], flat_keys: dict[str, str]) -> Any:
    for candidate in candidates:
        if candidate in item:
            return item[candidate]
        normalized = normalize_token(candidate)
        if normalized in flat_keys:
            return item[flat_keys[normalized]]
    return None


def extract_jsonld(html: str, spec: TaskSpec, *, source_url: str = "") -> ExtractionOutcome:
    items = jsonld_items(html, base_url=source_url)
    mapped: list[dict[str, Any]] = []
    for item in items:
        values = map_columns(item, spec.columns)
        if not values:
            continue
        if "website" not in values:
            url = item.get("url") or ""
            if isinstance(url, str) and normalize_domain(url):
                values["website"] = url
        mapped.append(values)
    if not mapped:
        return ExtractionOutcome(items=[], strategy="json_ld", confidence=0.0)
    # Deduplicate identical JSON-LD blocks (very common: item + breadcrumb).
    unique_items: list[dict[str, Any]] = []
    seen: set[str] = set()
    for values in mapped:
        marker = json.dumps(values, sort_keys=True, default=str)
        if marker in seen:
            continue
        seen.add(marker)
        unique_items.append(values)
    return ExtractionOutcome(
        items=unique_items, strategy="json_ld", confidence=0.9, warnings=[]
    )


def price_from_jsonld(html: str) -> float | None:
    for item in jsonld_items(html):
        raw = item.get("offers.price") or item.get("price")
        value = parse_number(raw)
        if value is not None:
            return value
    return None
