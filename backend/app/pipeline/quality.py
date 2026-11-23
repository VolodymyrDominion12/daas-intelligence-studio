"""Post-run data quality guards and offline gold-set evaluation harness (P1-01)."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from ..db.sqlite_store import OperationalStore
from ..extraction.api_json import extract_json_records
from ..extraction.engine import ExtractionEngine
from ..models import (
    CssField,
    CssSchema,
    EvalFieldMetrics,
    EvalReport,
    FieldSpec,
    PageContent,
    QualityCheckIssue,
    QualityChecksReport,
    TaskSpec,
)
from ..scraping.page import html_to_markdown, html_to_text
from ..utils import coerce_value, hash_text, normalize_domain, normalize_space

_SAFE_PRESET_RE = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")


class GoldCase(BaseModel):
    """Single labeled offline evaluation case (HTML page or JSON API response)."""

    url: str
    content: str = ""
    fixture: str | None = None
    content_type: Literal["auto", "html", "json"] = "auto"
    expected: list[dict[str, Any]] = Field(default_factory=list)


class GoldDataset(BaseModel):
    """Collection of offline gold cases loaded from JSON or built inline."""

    name: str = "inline"
    cases: list[GoldCase] = Field(default_factory=list)


def validate_gold_preset_name(name: str) -> str:
    cleaned = name.strip()
    if not _SAFE_PRESET_RE.match(cleaned):
        raise ValueError("Некоректна назва gold-набору: дозволені лише літери, цифри, '_' та '-'")
    return cleaned


def resolve_gold_preset_path(preset_name: str, search_dirs: list[Path]) -> Path:
    safe_name = validate_gold_preset_name(preset_name)
    for base in search_dirs:
        if not base.exists():
            continue
        candidate = (base / f"{safe_name}.json").resolve()
        if candidate.parent == base.resolve() and candidate.is_file():
            return candidate
    raise ValueError(f"Gold-набір '{safe_name}' не знайдено")


def load_gold_set(path: Path) -> GoldDataset:
    """Load an offline gold-set JSON file, resolving relative fixture files safely."""
    resolved = path.resolve()
    if not resolved.is_file():
        raise ValueError(f"Файл gold-набору не знайдено: {path}")
    raw = json.loads(resolved.read_text(encoding="utf-8"))
    dataset = GoldDataset.model_validate(raw)
    if dataset.name == "inline":
        dataset.name = resolved.stem

    base_dir = resolved.parent
    fixtures_root = base_dir.parent if base_dir.name == "gold" else base_dir
    for case in dataset.cases:
        if case.content:
            continue
        if not case.fixture:
            raise ValueError(f"У кейсі {case.url} відсутні 'content' і 'fixture'")
        rel = Path(case.fixture)
        if rel.is_absolute():
            raise ValueError(f"Абсолютний шлях fixture заборонений: {case.fixture}")
        candidate = (base_dir / rel).resolve()
        try:
            candidate.relative_to(fixtures_root.resolve())
        except ValueError as exc:
            raise ValueError(f"Небезпечний шлях fixture поза каталогом фікстур: {case.fixture}") from exc
        if not candidate.is_file():
            raise ValueError(f"Файл фікстури не знайдено: {candidate}")
        case.content = candidate.read_text(encoding="utf-8")
    return dataset


def evaluate_dataset_quality(
    *,
    row_count: int,
    previous_row_count: int | None,
    fill_rates: dict[str, float],
    columns: list[FieldSpec],
    observed_count: int | None = None,
    full_crawl: bool = False,
    max_drop_ratio: float = 0.5,
    min_required_fill_pct: float = 80.0,
) -> QualityChecksReport:
    """Evaluate deterministic post-run invariants against current dataset version."""
    issues: list[QualityCheckIssue] = []
    drop_ratio: float | None = None
    effective_count = (
        min(row_count, observed_count)
        if full_crawl and observed_count is not None
        else row_count
    )

    if previous_row_count is not None and previous_row_count > 0:
        drop_ratio = max(0.0, (previous_row_count - effective_count) / previous_row_count)
        if drop_ratio > max_drop_ratio:
            issues.append(
                QualityCheckIssue(
                    code="row_count_drop",
                    severity="warning",
                    field=None,
                    message=(
                        f"Кількість записів впала на {drop_ratio:.0%} "
                        f"({previous_row_count} → {effective_count})"
                    ),
                    metric=round(drop_ratio, 4),
                    threshold=max_drop_ratio,
                )
            )

    if row_count > 0:
        for column in columns:
            if not column.required or column.is_ai:
                continue
            rate = float(fill_rates.get(column.key, 0.0))
            if rate < min_required_fill_pct:
                issues.append(
                    QualityCheckIssue(
                        code="required_field_empty",
                        severity="warning",
                        field=column.key,
                        message=(
                            f"Обов'язкова колонка «{column.title}» ({column.key}) "
                            f"заповнена на {rate:.1f}% (< {min_required_fill_pct:.1f}%)"
                        ),
                        metric=round(rate, 2),
                        threshold=min_required_fill_pct,
                    )
                )

    return QualityChecksReport(
        passed=not issues,
        row_count=row_count,
        observed_count=observed_count,
        previous_row_count=previous_row_count,
        drop_ratio=round(drop_ratio, 4) if drop_ratio is not None else None,
        issues=issues,
    )


def _explicit_css_schema(spec: TaskSpec) -> CssSchema | None:
    fields = [
        CssField(
            key=column.key,
            selector=column.selector,
            attr=column.attr,
            dtype=column.dtype,
            confidence=0.9,
            aliases=column.aliases,
        )
        for column in spec.columns
        if column.selector and not column.is_ai
    ]
    if not fields:
        return None
    return CssSchema(
        container="body",
        item_scope=False,
        fields=fields,
        method="css",
        notes=["from explicit column selectors"],
    )


def _normalize_eval_value(value: Any, dtype: str) -> Any:
    if value in (None, "", []):
        return None
    if dtype in {"number", "integer"}:
        coerced = coerce_value(value, dtype)
        if coerced is None:
            return normalize_space(str(value)).casefold()
        return round(float(coerced), 6) if dtype == "number" else int(coerced)
    if dtype == "boolean":
        coerced = coerce_value(value, "boolean")
        return coerced if coerced is not None else normalize_space(str(value)).casefold()
    if dtype == "url":
        text = str(value).strip().rstrip("/")
        return text.casefold() if text else None
    if isinstance(value, (list, dict)):
        return json.dumps(value, sort_keys=True, ensure_ascii=False)
    text = normalize_space(str(value)).casefold()
    return text or None


def _match_records(
    expected: list[dict[str, Any]],
    extracted: list[dict[str, Any]],
    eval_keys: list[str],
    col_map: dict[str, FieldSpec],
) -> tuple[list[tuple[dict[str, Any], dict[str, Any]]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Greedily pair expected gold records with extracted payloads by field overlap."""
    remaining_ext = list(range(len(extracted)))
    pairs: list[tuple[dict[str, Any], dict[str, Any]]] = []
    unmatched_exp: list[dict[str, Any]] = []

    for exp in expected:
        best_idx: int | None = None
        best_score = -1
        for idx in remaining_ext:
            cand = extracted[idx]
            score = 0
            for key in eval_keys:
                dtype = col_map[key].dtype if key in col_map else "string"
                g_val = _normalize_eval_value(exp.get(key), dtype)
                p_val = _normalize_eval_value(cand.get(key), dtype)
                if g_val is not None and p_val is not None and g_val == p_val:
                    score += 2 if key in {"tender_id", "id", "name", "title", "website"} else 1
            if score > best_score:
                best_score = score
                best_idx = idx
        if best_idx is not None and best_score > 0:
            remaining_ext.remove(best_idx)
            pairs.append((exp, extracted[best_idx]))
        else:
            # Fallback positional match if extraction partially changed values but emitted records
            unmatched_exp.append(exp)

    still_unmatched_exp: list[dict[str, Any]] = []
    for exp in unmatched_exp:
        if remaining_ext:
            idx = remaining_ext.pop(0)
            pairs.append((exp, extracted[idx]))
        else:
            still_unmatched_exp.append(exp)

    extra_ext = [extracted[idx] for idx in remaining_ext]
    return pairs, still_unmatched_exp, extra_ext


async def run_gold_eval(
    spec: TaskSpec,
    cases: list[GoldCase],
    *,
    engine: ExtractionEngine,
    store: OperationalStore | None = None,
    min_f1: float = 0.8,
    gold_name: str = "inline",
) -> EvalReport:
    """Run deterministic offline extraction over ``cases`` and score against gold records."""
    if not cases:
        raise ValueError("Gold-набір порожній")

    eval_keys = sorted({key for case in cases for exp in case.expected for key in exp})
    col_map = spec.column_map
    counts: dict[str, dict[str, int]] = {
        key: {"tp": 0, "fp": 0, "fn": 0, "present": 0, "expected": 0}
        for key in eval_keys
    }

    total_expected = 0
    total_extracted = 0
    llm_cases = 0
    total_llm_usd = 0.0

    for case in cases:
        total_expected += len(case.expected)
        is_json = case.content_type == "json" or (
            case.content_type == "auto"
            and (spec.extraction == "api_json" or spec.api_source is not None)
            and case.content.lstrip().startswith(("{", "["))
        )

        if is_json:
            records, _warnings = extract_json_records(case.content, spec, case.url)
            extracted_payloads = [rec.payload for rec in records]
        else:
            domain = normalize_domain(case.url)
            css_schema = store.get_selector(domain, f"auto:{spec.entity}") if store is not None else None
            if css_schema is None:
                css_schema = _explicit_css_schema(spec)
            page = PageContent(
                url=case.url,
                final_url=case.url,
                status_code=200,
                html=case.content,
                text=html_to_text(case.content),
                markdown=html_to_markdown(case.content, base_url=case.url),
                content_hash=hash_text(case.content),
                engine="gold-fixture",
            )
            outcome = await engine.extract(page, spec, css_schema=css_schema, allow_llm=False)
            if outcome.llm_calls > 0 or outcome.strategy == "llm":
                llm_cases += 1
            total_llm_usd += outcome.llm_cost_usd
            records = engine.to_records(outcome, page, spec)
            extracted_payloads = [rec.payload for rec in records]

        total_extracted += len(extracted_payloads)
        pairs, missed_exp, extra_ext = _match_records(
            case.expected, extracted_payloads, eval_keys, col_map
        )

        for exp, pred in pairs:
            for key in eval_keys:
                dtype = col_map[key].dtype if key in col_map else "string"
                g_val = _normalize_eval_value(exp.get(key), dtype)
                p_val = _normalize_eval_value(pred.get(key), dtype)
                if g_val is not None:
                    counts[key]["expected"] += 1
                if p_val is not None:
                    counts[key]["present"] += 1
                if g_val is None and p_val is None:
                    continue
                if g_val is not None and p_val is None:
                    counts[key]["fn"] += 1
                elif g_val is None and p_val is not None:
                    counts[key]["fp"] += 1
                elif g_val == p_val:
                    counts[key]["tp"] += 1
                else:
                    counts[key]["fp"] += 1
                    counts[key]["fn"] += 1

        for exp in missed_exp:
            for key in eval_keys:
                dtype = col_map[key].dtype if key in col_map else "string"
                if _normalize_eval_value(exp.get(key), dtype) is not None:
                    counts[key]["expected"] += 1
                    counts[key]["fn"] += 1

        for pred in extra_ext:
            for key in eval_keys:
                dtype = col_map[key].dtype if key in col_map else "string"
                if _normalize_eval_value(pred.get(key), dtype) is not None:
                    counts[key]["present"] += 1
                    counts[key]["fp"] += 1

    field_metrics: dict[str, EvalFieldMetrics] = {}
    sum_tp = 0
    sum_fp = 0
    sum_fn = 0
    sum_present = 0
    sum_expected = 0

    for key, c in counts.items():
        tp, fp, fn = c["tp"], c["fp"], c["fn"]
        sum_tp += tp
        sum_fp += fp
        sum_fn += fn
        sum_present += c["present"]
        sum_expected += c["expected"]

        precision = tp / (tp + fp) if (tp + fp) > 0 else 0.0
        recall = tp / (tp + fn) if (tp + fn) > 0 else 0.0
        f1 = (2 * precision * recall / (precision + recall)) if (precision + recall) > 0 else 0.0
        coverage = (c["present"] / c["expected"] * 100.0) if c["expected"] > 0 else 0.0
        field_metrics[key] = EvalFieldMetrics(
            precision=round(precision, 4),
            recall=round(recall, 4),
            f1=round(f1, 4),
            coverage=round(min(100.0, coverage), 2),
            tp=tp,
            fp=fp,
            fn=fn,
        )

    total_p = sum_tp / (sum_tp + sum_fp) if (sum_tp + sum_fp) > 0 else 0.0
    total_r = sum_tp / (sum_tp + sum_fn) if (sum_tp + sum_fn) > 0 else 0.0
    total_f1 = (2 * total_p * total_r / (total_p + total_r)) if (total_p + total_r) > 0 else 0.0
    total_cov = (sum_present / sum_expected * 100.0) if sum_expected > 0 else 0.0
    llm_pct = (llm_cases / len(cases) * 100.0) if cases else 0.0
    usd_per_1k = (total_llm_usd / max(1, total_extracted)) * 1000.0

    return EvalReport(
        gold_name=gold_name,
        passed=round(total_f1, 4) >= min_f1,
        min_f1=min_f1,
        precision=round(total_p, 4),
        recall=round(total_r, 4),
        f1=round(total_f1, 4),
        coverage=round(min(100.0, total_cov), 2),
        cases=len(cases),
        records_expected=total_expected,
        records_extracted=total_extracted,
        llm_fallback_pct=round(llm_pct, 2),
        usd_per_1k_records=round(usd_per_1k, 4),
        fields=field_metrics,
    )
