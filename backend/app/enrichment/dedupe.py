"""Duplicate-candidate detection (blocking + fuzzy matching).

Full entity resolution with merge/unmerge is roadmap P1; this gives an honest,
reviewable list of likely duplicates instead of silently merging records.
"""

from __future__ import annotations

from rapidfuzz import fuzz

from ..models import DuplicateCandidate
from ..utils import normalize_company_name, normalize_domain, normalize_phone


def find_duplicates(
    rows: list[dict], *, threshold: float = 88.0, max_pairs: int = 200
) -> list[DuplicateCandidate]:
    enriched: list[tuple[str, str, str, str]] = []
    for row in rows:
        payload = row.get("payload") if isinstance(row.get("payload"), dict) else row
        key = row.get("record_key") or payload.get("_key") or ""
        name = str(payload.get("name") or payload.get("company") or payload.get("title") or "")
        if not key or not name:
            continue
        enriched.append((key, name, normalize_company_name(name), normalize_domain(str(payload.get("website") or ""))))

    blocks: dict[str, list[tuple[str, str, str]]] = {}
    for key, name, normalized, domain in enriched:
        blocking = domain or (normalized[:4] if normalized else "")
        if not blocking:
            continue
        blocks.setdefault(blocking[:6], []).append((key, name, normalized))
        if domain:
            # also compare domain-less twins by name prefix
            blocks.setdefault(normalized[:4], []).append((key, name, normalized))

    candidates: list[DuplicateCandidate] = []
    seen: set[tuple[str, str]] = set()
    for entries in blocks.values():
        for index, left in enumerate(entries):
            for right in entries[index + 1 :]:
                if left[0] == right[0]:
                    continue
                pair = tuple(sorted((left[0], right[0])))
                if pair in seen:
                    continue
                seen.add(pair)
                score = fuzz.token_set_ratio(left[2], right[2])
                if score >= threshold:
                    candidates.append(
                        DuplicateCandidate(
                            left_key=left[0],
                            right_key=right[0],
                            left_name=left[1],
                            right_name=right[1],
                            score=round(score / 100, 3),
                        )
                    )
                if len(candidates) >= max_pairs:
                    return candidates
    candidates.sort(key=lambda item: item.score, reverse=True)
    return candidates


def identity_quality(payload: dict) -> float:
    """How well a record can be deduplicated later (domain > phone > name)."""
    if normalize_domain(str(payload.get("website") or "")):
        return 1.0
    if normalize_phone(str(payload.get("phone") or "")):
        return 0.8
    if normalize_company_name(str(payload.get("name") or "")):
        return 0.5
    return 0.2
