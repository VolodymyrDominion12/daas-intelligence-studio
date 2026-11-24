"""Composition root: one Studio instance per process, owns storage, providers and runs."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
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
from .db.sqlite_store import _UNSET, ActiveRunError, OperationalStore
from .discovery import build_search
from .enrichment.dedupe import VALID_MERGE_POLICIES, find_duplicates, merge_records
from .enrichment.registry import build_registry, enricher_catalog
from .extraction.engine import ExtractionEngine
from .llm.router import LLMRouter
from .models import (
    ApiSourceSpec,
    CalibrationReport,
    DeliveryChannel,
    DeliveryFilters,
    EnrichmentSpec,
    EvalReport,
    ExtractedRecord,
    FieldSpec,
    MergePolicy,
    Provenance,
    RunInfo,
    ScheduleInfo,
    SubscriptionInfo,
    TaskInfo,
    TaskSpec,
)
from .pipeline.calibration import (
    Calibrator,
    build_calibration_history_entry,
    compute_schema_diff,
)
from .pipeline.delivery import DeltaDispatcher
from .pipeline.planner import Planner
from .pipeline.quality import GoldCase, load_gold_set, resolve_gold_preset_path, run_gold_eval
from .pipeline.runner import PipelineRunner
from .pipeline.scheduler import TaskScheduler, next_cron_after, validate_cron
from .scraping.fetcher import build_fetcher
from .scraping.js_render import JsRenderStats, build_sidecar_client
from .scraping.resilience import DomainCircuitBreaker, build_proxy_provider
from .storage.raw_cache import RawCache
from .utils import (
    assert_public_url,
    coerce_value,
    hash_text,
    iso,
    json_dumps,
    normalize_domain,
    record_key_for,
    utcnow,
)

logger = logging.getLogger(__name__)


class RunConflict(RuntimeError):
    pass


class MergeConflictError(RuntimeError):
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
        self.proxy_provider = build_proxy_provider(self.settings)
        self.circuit_breaker = DomainCircuitBreaker(
            threshold=self.settings.circuit_breaker_threshold,
            cooldown_s=self.settings.circuit_breaker_cooldown_s,
        )
        self.js_stats = getattr(fetcher, "js_stats", None) or JsRenderStats()
        self.sidecar_client = getattr(fetcher, "sidecar_client", None) or build_sidecar_client(
            self.settings
        )
        # Every request goes through the source-registry guard (P1-04).
        self.fetcher = PolicyGuardFetcher(
            fetcher
            or build_fetcher(
                self.settings,
                cache=self.cache,
                proxy_provider=self.proxy_provider,
                circuit_breaker=self.circuit_breaker,
                sidecar_client=self.sidecar_client,
                js_stats=self.js_stats,
            ),
            self.store,
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
            circuit_breaker=self.circuit_breaker,
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
        if self.sidecar_client is not None:
            await self.sidecar_client.aclose()
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
        plan_ref = f"plan:{uuid.uuid4().hex[:12]}"
        with run_scope(None, plan_ref):
            if api_source is not None:
                spec = await self.planner.plan(
                    prompt, seeds, title=title, api_source=api_source
                )
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
        self.store.bind_llm_calls(plan_ref, task.id)
        plan_usd = round(self.store.run_spend(plan_ref), 6)
        self.store.add_event(
            task.id,
            "task_created",
            f"Задачу створено: {spec.title}",
            data={
                "columns": [column.key for column in spec.columns],
                "llm": self.router.available,
                "api_source": spec.api_source is not None,
                "plan_llm_usd": plan_usd,
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
            for column in changes["spec"].columns:
                self._attach_enricher_to_spec(changes["spec"], column)
        return self.store.update_task(task_id, **changes)

    @staticmethod
    def _attach_enricher_to_spec(spec: TaskSpec, field: FieldSpec) -> None:
        mapping = {
            "website": "domain_resolver",
            "email": "site_audit",
            "phone": "site_audit",
            "tech_stack": "site_audit",
            "has_online_booking": "site_audit",
            "has_chat": "site_audit",
            "socials": "site_audit",
        }
        if field.method != "enrichment" and field.key not in mapping:
            return
        enricher_key = mapping.get(field.key, "site_audit")
        for item in spec.enrichment:
            if item.enricher == enricher_key:
                if field.key not in item.fields:
                    item.fields.append(field.key)
                return
        spec.enrichment.append(EnrichmentSpec(enricher=enricher_key, fields=[field.key]))

    def update_column(self, task_id: str, key: str, **updates: Any) -> TaskInfo:
        task = self._task(task_id)
        spec = task.spec.model_copy(deep=True)
        index = next((idx for idx, col in enumerate(spec.columns) if col.key == key), None)
        if index is None:
            raise KeyError(f"Колонку {key} не знайдено")
        current = spec.columns[index]
        merged_data = {**current.model_dump(), **updates}
        merged_data["key"] = current.key
        updated_field = FieldSpec.model_validate(merged_data)
        spec.columns[index] = updated_field
        self._attach_enricher_to_spec(spec, updated_field)
        validated = TaskSpec.model_validate(spec.model_dump())
        saved = self.store.update_task(task_id, spec=validated)
        assert saved is not None
        self.store.add_event(
            task_id,
            "column_updated",
            f"Оновлено колонку «{updated_field.title}»",
            data={"key": key, "fields": sorted(updates.keys())},
        )
        return saved

    def reorder_columns(self, task_id: str, keys: list[str]) -> TaskInfo:
        task = self._task(task_id)
        spec = task.spec.model_copy(deep=True)
        existing_map = {col.key: col for col in spec.columns}
        if len(keys) != len(spec.columns) or len(set(keys)) != len(keys) or set(keys) != set(existing_map):
            raise ValueError("Список ключів має точно відповідати наявним колонкам задачі")
        spec.columns = [existing_map[col_key] for col_key in keys]
        validated = TaskSpec.model_validate(spec.model_dump())
        saved = self.store.update_task(task_id, spec=validated)
        assert saved is not None
        self.store.add_event(
            task_id,
            "columns_reordered",
            "Змінено порядок колонок задачі",
            data={"keys": list(keys)},
        )
        return saved

    def calibration_state(self, task_id: str) -> dict[str, Any]:
        raw_history = self.store.list_calibration_history(task_id, limit=20)
        history_entries = [build_calibration_history_entry(entry) for entry in raw_history]

        current_report: CalibrationReport | None = None
        current_version: int | None = None
        prev_report: CalibrationReport | None = None
        prev_version: int | None = None

        if raw_history:
            current_report = CalibrationReport.model_validate_json(raw_history[0]["report_json"])
            current_version = int(raw_history[0]["version"])
            if len(raw_history) >= 2:
                prev_report = CalibrationReport.model_validate_json(raw_history[1]["report_json"])
                prev_version = int(raw_history[1]["version"])
        else:
            raw = self.store.get_calibration(task_id)
            if raw:
                current_report = CalibrationReport.model_validate_json(raw)
                current_version = 1

        diff = (
            compute_schema_diff(
                prev_report,
                current_report,
                current_version=current_version or 1,
                previous_version=prev_version,
            )
            if current_report is not None
            else None
        )
        return {
            "calibration": (
                current_report.model_dump(mode="json", by_alias=True)
                if current_report is not None
                else None
            ),
            "version": current_version,
            "history": [entry.model_dump(mode="json") for entry in history_entries],
            "diff": diff.model_dump(mode="json") if diff is not None else None,
        }

    def rollback_calibration(self, task_id: str, *, version: int | None = None) -> dict[str, Any]:
        task = self._task(task_id)
        self._ensure_idle(task_id, force=False)
        raw_history = self.store.list_calibration_history(task_id, limit=50)
        if version is None:
            if len(raw_history) < 2:
                raise ValueError("Немає попередньої версії калібрування для відкату")
            target_entry = raw_history[1]
        else:
            if not raw_history:
                raise ValueError("Немає попередньої версії калібрування для відкату")
            target_entry = self.store.get_calibration_entry(task_id, version)
            if target_entry is None:
                raise KeyError(f"Версію калібрування v{version} не знайдено")
            if int(target_entry["version"]) == int(raw_history[0]["version"]):
                raise ValueError("Ця версія калібрування вже є активною")

        restored = CalibrationReport.model_validate_json(target_entry["report_json"])
        from_version = int(raw_history[0]["version"])
        to_version = int(target_entry["version"])
        new_version = self.store.save_calibration(
            task_id,
            target_entry.get("run_id"),
            restored.model_dump_json(by_alias=True),
            spec_json=str(target_entry.get("spec_json") or task.spec.model_dump_json()),
            source="rollback",
        )

        signature = f"auto:{task.spec.entity}"
        for seed in task.spec.seeds[:1]:
            domain = normalize_domain(seed)
            if not domain:
                continue
            if restored.schema_ is not None and restored.schema_.fields:
                self.store.save_selector(domain, signature, restored.schema_)
            else:
                self.store.delete_selector(domain, signature)

        has_rows = self.analytics.count_records(task.dataset_id or task.id) > 0
        next_status = "done" if has_rows else ("ready" if restored.records_found > 0 else "draft")
        updated_task = self.store.update_task(task_id, status=next_status)
        assert updated_task is not None
        self.store.add_event(
            task_id,
            "calibration_rolled_back",
            f"Схему калібрування відкочено до v{to_version} (нова версія v{new_version})",
            level="info",
            data={
                "from_version": from_version,
                "to_version": to_version,
                "new_version": new_version,
                "strategy": restored.strategy,
                "container": restored.schema_.container if restored.schema_ else None,
            },
        )
        state = self.calibration_state(task_id)
        return {
            **state,
            "rolled_back_to": to_version,
            "task": updated_task.model_dump(mode="json"),
        }

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
        eff_filters = filters or DeliveryFilters()
        next_digest = (
            next_cron_after(eff_filters.digest_cron, utcnow())
            if eff_filters.delivery_mode == "digest_daily"
            else None
        )
        sub = self.store.create_subscription(
            task_id,
            name=name.strip(),
            channel=channel,
            target=clean_target,
            secret=secret,
            filters=eff_filters,
            enabled=enabled,
            next_digest_at=next_digest,
        )
        self.store.add_event(
            task_id,
            "subscription_created",
            f"Створено підписку доставки ({channel}): {sub.name or sub.id}",
            data={"subscription_id": sub.id, "channel": channel, "mode": eff_filters.delivery_mode},
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
        next_digest: Any = _UNSET
        if filters is not None:
            if filters.delivery_mode == "digest_daily":
                if (
                    existing.filters.delivery_mode != "digest_daily"
                    or existing.filters.digest_cron != filters.digest_cron
                    or existing.next_digest_at is None
                ):
                    next_digest = next_cron_after(filters.digest_cron, utcnow())
            else:
                next_digest = None

        updated = self.store.update_subscription(
            task_id,
            sub_id,
            name=name.strip() if name is not None else None,
            target=clean_target,
            secret=secret,
            filters=filters,
            enabled=enabled,
            next_digest_at=next_digest,
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

    async def flush_subscription(self, task_id: str, sub_id: str) -> tuple[bool, int, str]:
        task = self._task(task_id)
        sub = self.store.get_subscription(sub_id)
        if sub is None or sub.task_id != task_id:
            raise KeyError(f"subscription not found: {sub_id}")
        return await self.dispatcher.flush_subscription(task, sub)

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
    # quarantine management (P1-08)
    # ------------------------------------------------------------------
    def list_quarantine(
        self, task_id: str, *, limit: int = 50, offset: int = 0
    ) -> dict[str, Any]:
        task = self._task(task_id)
        dataset_id = task.dataset_id or task.id
        return self.analytics.list_quarantine(dataset_id, limit=limit, offset=offset)

    def approve_quarantine(self, task_id: str, item_id: str) -> dict[str, Any]:
        task = self._task(task_id)
        dataset_id = task.dataset_id or task.id
        item = self.analytics.get_quarantine_item(dataset_id, item_id)
        if item is None:
            raise KeyError(f"Запис у карантині не знайдено: {item_id}")

        raw_payload = dict(item.get("payload") or {})
        col_map = task.spec.column_map
        payload: dict[str, Any] = {}
        provenance: dict[str, Provenance] = {}
        now = utcnow()
        source_url = str(item.get("source_url") or "")
        reason = str(item.get("reason") or "")

        for key, value in raw_payload.items():
            if value in (None, "", []):
                continue
            col = col_map.get(key)
            if col is not None:
                coerced = coerce_value(value, col.dtype)
                payload[key] = coerced if coerced not in (None, "", []) else value
            else:
                payload[key] = value
            provenance[key] = Provenance(
                method="manual",
                confidence=0.8,
                source_url=source_url or None,
                observed_at=now,
                verified=True,
                note=f"Підтверджено з карантину: {reason[:160]}" if reason else "Підтверджено з карантину",
            )

        if not payload:
            raise ValueError("Неможливо підтвердити порожній запис із карантину")

        record_key = record_key_for(payload, fallback=source_url or item_id)
        record = ExtractedRecord(
            record_key=record_key,
            payload=payload,
            provenance=provenance,
            source_url=source_url,
            content_hash=hash_text(json_dumps(payload)),
        )

        dataset = self.store.dataset(dataset_id)
        version = dataset.version if dataset and dataset.version > 0 else 1
        self.analytics.append_records(dataset_id, version, item.get("run_id"), [record])
        self.analytics.delete_quarantine_item(dataset_id, item_id)

        row_count = self.analytics.count_records(dataset_id)
        if dataset is None or dataset.version == 0:
            self.store.add_dataset_version(
                dataset_id,
                1,
                item.get("run_id"),
                row_count,
                {
                    "task_id": task_id,
                    "dataset_version": 1,
                    "rows": row_count,
                    "approved_from_quarantine": True,
                },
            )
        else:
            self.store.update_version_row_count(dataset_id, version, row_count)

        if task.status in {"draft", "failed", "ready"} and row_count > 0:
            self.store.update_task(task_id, status="done")

        self.store.add_event(
            task_id,
            "quarantine_approved",
            f"Запис підтверджено з карантину ({record_key})",
            run_id=item.get("run_id"),
            level="info",
            data={"item_id": item_id, "record_key": record_key, "version": version},
        )
        saved = self.analytics.get_record(dataset_id, record_key)
        return {"record": saved, "row_count": row_count}

    def dismiss_quarantine(self, task_id: str, item_id: str) -> bool:
        task = self._task(task_id)
        dataset_id = task.dataset_id or task.id
        item = self.analytics.get_quarantine_item(dataset_id, item_id)
        if item is None:
            raise KeyError(f"Запис у карантині не знайдено: {item_id}")
        self.analytics.delete_quarantine_item(dataset_id, item_id)
        self.store.add_event(
            task_id,
            "quarantine_dismissed",
            f"Запис відхилено з карантину ({item_id})",
            run_id=item.get("run_id"),
            level="info",
            data={"item_id": item_id, "reason": item.get("reason", "")},
        )
        return True

    # ------------------------------------------------------------------
    # entity resolution & merge/unmerge (P1-02)
    # ------------------------------------------------------------------
    @staticmethod
    def _provenance_map(raw: dict[str, Any] | None) -> dict[str, Provenance]:
        result: dict[str, Provenance] = {}
        for key, value in (raw or {}).items():
            if isinstance(value, Provenance):
                result[key] = value
                continue
            try:
                result[key] = Provenance.model_validate(value)
            except Exception:  # noqa: BLE001
                continue
        return result

    def _sync_dataset_row_count(self, dataset_id: str, version: int) -> int:
        row_count = self.analytics.count_records(dataset_id)
        dataset = self.store.dataset(dataset_id)
        if dataset is None or dataset.version == 0:
            self.store.add_dataset_version(
                dataset_id,
                1,
                None,
                row_count,
                {"dataset_version": 1, "rows": row_count},
            )
        else:
            self.store.update_version_row_count(dataset_id, version, row_count)
        return row_count

    def list_duplicates(self, task_id: str, *, threshold: float = 88.0) -> dict[str, Any]:
        task = self._task(task_id)
        dataset_id = task.dataset_id or task.id
        rows = self.analytics.all_current(dataset_id)
        excluded = self.store.excluded_duplicate_pairs(task_id)
        candidates = find_duplicates(rows, threshold=threshold, excluded_pairs=excluded)
        links = self.store.list_entity_links(task_id)
        return {
            "items": [candidate.model_dump(mode="json") for candidate in candidates],
            "links": [link.model_dump(mode="json") for link in links],
            "scanned": len(rows),
            "note": "Кандидати на злиття. Оберіть політику, відхиліть пару або роз'єднайте активний зв'язок.",
        }

    def merge_duplicates(
        self,
        task_id: str,
        *,
        canonical_key: str,
        merged_key: str,
        policy: MergePolicy = "confidence",
        score: float = 0.0,
        rule: str = "manual",
        reviewer: str = "user",
        field_overrides: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        task = self._task(task_id)
        canonical_key = canonical_key.strip()
        merged_key = merged_key.strip()
        if not canonical_key or not merged_key:
            raise ValueError("Ключі записів не можуть бути порожніми")
        if canonical_key == merged_key:
            raise ValueError("Неможливо злити запис сам із собою")
        if policy not in VALID_MERGE_POLICIES:
            raise ValueError(f"Невідома політика злиття: {policy}")

        redirects = self.store.active_merged_key_map(task_id)
        if merged_key in redirects:
            raise MergeConflictError(
                f"Запис {merged_key} уже злито в {redirects[merged_key]}"
            )
        if canonical_key in redirects:
            raise MergeConflictError(
                f"Цільовий запис {canonical_key} уже поглинуто записом {redirects[canonical_key]}"
            )

        dataset_id = task.dataset_id or task.id
        left = self.analytics.get_current_revision_info(dataset_id, canonical_key)
        if left is None:
            raise KeyError(f"Запис не знайдено: {canonical_key}")
        right = self.analytics.get_current_revision_info(dataset_id, merged_key)
        if right is None:
            raise KeyError(f"Запис не знайдено: {merged_key}")

        merged_payload, merged_prov = merge_records(
            left, right, policy=policy, field_overrides=field_overrides
        )
        dataset = self.store.dataset(dataset_id)
        version = dataset.version if dataset and dataset.version > 0 else 1
        source_url = str(left.get("source_url") or right.get("source_url") or "")

        merged_record = ExtractedRecord(
            record_key=canonical_key,
            payload=merged_payload,
            provenance=merged_prov,
            source_url=source_url,
            content_hash=hash_text(json_dumps(merged_payload)),
        )
        self.analytics.append_records(dataset_id, version, None, [merged_record])
        self.analytics.tombstone(
            dataset_id, version, None, {merged_key: int(right["revision"])}
        )
        row_count = self._sync_dataset_row_count(dataset_id, version)

        link = self.store.create_entity_link(
            task_id,
            canonical_key,
            merged_key,
            status="merged",
            score=score,
            rule=rule,
            policy=policy,
            reviewer=reviewer,
            canonical_before_rev=int(left["revision"]),
            merged_before_rev=int(right["revision"]),
        )
        self.store.add_event(
            task_id,
            "entity_merged",
            f"Злито {merged_key} → {canonical_key} ({policy})",
            level="info",
            data={
                "link_id": link.id,
                "canonical_key": canonical_key,
                "merged_key": merged_key,
                "policy": policy,
                "score": score,
                "rule": rule,
            },
        )
        saved = self.analytics.get_record(dataset_id, canonical_key)
        return {"link": link.model_dump(mode="json"), "record": saved, "row_count": row_count}

    def dismiss_duplicate(
        self,
        task_id: str,
        *,
        left_key: str,
        right_key: str,
        score: float = 0.0,
        rule: str = "manual",
        reviewer: str = "user",
    ) -> dict[str, Any]:
        task = self._task(task_id)
        left_key = left_key.strip()
        right_key = right_key.strip()
        if not left_key or not right_key:
            raise ValueError("Ключі записів не можуть бути порожніми")
        if left_key == right_key:
            raise ValueError("Неможливо відхилити пару з однакових ключів")

        dataset_id = task.dataset_id or task.id
        if self.analytics.get_record(dataset_id, left_key) is None:
            raise KeyError(f"Запис не знайдено: {left_key}")
        if self.analytics.get_record(dataset_id, right_key) is None:
            raise KeyError(f"Запис не знайдено: {right_key}")

        existing = self.store.find_active_pair_link(task_id, left_key, right_key)
        if existing is not None:
            if existing.status == "merged":
                raise MergeConflictError("Пара вже злита в канонічний запис")
            return {"link": existing.model_dump(mode="json")}

        link = self.store.create_entity_link(
            task_id,
            left_key,
            right_key,
            status="dismissed",
            score=score,
            rule=rule,
            reviewer=reviewer,
        )
        self.store.add_event(
            task_id,
            "duplicate_dismissed",
            f"Відхилено дублікат {left_key} ↔ {right_key}",
            level="info",
            data={
                "link_id": link.id,
                "left_key": left_key,
                "right_key": right_key,
                "score": score,
                "rule": rule,
            },
        )
        return {"link": link.model_dump(mode="json")}

    def unmerge_link(self, task_id: str, link_id: str) -> dict[str, Any]:
        task = self._task(task_id)
        link = self.store.get_entity_link(task_id, link_id)
        if link is None:
            raise KeyError(f"Зв'язок не знайдено: {link_id}")
        if link.status != "merged":
            raise MergeConflictError(
                f"Зв'язок {link_id} уже не є активним злиттям (status={link.status})"
            )
        if link.canonical_before_rev is None or link.merged_before_rev is None:
            raise KeyError("У зв'язку відсутні ревізії для відновлення")

        dataset_id = task.dataset_id or task.id
        canon_before = self.analytics.get_record_revision(
            dataset_id, link.canonical_key, link.canonical_before_rev
        )
        merged_before = self.analytics.get_record_revision(
            dataset_id, link.merged_key, link.merged_before_rev
        )
        if canon_before is None or merged_before is None:
            raise KeyError("Не знайдено історичні ревізії для роз'єднання")

        link_rev = link.canonical_before_rev or 0
        later_links = [
            other
            for other in self.store.list_active_merges_for_canonical(task_id, link.canonical_key)
            if other.id != link.id
            and (
                (other.canonical_before_rev or 0) > link_rev
                or (
                    (other.canonical_before_rev or 0) == link_rev
                    and other.created_at > link.created_at
                )
            )
        ]
        canon_payload = dict(canon_before.get("payload") or {})
        canon_prov = self._provenance_map(canon_before.get("provenance"))
        canon_source = str(canon_before.get("source_url") or "")

        for other in later_links:
            if other.merged_before_rev is None:
                continue
            other_snap = self.analytics.get_record_revision(
                dataset_id, other.merged_key, other.merged_before_rev
            )
            if other_snap is None:
                continue
            canon_payload, canon_prov = merge_records(
                {
                    "record_key": link.canonical_key,
                    "payload": canon_payload,
                    "provenance": canon_prov,
                    "source_url": canon_source,
                },
                other_snap,
                policy=other.policy,
            )

        canon_record = ExtractedRecord(
            record_key=link.canonical_key,
            payload=canon_payload,
            provenance=canon_prov,
            source_url=canon_source,
            content_hash=hash_text(json_dumps(canon_payload)),
        )
        restored_payload = dict(merged_before.get("payload") or {})
        restored_prov = self._provenance_map(merged_before.get("provenance"))
        restored_source = str(merged_before.get("source_url") or "")
        restored_record = ExtractedRecord(
            record_key=link.merged_key,
            payload=restored_payload,
            provenance=restored_prov,
            source_url=restored_source,
            content_hash=hash_text(json_dumps(restored_payload)),
        )

        dataset = self.store.dataset(dataset_id)
        version = dataset.version if dataset and dataset.version > 0 else 1
        self.analytics.append_records(dataset_id, version, None, [canon_record, restored_record])
        updated_link = self.store.mark_entity_link_unmerged(task_id, link.id)
        assert updated_link is not None
        row_count = self._sync_dataset_row_count(dataset_id, version)

        self.store.add_event(
            task_id,
            "entity_unmerged",
            f"Роз'єднано {link.canonical_key} та {link.merged_key}",
            level="info",
            data={
                "link_id": link.id,
                "canonical_key": link.canonical_key,
                "merged_key": link.merged_key,
            },
        )
        return {
            "link": updated_link.model_dump(mode="json"),
            "canonical_record": self.analytics.get_record(dataset_id, link.canonical_key),
            "restored_record": self.analytics.get_record(dataset_id, link.merged_key),
            "row_count": row_count,
        }

    # ------------------------------------------------------------------
    # proxy & circuit resilience (P1-06) + JS renderer (P1-07)
    # ------------------------------------------------------------------
    def js_renderer_status(self) -> dict[str, Any]:
        return self.js_stats.snapshot(
            mode=self.settings.fetch_engine,
            active_engine=self.fetcher.name,
            sidecar_url=self.settings.playwright_sidecar_url,
        )

    def resilience_status(self) -> dict[str, Any]:
        data = self.proxy_provider.snapshot()
        data["circuits"] = [
            item.model_dump(mode="json") for item in self.circuit_breaker.snapshot()
        ]
        data["js_renderer"] = self.js_renderer_status()
        return data

    def reset_circuit(self, domain: str) -> dict[str, Any]:
        norm = normalize_domain(domain) or domain.strip().lower()
        if not norm:
            raise ValueError("Некоректний домен")
        status = self.circuit_breaker.reset(norm)
        self.store.add_event(
            "system",
            "circuit_reset",
            f"Circuit breaker скинуто для {norm}",
            level="info",
            data={"domain": norm, "state": status.state},
        )
        return {"domain": norm, "circuit": status.model_dump(mode="json")}

    # ------------------------------------------------------------------
    # info
    # ------------------------------------------------------------------
    def capabilities(self) -> dict[str, Any]:
        proxy_snap = self.proxy_provider.snapshot()
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
            "proxies": {
                "pool_size": proxy_snap["pool_size"],
                "healthy_count": proxy_snap["healthy_count"],
                "strategy": proxy_snap["strategy"],
            },
            "circuit_breaker": {
                "threshold": self.circuit_breaker.threshold,
                "cooldown_s": self.circuit_breaker.cooldown_s,
                "open_domains": self.circuit_breaker.open_count(),
            },
            "js_renderer": self.js_renderer_status(),
        }

    def overview(self) -> dict[str, Any]:
        data = self.store.overview()
        data["cache"] = self.cache.stats()
        return data

    def prometheus_metrics(self) -> str:
        try:
            analytics_ok = bool(self.analytics.ping())
        except Exception:  # noqa: BLE001
            analytics_ok = False

        op = self.store.metrics_snapshot()
        if analytics_ok:
            try:
                an = self.analytics.metrics_snapshot()
            except Exception:  # noqa: BLE001
                analytics_ok = False
                an = {"records_current": 0, "records_quarantined": 0}
        else:
            an = {"records_current": 0, "records_quarantined": 0}

        cache_pages = int(self.cache.stats().get("pages") or 0)
        proxy_snap = self.proxy_provider.snapshot()
        open_circuits = int(self.circuit_breaker.open_count())
        rejected_circuits = int(self.circuit_breaker.total_rejected)

        def esc(value: str) -> str:
            return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")

        def fmt_usd(value: float) -> str:
            text = f"{round(value, 6):.6f}".rstrip("0").rstrip(".")
            return text or "0"

        lines: list[str] = [
            "# HELP daas_up Whether the analytical store is reachable (1=up, 0=down).",
            "# TYPE daas_up gauge",
            f"daas_up {1 if analytics_ok else 0}",
            "# HELP daas_tasks_total Number of tasks by status.",
            "# TYPE daas_tasks_total gauge",
        ]
        for status, count in op["tasks_by_status"].items():
            lines.append(f'daas_tasks_total{{status="{esc(status)}"}} {int(count)}')

        lines.extend([
            "# HELP daas_runs_total Number of runs by status.",
            "# TYPE daas_runs_total counter",
        ])
        for status, count in op["runs_by_status"].items():
            lines.append(f'daas_runs_total{{status="{esc(status)}"}} {int(count)}')

        lines.extend([
            "# HELP daas_queue_jobs Number of queue jobs by status.",
            "# TYPE daas_queue_jobs gauge",
        ])
        for status, count in op["jobs_by_status"].items():
            lines.append(f'daas_queue_jobs{{status="{esc(status)}"}} {int(count)}')

        lines.extend([
            "# HELP daas_pages_fetched_total Total pages fetched from network.",
            "# TYPE daas_pages_fetched_total counter",
            f"daas_pages_fetched_total {int(op['pages_fetched'])}",
            "# HELP daas_pages_cached_total Total pages served from local cache.",
            "# TYPE daas_pages_cached_total counter",
            f"daas_pages_cached_total {int(op['pages_cached'])}",
            "# HELP daas_pages_failed_total Total page fetch failures.",
            "# TYPE daas_pages_failed_total counter",
            f"daas_pages_failed_total {int(op['pages_failed'])}",
            "# HELP daas_blocked_total Total blocked fetches or runs by reason.",
            "# TYPE daas_blocked_total counter",
            f'daas_blocked_total{{reason="robots"}} {int(op["robots_blocked"])}',
            f'daas_blocked_total{{reason="compliance"}} {int(op["compliance_blocked"])}',
            f'daas_blocked_total{{reason="circuit_open"}} {rejected_circuits}',
            "# HELP daas_records_current Current active records in analytical store.",
            "# TYPE daas_records_current gauge",
            f"daas_records_current {int(an['records_current'])}",
            "# HELP daas_records_quarantined Records awaiting review in quarantine.",
            "# TYPE daas_records_quarantined gauge",
            f"daas_records_quarantined {int(an['records_quarantined'])}",
            "# HELP daas_llm_spend_usd_total Total non-cached LLM spend in USD.",
            "# TYPE daas_llm_spend_usd_total counter",
            f"daas_llm_spend_usd_total {fmt_usd(op['llm_spend_usd_total'])}",
            "# HELP daas_llm_calls_total Total non-cached LLM calls.",
            "# TYPE daas_llm_calls_total counter",
            f"daas_llm_calls_total {int(op['llm_calls_total'])}",
        ])

        if op["llm_by_purpose"]:
            lines.extend([
                "# HELP daas_llm_spend_usd_by_purpose LLM spend in USD broken down by purpose.",
                "# TYPE daas_llm_spend_usd_by_purpose counter",
            ])
            for purpose, stats in sorted(op["llm_by_purpose"].items()):
                lines.append(
                    f'daas_llm_spend_usd_by_purpose{{purpose="{esc(purpose)}"}} {fmt_usd(stats["usd"])}'
                )
            lines.extend([
                "# HELP daas_llm_calls_by_purpose_total LLM calls broken down by purpose.",
                "# TYPE daas_llm_calls_by_purpose_total counter",
            ])
            for purpose, stats in sorted(op["llm_by_purpose"].items()):
                lines.append(
                    f'daas_llm_calls_by_purpose_total{{purpose="{esc(purpose)}"}} {int(stats["calls"])}'
                )

        lines.extend([
            "# HELP daas_raw_cache_pages Number of HTML/JSON pages in raw cache.",
            "# TYPE daas_raw_cache_pages gauge",
            f"daas_raw_cache_pages {cache_pages}",
            "# HELP daas_proxy_pool_size Configured proxy endpoints in the pool.",
            "# TYPE daas_proxy_pool_size gauge",
            f"daas_proxy_pool_size {int(proxy_snap['pool_size'])}",
            "# HELP daas_proxy_healthy Healthy proxies not currently in cooldown.",
            "# TYPE daas_proxy_healthy gauge",
            f"daas_proxy_healthy {int(proxy_snap['healthy_count'])}",
            "# HELP daas_proxy_bytes_total Total bytes transferred through proxies.",
            "# TYPE daas_proxy_bytes_total counter",
            f"daas_proxy_bytes_total {int(proxy_snap['total_bytes'])}",
            "# HELP daas_proxy_cost_usd_total Estimated proxy bandwidth cost in USD.",
            "# TYPE daas_proxy_cost_usd_total counter",
            f"daas_proxy_cost_usd_total {fmt_usd(float(proxy_snap['total_cost_usd']))}",
            "# HELP daas_circuit_open_domains Number of domains currently in open circuit breaker state.",
            "# TYPE daas_circuit_open_domains gauge",
            f"daas_circuit_open_domains {open_circuits}",
            "# HELP daas_js_renders_total Total JS-rendering outcomes across escalations and sidecar.",
            "# TYPE daas_js_renders_total counter",
            f'daas_js_renders_total{{result="ok"}} {int(self.js_stats.rendered_ok)}',
            f'daas_js_renders_total{{result="fallback"}} {int(self.js_stats.fallbacks)}',
            f'daas_js_renders_total{{result="error"}} {int(self.js_stats.errors)}',
        ])
        return "\n".join(lines) + "\n"

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
