"""Duplicate-candidate detection and deterministic entity resolution (P1-02)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from rapidfuzz import fuzz

from ..models import DuplicateCandidate, ExtractedRecord, MergePolicy, Provenance
from ..utils import normalize_company_name, normalize_domain, normalize_phone, utcnow

VALID_MERGE_POLICIES: tuple[MergePolicy, ...] = (
    "confidence",
    "freshness",
    "completeness",
    "prefer_left",
    "prefer_right",
)


def _is_empty(value: Any) -> bool:
    return value in (None, "", [])


def _extract_row_parts(
    row: dict[str, Any] | ExtractedRecord,
) -> tuple[str, dict[str, Any], dict[str, Provenance], str | None, datetime]:
    if isinstance(row, ExtractedRecord):
        return (
            row.record_key,
            dict(row.payload),
            dict(row.provenance),
            row.source_url,
            utcnow(),
        )
    key = str(row.get("record_key") or "")
    payload = dict(row.get("payload") if isinstance(row.get("payload"), dict) else row)
    raw_prov = row.get("provenance") if isinstance(row.get("provenance"), dict) else {}
    provenance: dict[str, Provenance] = {}
    for field, val in raw_prov.items():
        if isinstance(val, Provenance):
            provenance[field] = val
        elif isinstance(val, dict):
            try:
                provenance[field] = Provenance.model_validate(val)
            except Exception:  # noqa: BLE001
                continue
    source_url = row.get("source_url")
    obs = row.get("observed_at")
    row_ts = _as_utc(obs) or utcnow()
    return key, payload, provenance, str(source_url) if source_url else None, row_ts


def _as_utc(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return None
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _blocking_keys(name_norm: str, domain: str, phone: str) -> list[str]:
    keys: list[str] = []
    if domain:
        keys.append(f"d:{domain}")
    if phone:
        keys.append(f"p:{phone}")
    if name_norm:
        keys.append(f"n:{name_norm[:4]}")
    return keys


def _row_signature(
    row: dict[str, Any],
) -> tuple[str, str, str, str, str, float, dict[str, Any]] | None:
    payload = row.get("payload") if isinstance(row.get("payload"), dict) else row
    key = str(row.get("record_key") or payload.get("_key") or "")
    name = str(payload.get("name") or payload.get("company") or payload.get("title") or "")
    if not key or not name:
        return None
    normalized = normalize_company_name(name)
    domain = normalize_domain(str(payload.get("website") or ""))
    phone = normalize_phone(str(payload.get("phone") or ""))
    qual = identity_quality(payload)
    return key, name, normalized, domain, phone, qual, dict(payload)


def _pair_score(left_norm: str, left_domain: str, left_phone: str, right_norm: str, right_domain: str, right_phone: str) -> float:
    score = float(fuzz.token_set_ratio(left_norm, right_norm))
    if left_domain and right_domain and left_domain == right_domain:
        score = max(score, 96.0)
    if left_phone and right_phone and left_phone == right_phone:
        score = max(score, 93.0)
    return score


def find_duplicates(
    rows: list[dict],
    *,
    threshold: float = 88.0,
    max_pairs: int = 200,
    excluded_pairs: set[tuple[str, str]] | None = None,
) -> list[DuplicateCandidate]:
    """Find pairwise duplicate candidates via multi-key blocking and fuzzy matching."""
    excluded = excluded_pairs or set()
    blocks: dict[str, list[tuple[str, str, str, str, str, float, dict[str, Any]]]] = {}
    for row in rows:
        sig = _row_signature(row)
        if sig is None:
            continue
        for bkey in _blocking_keys(sig[2], sig[3], sig[4]):
            blocks.setdefault(bkey, []).append(sig)

    candidates: list[DuplicateCandidate] = []
    seen: set[tuple[str, str]] = set()
    for entries in blocks.values():
        for index, left in enumerate(entries):
            for right in entries[index + 1 :]:
                if left[0] == right[0]:
                    continue
                pair = tuple(sorted((left[0], right[0])))
                if pair in seen or pair in excluded:
                    continue
                seen.add(pair)
                score = _pair_score(left[2], left[3], left[4], right[2], right[3], right[4])
                if score >= threshold:
                    candidates.append(
                        DuplicateCandidate(
                            left_key=left[0],
                            right_key=right[0],
                            left_name=left[1],
                            right_name=right[1],
                            score=round(score / 100, 3),
                            left_quality=left[5],
                            right_quality=right[5],
                            left_payload=left[6],
                            right_payload=right[6],
                        )
                    )
                if len(candidates) >= max_pairs * 2:
                    candidates.sort(key=lambda item: item.score, reverse=True)
                    return candidates[:max_pairs]
    candidates.sort(key=lambda item: item.score, reverse=True)
    return candidates[:max_pairs]


def find_incremental_duplicates(
    new_rows: list[dict],
    existing_rows: list[dict],
    *,
    threshold: float = 88.0,
    max_pairs: int = 200,
    excluded_pairs: set[tuple[str, str]] | None = None,
) -> list[DuplicateCandidate]:
    """Compare incoming rows only against existing canonical records (and intra-batch) without O(N^2) rescan."""
    excluded = excluded_pairs or set()
    existing_blocks: dict[str, list[tuple[str, str, str, str, str, float, dict[str, Any]]]] = {}
    for row in existing_rows:
        sig = _row_signature(row)
        if sig is None:
            continue
        for bkey in _blocking_keys(sig[2], sig[3], sig[4]):
            existing_blocks.setdefault(bkey, []).append(sig)

    new_sigs = [sig for row in new_rows if (sig := _row_signature(row)) is not None]
    new_blocks: dict[str, list[tuple[str, str, str, str, str, float, dict[str, Any]]]] = {}
    for sig in new_sigs:
        for bkey in _blocking_keys(sig[2], sig[3], sig[4]):
            new_blocks.setdefault(bkey, []).append(sig)

    candidates: list[DuplicateCandidate] = []
    seen: set[tuple[str, str]] = set()

    def _maybe_add(
        left: tuple[str, str, str, str, str, float, dict[str, Any]],
        right: tuple[str, str, str, str, str, float, dict[str, Any]],
    ) -> None:
        if left[0] == right[0]:
            return
        pair = tuple(sorted((left[0], right[0])))
        if pair in seen or pair in excluded:
            return
        seen.add(pair)
        score = _pair_score(left[2], left[3], left[4], right[2], right[3], right[4])
        if score >= threshold:
            candidates.append(
                DuplicateCandidate(
                    left_key=left[0],
                    right_key=right[0],
                    left_name=left[1],
                    right_name=right[1],
                    score=round(score / 100, 3),
                    left_quality=left[5],
                    right_quality=right[5],
                    left_payload=left[6],
                    right_payload=right[6],
                )
            )

    for left in new_sigs:
        bkeys = _blocking_keys(left[2], left[3], left[4])
        for bkey in bkeys:
            for existing in existing_blocks.get(bkey, ()):
                _maybe_add(left, existing)
            for peer in new_blocks.get(bkey, ()):
                _maybe_add(left, peer)

    candidates.sort(key=lambda item: item.score, reverse=True)
    return candidates[:max_pairs]


def merge_records(
    left: dict[str, Any] | ExtractedRecord,
    right: dict[str, Any] | ExtractedRecord,
    *,
    policy: MergePolicy = "confidence",
    field_overrides: dict[str, str] | None = None,
) -> tuple[dict[str, Any], dict[str, Provenance]]:
    """Deterministically merge two records under a conflict-resolution policy."""
    if policy not in VALID_MERGE_POLICIES:
        raise ValueError(
            f"Невідома політика злиття: {policy}. "
            f"Доступні: {', '.join(VALID_MERGE_POLICIES)}"
        )

    left_key, left_payload, left_prov, left_src, left_row_ts = _extract_row_parts(left)
    right_key, right_payload, right_prov, right_src, right_row_ts = _extract_row_parts(right)
    overrides = field_overrides or {}
    now = utcnow()

    left_filled = sum(1 for v in left_payload.values() if not _is_empty(v))
    right_filled = sum(1 for v in right_payload.values() if not _is_empty(v))

    all_keys = list(dict.fromkeys([*left_payload.keys(), *right_payload.keys()]))
    merged_payload: dict[str, Any] = {}
    merged_prov: dict[str, Provenance] = {}

    for field in all_keys:
        l_val = left_payload.get(field)
        r_val = right_payload.get(field)
        l_empty = _is_empty(l_val)
        r_empty = _is_empty(r_val)
        if l_empty and r_empty:
            continue

        lp = left_prov.get(field)
        rp = right_prov.get(field)
        l_conf = lp.confidence if lp else 0.5
        r_conf = rp.confidence if rp else 0.5
        l_ts = (_as_utc(lp.observed_at) if lp else None) or left_row_ts
        r_ts = (_as_utc(rp.observed_at) if rp else None) or right_row_ts

        # Explicit manual override
        override = overrides.get(field)
        if override == "left" and not l_empty:
            chosen_val = l_val
            chosen_prov = lp
            chosen_src = (lp.source_url if lp else None) or left_src
            mark_merge = True
        elif (override == "right" and not r_empty) or l_empty:
            chosen_val = r_val
            chosen_prov = rp
            chosen_src = (rp.source_url if rp else None) or right_src
            mark_merge = True
        elif r_empty:
            chosen_val = l_val
            chosen_prov = lp
            chosen_src = (lp.source_url if lp else None) or left_src
            mark_merge = False
        elif isinstance(l_val, list) and isinstance(r_val, list):
            ordered = (
                list(dict.fromkeys([*r_val, *l_val]))
                if policy == "prefer_right"
                else list(dict.fromkeys([*l_val, *r_val]))
            )
            chosen_val = ordered
            win_right = policy == "prefer_right" or ordered != l_val
            chosen_prov = rp if (policy == "prefer_right" or r_conf > l_conf) else lp
            chosen_src = (chosen_prov.source_url if chosen_prov else None) or (
                right_src if win_right else left_src
            )
            mark_merge = ordered != l_val
        elif l_val == r_val:
            chosen_val = l_val
            chosen_prov = lp
            chosen_src = (lp.source_url if lp else None) or left_src
            mark_merge = False
        else:
            # Scalar conflict
            if policy == "prefer_left":
                win_left = True
            elif policy == "prefer_right":
                win_left = False
            elif policy == "confidence":
                win_left = (l_conf, l_ts, len(str(l_val)), 1) >= (
                    r_conf,
                    r_ts,
                    len(str(r_val)),
                    0,
                )
            elif policy == "freshness":
                win_left = (l_ts, l_conf, len(str(l_val)), 1) >= (
                    r_ts,
                    r_conf,
                    len(str(r_val)),
                    0,
                )
            else:  # completeness
                win_left = (left_filled, l_conf, l_ts, 1) >= (
                    right_filled,
                    r_conf,
                    r_ts,
                    0,
                )
            chosen_val = l_val if win_left else r_val
            chosen_prov = lp if win_left else rp
            chosen_src = (chosen_prov.source_url if chosen_prov else None) or (
                left_src if win_left else right_src
            )
            mark_merge = True

        merged_payload[field] = chosen_val
        if mark_merge:
            conf = chosen_prov.confidence if chosen_prov else 0.8
            merged_prov[field] = Provenance(
                method="merge",
                confidence=conf,
                source_url=chosen_src,
                observed_at=now,
                verified=True,
                note=f"Злито {left_key} + {right_key} ({policy})",
            )
        elif chosen_prov is not None:
            merged_prov[field] = chosen_prov
        else:
            merged_prov[field] = Provenance(
                method="manual",
                confidence=0.8,
                source_url=chosen_src,
                observed_at=now,
                verified=True,
            )

    return merged_payload, merged_prov


def identity_quality(payload: dict) -> float:
    """How well a record can be deduplicated later (domain > phone > name)."""
    if normalize_domain(str(payload.get("website") or "")):
        return 1.0
    if normalize_phone(str(payload.get("phone") or "")):
        return 0.8
    if normalize_company_name(str(payload.get("name") or "")):
        return 0.5
    return 0.2
