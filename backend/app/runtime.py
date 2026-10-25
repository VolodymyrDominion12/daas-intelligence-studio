"""Composition root: one Studio instance per process, owns storage, providers and runs."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any

from . import __version__
from .config import Settings, get_settings, load_runtime_token
from .db.duckdb_store import AnalyticsStore
from .db.sqlite_store import OperationalStore
from .discovery import build_search
from .enrichment.registry import build_registry, enricher_catalog
from .extraction.engine import ExtractionEngine
from .llm.router import LLMRouter
from .models import RunInfo, TaskInfo, TaskSpec
from .pipeline.calibration import Calibrator
from .pipeline.planner import Planner
from .pipeline.runner import PipelineRunner
from .scraping.fetcher import build_fetcher
from .storage.raw_cache import RawCache

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
        self.fetcher = fetcher or build_fetcher(self.settings, cache=self.cache)
        self.router = LLMRouter(self.settings, self.store)
        self.search = build_search(self.settings)
        self.registry = build_registry()
        self.engine = ExtractionEngine(self.settings, self.router)
        self.calibrator = Calibrator(self.settings, self.store, self.fetcher, self.engine)
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
        )
        self.planner = Planner(
            self.settings,
            self.router,
            enrichers=list(self.registry),
            search=self.search,
            store=self.store,
        )
        self._jobs: dict[str, asyncio.Task] = {}

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------
    def interrupt_stale_runs(self) -> int:
        """Since runs live in-process, anything left 'running' is dead after restart."""
        interrupted = 0
        for run in self.store.list_unfinished_runs():
            self.store.cancel_run_jobs(run.id)
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
        for job in list(self._jobs.values()):
            job.cancel()
        if self._jobs:
            await asyncio.gather(*self._jobs.values(), return_exceptions=True)
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
        self, prompt: str, seeds: list[str], *, title: str | None = None
    ) -> TaskInfo:
        spec = await self.planner.plan(prompt, seeds, title=title)
        task = self.store.create_task(spec)
        self.store.add_event(
            task.id,
            "task_created",
            f"Задачу створено: {spec.title}",
            data={"columns": [column.key for column in spec.columns], "llm": self.router.available},
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
        return self.store.update_task(task_id, **changes)

    # ------------------------------------------------------------------
    # runs
    # ------------------------------------------------------------------
    def active_run(self, task_id: str) -> RunInfo | None:
        for run in self.store.list_runs(task_id, limit=10):
            if run.status in {"queued", "running"}:
                return run
        return None

    async def calibrate(self, task_id: str, *, force: bool = False) -> RunInfo:
        task = self._task(task_id)
        self._ensure_idle(task_id, force=force)
        if not task.spec.seeds:
            raise ValueError("Додайте хоча б один seed URL перед калібруванням")
        run = self.store.create_run(task_id, "calibrate", budget_usd=task.spec.limits.budget_usd)
        self.store.update_task(task_id, status="running")
        self.store.add_event(
            task_id, "run_created", "Калібрування: аналізуємо структуру сторінок",
            run_id=run.id,
        )
        self._spawn(run.id, task_id, self.runner.calibrate(task_id, run.id))
        return run

    async def collect(self, task_id: str, *, force: bool = False, skip_calibration: bool = False) -> RunInfo:
        task = self._task(task_id)
        self._ensure_idle(task_id, force=force)
        if not task.spec.seeds:
            raise ValueError("Додайте хоча б один seed URL")
        if not skip_calibration and self.store.get_calibration(task_id) is None:
            raise RunConflict(
                "Спершу запустіть калібрування на тестовій вибірці — це головна економія токенів"
            )
        run = self.store.create_run(task_id, "collect", budget_usd=task.spec.limits.budget_usd)
        self.store.update_task(task_id, status="running")
        self.store.add_event(
            task_id, "run_created", "Збір даних запущено", run_id=run.id,
            data={"max_pages": task.spec.limits.max_pages},
        )
        self._spawn(run.id, task_id, self.runner.collect(task_id, run.id))
        return run

    async def enrich(self, task_id: str, column_keys: list[str] | None = None) -> RunInfo:
        task = self._task(task_id)
        self._ensure_idle(task_id, force=False)
        run = self.store.create_run(task_id, "enrich", budget_usd=task.spec.limits.budget_usd)
        self.store.update_task(task_id, status="running")
        self.store.add_event(
            task_id, "run_created", "Збагачення запущено", run_id=run.id,
            data={"columns": column_keys or []},
        )
        self._spawn(run.id, task_id, self.runner.enrich(task_id, run.id, column_keys=column_keys))
        return run

    async def snapshot(self, task_id: str) -> RunInfo:
        self._task(task_id)
        self._ensure_idle(task_id, force=False)
        run = self.store.create_run(task_id, "snapshot")
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

    def _ensure_idle(self, task_id: str, *, force: bool) -> None:
        if force:
            return
        active = self.active_run(task_id)
        if active is not None:
            raise RunConflict(f"Задача вже виконується (run {active.id}, {active.status})")

    def _spawn(self, run_id: str, task_id: str, coro) -> None:
        job = asyncio.create_task(self._guard(run_id, task_id, coro))
        self._jobs[run_id] = job
        job.add_done_callback(lambda _future: self._jobs.pop(run_id, None))

    async def _guard(self, run_id: str, task_id: str, coro) -> None:
        try:
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
