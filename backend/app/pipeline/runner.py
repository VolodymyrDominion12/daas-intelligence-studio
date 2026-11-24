"""Pipeline runner: durable queue processing, enrichment, snapshots and change detection."""

from __future__ import annotations

import asyncio
import logging
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from ..compliance import effective_policy, login_violation, redact_url
from ..config import Settings
from ..db.duckdb_store import AnalyticsStore, dataset_slug, detect_price_fields
from ..db.sqlite_store import OperationalStore
from ..enrichment.ai_column import AiColumns
from ..enrichment.base import EnrichmentContext, merge_payload, provenance_for
from ..extraction.api_json import extract_json_list_targets, extract_json_records_with_quarantine
from ..extraction.engine import ExtractionEngine
from ..extraction.learner import apply_manual_selectors, learn_schema, schema_signature
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
from ..scraping.resilience import DomainCircuitBreaker
from ..storage.raw_cache import RawCache
from ..utils import iso, normalize_domain, unique, utcnow
from .calibration import Calibrator
from .quality import evaluate_dataset_quality

logger = logging.getLogger(__name__)


def _with_query_param(url: str, key: str, value: str) -> str:
    parts = urlsplit(url)
    pairs = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k != key]
    pairs.append((key, value))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(pairs), ""))


def _build_fallback_url(original_url: str, fallback: str) -> str:
    cleaned = fallback.strip()
    if not cleaned:
        return ""
    fb_parts = urlsplit(cleaned)
    orig_parts = urlsplit(original_url)
    if fb_parts.path in {"", "/"} and not fb_parts.query:
        return urlunsplit(
            (fb_parts.scheme, fb_parts.netloc, orig_parts.path or "/", orig_parts.query, "")
        )
    return cleaned


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
    # tombstone bookkeeping (collect runs only)
    seen_keys: set[str] = field(default_factory=set)
    # Evidence for removals: pages of *this* run fetched fresh (not from cache) that yielded
    # records. A record may be judged missing only if all its known sources are in here.
    checked_sources: set[str] = field(default_factory=set)
    full_crawl: bool = False
    # API incremental collection state
    api_list_failed: bool = False
    api_next_cursor: str | None = None
    api_next_url: str | None = None
    seen_api_urls: set[str] = field(default_factory=set)
    # Entity resolution redirects (merged_key -> canonical_key)
    merge_redirects: dict[str, str] = field(default_factory=dict)
    # Circuit breaker / mirror fallback bookkeeping per run
    opened_circuits: set[str] = field(default_factory=set)
    fallback_attempted: set[str] = field(default_factory=set)


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
        dispatcher=None,
        circuit_breaker: DomainCircuitBreaker | None = None,
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
        self.dispatcher = dispatcher
        self.circuit_breaker = circuit_breaker
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
            await self._notify_run_failed(task_id, run_id, exc)
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
        ctx.merge_redirects = self.store.active_merged_key_map(task_id)
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
                if spec.api_source is not None:
                    start_urls = self._api_initial_urls(task_id, spec)
                    total = self.store.enqueue_urls(
                        run_id, task_id, [(url, 0, None) for url in start_urls], kind="api_list"
                    )
                    ctx.seen_api_urls.update(start_urls)
                else:
                    start_urls = list(spec.seeds)
                    total = self.store.enqueue_urls(
                        run_id, task_id, [(url, 0, None) for url in start_urls]
                    )
                ctx.stats.urls_total = total
                self.store.add_event(
                    task_id,
                    "run_started",
                    f"Збір запущено: {len(start_urls)} seed URL, версія датасету {version}",
                    run_id=run_id,
                    data={"version": version, "seeds": start_urls},
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
                    ctx.worker, run_id=run_id, limit=min(6, limit - processed), lease_seconds=240
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

            self._apply_tombstones(ctx)
            await self._finish_dataset(ctx, status_override=None)
            self._save_api_cursor(ctx)
        except Exception as exc:
            logger.exception("collect failed")
            self.rollback_removals(run_id)
            self._fail(task_id, run_id, exc)
            await self._notify_run_failed(task_id, run_id, exc)
            raise

    def _api_initial_urls(self, task_id: str, spec: TaskSpec) -> list[str]:
        api_source = spec.api_source
        if api_source is not None and api_source.incremental:
            state = self.store.get_api_state(task_id)
            if state:
                saved_url = state.get("next_url")
                if saved_url and login_violation(saved_url) is None:
                    return [saved_url]
                saved_cursor = state.get("next_cursor")
                if saved_cursor and api_source.cursor_param and spec.seeds:
                    return [_with_query_param(spec.seeds[0], api_source.cursor_param, saved_cursor)]
        return list(spec.seeds)

    def _save_api_cursor(self, ctx: RunContext) -> None:
        if ctx.spec.api_source is None or ctx.api_list_failed:
            return
        if not (ctx.api_next_cursor or ctx.api_next_url):
            return
        final_run = self.store.get_run(ctx.run_id)
        final_counts = self.store.job_counts(ctx.run_id)
        aborted = any(note.startswith(("run cancelled", "queue stalled")) for note in ctx.notes)
        if (
            final_run is None
            or final_run.status != "succeeded"
            or final_counts["failed"] > 0
            or final_counts["skipped"] > 0
            or aborted
        ):
            return
        self.store.save_api_state(
            ctx.task.id,
            next_cursor=ctx.api_next_cursor,
            next_url=ctx.api_next_url,
        )
        self.store.add_event(
            ctx.task.id,
            "api_cursor_saved",
            "Збережено курсор API для наступного інкрементального збору",
            run_id=ctx.run_id,
            level="info",
            data={
                "cursor": ctx.api_next_cursor,
                "next_url": ctx.api_next_url,
            },
        )

    async def _process_job(self, job: dict, ctx: RunContext) -> None:
        try:
            await self._process_job_inner(job, ctx)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - one bad page must not kill the run
            logger.exception("job %s failed", job.get("url"))
            if job.get("kind") == "api_list":
                ctx.api_list_failed = True
            ctx.stats.pages_failed += 1
            self.store.fail_job(job["id"], f"{type(exc).__name__}: {exc}", retryable=False)
            self.store.add_event(
                ctx.task.id, "job_failed",
                f"{job.get('url')}: {type(exc).__name__}: {exc}",
                run_id=ctx.run_id, level="error",
            )

    async def _process_job_inner(self, job: dict, ctx: RunContext) -> None:
        url = job["url"]
        kind = job.get("kind") or "fetch"
        if kind in {"api_list", "api_detail"}:
            result = await self.fetcher.fetch(
                url,
                force_refresh=(kind == "api_list"),
                allow_json=True,
            )
        else:
            result = await self.fetcher.fetch(url)
        page = result.page
        if page is None:
            await self._handle_fetch_failure(job, result, ctx)
            return

        ctx.stats.pages_fetched += 1
        if page.from_cache:
            ctx.stats.pages_cached += 1
        else:
            if page.headers.get("x-daas-js-rendered") == "1":
                reason = page.headers.get("x-daas-js-reason", "")
                self.store.add_event(
                    ctx.task.id,
                    "js_rendered",
                    f"JS-рендер {page.final_url or url} ({page.engine})",
                    run_id=ctx.run_id,
                    level="info",
                    data={
                        "url": page.final_url or url,
                        "engine": page.engine,
                        "reason": reason,
                    },
                )
            elif page.headers.get("x-daas-js-fallback"):
                fb = page.headers["x-daas-js-fallback"]
                self.store.add_event(
                    ctx.task.id,
                    "js_render_fallback",
                    f"Fallback на HTTP для {page.final_url or url}: {fb}",
                    run_id=ctx.run_id,
                    level="warning",
                    data={"url": page.final_url or url, "reason": fb},
                )
        if kind == "api_list":
            await self._process_api_list(page, job, ctx)
        elif kind == "api_detail":
            await self._process_api_detail(page, job, ctx)
        else:
            await self._extract_page(page, job, ctx)
        self.store.complete_job(job["id"], status="done")

    async def _handle_fetch_failure(self, job: dict, result, ctx: RunContext) -> None:
        task_id = ctx.task.id
        if result.kind == "robots":
            if job.get("kind") == "api_list":
                ctx.api_list_failed = True
            ctx.stats.robots_blocked += 1
            self.store.complete_job(job["id"], status="skipped", error=result.error)
            self.store.add_event(
                task_id, "robots_blocked", f"Пропущено (robots.txt): {job['url']}",
                run_id=ctx.run_id, level="warning",
            )
            return
        if result.kind == "policy":
            if job.get("kind") == "api_list":
                ctx.api_list_failed = True
            # The fetch guard already wrote a redacted compliance_blocked event.
            self.store.complete_job(job["id"], status="skipped", error=result.error)
            return

        if result.kind in {"blocked", "circuit_open"}:
            domain = normalize_domain(job["url"]) or urlsplit(job["url"]).hostname or ""
            circuit_status = (
                self.circuit_breaker.status(domain)
                if (self.circuit_breaker is not None and domain)
                else None
            )
            if result.kind == "circuit_open" or (
                circuit_status is not None and circuit_status.state == "open"
            ):
                if domain and domain not in ctx.opened_circuits:
                    ctx.opened_circuits.add(domain)
                    cooldown_s = (
                        int(self.circuit_breaker.cooldown_s)
                        if self.circuit_breaker is not None
                        else int(self.settings.circuit_breaker_cooldown_s)
                    )
                    self.store.add_event(
                        task_id,
                        "circuit_opened",
                        f"Circuit breaker відкрито для {domain}: пауза {cooldown_s}с",
                        run_id=ctx.run_id,
                        level="warning",
                        data={
                            "domain": domain,
                            "state": circuit_status.state if circuit_status else "open",
                            "reason": circuit_status.last_reason if circuit_status else result.error,
                        },
                    )
                self._maybe_enqueue_fallback(job, domain, ctx)

        if result.kind == "circuit_open":
            if job.get("kind") == "api_list":
                ctx.api_list_failed = True
            self.store.complete_job(job["id"], status="skipped", error=result.error)
            self.store.add_event(
                task_id,
                "fetch_failed",
                f"{job['url']}: {result.error or 'circuit_open'} [skipped]",
                run_id=ctx.run_id,
                level="warning",
                data={"kind": "circuit_open", "status": "skipped"},
            )
            return

        ctx.stats.pages_failed += 1
        retryable = result.kind in {"transient"}
        status = self.store.fail_job(job["id"], result.error or result.kind, retryable=retryable)
        if job.get("kind") == "api_list" and status == "failed":
            ctx.api_list_failed = True
        level = "warning" if result.kind in {"blocked", "transient"} else "error"
        self.store.add_event(
            task_id, "fetch_failed", f"{job['url']}: {result.error or result.kind} [{status}]",
            run_id=ctx.run_id, level=level, data={"kind": result.kind, "status": status},
        )

    def _maybe_enqueue_fallback(self, job: dict, domain: str, ctx: RunContext) -> None:
        if not domain or job["url"] in ctx.fallback_attempted:
            return
        policy = effective_policy(self.store, job["url"])
        if policy is None or not policy.fallback_urls:
            return
        ctx.fallback_attempted.add(job["url"])
        queued = self.store.job_counts(ctx.run_id)
        remaining = max(0, ctx.spec.limits.max_pages - queued["total"])
        if remaining <= 0:
            return
        for raw_fb in policy.fallback_urls:
            fb_url = _build_fallback_url(job["url"], raw_fb)
            if not fb_url or fb_url == job["url"] or fb_url in ctx.fallback_attempted:
                continue
            if login_violation(fb_url) is not None:
                self.store.add_event(
                    ctx.task.id,
                    "compliance_blocked",
                    f"Відхилено небезпечний fallback URL: {redact_url(fb_url)}",
                    run_id=ctx.run_id,
                    level="warning",
                )
                continue
            ctx.fallback_attempted.add(fb_url)
            total = self.store.enqueue_urls(
                ctx.run_id,
                ctx.task.id,
                [(fb_url, job["depth"], job["url"])],
                kind=job.get("kind") or "fetch",
            )
            ctx.stats.urls_total = total
            self.store.add_event(
                ctx.task.id,
                "circuit_fallback",
                f"Перемикання {job['url']} → {fb_url}",
                run_id=ctx.run_id,
                level="info",
                data={"from_url": job["url"], "to_url": fb_url, "domain": domain},
            )
            break

    def _quarantine_items(
        self, items: list[dict], source_url: str, ctx: RunContext
    ) -> None:
        for item in items:
            reason = str(item.get("reason") or "Запис відхилено валідацією")
            payload = dict(item.get("payload") or {})
            self.analytics.quarantine(
                ctx.dataset_id, ctx.run_id, source_url, reason, payload
            )
            ctx.stats.records_quarantined += 1
            if ctx.stats.records_quarantined <= 25:
                self.store.add_event(
                    ctx.task.id,
                    "record_quarantined",
                    f"Карантин ← {source_url}: {reason}",
                    run_id=ctx.run_id,
                    level="warning",
                    data={"url": source_url, "reason": reason},
                )

    def _persist_records(
        self,
        records: list[ExtractedRecord],
        page: PageContent,
        job: dict,
        ctx: RunContext,
        *,
        strategy: str,
        confidence: float = 1.0,
        llm_cost_usd: float = 0.0,
    ) -> None:
        persisted = 0
        source_url = page.final_url or page.url
        for record in records:
            mean_conf = record.mean_confidence()
            if record.provenance and mean_conf < 0.40:
                self._quarantine_items(
                    [
                        {
                            "payload": record.payload,
                            "reason": f"Низька впевненість екстракції ({mean_conf:.2f} < 0.40)",
                        }
                    ],
                    source_url,
                    ctx,
                )
                continue
            target_key = ctx.merge_redirects.get(record.record_key, record.record_key)
            if target_key != record.record_key:
                record = record.model_copy(update={"record_key": target_key})
            ctx.seen_keys.add(record.record_key)
            record = self._merge_with_existing(record, ctx)
            self.analytics.append_records(
                ctx.dataset_id, ctx.version, ctx.run_id, [record]
            )
            persisted += 1
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

        if persisted > 0:
            if not page.from_cache:
                # Fresh page that yielded valid records: counts as checked evidence for tombstones.
                ctx.checked_sources.update({page.url, source_url, job["url"]})
            self.store.add_event(
                ctx.task.id, "page_extracted",
                f"{persisted} записів ← {source_url} [{strategy}]",
                run_id=ctx.run_id, level="debug",
                data={
                    "strategy": strategy,
                    "confidence": confidence,
                    "llm_usd": llm_cost_usd,
                    "cached": page.from_cache,
                },
            )

    async def _process_api_list(self, page: PageContent, job: dict, ctx: RunContext) -> None:
        api_source = ctx.spec.api_source
        if api_source is None:
            await self._extract_page(page, job, ctx)
            return

        source_url = page.final_url or page.url
        targets, next_url, cursor, warnings = extract_json_list_targets(page.html, api_source)
        if warnings:
            ctx.api_list_failed = True
            self.store.add_event(
                ctx.task.id,
                "json_parse_failed",
                f"Помилка розбору JSON списку: {source_url}",
                run_id=ctx.run_id,
                level="warning",
                data={"url": source_url, "error": warnings[0]},
            )
            return

        if cursor:
            ctx.api_next_cursor = cursor
        if next_url:
            ctx.api_next_url = next_url
        elif cursor and api_source.cursor_param and ctx.spec.seeds:
            candidate = _with_query_param(ctx.spec.seeds[0], api_source.cursor_param, cursor)
            if candidate != job["url"]:
                ctx.api_next_url = candidate
                next_url = candidate

        if api_source.detail_url_template:
            detail_urls = [
                detail_url
                for _item_id, detail_url in targets
                if detail_url and login_violation(detail_url) is None
            ]
            if detail_urls:
                queued = self.store.job_counts(ctx.run_id)
                remaining = max(0, ctx.spec.limits.max_pages - queued["total"])
                batch = detail_urls[:remaining]
                if batch:
                    total = self.store.enqueue_urls(
                        ctx.run_id,
                        ctx.task.id,
                        [(u, job["depth"] + 1, source_url) for u in batch],
                        kind="api_detail",
                    )
                    ctx.stats.urls_total = total
        else:
            records, quarantined, rec_warnings = extract_json_records_with_quarantine(
                page.html, ctx.spec, source_url
            )
            if rec_warnings:
                ctx.api_list_failed = True
                self.store.add_event(
                    ctx.task.id,
                    "json_parse_failed",
                    f"Помилка розбору JSON записів: {source_url}",
                    run_id=ctx.run_id,
                    level="warning",
                    data={"url": source_url, "error": rec_warnings[0]},
                )
                return
            ctx.strategies["api_json"] += 1
            if quarantined:
                self._quarantine_items(quarantined, source_url, ctx)
            if records:
                self._persist_records(
                    records, page, job, ctx, strategy="api_json", confidence=0.98
                )
            elif not quarantined:
                self.store.add_event(
                    ctx.task.id,
                    "no_records",
                    f"0 записів: {source_url}",
                    run_id=ctx.run_id,
                    level="debug",
                    data={"strategy": "api_json"},
                )

        if (
            targets
            and next_url
            and next_url not in ctx.seen_api_urls
            and next_url not in {job["url"], source_url}
            and login_violation(next_url) is None
        ):
            queued = self.store.job_counts(ctx.run_id)
            remaining = max(0, ctx.spec.limits.max_pages - queued["total"])
            if remaining > 0:
                ctx.seen_api_urls.add(next_url)
                total = self.store.enqueue_urls(
                    ctx.run_id,
                    ctx.task.id,
                    [(next_url, job["depth"] + 1, source_url)],
                    kind="api_list",
                )
                ctx.stats.urls_total = total

    async def _process_api_detail(self, page: PageContent, job: dict, ctx: RunContext) -> None:
        source_url = page.final_url or page.url
        records, quarantined, warnings = extract_json_records_with_quarantine(
            page.html, ctx.spec, source_url
        )
        if warnings:
            self.store.add_event(
                ctx.task.id,
                "json_parse_failed",
                f"Помилка розбору JSON картки: {source_url}",
                run_id=ctx.run_id,
                level="warning",
                data={"url": source_url, "error": warnings[0]},
            )
            return
        ctx.strategies["api_json"] += 1
        if quarantined:
            self._quarantine_items(quarantined, source_url, ctx)
        if not records:
            if not quarantined:
                self.store.add_event(
                    ctx.task.id,
                    "no_records",
                    f"0 записів: {source_url}",
                    run_id=ctx.run_id,
                    level="debug",
                    data={"strategy": "api_json"},
                )
            return
        self._persist_records(records, page, job, ctx, strategy="api_json", confidence=0.98)

    async def _extract_page(self, page: PageContent, job: dict, ctx: RunContext) -> None:
        spec = ctx.spec
        domain = normalize_domain(page.final_url or page.url)
        schema = self.store.get_selector(domain, f"auto:{spec.entity}")
        if schema is None:
            schema = schema_from_spec(spec)
        if schema is None and not self.settings.offline:
            schema = self._heal(page, spec, domain, ctx)
        schema = apply_manual_selectors(schema, spec)

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

        if outcome.quarantined:
            self._quarantine_items(outcome.quarantined, page.final_url or page.url, ctx)

        records = self.engine.to_records(outcome, page, spec)
        if not records:
            if not outcome.quarantined:
                level = "warning" if not outcome.warnings else "debug"
                self.store.add_event(
                    ctx.task.id, "no_records", f"0 записів: {page.final_url or page.url}",
                    run_id=ctx.run_id, level=level,
                    data={"warnings": outcome.warnings[:3], "strategy": outcome.strategy},
                )
            await self._expand(page, job, ctx, empty=True)
            return

        self._persist_records(
            records,
            page,
            job,
            ctx,
            strategy=outcome.strategy,
            confidence=outcome.confidence,
            llm_cost_usd=outcome.llm_cost_usd,
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
        # Never queue (and so never persist) links that carry credentials or session tokens.
        links = [link for link in links if login_violation(link) is None]
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
            await self._notify_run_failed(task_id, run_id, exc)
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
            await self._notify_run_failed(task_id, run_id, exc)
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

        existing_versions = [
            item
            for item in self.store.dataset_versions(dataset_id)
            if int(item["version"]) < ctx.version
        ]
        previous_row_count = int(existing_versions[0]["row_count"]) if existing_versions else None
        fill = self.analytics.field_fill_rates(dataset_id, [c for c in ctx.spec.columns if not c.is_ai])
        quality_checks = evaluate_dataset_quality(
            row_count=row_count,
            previous_row_count=previous_row_count,
            fill_rates=fill,
            columns=ctx.spec.columns,
            observed_count=len(ctx.seen_keys) if ctx.full_crawl else None,
            full_crawl=ctx.full_crawl,
            max_drop_ratio=self.settings.quality_max_drop_ratio,
            min_required_fill_pct=self.settings.quality_min_required_fill_pct,
        )
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
            "quality_checks": quality_checks.model_dump(mode="json"),
            "changes": len(changes),
            "full_crawl": ctx.full_crawl,
            "removed": ctx.stats.records_removed,
            "quarantined": ctx.stats.records_quarantined,
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
                "removed": ctx.stats.records_removed,
                "quarantined": ctx.stats.records_quarantined,
                "failed_jobs": counts["failed"],
                "llm_usd": round(ctx.budget.spent_usd, 4),
            },
        )
        if not quality_checks.passed:
            summary_msg = "; ".join(issue.message for issue in quality_checks.issues)
            self.store.add_event(
                ctx.task.id,
                "quality_warning",
                f"Попередження якості даних (v{ctx.version}): {summary_msg}",
                run_id=ctx.run_id,
                level="warning",
                data={
                    "version": ctx.version,
                    **quality_checks.model_dump(mode="json"),
                },
            )
        if changes:
            self._emit_changes(ctx, changes)
        if self.dispatcher is not None:
            try:
                if changes:
                    records_by_key = {
                        row["record_key"]: row["payload"]
                        for row in self.analytics.all_current(dataset_id)
                    }
                    await self.dispatcher.dispatch_changes(
                        ctx.task,
                        version=ctx.version,
                        run_id=ctx.run_id,
                        changes=changes,
                        records_by_key=records_by_key,
                    )
                elif row_count == 0:
                    await self.dispatcher.dispatch_alert(
                        ctx.task,
                        run_id=ctx.run_id,
                        kind="empty_dataset",
                        message=f"Збір завершився без записів (v{ctx.version})",
                    )
                if not quality_checks.passed:
                    summary_msg = "; ".join(issue.message for issue in quality_checks.issues)
                    await self.dispatcher.dispatch_alert(
                        ctx.task,
                        run_id=ctx.run_id,
                        kind="quality_warning",
                        message=f"v{ctx.version}: {summary_msg}",
                    )
            except Exception:  # noqa: BLE001 - delivery failure must not fail a completed run
                logger.warning("delivery dispatch failed for run %s", ctx.run_id, exc_info=True)

    def _apply_tombstones(self, ctx: RunContext) -> None:
        """Mark records that a *complete* crawl no longer finds as removed (P1-03).

        Conservative by design — a false «removed» is worse than a late one:

        * only after a full crawl: not cancelled or stalled, no failed or skipped jobs, and no
          work left behind when a page limit was hit;
        * only for records whose **every known source page** was fetched fresh in this run and
          yielded records (no cache, no empty/broken pages, only this run's own jobs);
        * a miss is counted only for such eligible runs, and the record is removed after
          ``tombstone_after_misses`` of them in a row;
        * never more than ``tombstone_max_ratio`` of the dataset at once;
        * only if the record's revision is unchanged since it was judged missing.
        """
        if ctx.spec.api_source is not None and ctx.spec.api_source.incremental:
            ctx.full_crawl = False
            self.store.add_event(
                ctx.task.id,
                "tombstones_skipped",
                "Видалені записи не позначались: інкрементальний режим API",
                run_id=ctx.run_id,
                level="debug",
            )
            return

        counts = self.store.job_counts(ctx.run_id)
        remaining = counts["queued"] + counts["leased"]
        incomplete = [
            note for note in ctx.notes
            if note.startswith(("run cancelled", "queue stalled"))
            or (note.startswith("page limit") and remaining)
        ]
        ctx.full_crawl = not incomplete and not counts["failed"] and not counts["skipped"]
        if not ctx.full_crawl:
            reason = incomplete[0] if incomplete else (
                f"{counts['failed']} failed / {counts['skipped']} skipped pages"
            )
            self.store.add_event(
                ctx.task.id, "tombstones_skipped",
                f"Видалені записи не позначались: неповний прохід ({reason})",
                run_id=ctx.run_id, level="debug",
            )
            return

        state = self.analytics.current_state(ctx.dataset_id)
        sources = self.analytics.known_sources(ctx.dataset_id)
        self.store.reset_misses(ctx.dataset_id, ctx.seen_keys)
        eligible = [
            key
            for key, (_revision, version) in state.items()
            if key not in ctx.seen_keys
            and version < ctx.version
            and sources.get(key)
            and sources[key] <= ctx.checked_sources
        ]
        if not eligible:
            return
        misses = self.store.bump_misses(ctx.dataset_id, eligible, ctx.run_id)
        threshold = self.settings.tombstone_after_misses
        candidates = [key for key in eligible if misses.get(key, 0) >= threshold]
        if not candidates:
            return

        total = len(state)
        if total and len(candidates) / total > self.settings.tombstone_max_ratio:
            self.store.add_event(
                ctx.task.id, "tombstones_suspicious",
                f"Не позначено {len(candidates)} з {total} записів як видалені: це більше "
                f"{self.settings.tombstone_max_ratio:.0%} датасету — схоже на зламаний сайт "
                "або верстку. Перевірте джерело.",
                run_id=ctx.run_id, level="warning",
                data={"candidates": len(candidates), "total": total},
            )
            return

        removed = self.analytics.tombstone(
            ctx.dataset_id, ctx.version, ctx.run_id, {key: state[key][0] for key in candidates}
        )
        self.store.reset_misses(ctx.dataset_id, candidates)
        ctx.stats.records_removed += removed
        self.store.add_event(
            ctx.task.id, "records_removed",
            f"Позначено як видалені: {removed} записів (не знайдено при повному проході)",
            run_id=ctx.run_id, level="info",
            data={"keys": candidates[:50], "count": removed},
        )

    def rollback_removals(self, run_id: str) -> None:
        """Undo tombstones and miss counts of a run that did not finalize its version."""
        if self.store.run_has_version(run_id):
            return  # the version is committed: its removals are part of history now
        try:
            removed = self.analytics.rollback_tombstones(run_id)
            self.store.rollback_misses(run_id)
        except Exception:  # noqa: BLE001 - best effort on an already failing path
            logger.exception("could not roll back removals of run %s", run_id)
            return
        if removed:
            logger.warning("rolled back %d tombstones of unfinished run %s", removed, run_id)

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

    async def _notify_run_failed(self, task_id: str, run_id: str, exc: Exception) -> None:
        if self.dispatcher is None:
            return
        try:
            task = self.store.get_task(task_id)
            if task is not None:
                await self.dispatcher.dispatch_alert(
                    task,
                    run_id=run_id,
                    kind="run_failed",
                    message=f"{type(exc).__name__}: {str(exc)[:300]}",
                )
        except Exception:  # pragma: no cover - never mask the original failure
            logger.warning("failed to dispatch run_failed alert for run %s", run_id, exc_info=True)


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
