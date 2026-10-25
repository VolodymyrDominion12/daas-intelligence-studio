"""Deterministic CSS extraction from a learned schema."""

from __future__ import annotations

import re
from typing import Any

from ..models import CssSchema, FieldSpec, TaskSpec
from ..scraping.page import parse_html
from ..utils import coerce_value, find_emails, find_phones, normalize_space
from .base import ExtractionOutcome

_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")


def extract_css(
    html: str, schema: CssSchema, spec: TaskSpec, *, base_url: str = ""
) -> ExtractionOutcome:
    tree = parse_html(html)
    if not schema.container:
        return ExtractionOutcome(strategy="css", confidence=0.0)
    containers = tree.css(schema.container)
    if not containers:
        return ExtractionOutcome(
            strategy="css", confidence=0.0, warnings=[f"container not found: {schema.container}"]
        )
    if not schema.item_scope:
        containers = containers[:1]

    fields_by_key = {column.key: column for column in spec.columns}
    items: list[dict[str, Any]] = []
    confidence_sum = 0.0
    confidence_count = 0

    for container in containers:
        values: dict[str, Any] = {}
        for css_field in schema.fields:
            column = fields_by_key.get(css_field.key)
            if column is None or column.is_ai:
                continue
            node = _select(container, css_field.selector, root=tree, scoped=schema.item_scope)
            raw = _read_node(node, css_field.attr)
            value = coerce_value(raw, css_field.dtype)
            if value in (None, "", []):
                continue
            values[column.key] = value
            confidence_sum += css_field.confidence
            confidence_count += 1

        # Columns added after calibration: try itemprop/class-based fallbacks.
        for column in spec.columns:
            if column.is_ai or column.key in values:
                continue
            value = fallback_field(container, column)
            if value not in (None, "", []):
                values[column.key] = value

        if not values:
            continue
        if "name" not in values:
            heading = container.css_first("h1, h2, h3, h4, .title, .name")
            if heading is not None:
                title = normalize_space(heading.text(strip=True))
                if title:
                    values["name"] = title

        items.append(values)

    confidence = round(confidence_sum / confidence_count, 3) if confidence_count else 0.0
    warnings: list[str] = []
    if not items:
        warnings.append("no items matched learned schema")
    return ExtractionOutcome(
        items=items, strategy="css", confidence=confidence, warnings=warnings
    )


def fallback_field(container, column: FieldSpec) -> Any:
    selectors = [
        f'[itemprop="{column.key}"]',
        f'[data-field="{column.key}"]',
        f'.{column.key}',
        f'[class*="{column.key}"]',
    ]
    for selector in selectors:
        try:
            node = container.css_first(selector)
        except Exception:  # invalid selector guard
            continue
        if node is None:
            continue
        raw = _read_node(node, "href" if column.dtype == "url" else "src" if column.dtype == "image" else None)
        value = coerce_value(raw, column.dtype)
        if value not in (None, "", []):
            return value

    text = container.text(separator=" ", strip=True)
    if column.dtype == "email":
        emails = find_emails(text, limit=1)
        return emails[0] if emails else None
    if column.dtype == "phone":
        phones = find_phones(text, limit=1)
        return phones[0] if phones else None
    return None


def _select(container, selector: str, *, root, scoped: bool):
    if not selector:
        return None
    target = container if scoped else root
    try:
        node = target.css_first(selector)
    except Exception:
        return None
    return node


def _read_node(node, attr: str | None) -> str | None:
    if node is None:
        return None
    attributes = node.attributes or {}
    if attr:
        value = attributes.get(attr)
        if value:
            return value
    if node.tag == "a" and attributes.get("href"):
        text = normalize_space(node.text(strip=True))
        return text or attributes.get("href")
    if node.tag == "img" and attributes.get("src"):
        return attributes.get("src")
    if node.tag == "time" and attributes.get("datetime"):
        return attributes.get("datetime")
    return normalize_space(node.text(strip=True))
