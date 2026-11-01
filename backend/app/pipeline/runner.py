"""Pipeline runner: durable queue processing, enrichment, snapshots and change detection."""

from __future__ import annotations

import asyncio
import logging
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from ..config import Settings
from ..db.duckdb_store import AnalyticsStore, dataset_slug, detect_price_fields
from ..db.sqlite_store import OperationalStore
from ..enrichment.ai_column import AiColumns
from ..enrichment.base import EnrichmentContext, merge_payload, provenance_for
from ..extraction.engine import ExtractionEngine
from ..extraction.learner import learn_schema, schema_signature
from ..llm.base import LLMBudget
from ..llm.router import LLMRouter
from ..models import (
    CssField,
    CssSchema,
    ExtractedRecord,
    PageContent,
    Provenance,
    RunStats,
    TaskInfo,
    TaskSpec,
)
from ..scraping.fetcher import Fetcher
from ..scraping.page import extract_links
from ..storage.raw_cache import RawCache
from ..utils import iso, normalize_domain, unique, utcnow
from .calibration import Calibrator

logger = logging.getLogger(__name__)


@dataclass
class RunContext:
    task: TaskInfo
    spec: TaskSpec
    run_id: str
    version: int
    budget: LLMBudget
    stats: RunStats = field(default_factory=RunStats)
    worker: str = ""
    dataset_id: str = ""
    known_keys: set[str] = field(default_factory=set)
    existing: dict[str, dict] = field(default_factory=dict)
    llm_calls: int = 0
    healed_domains: set[str] = field(default_factory=set)
    strategies: Counter = field(default_factory=Counter)
    notes: list[str] = field(default_factory=list)
    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))


class PipelineRunner:
    def __init__(
        self,
        settings: Settings,
        store: OperationalStore,
        analytics: AnalyticsStore,
        cache: RawCache,
        fetcher: Fetcher,
        engine: ExtractionEngine,
        calibrator: Calibrator,
        registry,
        router: LLMRouter,
        search=None,
    ) -> None:
        self.settings = settings
        self.store = store
        self.analytics = analytics
        self.cache = cache
        self.fetcher = fetcher
        self.engine = engine
        self.calibrator = calibrator
        self.registry = registry
        self.router = router
        self.search = search
        self.ai = AiColumns()

    # ------------------------------------------------------------------
    # calibration
    # ------------------------------------------------------------------
    async def calibrate(self, task_id: str, run_id: str) -> None:
        task = self._task(task_id)
        ctx = self._context(task, run_id, version=0)
        self.store.update_run(run_id, status="running", started=True)
        try:
            report = await self.calibrator.run(
                task_id, task.spec, run_id, budget=ctx.budget
            )
            ctx.stats.pages_fetched = report.pages_ok
            ctx.stats.records_new = report.records_found
            ctx.stats.llm_calls = ctx.budget.calls
            ctx.stats.llm_usd = ctx.budget.spent_usd
            ctx.stats.finished_at = utcnow()
            status = "succeeded" if report.records_found else "partial"
            self.store.update_run(
                run_id, status=status, stats=ctx.stats, spent_usd=ctx.budget.spent_usd, finished=True
            )
            self.store.update_task(task_id, status="ready" if report.records_found else "draft")
            self.store.add_event(
                task_id,
                "run_finished",
                f"Калібрування завершено: {report.records_found} рядків у прев'ю",
                run_id=run_id,
                level="info" if report.records_found else "warning",
                data={"strategy": report.strategy, "coverage": report.coverage},
            )
        except Exception as exc:
            logger.exception("calibration failed")
            self._fail(task_id, run_id, exc)
            raise

    # ------------------------------------------------------------------
    # collection
    # ------------------------------------------------------------------
    async def collect(self, task_id: str, run_id: str) -> None:
        task = self._task(task_id)
        spec = task.spec
        version = self.store.next_dataset_version(task.dataset_id or task.id)
        ctx = self._context(task, run_id, version=version)
        current_rows = self.analytics.all_current(task.dataset_id or task.id)
        ctx.known_keys = {row["record_key"] for row in current_rows}
        ctx.existing = {
            row["record_key"]: {
                "payload": row["payload"],
                "provenance": row["provenance"],
            }
            for row in current_rows
        }
        self.store.update_run(run_id, status="running", started=True)

        try:
            counts = self.store.job_counts(run_id)
            if counts["total"] == 0:
                if not spec.seeds:
                    raise ValueError("Немає стартових URL — спершу додайте seed URL до задачі")
                self.store.enqueue_urls(
                    run_id, task_id, [(url, 0, None) for url in spec.seeds]
                )
                self.store.add_event(
                    task_id,
                    "run_started",
                    f"Збір запущено: {len(spec.seeds)} seed URL, версія датасету {version}",
                    run_id=run_id,
                    data={"version": version, "seeds": spec.seeds},
                )

            limit = spec.limits.max_pages
            processed = 0
            idle_rounds = 0

            while True:
                run = self.store.get_run(run_id)
                if run is None or run.status in {"cancelled", "failed"}:
                    ctx.notes.append("run cancelled")
                    break
                if processed >= limit:
                    ctx.notes.append(f"page limit reached ({limit})")
                    self.store.add_event(
                        task_id, "limit_reached",
                        f"Досягнуто ліміту сторінок ({limit}). Запустіть ще раз, щоб продовжити.",
                        run_id=run_id, level="warning",
                    )
                    break
                if ctx.llm_calls >= self.settings.max_llm_calls_per_run:
                    self.store.add_event(
                        task_id, "llm_limit",
                        f"Ліміт LLM-викликів ({self.settings.max_llm_calls_per_run}) вичерпано",
                        run_id=run_id, level="warning",
                    )
                    ctx.notes.append("llm limit reached")

                self.store.release_stale_jobs()
                jobs = self.store.claim_jobs(
                    ctx.worker, limit=min(6, limit - processed), lease_seconds=240
                )
                if not jobs:
                    counts = self.store.job_counts(run_id)
                    if counts["queued"] == 0 and counts["leased"] == 0:
                        break
                    idle_rounds += 1
                    if idle_rounds > 200:
                        ctx.notes.append("queue stalled — aborting")
                        break
                    await asyncio.sleep(0.25)
                    continue
                idle_rounds = 0

                await asyncio.gather(
                    *[
                        self._process_job(job, ctx)
                        for job in jobs
                    ]
                )
                processed += len(jobs)
                ctx.stats.urls_done = processed
                self.store.update_run(
                    run_id, stats=ctx.stats, spent_usd=ctx.budget.spent_usd
                )
                self.store.heartbeat(ctx.worker)

            await self._finish_dataset(ctx, status_override=None)
        except Exception as exc:
            logger.exception("collect failed")
            self._fail(task_id, run_id, exc)
            raise

    async def _process_job(self, job: dict, ctx: RunContext) -> None:
        try:
            await self._process_job_inner(job, ctx)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - one bad page must not kill the run
            logger.exception("job %s failed", job.get("url"))
            ctx.stats.pages_failed += 1
            self.store.fail_job(job["id"], f"{type(exc).__name__}: {exc}", retryable=False)
            self.store.add_event(
                ctx.task.id, "job_failed",
                f"{job.get('url')}: {type(exc).__name__}: {exc}",
                run_id=ctx.run_id, level="error",
            )

    async def _process_job_inner(self, job: dict, ctx: RunContext) -> None:
        url = job["url"]
        result = await self.fetcher.fetch(url)
        page = result.page
        if page is None:
            await self._handle_fetch_failure(job, result, ctx)
            return

        ctx.stats.pages_fetched += 1
        if page.from_cache:
            ctx.stats.pages_cached += 1
        await self._extract_page(page, job, ctx)
        self.store.complete_job(job["id"], status="done")

    async def _handle_fetch_failure(self, job: dict, result, ctx: RunContext) -> None:
        task_id = ctx.task.id
        if result.kind == "robots":
            ctx.stats.robots_blocked += 1
            self.store.complete_job(job["id"], status="skipped", error=result.error)
            self.store.add_event(
                task_id, "robots_blocked", f"Пропущено (robots.txt): {job['url']}",
                run_id=ctx.run_id, level="warning",
            )
            return
        ctx.stats.pages_failed += 1
        retryable = result.kind in {"transient"}
        status = self.store.fail_job(job["id"], result.error or result.kind, retryable=retryable)
        level = "warning" if result.kind in {"blocked", "transient"} else "error"
        self.store.add_event(
            task_id, "fetch_failed", f"{job['url']}: {result.error or result.kind} [{status}]",
            run_id=ctx.run_id, level=level, data={"kind": result.kind, "status": status},
        )

    async def _extract_page(self, page: PageContent, job: dict, ctx: RunContext) -> None:
        spec = ctx.spec
        domain = normalize_domain(page.final_url or page.url)
        schema = self.store.get_selector(domain, f"auto:{spec.entity}")
        if schema is None:
            schema = schema_from_spec(spec)
        if schema is None and not self.settings.offline:
            schema = self._heal(page, spec, domain, ctx)

        allow_llm = (
            spec.extraction in {"auto", "llm"}
            and self.router.available
            and ctx.llm_calls < self.settings.max_llm_calls_per_run
            and ctx.budget.can_afford(0.001)
            and not self.settings.offline
        )
        outcome = await self.engine.extract(
            page, spec, css_schema=schema, allow_llm=allow_llm, budget=ctx.budget
        )
        ctx.llm_calls += outcome.llm_calls
        ctx.stats.llm_calls += outcome.llm_calls
        ctx.stats.llm_usd = ctx.budget.spent_usd
        ctx.strategies[outcome.strategy] += 1

        if schema is not None and not outcome.items and domain not in ctx.healed_domains:
            self.store.mark_selector_miss(domain, f"auto:{spec.entity}")
            healed = self._heal(page, spec, domain, ctx)
            if healed is not None and healed.fields:
                schema = healed
                outcome = await self.engine.extract(
                    page, spec, css_schema=schema, allow_llm=False, budget=ctx.budget
                )
                self.store.add_event(
                    ctx.task.id, "selector_healed",
                    f"Схема перегенеровано для {domain} ({len(healed.fields)} полів)",
                    run_id=ctx.run_id, level="info",
                    data={"signature": schema_signature(healed)},
                )

        records = self.engine.to_records(outcome, page, spec)
        if not records:
            level = "warning" if not outcome.warnings else "debug"
            self.store.add_event(
                ctx.task.id, "no_records", f"0 записів: {page.final_url or page.url}",
                run_id=ctx.run_id, level=level,
                data={"warnings": outcome.warnings[:3], "strategy": outcome.strategy},
            )
            await self._expand(page, job, ctx, empty=True)
            return

        for record in records:
            record = self._merge_with_existing(record, ctx)
            self.analytics.append_records(
                ctx.dataset_id, ctx.version, ctx.run_id, [record]
            )
            if record.record_key in ctx.known_keys:
                ctx.stats.records_updated += 1
            else:
                ctx.stats.records_new += 1
                ctx.known_keys.add(record.record_key)
            ctx.existing[record.record_key] = {
                "payload": record.payload,
                "provenance": {
                    key: value.model_dump(mode="json")
                    for key, value in record.provenance.items()
                },
            }

        self.store.add_event(
            ctx.task.id, "page_extracted",
            f"{len(records)} записів ← {page.final_url or page.url} [{outcome.strategy}]",
            run_id=ctx.run_id, level="debug",
            data={
                "strategy": outcome.strategy,
                "confidence": outcome.confidence,
                "llm_usd": outcome.llm_cost_usd,
                "cached": page.from_cache,
            },
        )
        await self._expand(page, job, ctx, empty=False, records_count=len(records))

    async def _expand(
        self, page: PageContent, job: dict, ctx: RunContext, *, empty: bool, records_count: int = 0
    ) -> None:
        if job["depth"] >= ctx.spec.limits.max_depth:
            return
        if not empty and records_count > 1:
            return
        links = extract_links(
            page.html, page.final_url or page.url, same_site=True, limit=10 if empty else 5
        )
        if not links:
            return
        queued = self.store.job_counts(ctx.run_id)
        remaining = max(0, ctx.spec.limits.max_pages - queued["total"])
        links = links[:remaining]
        if not links:
            return
        total = self.store.enqueue_urls(
            ctx.run_id,
            ctx.task.id,
            [(link, job["depth"] + 1, page.final_url or page.url) for link in links],
        )
        ctx.stats.urls_total = total

    def _heal(self, page: PageContent, spec: TaskSpec, domain: str, ctx: RunContext) -> CssSchema | None:
        ctx.healed_domains.add(domain)
        samples = [
            (page.final_url or page.url, page.html)
        ]
        if self.cache is not None:
            previous = self.cache.get(page.final_url or page.url)
            if previous is not None and previous.html != page.html:
                samples.append((previous.url, previous.html))
        schema = learn_schema(samples, spec)
        if schema.fields and schema.score > 0:
            self.store.save_selector(domain, f"auto:{spec.entity}", schema)
            return schema
        return None

    @staticmethod
    def _merge_with_existing(record: ExtractedRecord, ctx: RunContext) -> ExtractedRecord:
        """A new crawl must not erase fields produced by enrichment.

        Scraped columns win for the fields they returned; enrichment-only columns are
        carried over until a later enrichment pass updates them.
        """
        previous = ctx.existing.get(record.record_key)
        if not previous:
            return record
        payload = {**(previous.get("payload") or {}), **record.payload}
        provenance: dict[str, Provenance] = {}
        for key, value in (previous.get("provenance") or {}).items():
            try:
                provenance[key] = Provenance.model_validate(value)
            except Exception:  # noqa: BLE001 - tolerate legacy payloads
                continue
        provenance.update(record.provenance)
        return record.model_copy(update={"payload": payload, "provenance": provenance})

    # ------------------------------------------------------------------
    # enrichment
    # ------------------------------------------------------------------
    async def enrich(
        self, task_id: str, run_id: str, *, column_keys: list[str] | None = None
    ) -> None:
        task = self._task(task_id)
        spec = task.spec
        dataset_id = task.dataset_id or task.id
        rows = self.analytics.all_current(dataset_id)
        version = self.store.next_dataset_version(dataset_id)
        ctx = self._context(task, run_id, version=version)
        self.store.update_run(run_id, status="running", started=True)

        targets = [
            column
            for column in spec.columns
            if (column.method == "enrichment" or column.is_ai)
            and (not column_keys or column.key in column_keys)
        ]
        if not targets:
            self.store.update_run(
                run_id, status="failed", error="no enrichment columns selected", finished=True
            )
            self.store.update_task(task_id, status="done" if rows else "draft")
            self.store.add_event(
                task_id, "run_failed", "Немає колонок для збагачення", run_id=run_id, level="error"
            )
            return
        if not rows:
            self.store.update_run(
                run_id, status="failed", error="dataset is empty", finished=True
            )
            self.store.add_event(
                task_id, "run_failed", "Датасет порожній — спершу запустіть збір",
                run_id=run_id, level="error",
            )
            return

        self.store.add_event(
            task_id, "run_started",
            f"Збагачення {len(rows)} записів: {', '.join(column.key for column in targets)}",
            run_id=run_id, data={"version": version},
        )
        semaphore = asyncio.Semaphore(self.settings.max_enrich_concurrency)
        context = EnrichmentContext(
            settings=self.settings,
            fetcher=self.fetcher,
            router=self.router,
            store=self.store,
            task_id=task_id,
            run_id=run_id,
            budget=ctx.budget,
            search=self.search,
        )

        try:
            async def process(row: dict) -> None:
                async with semaphore:
                    await self._enrich_row(row, ctx, context, targets)

            await asyncio.gather(*[process(row) for row in rows])
            await self._finish_dataset(ctx, status_override=None, diff_only=False)
        except Exception as exc:
            logger.exception("enrichment failed")
            self._fail(task_id, run_id, exc)
            raise

    async def _enrich_row(self, row: dict, ctx: RunContext, context: EnrichmentContext, targets) -> None:
        payload = dict(row.get("payload") or {})
        provenance: dict[str, Provenance] = {}
        for key, value in (row.get("provenance") or {}).items():
            try:
                provenance[key] = Provenance.model_validate(value)
            except Exception:  # noqa: BLE001 - tolerate old payloads
                continue
        changed = False

        for enrichment in ctx.spec.enrichment:
            if not enrichment.enabled:
                continue
            enricher = self.registry.get(enrichment.enricher)
            if enricher is None or not enricher.applies(payload):
                continue
            produced = [
                key
                for key in enricher.produces
                if key in ctx.spec.column_map and any(column.key == key for column in targets)
            ]
            if not produced:
                continue
            missing = [key for key in produced if payload.get(key) in (None, "", [])]
            if not missing:
                continue
            result = await enricher.run(payload, context)
            if result.skipped or not result.values:
                continue
            before = dict(payload)
            merged = merge_payload(payload, result, ctx.spec)
            if merged != payload:
                changed = True
                payload = merged
                applied = [
                    key for key, value in result.values.items()
                    if merged.get(key) != before.get(key)
                ]
                provenance.update(
                    provenance_for(applied, result, source_url=str(payload.get("website") or ""))
                )
            ctx.stats.enrichments_run += 1
            ctx.stats.llm_usd = ctx.budget.spent_usd
            if result.notes:
                ctx.notes.extend(result.notes[:1])

        if any(column.is_ai for column in targets):
            result = await self.ai.run(payload, ctx.spec, context)
            if result.values:
                before = dict(payload)
                merged = merge_payload(payload, result, ctx.spec)
                if merged != payload:
                    changed = True
                    payload = merged
                    applied = {
                        key: value for key, value in result.provenance.items()
                        if merged.get(key) != before.get(key)
                    }
                    provenance.update(applied)
            ctx.stats.llm_calls += result.calls
            ctx.stats.llm_usd = ctx.budget.spent_usd

        if not changed:
            return
        record = ExtractedRecord(
            record_key=row["record_key"],
            payload=payload,
            provenance=provenance,
            source_url=str(row.get("source_url") or ""),
            content_hash="",
        )
        self.analytics.append_records(ctx.dataset_id, ctx.version, ctx.run_id, [record])
        ctx.stats.records_updated += 1

    # ------------------------------------------------------------------
    # snapshots / change detection
    # ------------------------------------------------------------------
    async def snapshot(self, task_id: str, run_id: str) -> None:
        task = self._task(task_id)
        dataset_id = task.dataset_id or task.id
        versions = self.store.dataset_versions(dataset_id)
        if not versions:
            self.store.update_run(run_id, status="failed", error="no dataset versions", finished=True)
            self.store.add_event(
                task_id, "run_failed", "Немає даних для снапшота", run_id=run_id, level="error"
            )
            return
        current_version = int(versions[0]["version"])
        ctx = self._context(task, run_id, version=current_version)
        self.store.update_run(run_id, status="running", started=True)
        try:
            changes = await self._write_snapshot_and_diff(ctx, current_version)
            ctx.stats.records_updated = 0
            ctx.stats.records_new = 0
            ctx.stats.finished_at = utcnow()
            self.store.update_run(run_id, status="succeeded", stats=ctx.stats, finished=True)
            self.store.add_event(
                task_id, "run_finished",
                f"Снапшот v{current_version} записано; змін: {len(changes)}",
                run_id=run_id, data={"version": current_version, "changes": len(changes)},
            )
        except Exception as exc:
            logger.exception("snapshot failed")
            self._fail(task_id, run_id, exc)
            raise

    # ------------------------------------------------------------------
    # finish helpers
    # ------------------------------------------------------------------
    async def _finish_dataset(
        self, ctx: RunContext, *, status_override: str | None, diff_only: bool = False
    ) -> None:
        dataset_id = ctx.dataset_id
        row_count = self.analytics.count_records(dataset_id)
        changes = await self._write_snapshot_and_diff(ctx, ctx.version)

        fill = self.analytics.field_fill_rates(dataset_id, [c for c in ctx.spec.columns if not c.is_ai])
        manifest = {
            "task_id": ctx.task.id,
            "dataset_version": ctx.version,
            "run_id": ctx.run_id,
            "generated_at": iso(),
            "rows": row_count,
            "pages": ctx.stats.pages_fetched,
            "strategies": dict(ctx.strategies),
            "llm_calls": ctx.stats.llm_calls,
            "llm_spend_usd": round(ctx.budget.spent_usd, 4),
            "fill_rates": fill,
            "changes": len(changes),
            "notes": ctx.notes[:20],
            "sources": unique(
                [normalize_domain(seed) for seed in ctx.spec.seeds if normalize_domain(seed)]
            ),
        }
        self.store.add_dataset_version(dataset_id, ctx.version, ctx.run_id, row_count, manifest)

        ctx.stats.finished_at = utcnow()
        counts = self.store.job_counts(ctx.run_id)
        status = status_override or "succeeded"
        if counts["failed"] and row_count:
            status = "partial"
        if not row_count:
            status = "partial" if ctx.stats.pages_fetched else "failed"
        self.store.update_run(
            ctx.run_id, status=status, stats=ctx.stats, spent_usd=ctx.budget.spent_usd, finished=True
        )
        self.store.update_task(ctx.task.id, status="done" if row_count else "failed")
        self.store.add_event(
            ctx.task.id, "run_finished",
            f"Готово: {row_count} записів у v{ctx.version} ({len(changes)} змін)",
            run_id=ctx.run_id,
            level="info" if row_count else "warning",
            data={
                "version": ctx.version,
                "rows": row_count,
                "new": ctx.stats.records_new,
                "updated": ctx.stats.records_updated,
                "failed_jobs": counts["failed"],
                "llm_usd": round(ctx.budget.spent_usd, 4),
            },
        )
        if changes:
            self._emit_changes(ctx, changes)

    async def _write_snapshot_and_diff(self, ctx: RunContext, version: int) -> list:
        previous_versions = [
            int(item["version"]) for item in self.store.dataset_versions(ctx.dataset_id)
        ]
        previous_version = max([v for v in previous_versions if v < version], default=version - 1)
        changes = self.analytics.diff_versions(
            ctx.dataset_id,
            from_version=previous_version,
            to_version=version,
            columns=ctx.spec.columns,
            price_fields=detect_price_fields(ctx.spec.columns),
            price_drop_pct=self.settings.price_drop_threshold_pct,
        )
        directory = (
            self.settings.paths.snapshots
            / f"dataset={dataset_slug(ctx.spec.title)}"
            / f"version={version:04d}"
        )
        target = directory / "part.parquet"
        self.analytics.snapshot(ctx.dataset_id, version, target)
        manifest_path = directory / "manifest.json"
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        import json

        manifest_path.write_text(
            json.dumps(
                {
                    "dataset_id": ctx.dataset_id,
                    "version": version,
                    "previous_version": previous_version,
                    "rows": self.analytics.count_records(ctx.dataset_id),
                    "changes": [change.model_dump(mode="json") for change in changes[:500]],
                    "generated_at": iso(),
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        return changes

    def _emit_changes(self, ctx: RunContext, changes: list) -> None:
        summary: Counter = Counter(change.type for change in changes)
        self.store.add_event(
            ctx.task.id,
            "changes_detected",
            ", ".join(f"{key}: {value}" for key, value in summary.items()),
            run_id=ctx.run_id,
            data={"summary": dict(summary)},
        )
        for change in changes[:25]:
            verb = {
                "new_entity": "Новий запис",
                "removed_entity": "Запис зник",
                "field_changed": "Зміна поля",
                "price_drop": "Ціна впала",
            }.get(change.type, change.type)
            detail = f"{change.record_key}"
            if change.field:
                detail += f" · {change.field}: {change.old} → {change.new}"
            if change.delta_pct is not None:
                detail += f" ({change.delta_pct:+.1f}%)"
            self.store.add_event(
                ctx.task.id,
                change.type,
                f"{verb}: {detail}",
                run_id=ctx.run_id,
                level="info" if change.type != "price_drop" else "warning",
                data=change.model_dump(mode="json"),
            )

    # ------------------------------------------------------------------
    # infrastructure
    # ------------------------------------------------------------------
    def _task(self, task_id: str) -> TaskInfo:
        task = self.store.get_task(task_id)
        if task is None:
            raise ValueError(f"task not found: {task_id}")
        return task

    def _context(self, task: TaskInfo, run_id: str, *, version: int) -> RunContext:
        run = self.store.get_run(run_id)
        budget_usd = (
            task.spec.limits.budget_usd
            if run is None or run.budget_usd <= 0
            else min(task.spec.limits.budget_usd, run.budget_usd)
        )
        budget = LLMBudget(budget_usd, self.store.run_spend(run_id))
        self.router.bind(task_id=task.id, run_id=run_id, budget_usd=budget_usd)
        self.router.budget = budget
        return RunContext(
            task=task,
            spec=task.spec,
            run_id=run_id,
            version=version,
            budget=budget,
            worker=f"worker-{run_id}",
            dataset_id=task.dataset_id or task.id,
            stats=RunStats(started_at=utcnow()),
        )

    def _fail(self, task_id: str, run_id: str, exc: Exception) -> None:
        try:
            self.store.update_run(run_id, status="failed", error=str(exc)[:500], finished=True)
            self.store.update_task(task_id, status="failed")
            self.store.add_event(
                task_id, "run_failed", f"{type(exc).__name__}: {exc}", run_id=run_id, level="error"
            )
        except Exception:  # pragma: no cover - never mask the original failure
            logger.exception("failed to persist run failure")


def schema_from_spec(spec: TaskSpec) -> CssSchema | None:
    """Use explicit user-provided selectors, if any."""
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
    return CssSchema(container="body", item_scope=False, fields=fields, method="css",
                     notes=["from explicit column selectors"])


def snapshot_path(settings: Settings, title: str, version: int) -> Path:
    return (
        settings.paths.snapshots
        / f"dataset={dataset_slug(title)}"
        / f"version={version:04d}"
        / "part.parquet"
    )
