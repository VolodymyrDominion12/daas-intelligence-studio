"""Composition root: one Studio instance per process, owns storage, providers and runs."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any

from . import __version__
from .compliance import (
    ComplianceError,
    PolicyGuardFetcher,
    assert_logged_out,
    check_run,
    login_violation,
    run_scope,
)
from .config import PROJECT_ROOT, Settings, get_settings, load_runtime_token
from .db.duckdb_store import AnalyticsStore
from .db.sqlite_store import ActiveRunError, OperationalStore
from .discovery import build_search
from .enrichment.registry import build_registry, enricher_catalog
from .extraction.engine import ExtractionEngine
from .llm.router import LLMRouter
from .models import (
    ApiSourceSpec,
    DeliveryChannel,
    DeliveryFilters,
    EvalReport,
    RunInfo,
    ScheduleInfo,
    SubscriptionInfo,
    TaskInfo,
    TaskSpec,
)
from .pipeline.calibration import Calibrator
from .pipeline.delivery import DeltaDispatcher
from .pipeline.planner import Planner
from .pipeline.quality import GoldCase, load_gold_set, resolve_gold_preset_path, run_gold_eval
from .pipeline.runner import PipelineRunner
from .pipeline.scheduler import TaskScheduler, next_cron_after, validate_cron
from .scraping.fetcher import build_fetcher
from .storage.raw_cache import RawCache
from .utils import assert_public_url, iso, utcnow

logger = logging.getLogger(__name__)


class RunConflict(RuntimeError):
    pass


class Studio:
    def __init__(self, settings: Settings | None = None, *, fetcher=None) -> None:
        self.settings = settings or get_settings()
        self.paths = self.settings.paths
        self.paths.ensure()
        self.token = load_runtime_token(self.settings)

        self.store = OperationalStore(self.paths)
        self.analytics = AnalyticsStore(self.paths)
        self.cache = RawCache(self.paths.raw_cache, self.store)
        # Every request goes through the source-registry guard (P1-04).
        self.fetcher = PolicyGuardFetcher(
            fetcher or build_fetcher(self.settings, cache=self.cache), self.store
        )
        self.router = LLMRouter(self.settings, self.store)
        self.search = build_search(self.settings)
        self.registry = build_registry()
        self.engine = ExtractionEngine(self.settings, self.router)
        self.calibrator = Calibrator(self.settings, self.store, self.fetcher, self.engine)
        self.dispatcher = DeltaDispatcher(self.settings, self.store)
        self.runner = PipelineRunner(
            self.settings,
            self.store,
            self.analytics,
            self.cache,
            self.fetcher,
            self.engine,
            self.calibrator,
            self.registry,
            self.router,
            self.search,
            dispatcher=self.dispatcher,
        )
        self.planner = Planner(
            self.settings,
            self.router,
            enrichers=list(self.registry),
            search=self.search,
            store=self.store,
        )
        self.scheduler = TaskScheduler(self)
        self._jobs: dict[str, asyncio.Task] = {}

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------
    def start_scheduler(self) -> None:
        self.scheduler.start()

    async def stop_scheduler(self) -> None:
        await self.scheduler.stop()

    def interrupt_stale_runs(self) -> int:
        """Since runs live in-process, anything left 'running' is dead after restart."""
        interrupted = 0
        for run in self.store.list_unfinished_runs():
            self.store.cancel_run_jobs(run.id)
            # Removals of a run that never finalized its dataset version must not stick.
            self.runner.rollback_removals(run.id)
            self.store.update_run(
                run.id,
                status="failed",
                error="interrupted by process restart",
                finished=True,
            )
            self.store.add_event(
                run.task_id,
                "run_interrupted",
                "Запуск перервано перезапуском процесу — перезапустіть задачу",
                run_id=run.id,
                level="warning",
            )
            interrupted += 1
        return interrupted

    async def aclose(self) -> None:
        await self.stop_scheduler()
        for job in list(self._jobs.values()):
            job.cancel()
        if self._jobs:
            await asyncio.gather(*self._jobs.values(), return_exceptions=True)
        await self.dispatcher.aclose()
        await self.router.aclose()
        await self.fetcher.aclose()
        if self.search is not None:
            await self.search.aclose()
        self.analytics.close()
        self.store.close()

    # ------------------------------------------------------------------
    # tasks
    # ------------------------------------------------------------------
    async def create_task(
        self,
        prompt: str,
        seeds: list[str],
        *,
        title: str | None = None,
        api_source: ApiSourceSpec | None = None,
    ) -> TaskInfo:
        for seed in seeds:
            if seed and seed.strip():
                assert_logged_out(seed.strip())
        if api_source is not None:
            spec = await self.planner.plan(prompt, seeds, title=title, api_source=api_source)
        else:
            spec = await self.planner.plan(prompt, seeds, title=title)
        # Seeds discovered by search are validated too, before anything is persisted.
        dropped = [seed for seed in spec.seeds if login_violation(seed)]
        if dropped:
            spec.seeds = [seed for seed in spec.seeds if seed not in dropped]
            spec.assumptions.append(
                f"Відкинуто {len(dropped)} знайдених URL з токенами доступу (лише публічні сторінки)."
            )
        task = self.store.create_task(spec)
        self.store.add_event(
            task.id,
            "task_created",
            f"Задачу створено: {spec.title}",
            data={
                "columns": [column.key for column in spec.columns],
                "llm": self.router.available,
                "api_source": spec.api_source is not None,
            },
        )
        self.store.add_event(
            task.id,
            "plan_ready",
            spec.rationale or "План готовий",
            data={"spec": spec.model_dump(mode="json")},
        )
        return task

    def update_task(self, task_id: str, **changes: Any) -> TaskInfo | None:
        if "spec" in changes and isinstance(changes["spec"], dict):
            changes["spec"] = TaskSpec.model_validate(changes["spec"])
        if isinstance(changes.get("spec"), TaskSpec):
            for seed in changes["spec"].seeds:
                assert_logged_out(seed)
        return self.store.update_task(task_id, **changes)

    # ------------------------------------------------------------------
    # runs
    # ------------------------------------------------------------------
    def active_run(self, task_id: str) -> RunInfo | None:
        for run in self.store.list_runs(task_id, limit=10):
            if run.status in {"queued", "running"}:
                return run
        return None

    async def calibrate(
        self,
        task_id: str,
        *,
        force: bool = False,
        acknowledge_high_risk: bool = False,
        reason: str = "",
    ) -> RunInfo:
        task = self._task(task_id)
        self._ensure_idle(task_id, force=force)
        if not task.spec.seeds:
            raise ValueError("Додайте хоча б один seed URL перед калібруванням")
        acknowledged = self._compliance_gate(
            task, "calibrate", acknowledge_high_risk=acknowledge_high_risk, reason=reason
        )
        run = self._create_run(
            task, "calibrate", exclusive=not force, acknowledged=acknowledged, reason=reason
        )
        self.store.update_task(task_id, status="running")
        self.store.add_event(
            task_id, "run_created", "Калібрування: аналізуємо структуру сторінок",
            run_id=run.id,
        )
        self._spawn(run.id, task_id, self.runner.calibrate(task_id, run.id), acknowledged)
        return run

    async def collect(
        self,
        task_id: str,
        *,
        force: bool = False,
        skip_calibration: bool = False,
        acknowledge_high_risk: bool = False,
        reason: str = "",
    ) -> RunInfo:
        task = self._task(task_id)
        self._ensure_idle(task_id, force=force)
        if not task.spec.seeds:
            raise ValueError("Додайте хоча б один seed URL")
        acknowledged = self._compliance_gate(
            task, "collect", acknowledge_high_risk=acknowledge_high_risk, reason=reason
        )
        if (
            not skip_calibration
            and task.spec.api_source is None
            and self.store.get_calibration(task_id) is None
        ):
            raise RunConflict(
                "Спершу запустіть калібрування на тестовій вибірці — це головна економія токенів"
            )
        run = self._create_run(
            task, "collect", exclusive=not force, acknowledged=acknowledged, reason=reason
        )
        self.store.update_task(task_id, status="running")
        self.store.add_event(
            task_id, "run_created", "Збір даних запущено", run_id=run.id,
            data={"max_pages": task.spec.limits.max_pages},
        )
        self._spawn(run.id, task_id, self.runner.collect(task_id, run.id), acknowledged)
        return run

    async def enrich(self, task_id: str, column_keys: list[str] | None = None) -> RunInfo:
        task = self._task(task_id)
        self._ensure_idle(task_id, force=False)
        run = self._create_run(task, "enrich", exclusive=True)
        self.store.update_task(task_id, status="running")
        self.store.add_event(
            task_id, "run_created", "Збагачення запущено", run_id=run.id,
            data={"columns": column_keys or []},
        )
        self._spawn(run.id, task_id, self.runner.enrich(task_id, run.id, column_keys=column_keys))
        return run

    async def snapshot(self, task_id: str) -> RunInfo:
        task = self._task(task_id)
        self._ensure_idle(task_id, force=False)
        run = self._create_run(task, "snapshot", exclusive=True, budget=False)
        self.store.add_event(
            task_id, "run_created", "Створюємо снапшот та рахуємо зміни", run_id=run.id
        )
        self._spawn(run.id, task_id, self.runner.snapshot(task_id, run.id))
        return run

    async def cancel(self, run_id: str) -> RunInfo | None:
        run = self.store.get_run(run_id)
        if run is None:
            return None
        if run.status in {"queued", "running"}:
            self.store.update_run(run_id, status="cancelled", finished=True)
            self.store.cancel_run_jobs(run_id)
            self.store.add_event(
                run.task_id, "run_cancelled", "Запуск скасовано користувачем", run_id=run_id,
                level="warning",
            )
            self.store.update_task(run.task_id, status="ready")
        job = self._jobs.get(run_id)
        if job is not None:
            job.cancel()
        return self.store.get_run(run_id)

    async def wait(self, run_id: str, timeout: float | None = None) -> RunInfo | None:
        job = self._jobs.get(run_id)
        if job is not None:
            with contextlib.suppress(TimeoutError, asyncio.CancelledError):
                await asyncio.wait_for(asyncio.shield(job), timeout=timeout)
        return self.store.get_run(run_id)

    # ------------------------------------------------------------------
    # schedules
    # ------------------------------------------------------------------
    def set_schedule(
        self,
        task_id: str,
        *,
        cron: str,
        enabled: bool = True,
        catch_up: bool = True,
        acknowledge_high_risk: bool = False,
        reason: str = "",
        now: Any = None,
    ) -> ScheduleInfo:
        self._task(task_id)
        normalized = validate_cron(cron)
        if acknowledge_high_risk and len(reason.strip()) < 10:
            raise ValueError("Вкажіть причину підтвердження високого ризику (мін. 10 символів)")
        current = now or utcnow()
        next_run_at = next_cron_after(normalized, current) if enabled else None
        sched = self.store.upsert_schedule(
            task_id,
            cron=normalized,
            enabled=enabled,
            catch_up=catch_up,
            acknowledge_high_risk=acknowledge_high_risk,
            reason=reason.strip(),
            next_run_at=next_run_at,
        )
        msg = f"Оновлено розклад: {normalized}" if enabled else f"Розклад вимкнено: {normalized}"
        self.store.add_event(
            task_id,
            "schedule_updated",
            msg,
            data={
                "cron": normalized,
                "enabled": enabled,
                "catch_up": catch_up,
                "next_run_at": iso(next_run_at) if next_run_at else None,
            },
        )
        return sched

    def delete_schedule(self, task_id: str) -> bool:
        self._task(task_id)
        deleted = self.store.delete_schedule(task_id)
        if deleted:
            self.store.add_event(task_id, "schedule_deleted", "Розклад запусків видалено")
        return deleted

    # ------------------------------------------------------------------
    # subscriptions (delta delivery, D-01)
    # ------------------------------------------------------------------
    def _validate_subscription_target(self, channel: str, target: str) -> str:
        cleaned = target.strip()
        if not cleaned:
            raise ValueError("Вкажіть ціль доставки (Webhook URL або Telegram chat_id)")
        if channel == "webhook":
            assert_logged_out(cleaned)
            assert_public_url(cleaned, allow_private=self.settings.allow_private_network)
        return cleaned

    def list_subscriptions(self, task_id: str) -> list[SubscriptionInfo]:
        self._task(task_id)
        return self.store.list_subscriptions(task_id)

    def create_subscription(
        self,
        task_id: str,
        *,
        name: str = "",
        channel: DeliveryChannel,
        target: str,
        secret: str = "",
        filters: DeliveryFilters | None = None,
        enabled: bool = True,
    ) -> SubscriptionInfo:
        self._task(task_id)
        clean_target = self._validate_subscription_target(channel, target)
        sub = self.store.create_subscription(
            task_id,
            name=name.strip(),
            channel=channel,
            target=clean_target,
            secret=secret,
            filters=filters,
            enabled=enabled,
        )
        self.store.add_event(
            task_id,
            "subscription_created",
            f"Створено підписку доставки ({channel}): {sub.name or sub.id}",
            data={"subscription_id": sub.id, "channel": channel},
        )
        return sub

    def update_subscription(
        self,
        task_id: str,
        sub_id: str,
        *,
        name: str | None = None,
        target: str | None = None,
        secret: str | None = None,
        filters: DeliveryFilters | None = None,
        enabled: bool | None = None,
    ) -> SubscriptionInfo | None:
        self._task(task_id)
        existing = self.store.get_subscription(sub_id)
        if existing is None or existing.task_id != task_id:
            return None
        clean_target = (
            self._validate_subscription_target(existing.channel, target)
            if target is not None
            else None
        )
        updated = self.store.update_subscription(
            task_id,
            sub_id,
            name=name.strip() if name is not None else None,
            target=clean_target,
            secret=secret,
            filters=filters,
            enabled=enabled,
        )
        if updated is not None:
            self.store.add_event(
                task_id,
                "subscription_updated",
                f"Оновлено підписку доставки ({updated.channel}): {updated.name or updated.id}",
                data={"subscription_id": updated.id, "enabled": updated.enabled},
            )
        return updated

    def delete_subscription(self, task_id: str, sub_id: str) -> bool:
        self._task(task_id)
        deleted = self.store.delete_subscription(task_id, sub_id)
        if deleted:
            self.store.add_event(
                task_id,
                "subscription_deleted",
                f"Видалено підписку доставки ({sub_id})",
                data={"subscription_id": sub_id},
            )
        return deleted

    async def test_subscription(self, task_id: str, sub_id: str) -> tuple[bool, str]:
        task = self._task(task_id)
        sub = self.store.get_subscription(sub_id)
        if sub is None or sub.task_id != task_id:
            raise KeyError(f"subscription not found: {sub_id}")
        return await self.dispatcher.send_test(task, sub)

    # ------------------------------------------------------------------
    # quality & gold-set eval (P1-01)
    # ------------------------------------------------------------------
    async def evaluate_task(
        self,
        task_id: str,
        *,
        cases: list[GoldCase] | None = None,
        gold_preset: str | None = None,
        min_f1: float | None = None,
    ) -> EvalReport:
        task = self._task(task_id)
        threshold = self.settings.eval_min_f1 if min_f1 is None else min_f1

        if cases:
            eval_cases = cases
            gold_name = "inline"
        elif gold_preset is not None:
            path = resolve_gold_preset_path(
                gold_preset,
                [
                    self.paths.root / "gold",
                    PROJECT_ROOT / "backend" / "tests" / "fixtures" / "gold",
                ],
            )
            dataset = load_gold_set(path)
            eval_cases = dataset.cases
            gold_name = dataset.name
        else:
            raise ValueError("Потрібно передати cases або gold_preset")

        report = await run_gold_eval(
            task.spec,
            eval_cases,
            engine=self.engine,
            store=self.store,
            min_f1=threshold,
            gold_name=gold_name,
        )
        dataset_id = task.dataset_id or task.id
        version = self.store.update_latest_version_manifest(
            dataset_id, {"eval": report.model_dump(mode="json")}
        )
        if version is not None:
            report = report.model_copy(update={"dataset_version": version})
            self.store.update_latest_version_manifest(
                dataset_id, {"eval": report.model_dump(mode="json")}
            )

        self.store.add_event(
            task.id,
            "eval_completed",
            (
                f"Gold eval ({report.gold_name}): F1={report.f1:.2%}, "
                f"P={report.precision:.2%}, R={report.recall:.2%} "
                f"({'пройдено' if report.passed else 'нижче порогу'})"
            ),
            level="info" if report.passed else "warning",
            data={
                "gold_name": report.gold_name,
                "passed": report.passed,
                "f1": report.f1,
                "precision": report.precision,
                "recall": report.recall,
                "min_f1": report.min_f1,
                "cases": report.cases,
                "records_expected": report.records_expected,
                "records_extracted": report.records_extracted,
                "llm_fallback_pct": report.llm_fallback_pct,
                "usd_per_1k_records": report.usd_per_1k_records,
                "dataset_version": report.dataset_version,
            },
        )
        return report

    # ------------------------------------------------------------------
    # info
    # ------------------------------------------------------------------
    def capabilities(self) -> dict[str, Any]:
        return {
            "version": __version__,
            "offline": self.settings.offline,
            "auth_required": not self.settings.disable_auth,
            "fetch_engine": self.fetcher.name,
            "respect_robots": self.settings.respect_robots,
            "llm_ready": self.router.available,
            "llm_providers": self.router.description,
            "llm_models": {
                "primary": self.settings.llm_model,
                "local": self.settings.llm_local_model,
            },
            "search_provider": self.search.name if self.search else None,
            "enrichers": enricher_catalog(),
            "storage": str(self.paths.root),
            "budget_usd_default": self.settings.llm_budget_usd,
            "max_pages_default": self.settings.max_pages_per_run,
            "scheduler_enabled": self.settings.scheduler_enabled,
        }

    def overview(self) -> dict[str, Any]:
        data = self.store.overview()
        data["cache"] = self.cache.stats()
        return data

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------
    def _task(self, task_id: str) -> TaskInfo:
        task = self.store.get_task(task_id)
        if task is None:
            raise KeyError(f"task not found: {task_id}")
        return task

    def _compliance_gate(
        self, task: TaskInfo, kind: str, *, acknowledge_high_risk: bool, reason: str
    ) -> list[str]:
        try:
            decision = check_run(
                self.store,
                task.spec.seeds,
                acknowledge_high_risk=acknowledge_high_risk,
                reason=reason,
            )
        except ComplianceError as exc:
            self.store.add_event(
                task.id, "compliance_blocked", str(exc), level="error",
                data={"code": exc.code, "domains": exc.domains, "run_kind": kind},
            )
            raise
        return decision.acknowledged

    def _create_run(
        self,
        task: TaskInfo,
        kind: str,
        *,
        exclusive: bool,
        acknowledged: list[str] | None = None,
        reason: str = "",
        budget: bool = True,
    ) -> RunInfo:
        """Create the run atomically with its compliance audit record (if any).

        ``exclusive`` makes the active-run check part of the same SQLite transaction, so two
        overlapping requests cannot both start a run for one task.
        """
        audit = None
        if acknowledged:
            audit = (
                "compliance_override",
                "warning",
                "Запуск по джерелах високого ризику підтверджено: " + ", ".join(acknowledged),
                {"domains": acknowledged, "reason": reason.strip(), "at": utcnow().isoformat()},
            )
        try:
            return self.store.create_run(
                task.id,
                kind,
                budget_usd=task.spec.limits.budget_usd if budget else 0.0,
                exclusive=exclusive,
                audit=audit,
            )
        except ActiveRunError as exc:
            raise RunConflict(str(exc)) from exc

    def _ensure_idle(self, task_id: str, *, force: bool) -> None:
        if force:
            return
        active = self.active_run(task_id)
        if active is not None:
            raise RunConflict(f"Задача вже виконується (run {active.id}, {active.status})")

    def _spawn(
        self, run_id: str, task_id: str, coro, acknowledged: list[str] | None = None
    ) -> None:
        job = asyncio.create_task(
            self._guard(run_id, task_id, coro, tuple(acknowledged or ()))
        )
        self._jobs[run_id] = job
        job.add_done_callback(lambda _future: self._jobs.pop(run_id, None))

    async def _guard(
        self, run_id: str, task_id: str, coro, acknowledged: tuple[str, ...] = ()
    ) -> None:
        try:
            # The scope tells the fetch guard which run is fetching and which high-risk
            # domains were acknowledged for it (contextvars are per asyncio task).
            with run_scope(task_id, run_id, acknowledged):
                await coro
        except asyncio.CancelledError:
            logger.info("run %s cancelled", run_id)
            self.store.update_run(
                run_id, status="cancelled", error="cancelled", finished=True
            )
        except Exception as exc:  # noqa: BLE001 - runner persisted details already
            logger.exception("run %s failed", run_id)
            run = self.store.get_run(run_id)
            if run is not None and run.status not in {"failed", "cancelled"}:
                self.store.update_run(run_id, status="failed", error=str(exc)[:500], finished=True)
                self.store.add_event(
                    task_id, "run_failed", f"{type(exc).__name__}: {exc}",
                    run_id=run_id, level="error",
                )
