"""Calibration: sample pages -> deterministic schema -> preview rows -> approval."""

from __future__ import annotations

import asyncio
import logging
from collections import Counter

from ..config import Settings
from ..db.sqlite_store import OperationalStore
from ..extraction.engine import ExtractionEngine
from ..extraction.learner import learn_schema, schema_signature
from ..llm.base import LLMBudget
from ..models import CalibrationReport, CalibrationSample, CssSchema, PageContent, TaskSpec
from ..scraping.fetcher import Fetcher
from ..scraping.page import extract_links
from ..utils import normalize_domain, unique

logger = logging.getLogger(__name__)


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
            self._save(task_id, run_id, report)
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

        if not pages:
            report = CalibrationReport(
                warnings=["Не вдалося завантажити жодної сторінки. Перевірте URL, robots.txt або проксі."],
                pages_tried=len(seeds),
            )
            self._save(task_id, run_id, report)
            return report

        samples = [(page.final_url or page.url, page.html) for page in pages]
        if css_schema is None or not css_schema.fields:
            learned = learn_schema(samples, spec)
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
        self._save(task_id, run_id, report)
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
        if not schema.fields or schema.score <= 0:
            return
        for seed in spec.seeds[:1]:
            domain = normalize_domain(seed)
            if not domain:
                continue
            self.store.save_selector(domain, f"auto:{spec.entity}", schema)
            logger.info("saved selector for %s (%s)", domain, schema_signature(schema))

    def _save(self, task_id: str, run_id: str, report: CalibrationReport) -> None:
        self.store.save_calibration(task_id, run_id, report.model_dump_json(by_alias=True))

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

    def _emit(
        self, task_id: str, run_id: str, event_type: str, message: str,
        level: str = "info", data: dict | None = None,
    ) -> None:
        self.store.add_event(task_id, event_type, message, run_id=run_id, level=level, data=data or {})
