"""Calibration: sample pages -> deterministic schema -> preview rows -> approval."""

from __future__ import annotations

import asyncio
import logging
from collections import Counter

from ..config import Settings
from ..db.sqlite_store import OperationalStore
from ..extraction.engine import ExtractionEngine
from ..extraction.learner import apply_manual_selectors, learn_schema, schema_signature
from ..llm.base import LLMBudget
from ..models import (
    CalibrationHistoryEntry,
    CalibrationReport,
    CalibrationSample,
    CssField,
    CssSchema,
    PageContent,
    SchemaDiffReport,
    SchemaFieldDiff,
    TaskSpec,
)
from ..scraping.fetcher import Fetcher
from ..scraping.page import extract_links
from ..utils import normalize_domain, unique

logger = logging.getLogger(__name__)


def build_calibration_history_entry(entry: dict) -> CalibrationHistoryEntry:
    report = CalibrationReport.model_validate_json(entry["report_json"])
    schema = report.schema_
    from datetime import datetime

    return CalibrationHistoryEntry(
        version=int(entry["version"]),
        run_id=entry.get("run_id"),
        source=str(entry.get("source") or "calibrate"),
        strategy=report.strategy,
        container=schema.container if schema and schema.container else None,
        item_scope=schema.item_scope if schema else None,
        score=float(schema.score if schema else 0.0),
        records_found=int(report.records_found),
        pages_ok=int(report.pages_ok),
        pages_tried=int(report.pages_tried),
        fields_count=len(schema.fields) if schema else 0,
        created_at=datetime.fromisoformat(entry["created_at"]),
    )


def compute_schema_diff(
    previous: CalibrationReport | None,
    current: CalibrationReport | None,
    *,
    current_version: int,
    previous_version: int | None = None,
) -> SchemaDiffReport | None:
    if current is None:
        return None
    if previous is None or previous_version is None:
        return SchemaDiffReport(
            current_version=current_version,
            previous_version=None,
            has_previous=False,
            has_changes=False,
            strategy_after=current.strategy,
            container_after=current.schema_.container if current.schema_ else None,
            item_scope_after=current.schema_.item_scope if current.schema_ else None,
            score_after=current.schema_.score if current.schema_ else None,
            records_after=current.records_found,
        )

    prev_schema = previous.schema_
    curr_schema = current.schema_
    prev_container = prev_schema.container if prev_schema and prev_schema.container else None
    curr_container = curr_schema.container if curr_schema and curr_schema.container else None
    container_changed = prev_container != curr_container

    prev_scope = prev_schema.item_scope if prev_schema else None
    curr_scope = curr_schema.item_scope if curr_schema else None
    score_before = prev_schema.score if prev_schema else None
    score_after = curr_schema.score if curr_schema else None
    score_delta = (
        round(score_after - score_before, 2)
        if score_before is not None and score_after is not None
        else None
    )

    prev_fields: dict[str, CssField] = {f.key: f for f in (prev_schema.fields if prev_schema else [])}
    curr_fields: dict[str, CssField] = {f.key: f for f in (curr_schema.fields if curr_schema else [])}

    fields_added = [f for key, f in curr_fields.items() if key not in prev_fields]
    fields_removed = [f for key, f in prev_fields.items() if key not in curr_fields]
    fields_changed: list[SchemaFieldDiff] = []

    all_keys = list(dict.fromkeys([*prev_fields.keys(), *curr_fields.keys()]))
    coverage_deltas: dict[str, float] = {}
    for key in sorted(set(previous.coverage) | set(current.coverage)):
        before_cov = previous.coverage.get(key)
        after_cov = current.coverage.get(key)
        if before_cov is not None and after_cov is not None:
            delta = round(after_cov - before_cov, 1)
            if delta != 0:
                coverage_deltas[key] = delta
        elif after_cov is not None:
            coverage_deltas[key] = round(after_cov, 1)
        elif before_cov is not None:
            coverage_deltas[key] = round(-before_cov, 1)

    for key in all_keys:
        if key not in prev_fields or key not in curr_fields:
            continue
        pf = prev_fields[key]
        cf = curr_fields[key]
        cov_before = previous.coverage.get(key)
        cov_after = current.coverage.get(key)
        if (
            pf.selector != cf.selector
            or pf.attr != cf.attr
            or pf.dtype != cf.dtype
            or (cov_before != cov_after and (cov_before is not None or cov_after is not None))
        ):
            fields_changed.append(
                SchemaFieldDiff(
                    key=key,
                    selector_before=pf.selector,
                    selector_after=cf.selector,
                    attr_before=pf.attr,
                    attr_after=cf.attr,
                    dtype_before=pf.dtype,
                    dtype_after=cf.dtype,
                    confidence_before=pf.confidence,
                    confidence_after=cf.confidence,
                    coverage_before=cov_before,
                    coverage_after=cov_after,
                )
            )

    has_changes = any(
        [
            previous.strategy != current.strategy,
            container_changed,
            prev_scope != curr_scope,
            bool(fields_added),
            bool(fields_removed),
            bool(fields_changed),
            bool(coverage_deltas),
        ]
    )
    return SchemaDiffReport(
        current_version=current_version,
        previous_version=previous_version,
        has_previous=True,
        has_changes=has_changes,
        strategy_before=previous.strategy,
        strategy_after=current.strategy,
        container_before=prev_container,
        container_after=curr_container,
        container_changed=container_changed,
        item_scope_before=prev_scope,
        item_scope_after=curr_scope,
        score_before=score_before,
        score_after=score_after,
        score_delta=score_delta,
        records_before=previous.records_found,
        records_after=current.records_found,
        records_delta=current.records_found - previous.records_found,
        fields_added=fields_added,
        fields_removed=fields_removed,
        fields_changed=fields_changed,
        coverage_deltas=coverage_deltas,
    )


class Calibrator:
    def __init__(
        self,
        settings: Settings,
        store: OperationalStore,
        fetcher: Fetcher,
        engine: ExtractionEngine,
    ) -> None:
        self.settings = settings
        self.store = store
        self.fetcher = fetcher
        self.engine = engine

    async def run(
        self,
        task_id: str,
        spec: TaskSpec,
        run_id: str,
        *,
        budget: LLMBudget | None = None,
        css_schema: CssSchema | None = None,
    ) -> CalibrationReport:
        budget = budget or LLMBudget(spec.limits.budget_usd)
        warnings: list[str] = []
        pages: list[PageContent] = []
        sample_size = min(spec.limits.sample_size, self.settings.sample_size)

        seeds = spec.seeds[:sample_size]
        if not seeds:
            report = CalibrationReport(warnings=["Немає стартових URL для калібрування."])
            self._save(task_id, run_id, report, spec)
            return report

        self._emit(task_id, run_id, "calibration_started", f"Калібрування на {len(seeds)} URL")
        results = await asyncio.gather(
            *[self.fetcher.fetch(url) for url in seeds], return_exceptions=True
        )
        for url, result in zip(seeds, results, strict=False):
            if isinstance(result, Exception):
                warnings.append(f"{url}: {type(result).__name__}: {result}")
                continue
            if result.page is None:
                level = "warning" if result.kind in {"blocked", "robots"} else "error"
                warnings.append(f"{url}: {result.error} ({result.kind})")
                self._emit(task_id, run_id, "fetch_error", f"{url}: {result.error}", level,
                           {"kind": result.kind})
                continue
            pages.append(result.page)
            self._note_js_render(task_id, run_id, result.page, warnings)
            self._emit(task_id, run_id, "page_fetched", f"OK {url} ({len(result.page.html)} B)")

        extras_used = 0
        # A seed may be a landing page without repeated items: follow a few detail links.
        if len(pages) < sample_size:
            extra_urls: list[str] = []
            for page in pages:
                extra_urls.extend(
                    extract_links(page.html, page.final_url, same_site=True, limit=6)
                )
            extra_urls = [url for url in unique(extra_urls) if url not in seeds][: sample_size - len(pages)]
            if extra_urls:
                extras_used = len(extra_urls)
                self._emit(task_id, run_id, "discovery", f"Додаткові сторінки: {len(extra_urls)}")
                extra_results = await asyncio.gather(
                    *[self.fetcher.fetch(url) for url in extra_urls], return_exceptions=True
                )
                for url, result in zip(extra_urls, extra_results, strict=False):
                    if isinstance(result, Exception) or result.page is None:
                        warnings.append(f"{url}: не вдалося завантажити")
                        continue
                    pages.append(result.page)
                    self._note_js_render(task_id, run_id, result.page, warnings)

        if not pages:
            report = CalibrationReport(
                warnings=["Не вдалося завантажити жодної сторінки. Перевірте URL, robots.txt або проксі."],
                pages_tried=len(seeds),
            )
            self._save(task_id, run_id, report, spec)
            return report

        samples = [(page.final_url or page.url, page.html) for page in pages]
        if css_schema is None or not css_schema.fields:
            learned = learn_schema(samples, spec)
            learned = apply_manual_selectors(learned, spec) or learned
            if learned.fields:
                css_schema = learned
                self._emit(
                    task_id,
                    run_id,
                    "schema_learned",
                    f"CSS-схема: {len(learned.fields)} полів, score={learned.score}",
                    data={"container": learned.container},
                )
            self._save_selector(spec, learned)
        else:
            css_schema = apply_manual_selectors(css_schema, spec) or css_schema
            self._save_selector(spec, css_schema)

        report = CalibrationReport(
            samples=[],
            pages_tried=len(seeds) + extras_used,
            pages_ok=len(pages),
        )
        if css_schema and css_schema.container:
            report.strategy = "css" if css_schema.method != "json_ld" else "json_ld"
            report.schema_ = css_schema

        # Preview via the same engine the full run will use.
        rows: list[dict] = []
        for page in pages[: max(2, min(len(pages), 3))]:
            outcome = await self.engine.extract(
                page, spec, css_schema=css_schema, allow_llm=False, budget=budget
            )
            for item in outcome.items[:5]:
                rows.append(item)
            report.samples.append(
                CalibrationSample(
                    url=page.final_url or page.url,
                    records=outcome.items[:5],
                    error=outcome.warnings[0] if outcome.warnings else None,
                )
            )
            relevant = [
                warning for warning in outcome.warnings if "container not found" not in warning
            ]
            warnings.extend(relevant[:2])

        if not rows and self.engine.router.available:
            self._emit(
                task_id, run_id, "llm_fallback",
                "Детерміновані стратегії не дали результату — пробуємо LLM на одній сторінці",
                "warning",
            )
            page = pages[0]
            outcome = await self.engine.extract(
                page, spec, css_schema=None, allow_llm=True, budget=budget
            )
            if outcome.items:
                report.strategy = "llm"
                report.schema_ = None
                rows = outcome.items[:8]
                report.samples = [CalibrationSample(url=page.url, records=rows)]
                warnings.append(
                    "Записи отримано через LLM: це дорого на великих обсягах. "
                    "Додайте CSS-селектори або перевірте шаблон сторінки."
                )
            report.estimated_llm_cost_usd = outcome.llm_cost_usd

        report.records_found = len(rows)
        report.warnings.extend(unique(warnings)[:20])
        report.coverage = self._coverage(rows, spec)
        if rows and not css_schema:
            report.strategy = report.strategy or "llm"
        if not rows:
            report.warnings.append(
                "Тестова вибірка порожня. Перевірте, що seed-URL — це сторінка зі списком/картками, "
                "а не головна сторінка без даних."
            )
        self._save(task_id, run_id, report, spec)
        self._emit(
            task_id,
            run_id,
            "calibration_finished",
            f"Калібрування: {report.records_found} записів, стратегія={report.strategy}",
            "warning" if not rows else "info",
            {"coverage": report.coverage},
        )
        return report

    # -- helpers ----------------------------------------------------------
    def _save_selector(self, spec: TaskSpec, schema: CssSchema) -> None:
        if not schema.fields or (schema.score <= 0 and not any(c.selector for c in spec.columns if not c.is_ai)):
            return
        for seed in spec.seeds[:1]:
            domain = normalize_domain(seed)
            if not domain:
                continue
            self.store.save_selector(domain, f"auto:{spec.entity}", schema)
            logger.info("saved selector for %s (%s)", domain, schema_signature(schema))

    def _save(
        self,
        task_id: str,
        run_id: str,
        report: CalibrationReport,
        spec: TaskSpec | None = None,
    ) -> None:
        self.store.save_calibration(
            task_id,
            run_id,
            report.model_dump_json(by_alias=True),
            spec_json=spec.model_dump_json() if spec is not None else "{}",
            source="calibrate",
        )

    def _coverage(self, rows: list[dict], spec: TaskSpec) -> dict[str, float]:
        if not rows:
            return {}
        counter: Counter[str] = Counter()
        for row in rows:
            for key, value in row.items():
                if value not in (None, "", []):
                    counter[key] += 1
        return {
            column.key: round(counter.get(column.key, 0) / len(rows) * 100, 1)
            for column in spec.columns
            if not column.is_ai
        }

    def _note_js_render(
        self, task_id: str, run_id: str, page: PageContent, warnings: list[str]
    ) -> None:
        if not page.from_cache and page.headers.get("x-daas-js-rendered") == "1":
            reason = page.headers.get("x-daas-js-reason", "")
            self._emit(
                task_id,
                run_id,
                "js_rendered",
                f"JS-рендер {page.final_url or page.url} ({page.engine})",
                data={
                    "url": page.final_url or page.url,
                    "engine": page.engine,
                    "reason": reason,
                },
            )
        elif not page.from_cache and page.headers.get("x-daas-js-fallback"):
            fb = page.headers["x-daas-js-fallback"]
            self._emit(
                task_id,
                run_id,
                "js_render_fallback",
                f"Fallback на HTTP для {page.final_url or page.url}: {fb}",
                level="warning",
                data={"url": page.final_url or page.url, "reason": fb},
            )
            warnings.append(f"{page.url}: JS-рендер недоступний ({fb}), використано HTTP.")
        elif self.settings.fetch_engine == "http" and page.headers.get("x-daas-js-shell"):
            hint = page.headers["x-daas-js-shell"]
            warnings.append(
                f"{page.url}: виявлено SPA-оболонку ({hint}). "
                "Увімкніть DAAS_FETCH_ENGINE=auto і Playwright-сайдкар (DAAS_PLAYWRIGHT_SIDECAR_URL)."
            )

    def _emit(
        self, task_id: str, run_id: str, event_type: str, message: str,
        level: str = "info", data: dict | None = None,
    ) -> None:
        self.store.add_event(task_id, event_type, message, run_id=run_id, level=level, data=data or {})
