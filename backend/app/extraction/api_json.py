"""Deterministic extractor for public REST JSON API sources (D-02)."""

from __future__ import annotations

import json
import re
from typing import Any

from ..models import ApiSourceSpec, ExtractedRecord, Provenance, TaskSpec
from ..utils import coerce_value, hash_text, record_key_for, utcnow

_INT_RE = re.compile(r"^\d+$")


def _walk_path(node: Any, path: str) -> Any:
    """Resolve dot-separated path with optional integer list indices and ``|`` fallbacks."""
    if not path:
        return node
    for branch in path.split("|"):
        candidate = branch.strip()
        if not candidate:
            continue
        cur = node
        ok = True
        for part in candidate.split("."):
            if isinstance(cur, dict):
                if part in cur:
                    cur = cur[part]
                else:
                    ok = False
                    break
            elif isinstance(cur, list) and _INT_RE.match(part):
                idx = int(part)
                if 0 <= idx < len(cur):
                    cur = cur[idx]
                else:
                    ok = False
                    break
            else:
                ok = False
                break
        if ok and cur not in (None, "", []):
            return cur
    return None


def _render_template(template: str, item_id: str) -> str:
    return template.replace("{id}", str(item_id))


def extract_json_list_targets(
    raw_json: str,
    api_source: ApiSourceSpec,
    *,
    detail_url_template: str | None = None,
) -> tuple[list[tuple[str, str]], str | None, str | None, list[str]]:
    """Extract ``(id, detail_url)`` pairs, next-page URL, and cursor from a list response."""
    warnings: list[str] = []
    try:
        doc = json.loads(raw_json)
    except (json.JSONDecodeError, TypeError) as exc:
        return [], None, None, [f"Невалідний JSON у відповіді API: {exc}"]

    items = _walk_path(doc, api_source.items_path) if api_source.items_path else doc
    if items is None:
        items = []
    if not isinstance(items, list):
        items = [items]

    template = detail_url_template or api_source.detail_url_template
    targets: list[tuple[str, str]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        raw_id = _walk_path(item, api_source.id_path)
        if raw_id is None:
            continue
        item_id = str(raw_id).strip()
        if not item_id:
            continue
        if template:
            targets.append((item_id, _render_template(template, item_id)))
        else:
            targets.append((item_id, ""))

    next_raw = _walk_path(doc, api_source.next_page_path) if api_source.next_page_path else None
    next_url = str(next_raw).strip() if isinstance(next_raw, str) and next_raw.strip() else None

    cursor_raw = _walk_path(doc, api_source.cursor_path) if api_source.cursor_path else None
    cursor = str(cursor_raw).strip() if cursor_raw not in (None, "") else None

    return targets, next_url, cursor, warnings


def extract_json_records(
    raw_json: str,
    spec: TaskSpec,
    source_url: str,
) -> tuple[list[ExtractedRecord], list[str]]:
    """Map JSON list or detail response into validated ``ExtractedRecord`` items."""
    records, _quarantined, warnings = extract_json_records_with_quarantine(
        raw_json, spec, source_url, strict_quarantine=False
    )
    return records, warnings


def extract_json_records_with_quarantine(
    raw_json: str,
    spec: TaskSpec,
    source_url: str,
    *,
    strict_quarantine: bool = True,
) -> tuple[list[ExtractedRecord], list[dict[str, Any]], list[str]]:
    """Map JSON items into validated ``ExtractedRecord`` items and quarantined suspects."""
    api_source = spec.api_source
    if api_source is None:
        return [], [], ["У задачі відсутня конфігурація api_source"]

    try:
        doc = json.loads(raw_json)
    except (json.JSONDecodeError, TypeError) as exc:
        return [], [], [f"Невалідний JSON у відповіді API: {exc}"]

    container = _walk_path(doc, api_source.items_path) if api_source.items_path else doc
    if container is None:
        return [], [], []
    items = container if isinstance(container, list) else [container]

    now = utcnow()
    col_map = spec.column_map
    min_fields = 1 if len(spec.source_columns()) <= 1 else 2
    records: list[ExtractedRecord] = []
    quarantined: list[dict[str, Any]] = []

    for item in items:
        if not isinstance(item, dict):
            continue
        payload: dict[str, Any] = {}
        raw_payload: dict[str, Any] = {}
        provenance: dict[str, Provenance] = {}
        coercion_errors: list[str] = []

        for col_key, json_path in api_source.field_map.items():
            col = col_map.get(col_key)
            if col is None or col.is_ai:
                continue
            raw_val = _walk_path(item, json_path)
            if raw_val not in (None, "", []):
                raw_payload[col_key] = raw_val
            coerced = coerce_value(raw_val, col.dtype)
            if coerced is None or coerced == "" or coerced == []:
                if raw_val not in (None, "", []):
                    coercion_errors.append(f"{col.title} ({col.key}: очікувався тип {col.dtype})")
                continue
            payload[col_key] = coerced
            provenance[col_key] = Provenance(
                method="api_json",
                confidence=0.98,
                source_url=source_url,
                observed_at=now,
                verified=True,
            )

        raw_id = _walk_path(item, api_source.id_path)
        item_id = str(raw_id).strip() if raw_id not in (None, "") else ""

        if (
            api_source.record_url_template
            and item_id
            and "url" in col_map
            and "url" not in payload
        ):
            rendered_url = _render_template(api_source.record_url_template, item_id)
            payload["url"] = rendered_url
            raw_payload.setdefault("url", rendered_url)
            provenance["url"] = Provenance(
                method="api_json",
                confidence=0.98,
                source_url=source_url,
                observed_at=now,
                verified=True,
            )

        if not payload and not raw_payload:
            continue

        if strict_quarantine:
            if coercion_errors:
                quarantined.append(
                    {
                        "payload": raw_payload or payload,
                        "reason": "Невалідне значення типу: " + "; ".join(coercion_errors),
                    }
                )
                continue
            if len(payload) < min_fields:
                quarantined.append(
                    {
                        "payload": raw_payload or payload,
                        "reason": f"Менше {min_fields} заповнених полів у записі API ({len(payload)})",
                    }
                )
                continue

        if not payload:
            continue

        record_key = f"api:{item_id}" if item_id else record_key_for(payload, fallback=source_url)
        records.append(
            ExtractedRecord(
                record_key=record_key,
                payload=payload,
                provenance=provenance,
                source_url=source_url,
                content_hash=hash_text(json.dumps(payload, sort_keys=True, default=str)),
            )
        )

    return records, quarantined, []
