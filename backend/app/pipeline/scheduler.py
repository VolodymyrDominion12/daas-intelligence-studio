"""Deterministic 5-field UTC cron parser and in-process task scheduler (P1-05)."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from ..compliance import ComplianceError
from ..utils import iso, utcnow

if TYPE_CHECKING:
    from ..runtime import Studio

logger = logging.getLogger(__name__)

_MACROS = {
    "@hourly": "0 * * * *",
    "@daily": "0 0 * * *",
    "@weekly": "0 0 * * 0",
}

_MAX_DAYS_IN_MONTH = {
    1: 31,
    2: 29,
    3: 31,
    4: 30,
    5: 31,
    6: 30,
    7: 31,
    8: 31,
    9: 30,
    10: 31,
    11: 30,
    12: 31,
}

CATCH_UP_GRACE_SECONDS = 120.0


class CronError(ValueError):
    """Raised when a cron expression is syntactically invalid or unreachable."""


@dataclass(frozen=True)
class CronExpression:
    raw: str
    minutes: frozenset[int]
    hours: frozenset[int]
    days_of_month: frozenset[int]
    months: frozenset[int]
    days_of_week: frozenset[int]
    dom_restricted: bool
    dow_restricted: bool


def _parse_field(token: str, min_val: int, max_val: int, *, wrap_sunday: bool = False) -> tuple[frozenset[int], bool]:
    token = token.strip()
    if not token:
        raise CronError("Порожнє поле cron-виразу")
    restricted = token != "*"
    values: set[int] = set()
    for part in token.split(","):
        part = part.strip()
        if not part:
            raise CronError(f"Порожній елемент у полі '{token}'")
        if "/" in part:
            pieces = part.split("/")
            if len(pieces) != 2 or not pieces[1].isdigit():
                raise CronError(f"Некоректний крок у '{part}'")
            step = int(pieces[1])
            if step <= 0:
                raise CronError(f"Крок cron має бути > 0: '{part}'")
            base = pieces[0]
        else:
            step = 1
            base = part

        if base == "*":
            start, end = min_val, max_val
        elif "-" in base:
            bounds = base.split("-")
            if len(bounds) != 2 or not bounds[0].isdigit() or not bounds[1].isdigit():
                raise CronError(f"Некоректний діапазон '{base}'")
            start, end = int(bounds[0]), int(bounds[1])
            if start > end or start < min_val or end > max_val:
                raise CronError(f"Діапазон '{base}' виходить за межі {min_val}..{max_val}")
        elif base.isdigit():
            val = int(base)
            if val < min_val or val > max_val:
                raise CronError(f"Значення {val} виходить за межі {min_val}..{max_val}")
            if "/" in part:
                start, end = val, max_val
            else:
                values.add(0 if (wrap_sunday and val == 7) else val)
                continue
        else:
            raise CronError(f"Некоректний токен cron '{part}'")

        for v in range(start, end + 1, step):
            values.add(0 if (wrap_sunday and v == 7) else v)

    if not values:
        raise CronError(f"Поле '{token}' не дає жодного значення")
    return frozenset(values), restricted


def parse_cron(expr: str) -> CronExpression:
    """Parse a standard 5-field UTC cron expression (plus @hourly/@daily/@weekly)."""
    raw = " ".join((expr or "").strip().split())
    if not raw:
        raise CronError("Cron-вираз не може бути порожнім")
    expanded = _MACROS.get(raw.lower(), raw)
    parts = expanded.split(" ")
    if len(parts) != 5:
        raise CronError(f"Очікується 5 полів cron (отримано {len(parts)}): '{raw}'")

    minutes, _ = _parse_field(parts[0], 0, 59)
    hours, _ = _parse_field(parts[1], 0, 23)
    dom, dom_restricted = _parse_field(parts[2], 1, 31)
    months, _ = _parse_field(parts[3], 1, 12)
    dow, dow_restricted = _parse_field(parts[4], 0, 7, wrap_sunday=True)

    # Reject impossible calendar dates like Feb 31 or Apr 31.
    if not any(day <= _MAX_DAYS_IN_MONTH[month] for month in months for day in dom):
        raise CronError(f"Неможлива календарна дата в cron-виразі: '{raw}'")

    return CronExpression(
        raw=raw,
        minutes=minutes,
        hours=hours,
        days_of_month=dom,
        months=months,
        days_of_week=dow,
        dom_restricted=dom_restricted,
        dow_restricted=dow_restricted,
    )


def validate_cron(expr: str) -> str:
    """Validate cron expression and return its normalized representation."""
    return parse_cron(expr).raw


def _day_matches(spec: CronExpression, dt: datetime) -> bool:
    # Standard cron Sunday is 0 (Python weekday(): Monday=0 .. Sunday=6).
    cron_dow = (dt.weekday() + 1) % 7
    dom_ok = dt.day in spec.days_of_month
    dow_ok = cron_dow in spec.days_of_week
    if spec.dom_restricted and spec.dow_restricted:
        return dom_ok or dow_ok
    return dom_ok and dow_ok


def next_cron_after(expr: str | CronExpression, after: datetime) -> datetime:
    """Return the earliest UTC timestamp strictly after ``after`` matching ``expr``."""
    spec = parse_cron(expr) if isinstance(expr, str) else expr
    base = after.replace(tzinfo=UTC) if after.tzinfo is None else after.astimezone(UTC)
    candidate = base.replace(second=0, microsecond=0) + timedelta(minutes=1)
    limit = base + timedelta(days=366 * 4 + 2)

    sorted_hours = sorted(spec.hours)
    sorted_minutes = sorted(spec.minutes)

    while candidate <= limit:
        if candidate.month not in spec.months:
            next_month = 1 if candidate.month == 12 else candidate.month + 1
            next_year = candidate.year + 1 if candidate.month == 12 else candidate.year
            candidate = datetime(next_year, next_month, 1, 0, 0, tzinfo=UTC)
            continue

        if not _day_matches(spec, candidate):
            candidate = (candidate + timedelta(days=1)).replace(hour=0, minute=0)
            continue

        matching_hour = next((h for h in sorted_hours if h >= candidate.hour), None)
        if matching_hour is None:
            candidate = (candidate + timedelta(days=1)).replace(hour=0, minute=0)
            continue
        if matching_hour > candidate.hour:
            candidate = candidate.replace(hour=matching_hour, minute=sorted_minutes[0])
            return candidate

        matching_minute = next((m for m in sorted_minutes if m >= candidate.minute), None)
        if matching_minute is None:
            next_hour = next((h for h in sorted_hours if h > candidate.hour), None)
            if next_hour is None:
                candidate = (candidate + timedelta(days=1)).replace(hour=0, minute=0)
            else:
                candidate = candidate.replace(hour=next_hour, minute=sorted_minutes[0])
                return candidate
            continue

        return candidate.replace(minute=matching_minute)

    raise CronError(f"Не вдалося знайти наступний запуск за 4 роки для '{spec.raw}'")


class TaskScheduler:
    """In-process cron scheduler firing scheduled collects without DuckDB lock conflicts."""

    def __init__(self, studio: Studio) -> None:
        self.studio = studio
        self._task: asyncio.Task[None] | None = None
        self._stop_event: asyncio.Event | None = None

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self) -> None:
        if not self.studio.settings.scheduler_enabled or self.running:
            return
        self._stop_event = asyncio.Event()
        self._task = asyncio.create_task(self._loop(), name="daas-scheduler")

    async def stop(self) -> None:
        if self._stop_event is not None:
            self._stop_event.set()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def _loop(self) -> None:
        while self._stop_event is not None and not self._stop_event.is_set():
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - background loop must survive transient errors
                logger.exception("scheduler tick failed")
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(),
                    timeout=self.studio.settings.scheduler_poll_seconds,
                )
            except TimeoutError:
                continue

    async def tick(self, *, now: datetime | None = None) -> list[dict[str, Any]]:
        """Evaluate all due schedules once at ``now`` (UTC) and return action outcomes."""
        current = now or utcnow()
        current = current.replace(tzinfo=UTC) if current.tzinfo is None else current.astimezone(UTC)

        outcomes: list[dict[str, Any]] = []
        for sched in self.studio.store.list_due_schedules(iso(current)):
            if sched.next_run_at is None:
                continue
            task = self.studio.store.get_task(sched.task_id)
            if task is None:
                continue

            next_slot = next_cron_after(sched.cron, current)
            scheduled_at = (
                sched.next_run_at.replace(tzinfo=UTC)
                if sched.next_run_at.tzinfo is None
                else sched.next_run_at.astimezone(UTC)
            )
            delay_seconds = (current - scheduled_at).total_seconds()

            if not sched.catch_up and delay_seconds > CATCH_UP_GRACE_SECONDS:
                self.studio.store.mark_schedule_tick(sched.task_id, next_run_at=next_slot)
                self.studio.store.add_event(
                    sched.task_id,
                    "schedule_skipped",
                    f"Пропущено прострочений слот за розкладом ({sched.cron})",
                    level="info",
                    data={
                        "reason": "missed_no_catchup",
                        "cron": sched.cron,
                        "scheduled_at": iso(scheduled_at),
                        "next_run_at": iso(next_slot),
                        "delay_seconds": round(delay_seconds, 1),
                    },
                )
                outcomes.append(
                    {"task_id": sched.task_id, "action": "skipped", "reason": "missed_no_catchup"}
                )
                continue

            if self.studio.active_run(sched.task_id) is not None:
                self.studio.store.mark_schedule_tick(sched.task_id, next_run_at=next_slot)
                self.studio.store.add_event(
                    sched.task_id,
                    "schedule_skipped",
                    f"Пропущено запуск за розкладом ({sched.cron}): задача вже виконується",
                    level="info",
                    data={
                        "reason": "active_run",
                        "cron": sched.cron,
                        "next_run_at": iso(next_slot),
                    },
                )
                outcomes.append(
                    {"task_id": sched.task_id, "action": "skipped", "reason": "active_run"}
                )
                continue

            try:
                run = await self.studio.collect(
                    sched.task_id,
                    acknowledge_high_risk=sched.acknowledge_high_risk,
                    reason=sched.reason,
                )
            except ComplianceError as exc:
                self.studio.store.mark_schedule_tick(sched.task_id, next_run_at=next_slot)
                self.studio.store.add_event(
                    sched.task_id,
                    "schedule_blocked",
                    f"Розклад заблоковано комплаєнсом ({sched.cron}): {exc}",
                    level="warning",
                    data={
                        "error": str(exc),
                        "code": exc.code,
                        "domains": exc.domains,
                        "cron": sched.cron,
                        "next_run_at": iso(next_slot),
                    },
                )
                outcomes.append({"task_id": sched.task_id, "action": "blocked", "reason": exc.code})
                continue
            except ValueError as exc:
                self.studio.store.mark_schedule_tick(sched.task_id, next_run_at=next_slot)
                self.studio.store.add_event(
                    sched.task_id,
                    "schedule_blocked",
                    f"Розклад не запущено ({sched.cron}): {exc}",
                    level="warning",
                    data={"error": str(exc), "cron": sched.cron, "next_run_at": iso(next_slot)},
                )
                outcomes.append({"task_id": sched.task_id, "action": "blocked", "reason": "invalid"})
                continue
            except Exception as exc:  # noqa: BLE001 - distinguish RunConflict subtypes safely
                # A RunConflict may come from a simultaneous active run or missing web calibration.
                is_active = self.studio.active_run(sched.task_id) is not None or "виконується" in str(exc)
                self.studio.store.mark_schedule_tick(sched.task_id, next_run_at=next_slot)
                if is_active:
                    self.studio.store.add_event(
                        sched.task_id,
                        "schedule_skipped",
                        f"Пропущено запуск за розкладом ({sched.cron}): задача вже виконується",
                        level="info",
                        data={
                            "reason": "active_run",
                            "cron": sched.cron,
                            "next_run_at": iso(next_slot),
                        },
                    )
                    outcomes.append(
                        {"task_id": sched.task_id, "action": "skipped", "reason": "active_run"}
                    )
                else:
                    self.studio.store.add_event(
                        sched.task_id,
                        "schedule_blocked",
                        f"Розклад заблоковано ({sched.cron}): {exc}",
                        level="warning",
                        data={
                            "error": str(exc),
                            "cron": sched.cron,
                            "next_run_at": iso(next_slot),
                        },
                    )
                    outcomes.append(
                        {"task_id": sched.task_id, "action": "blocked", "reason": "uncalibrated"}
                    )
                continue

            is_catch_up = delay_seconds > CATCH_UP_GRACE_SECONDS
            self.studio.store.mark_schedule_tick(
                sched.task_id,
                next_run_at=next_slot,
                last_run_at=current,
                last_run_id=run.id,
            )
            self.studio.store.add_event(
                sched.task_id,
                "schedule_triggered",
                f"Запущено збір за розкладом ({sched.cron})",
                run_id=run.id,
                level="info",
                data={
                    "run_id": run.id,
                    "cron": sched.cron,
                    "catch_up": is_catch_up,
                    "next_run_at": iso(next_slot),
                },
            )
            outcomes.append(
                {
                    "task_id": sched.task_id,
                    "action": "triggered",
                    "run_id": run.id,
                    "catch_up": is_catch_up,
                }
            )
        dispatcher = getattr(self.studio, "dispatcher", None)
        if dispatcher is not None:
            await dispatcher.flush_due_digests(now=current)
        return outcomes
