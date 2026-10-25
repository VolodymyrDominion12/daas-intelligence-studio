"""Robust JSON parsing for model output (no third-party repair dependency)."""

from __future__ import annotations

import json
import re
from typing import Any

_FENCE = re.compile(r"```(?:json|JSON)?\s*(.*?)```", re.S)
_TRAILING_COMMA = re.compile(r",\s*([}\]])")


def parse_json_loose(text: str) -> Any:
    """Try hard to recover a JSON value from a chatty model response."""
    if not text:
        raise ValueError("empty response")
    candidates: list[str] = []
    stripped = text.strip()
    candidates.append(stripped)

    for match in _FENCE.finditer(text):
        candidates.append(match.group(1).strip())

    for opener, closer in (("{", "}"), ("[", "]")):
        start = text.find(opener)
        end = text.rfind(closer)
        if start != -1 and end > start:
            candidates.append(text[start : end + 1])

    for candidate in candidates:
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            pass
        repaired = _TRAILING_COMMA.sub(r"\1", candidate)
        repaired = repaired.replace("\u201c", '"').replace("\u201d", '"').replace("\u2019", "'")
        try:
            return json.loads(repaired)
        except json.JSONDecodeError:
            continue

    # Last resort: cut the candidate down at the last closing brace.
    start = stripped.find("{")
    if start != -1:
        for end in range(len(stripped), start, -1):
            chunk = _TRAILING_COMMA.sub(r"\1", stripped[start:end])
            try:
                return json.loads(chunk)
            except json.JSONDecodeError:
                continue
    raise ValueError("could not parse JSON from model response")
