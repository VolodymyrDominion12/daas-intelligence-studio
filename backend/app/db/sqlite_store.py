"""Operational store (SQLite, WAL): tasks, runs, durable URL queue, events, ledger.

The queue is crash-safe: workers *lease* a batch of rows atomically, heartbeat the
lease while working, and stale leases are reclaimed by the next worker tick.
"""

from __future__ import annotations

import sqlite3
import threading
import uuid
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from ..config import Paths
from ..models import (
    CssSchema,
    DatasetInfo,
    DeliveryFilters,
    EntityLinkInfo,
    EntityLinkStatus,
    EventInfo,
    FieldSpec,
    MergePolicy,
    RunInfo,
    RunStats,
    ScheduleInfo,
    SourcePolicy,
    SubscriptionInfo,
    TaskInfo,
    TaskSpec,
)
from ..utils import iso, json_dumps, json_loads, normalize_domain, utcnow

MIGRATIONS: list[tuple[int, str]] = [
    (
        1,
        """
        CREATE TABLE tasks (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            prompt TEXT NOT NULL,
            spec_json TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'draft',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE runs (
            id TEXT PRIMARY KEY,
            task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
            kind TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'queued',
            stats_json TEXT NOT NULL DEFAULT '{}',
            error TEXT,
            budget_usd REAL NOT NULL DEFAULT 0,
            spent_usd REAL NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            started_at TEXT,
            finished_at TEXT
        );
        CREATE INDEX idx_runs_task ON runs(task_id, created_at DESC);

        CREATE TABLE jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
            task_id TEXT NOT NULL,
            kind TEXT NOT NULL,
            url TEXT NOT NULL,
            depth INTEGER NOT NULL DEFAULT 0,
            parent_url TEXT,
            status TEXT NOT NULL DEFAULT 'queued',
            attempts INTEGER NOT NULL DEFAULT 0,
            max_attempts INTEGER NOT NULL DEFAULT 3,
            lease_until TEXT,
            worker TEXT,
            last_error TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(run_id, url)
        );
        CREATE INDEX idx_jobs_claim ON jobs(status, run_id);
        CREATE INDEX idx_jobs_run ON jobs(run_id, status);

        CREATE TABLE events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id TEXT NOT NULL,
            run_id TEXT,
            type TEXT NOT NULL,
            level TEXT NOT NULL DEFAULT 'info',
            message TEXT NOT NULL,
            data_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL
        );
        CREATE INDEX idx_events_task ON events(task_id, id);

        CREATE TABLE selector_cache (
            domain TEXT NOT NULL,
            signature TEXT NOT NULL,
            schema_json TEXT NOT NULL,
            hits INTEGER NOT NULL DEFAULT 0,
            misses INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (domain, signature)
        );

        CREATE TABLE datasets (
            id TEXT PRIMARY KEY,
            task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
            name TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE dataset_versions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            dataset_id TEXT NOT NULL REFERENCES datasets(id) ON DELETE CASCADE,
            version INTEGER NOT NULL,
            run_id TEXT,
            created_at TEXT NOT NULL,
            row_count INTEGER NOT NULL DEFAULT 0,
            manifest_json TEXT NOT NULL DEFAULT '{}',
            UNIQUE(dataset_id, version)
        );

        CREATE TABLE raw_pages (
            url TEXT PRIMARY KEY,
            content_hash TEXT NOT NULL,
            status INTEGER NOT NULL,
            fetched_at TEXT NOT NULL,
            size INTEGER NOT NULL DEFAULT 0,
            engine TEXT NOT NULL DEFAULT 'http'
        );

        CREATE TABLE llm_calls (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id TEXT,
            run_id TEXT,
            purpose TEXT NOT NULL,
            provider TEXT NOT NULL,
            model TEXT NOT NULL,
            tokens_in INTEGER NOT NULL DEFAULT 0,
            tokens_out INTEGER NOT NULL DEFAULT 0,
            usd REAL NOT NULL DEFAULT 0,
            cached INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        );
        CREATE INDEX idx_llm_run ON llm_calls(run_id);

        CREATE TABLE source_registry (
            domain TEXT PRIMARY KEY,
            payload_json TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        """,
    ),
    (
        2,
        """
        CREATE TABLE calibrations (
            task_id TEXT PRIMARY KEY REFERENCES tasks(id) ON DELETE CASCADE,
            run_id TEXT,
            report_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        """,
    ),
    (
        3,
        """
        CREATE TABLE record_misses (
            dataset_id TEXT NOT NULL,
            record_key TEXT NOT NULL,
            misses INTEGER NOT NULL DEFAULT 0,
            last_run_id TEXT,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (dataset_id, record_key)
        );
        """,
    ),
    (
        4,
        """
        CREATE TABLE api_state (
            task_id TEXT PRIMARY KEY REFERENCES tasks(id) ON DELETE CASCADE,
            next_cursor TEXT,
            next_url TEXT,
            last_modified TEXT,
            updated_at TEXT NOT NULL
        );
        """,
    ),
    (
        5,
        """
        CREATE TABLE schedules (
            task_id TEXT PRIMARY KEY REFERENCES tasks(id) ON DELETE CASCADE,
            cron TEXT NOT NULL,
            enabled INTEGER NOT NULL DEFAULT 1,
            catch_up INTEGER NOT NULL DEFAULT 1,
            acknowledge_high_risk INTEGER NOT NULL DEFAULT 0,
            reason TEXT NOT NULL DEFAULT '',
            next_run_at TEXT,
            last_run_at TEXT,
            last_run_id TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE INDEX idx_schedules_due ON schedules(enabled, next_run_at);
        """,
    ),
    (
        6,
        """
        CREATE TABLE subscriptions (
            id TEXT PRIMARY KEY,
            task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
            name TEXT NOT NULL DEFAULT '',
            channel TEXT NOT NULL,
            target TEXT NOT NULL,
            secret TEXT NOT NULL DEFAULT '',
            filters_json TEXT NOT NULL DEFAULT '{}',
            enabled INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE INDEX idx_subscriptions_task ON subscriptions(task_id, enabled);

        CREATE TABLE delivery_log (
            subscription_id TEXT NOT NULL REFERENCES subscriptions(id) ON DELETE CASCADE,
            dedup_key TEXT NOT NULL,
            run_id TEXT,
            status TEXT NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 1,
            error TEXT,
            created_at TEXT NOT NULL,
            delivered_at TEXT,
            PRIMARY KEY (subscription_id, dedup_key)
        );
        """,
    ),
    (
        7,
        """
        CREATE TABLE entity_links (
            id TEXT PRIMARY KEY,
            task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
            canonical_key TEXT NOT NULL,
            merged_key TEXT NOT NULL,
            status TEXT NOT NULL,
            score REAL NOT NULL DEFAULT 0,
            rule TEXT NOT NULL DEFAULT 'manual',
            policy TEXT NOT NULL DEFAULT 'confidence',
            reviewer TEXT NOT NULL DEFAULT 'user',
            canonical_before_rev INTEGER,
            merged_before_rev INTEGER,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            unmerged_at TEXT
        );
        CREATE INDEX idx_entity_links_task ON entity_links(task_id, status);
        CREATE INDEX idx_entity_links_pair ON entity_links(task_id, canonical_key, merged_key);
        """,
    ),
    (
        8,
        """
        CREATE TABLE calibration_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
            version INTEGER NOT NULL,
            run_id TEXT,
            report_json TEXT NOT NULL,
            spec_json TEXT NOT NULL DEFAULT '{}',
            source TEXT NOT NULL DEFAULT 'calibrate',
            created_at TEXT NOT NULL,
            UNIQUE(task_id, version)
        );
        CREATE INDEX idx_calibration_history_task ON calibration_history(task_id, version DESC);
        CREATE TABLE IF NOT EXISTS calibrations (
            task_id TEXT PRIMARY KEY REFERENCES tasks(id) ON DELETE CASCADE,
            run_id TEXT,
            report_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        INSERT INTO calibration_history(
            task_id, version, run_id, report_json, spec_json, source, created_at
        )
        SELECT task_id, 1, run_id, report_json, '{}', 'legacy', created_at
        FROM calibrations;
        """,
    ),
    (
        9,
        """
        ALTER TABLE subscriptions ADD COLUMN next_digest_at TEXT;
        ALTER TABLE subscriptions ADD COLUMN last_digest_at TEXT;

        CREATE TABLE delivery_digest_queue (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            subscription_id TEXT NOT NULL REFERENCES subscriptions(id) ON DELETE CASCADE,
            task_id TEXT NOT NULL,
            dataset_version INTEGER NOT NULL,
            run_id TEXT,
            dedup_key TEXT NOT NULL,
            change_json TEXT NOT NULL,
            record_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            flushed_at TEXT,
            UNIQUE(subscription_id, dedup_key)
        );
        CREATE INDEX idx_digest_queue_pending
            ON delivery_digest_queue(subscription_id, flushed_at, id);
        """,
    ),
]


_UNSET = object()


class ActiveRunError(RuntimeError):
    """A task already has a queued or running run (raised by an exclusive ``create_run``)."""

    def __init__(self, run_id: str, status: str) -> None:
        super().__init__(f"Задача вже виконується (run {run_id}, {status})")
        self.run_id = run_id
        self.status = status


class OperationalStore:
    def __init__(self, paths: Paths) -> None:
        paths.ensure()
        self.path: Path = paths.sqlite
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            self.path, check_same_thread=False, isolation_level=None, timeout=20.0
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA busy_timeout=15000")
        self._migrate()

    # -- infrastructure ---------------------------------------------------
    def _migrate(self) -> None:
        with self._lock:
            version = self._conn.execute("PRAGMA user_version").fetchone()[0]
            for target, script in MIGRATIONS:
                if target <= version:
                    continue
                self._conn.executescript("BEGIN;" + script + f"PRAGMA user_version={target};COMMIT;")

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
            else:
                self._conn.execute("COMMIT")

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def backup_to(self, destination: Path) -> Path:
        """Consistent SQLite copy via the online backup API (safe while WAL is active)."""
        destination.parent.mkdir(parents=True, exist_ok=True)
        target = sqlite3.connect(destination)
        try:
            with self._lock:
                self._conn.backup(target)
        finally:
            target.close()
        return destination

    def _one(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(sql, tuple(params)).fetchone()

    def _all(self, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return list(self._conn.execute(sql, tuple(params)).fetchall())

    # -- tasks ------------------------------------------------------------
    def create_task(self, spec: TaskSpec, *, task_id: str | None = None) -> TaskInfo:
        task_id = task_id or uuid.uuid4().hex[:12]
        now = iso()
        with self._tx() as conn:
            conn.execute(
                "INSERT INTO tasks(id, title, prompt, spec_json, status, created_at, updated_at)"
                " VALUES (?,?,?,?,?,?,?)",
                (task_id, spec.title, spec.prompt, spec.model_dump_json(), "draft", now, now),
            )
            conn.execute(
                "INSERT INTO datasets(id, task_id, name, created_at, updated_at) VALUES (?,?,?,?,?)",
                (task_id, task_id, spec.title, now, now),
            )
        task = self.get_task(task_id)
        assert task is not None
        return task

    def update_task(
        self,
        task_id: str,
        *,
        spec: TaskSpec | None = None,
        status: str | None = None,
        title: str | None = None,
    ) -> TaskInfo | None:
        fields: list[str] = []
        values: list[Any] = []
        if spec is not None:
            fields += ["spec_json = ?", "title = ?", "prompt = ?"]
            values += [spec.model_dump_json(), spec.title, spec.prompt]
        if title is not None:
            fields.append("title = ?")
            values.append(title)
        if status is not None:
            fields.append("status = ?")
            values.append(status)
        if not fields:
            return self.get_task(task_id)
        fields.append("updated_at = ?")
        values.append(iso())
        values.append(task_id)
        with self._lock:
            self._conn.execute(f"UPDATE tasks SET {', '.join(fields)} WHERE id = ?", values)
        return self.get_task(task_id)

    def delete_task(self, task_id: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))

    def get_task(self, task_id: str) -> TaskInfo | None:
        row = self._one("SELECT * FROM tasks WHERE id = ?", (task_id,))
        return self._to_task(row) if row else None

    def list_tasks(self, limit: int = 100) -> list[TaskInfo]:
        rows = self._all("SELECT * FROM tasks ORDER BY updated_at DESC LIMIT ?", (limit,))
        return [self._to_task(row) for row in rows]

    def _to_task(self, row: sqlite3.Row) -> TaskInfo:
        spec = TaskSpec.model_validate_json(row["spec_json"])
        last_run_row = self._one(
            "SELECT * FROM runs WHERE task_id = ? ORDER BY created_at DESC, rowid DESC LIMIT 1",
            (row["id"],),
        )
        dataset_row = self._one("SELECT id FROM datasets WHERE task_id = ?", (row["id"],))
        return TaskInfo(
            id=row["id"],
            title=row["title"],
            prompt=row["prompt"],
            spec=spec,
            status=row["status"],
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
            dataset_id=dataset_row["id"] if dataset_row else None,
            last_run=self._to_run(last_run_row) if last_run_row else None,
        )

    # -- runs -------------------------------------------------------------
    def create_run(
        self,
        task_id: str,
        kind: str,
        *,
        budget_usd: float = 0.0,
        stats: RunStats | None = None,
        run_id: str | None = None,
        exclusive: bool = False,
        audit: tuple[str, str, str, dict[str, Any]] | None = None,
    ) -> RunInfo:
        """Insert a queued run.

        ``exclusive`` refuses to start while the task has a queued/running run; the check and the
        insert share one ``BEGIN IMMEDIATE`` transaction, so concurrent requests (API, CLI, another
        process) cannot both win. ``audit`` = ``(type, level, message, data)`` is written in the
        same transaction, so a run never exists without its compliance audit record.
        """
        run_id = run_id or uuid.uuid4().hex[:12]
        now = iso()
        with self._tx() as conn:
            if exclusive:
                active = conn.execute(
                    "SELECT id, status FROM runs WHERE task_id = ? AND status IN ('queued','running')"
                    " ORDER BY created_at DESC LIMIT 1",
                    (task_id,),
                ).fetchone()
                if active is not None:
                    raise ActiveRunError(active["id"], active["status"])
            conn.execute(
                "INSERT INTO runs(id, task_id, kind, status, stats_json, budget_usd, created_at)"
                " VALUES (?,?,?,?,?,?,?)",
                (run_id, task_id, kind, "queued", (stats or RunStats()).model_dump_json(), budget_usd, now),
            )
            if audit is not None:
                event_type, level, message, data = audit
                conn.execute(
                    "INSERT INTO events(task_id, run_id, type, level, message, data_json, created_at)"
                    " VALUES (?,?,?,?,?,?,?)",
                    (task_id, run_id, event_type, level, message, json_dumps(data), now),
                )
        run = self.get_run(run_id)
        assert run is not None
        return run

    def update_run(
        self,
        run_id: str,
        *,
        status: str | None = None,
        stats: RunStats | None = None,
        error: str | None = None,
        spent_usd: float | None = None,
        started: bool = False,
        finished: bool = False,
    ) -> None:
        fields: list[str] = []
        values: list[Any] = []
        if status is not None:
            fields.append("status = ?")
            values.append(status)
        if stats is not None:
            fields.append("stats_json = ?")
            values.append(stats.model_dump_json())
        if error is not None:
            fields.append("error = ?")
            values.append(error)
        if spent_usd is not None:
            fields.append("spent_usd = ?")
            values.append(round(spent_usd, 6))
        if started:
            fields.append("started_at = COALESCE(started_at, ?)")
            values.append(iso())
        if finished:
            fields.append("finished_at = ?")
            values.append(iso())
        if not fields:
            return
        values.append(run_id)
        with self._lock:
            self._conn.execute(f"UPDATE runs SET {', '.join(fields)} WHERE id = ?", values)

    def get_run(self, run_id: str) -> RunInfo | None:
        row = self._one("SELECT * FROM runs WHERE id = ?", (run_id,))
        return self._to_run(row) if row else None

    def list_runs(self, task_id: str, limit: int = 30) -> list[RunInfo]:
        rows = self._all(
            "SELECT * FROM runs WHERE task_id = ? ORDER BY created_at DESC, rowid DESC LIMIT ?",
            (task_id, limit),
        )
        return [self._to_run(row) for row in rows]

    def list_unfinished_runs(self) -> list[RunInfo]:
        rows = self._all(
            "SELECT * FROM runs WHERE status IN ('queued', 'running') ORDER BY created_at, rowid"
        )
        return [self._to_run(row) for row in rows]

    def _to_run(self, row: sqlite3.Row) -> RunInfo:
        stats = RunStats.model_validate_json(row["stats_json"] or "{}")
        return RunInfo(
            id=row["id"],
            task_id=row["task_id"],
            kind=row["kind"],
            status=row["status"],
            stats=stats,
            error=row["error"],
            created_at=datetime.fromisoformat(row["created_at"]),
            started_at=datetime.fromisoformat(row["started_at"]) if row["started_at"] else None,
            finished_at=datetime.fromisoformat(row["finished_at"]) if row["finished_at"] else None,
            budget_usd=row["budget_usd"] or 0.0,
            spent_usd=row["spent_usd"] or 0.0,
        )

    # -- queue ------------------------------------------------------------
    def enqueue_urls(
        self,
        run_id: str,
        task_id: str,
        urls: Iterable[tuple[str, int, str | None]],
        *,
        kind: str = "fetch",
        max_attempts: int = 3,
    ) -> int:
        now = iso()
        rows = [
            (run_id, task_id, kind, url, depth, parent, "queued", max_attempts, now, now)
            for url, depth, parent in urls
        ]
        if not rows:
            return 0
        with self._tx() as conn:
            conn.executemany(
                "INSERT OR IGNORE INTO jobs(run_id, task_id, kind, url, depth, parent_url, status,"
                " max_attempts, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                rows,
            )
            total = conn.execute("SELECT COUNT(*) FROM jobs WHERE run_id = ?", (run_id,)).fetchone()[0]
        return int(total)

    def claim_jobs(
        self,
        worker: str,
        *,
        run_id: str | None = None,
        limit: int = 8,
        lease_seconds: int = 120,
    ) -> list[dict[str, Any]]:
        """Lease queued jobs. Pass ``run_id`` so a run never processes another run's pages."""
        now = utcnow()
        lease_until = iso(now + timedelta(seconds=lease_seconds))
        with self._lock:
            cursor = self._conn.execute(
                """
                UPDATE jobs SET status = 'leased', worker = ?, lease_until = ?,
                       attempts = attempts + 1, updated_at = ?
                WHERE id IN (
                    SELECT id FROM jobs
                    WHERE status = 'queued' AND (? IS NULL OR run_id = ?)
                    ORDER BY id
                    LIMIT ?
                )
                RETURNING id, run_id, task_id, kind, url, depth, parent_url, attempts, max_attempts
                """,
                (worker, lease_until, iso(now), run_id, run_id, limit),
            )
            rows = [dict(row) for row in cursor.fetchall()]
        return rows

    def complete_job(self, job_id: int, *, status: str = "done", error: str | None = None) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE jobs SET status = ?, last_error = ?, lease_until = NULL, updated_at = ?"
                " WHERE id = ?",
                (status, error, iso(), job_id),
            )

    def fail_job(self, job_id: int, error: str, *, retryable: bool = True) -> str:
        with self._lock:
            row = self._conn.execute(
                "SELECT attempts, max_attempts FROM jobs WHERE id = ?", (job_id,)
            ).fetchone()
            if row is None:
                return "missing"
            attempts, max_attempts = row["attempts"], row["max_attempts"]
            new_status = "queued" if retryable and attempts < max_attempts else "failed"
            self._conn.execute(
                "UPDATE jobs SET status = ?, last_error = ?, lease_until = NULL, updated_at = ?"
                " WHERE id = ?",
                (new_status, error[:500], iso(), job_id),
            )
            return new_status

    def release_stale_jobs(self) -> int:
        with self._lock:
            cursor = self._conn.execute(
                "UPDATE jobs SET status = 'queued', worker = NULL, lease_until = NULL"
                " WHERE status = 'leased' AND lease_until IS NOT NULL AND lease_until < ?",
                (iso(),),
            )
            return cursor.rowcount or 0

    def heartbeat(self, worker: str, lease_seconds: int = 120) -> None:
        lease_until = iso(utcnow() + timedelta(seconds=lease_seconds))
        with self._lock:
            self._conn.execute(
                "UPDATE jobs SET lease_until = ? WHERE status = 'leased' AND worker = ?",
                (lease_until, worker),
            )

    def job_counts(self, run_id: str) -> dict[str, int]:
        rows = self._all("SELECT status, COUNT(*) AS c FROM jobs WHERE run_id = ? GROUP BY status", (run_id,))
        counts = {"queued": 0, "leased": 0, "done": 0, "failed": 0, "skipped": 0, "cancelled": 0}
        for row in rows:
            counts[row["status"]] = row["c"]
        counts["total"] = sum(counts.values())
        return counts

    def cancel_run_jobs(self, run_id: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE jobs SET status = 'cancelled', lease_until = NULL, updated_at = ?"
                " WHERE run_id = ? AND status IN ('queued', 'leased')",
                (iso(), run_id),
            )

    # -- events -----------------------------------------------------------
    def add_event(
        self,
        task_id: str,
        event_type: str,
        message: str,
        *,
        run_id: str | None = None,
        level: str = "info",
        data: dict[str, Any] | None = None,
    ) -> int:
        with self._lock:
            cursor = self._conn.execute(
                "INSERT INTO events(task_id, run_id, type, level, message, data_json, created_at)"
                " VALUES (?,?,?,?,?,?,?)",
                (task_id, run_id, event_type, level, message, json_dumps(data or {}), iso()),
            )
            return int(cursor.lastrowid or 0)

    def list_events(
        self, task_id: str, *, after_id: int = 0, limit: int = 200, run_id: str | None = None
    ) -> list[EventInfo]:
        sql = "SELECT * FROM events WHERE task_id = ? AND id > ?"
        params: list[Any] = [task_id, after_id]
        if run_id:
            sql += " AND run_id = ?"
            params.append(run_id)
        sql += " ORDER BY id ASC LIMIT ?"
        params.append(limit)
        return [self._to_event(row) for row in self._all(sql, params)]

    def _to_event(self, row: sqlite3.Row) -> EventInfo:
        return EventInfo(
            id=row["id"],
            task_id=row["task_id"],
            run_id=row["run_id"],
            type=row["type"],
            level=row["level"],
            message=row["message"],
            data=json_loads(row["data_json"], {}),
            created_at=datetime.fromisoformat(row["created_at"]),
        )

    # -- calibration ------------------------------------------------------
    def save_calibration(
        self,
        task_id: str,
        run_id: str | None,
        report_json: str,
        *,
        spec_json: str = "{}",
        source: str = "calibrate",
    ) -> int:
        now_str = iso()
        with self._tx() as conn:
            conn.execute(
                "INSERT INTO calibrations(task_id, run_id, report_json, created_at)"
                " VALUES (?,?,?,?) ON CONFLICT(task_id) DO UPDATE SET"
                " run_id = excluded.run_id, report_json = excluded.report_json,"
                " created_at = excluded.created_at",
                (task_id, run_id, report_json, now_str),
            )
            row = conn.execute(
                "SELECT COALESCE(MAX(version), 0) AS v FROM calibration_history WHERE task_id = ?",
                (task_id,),
            ).fetchone()
            next_version = int(row["v"]) + 1 if row else 1
            conn.execute(
                "INSERT INTO calibration_history("
                " task_id, version, run_id, report_json, spec_json, source, created_at"
                ") VALUES (?,?,?,?,?,?,?)",
                (task_id, next_version, run_id, report_json, spec_json, source, now_str),
            )
        return next_version

    def get_calibration(self, task_id: str) -> str | None:
        row = self._one("SELECT report_json FROM calibrations WHERE task_id = ?", (task_id,))
        return row["report_json"] if row else None

    def list_calibration_history(self, task_id: str, *, limit: int = 20) -> list[dict[str, Any]]:
        rows = self._all(
            "SELECT version, run_id, report_json, spec_json, source, created_at"
            " FROM calibration_history WHERE task_id = ? ORDER BY version DESC LIMIT ?",
            (task_id, limit),
        )
        return [dict(row) for row in rows]

    def get_calibration_entry(self, task_id: str, version: int) -> dict[str, Any] | None:
        row = self._one(
            "SELECT version, run_id, report_json, spec_json, source, created_at"
            " FROM calibration_history WHERE task_id = ? AND version = ?",
            (task_id, version),
        )
        return dict(row) if row else None

    # -- datasets ---------------------------------------------------------
    def dataset(self, dataset_id: str) -> DatasetInfo | None:
        row = self._one("SELECT * FROM datasets WHERE id = ?", (dataset_id,))
        if not row:
            return None
        task = self.get_task(row["task_id"])
        latest = self._one(
            "SELECT * FROM dataset_versions WHERE dataset_id = ? ORDER BY version DESC LIMIT 1",
            (dataset_id,),
        )
        return DatasetInfo(
            id=row["id"],
            task_id=row["task_id"],
            name=row["name"],
            version=latest["version"] if latest else 0,
            row_count=latest["row_count"] if latest else 0,
            columns=task.spec.columns if task else [],
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
            manifest=json_loads(latest["manifest_json"], {}) if latest else {},
        )

    def next_dataset_version(self, dataset_id: str) -> int:
        row = self._one(
            "SELECT COALESCE(MAX(version), 0) AS v FROM dataset_versions WHERE dataset_id = ?",
            (dataset_id,),
        )
        return int(row["v"]) + 1 if row else 1

    def add_dataset_version(
        self,
        dataset_id: str,
        version: int,
        run_id: str | None,
        row_count: int,
        manifest: dict[str, Any],
    ) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO dataset_versions(dataset_id, version, run_id, created_at,"
                " row_count, manifest_json) VALUES (?,?,?,?,?,?)",
                (dataset_id, version, run_id, iso(), row_count, json_dumps(manifest)),
            )
            self._conn.execute(
                "UPDATE datasets SET updated_at = ? WHERE id = ?", (iso(), dataset_id)
            )

    def update_version_row_count(self, dataset_id: str, version: int, row_count: int) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE dataset_versions SET row_count = ? WHERE dataset_id = ? AND version = ?",
                (row_count, dataset_id, version),
            )
            self._conn.execute(
                "UPDATE datasets SET updated_at = ? WHERE id = ?", (iso(), dataset_id)
            )

    def run_has_version(self, run_id: str) -> bool:
        return self._one("SELECT 1 FROM dataset_versions WHERE run_id = ? LIMIT 1", (run_id,)) is not None

    def dataset_versions(self, dataset_id: str) -> list[dict[str, Any]]:
        rows = self._all(
            "SELECT * FROM dataset_versions WHERE dataset_id = ? ORDER BY version DESC",
            (dataset_id,),
        )
        return [
            {
                "version": row["version"],
                "run_id": row["run_id"],
                "created_at": row["created_at"],
                "row_count": row["row_count"],
                "manifest": json_loads(row["manifest_json"], {}),
            }
            for row in rows
        ]

    def update_latest_version_manifest(self, dataset_id: str, patch: dict[str, Any]) -> int | None:
        """Merge ``patch`` into the latest dataset version manifest (for post-run eval metrics)."""
        if not patch:
            return None
        with self._tx() as conn:
            row = conn.execute(
                "SELECT version, manifest_json FROM dataset_versions"
                " WHERE dataset_id = ? ORDER BY version DESC LIMIT 1",
                (dataset_id,),
            ).fetchone()
            if row is None:
                return None
            manifest = json_loads(row["manifest_json"], {})
            if not isinstance(manifest, dict):
                manifest = {}
            manifest.update(patch)
            version = int(row["version"])
            conn.execute(
                "UPDATE dataset_versions SET manifest_json = ? WHERE dataset_id = ? AND version = ?",
                (json_dumps(manifest), dataset_id, version),
            )
            return version

    # -- selector cache ---------------------------------------------------
    def get_selector(self, domain: str, signature: str) -> CssSchema | None:
        row = self._one(
            "SELECT schema_json FROM selector_cache WHERE domain = ? AND signature = ?",
            (domain, signature),
        )
        if not row:
            return None
        with self._lock:
            self._conn.execute(
                "UPDATE selector_cache SET hits = hits + 1, updated_at = ?"
                " WHERE domain = ? AND signature = ?",
                (iso(), domain, signature),
            )
        return CssSchema.model_validate_json(row["schema_json"])

    def save_selector(self, domain: str, signature: str, schema: CssSchema) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO selector_cache(domain, signature, schema_json, hits, misses, updated_at)"
                " VALUES (?,?,?,0,0,?) ON CONFLICT(domain, signature) DO UPDATE SET"
                " schema_json = excluded.schema_json, updated_at = excluded.updated_at",
                (domain, signature, schema.model_dump_json(), iso()),
            )

    def mark_selector_miss(self, domain: str, signature: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE selector_cache SET misses = misses + 1, updated_at = ?"
                " WHERE domain = ? AND signature = ?",
                (iso(), domain, signature),
            )

    def delete_selector(self, domain: str, signature: str) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM selector_cache WHERE domain = ? AND signature = ?",
                (domain, signature),
            )
        return cur.rowcount > 0

    # -- raw page index ---------------------------------------------------
    def remember_page(self, url: str, content_hash: str, status: int, size: int, engine: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO raw_pages(url, content_hash, status, fetched_at, size, engine)"
                " VALUES (?,?,?,?,?,?) ON CONFLICT(url) DO UPDATE SET"
                " content_hash = excluded.content_hash, status = excluded.status,"
                " fetched_at = excluded.fetched_at, size = excluded.size, engine = excluded.engine",
                (url, content_hash, status, iso(), size, engine),
            )

    # -- tombstone bookkeeping (P1-03) -------------------------------------
    def bump_misses(self, dataset_id: str, keys: Iterable[str], run_id: str) -> dict[str, int]:
        """+1 miss for each key (once per run); returns the new miss counts."""
        keys = list(dict.fromkeys(keys))
        if not keys:
            return {}
        now = iso()
        with self._tx() as conn:
            conn.executemany(
                "INSERT INTO record_misses(dataset_id, record_key, misses, last_run_id, updated_at)"
                " VALUES (?,?,1,?,?) ON CONFLICT(dataset_id, record_key) DO UPDATE SET"
                " misses = CASE WHEN record_misses.last_run_id IS excluded.last_run_id"
                "   THEN record_misses.misses ELSE record_misses.misses + 1 END,"
                " last_run_id = excluded.last_run_id, updated_at = excluded.updated_at",
                [(dataset_id, key, run_id, now) for key in keys],
            )
            placeholders = ",".join("?" * len(keys))
            rows = conn.execute(
                f"SELECT record_key, misses FROM record_misses WHERE dataset_id = ?"
                f" AND record_key IN ({placeholders})",
                (dataset_id, *keys),
            ).fetchall()
        return {row["record_key"]: int(row["misses"]) for row in rows}

    def reset_misses(self, dataset_id: str, keys: Iterable[str]) -> None:
        batch = [(dataset_id, key) for key in keys]
        if batch:
            with self._lock:
                self._conn.executemany(
                    "DELETE FROM record_misses WHERE dataset_id = ? AND record_key = ?", batch
                )

    def rollback_misses(self, run_id: str) -> None:
        """Undo the misses counted by a run that never finished."""
        with self._lock:
            self._conn.execute(
                "UPDATE record_misses SET misses = misses - 1, last_run_id = NULL"
                " WHERE last_run_id = ?",
                (run_id,),
            )
            self._conn.execute("DELETE FROM record_misses WHERE misses <= 0")

    def forget_pages(self, urls: Iterable[str]) -> None:
        batch = [(url,) for url in urls]
        if not batch:
            return
        with self._lock:
            self._conn.executemany("DELETE FROM raw_pages WHERE url = ?", batch)

    def page_meta(self, url: str) -> dict[str, Any] | None:
        row = self._one("SELECT * FROM raw_pages WHERE url = ?", (url,))
        return dict(row) if row else None

    # -- llm ledger -------------------------------------------------------
    def record_llm_call(
        self,
        *,
        purpose: str,
        provider: str,
        model: str,
        tokens_in: int,
        tokens_out: int,
        usd: float,
        cached: bool,
        task_id: str | None = None,
        run_id: str | None = None,
    ) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO llm_calls(task_id, run_id, purpose, provider, model, tokens_in,"
                " tokens_out, usd, cached, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (task_id, run_id, purpose, provider, model, tokens_in, tokens_out, usd,
                 int(cached), iso()),
            )

    def run_spend(self, run_id: str) -> float:
        row = self._one("SELECT COALESCE(SUM(usd), 0) AS s FROM llm_calls WHERE run_id = ?", (run_id,))
        return float(row["s"]) if row else 0.0

    def task_spend(self, task_id: str) -> float:
        row = self._one("SELECT COALESCE(SUM(usd), 0) AS s FROM llm_calls WHERE task_id = ?", (task_id,))
        return float(row["s"]) if row else 0.0

    def bind_llm_calls(self, run_id: str, task_id: str) -> int:
        with self._lock:
            cur = self._conn.execute(
                "UPDATE llm_calls SET task_id = ? WHERE run_id = ? AND task_id IS NULL",
                (task_id, run_id),
            )
        return cur.rowcount

    # -- source registry --------------------------------------------------
    def get_source_policy(self, domain: str) -> SourcePolicy | None:
        key = normalize_domain(domain) or domain.strip().lower()
        row = self._one(
            "SELECT payload_json FROM source_registry WHERE domain IN (?, ?)"
            " ORDER BY domain = ? DESC LIMIT 1",
            (key, domain, key),
        )
        if not row:
            return None
        policy = SourcePolicy.model_validate_json(row["payload_json"])
        policy.domain = key
        return policy

    def upsert_source_policy(self, policy: SourcePolicy) -> None:
        # One canonical key per host: «WWW.Example.com» and «example.com» are the same source.
        key = normalize_domain(policy.domain) or policy.domain.strip().lower()
        policy = policy.model_copy(update={"domain": key})
        with self._lock:
            self._conn.execute(
                "INSERT INTO source_registry(domain, payload_json, updated_at) VALUES (?,?,?)"
                " ON CONFLICT(domain) DO UPDATE SET payload_json = excluded.payload_json,"
                " updated_at = excluded.updated_at",
                (policy.domain, policy.model_dump_json(), iso()),
            )

    def list_source_policies(self) -> list[SourcePolicy]:
        return [
            SourcePolicy.model_validate_json(row["payload_json"])
            for row in self._all("SELECT payload_json FROM source_registry ORDER BY domain")
        ]

    # -- api state --------------------------------------------------------
    def get_api_state(self, task_id: str) -> dict[str, Any] | None:
        row = self._one(
            "SELECT task_id, next_cursor, next_url, last_modified, updated_at"
            " FROM api_state WHERE task_id = ?",
            (task_id,),
        )
        return dict(row) if row else None

    def save_api_state(
        self,
        task_id: str,
        *,
        next_cursor: str | None = None,
        next_url: str | None = None,
        last_modified: str | None = None,
    ) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO api_state(task_id, next_cursor, next_url, last_modified, updated_at)"
                " VALUES (?,?,?,?,?)"
                " ON CONFLICT(task_id) DO UPDATE SET"
                " next_cursor = excluded.next_cursor,"
                " next_url = excluded.next_url,"
                " last_modified = COALESCE(excluded.last_modified, api_state.last_modified),"
                " updated_at = excluded.updated_at",
                (task_id, next_cursor, next_url, last_modified, iso()),
            )

    def reset_api_state(self, task_id: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM api_state WHERE task_id = ?", (task_id,))

    # -- schedules --------------------------------------------------------
    def _to_schedule(self, row: sqlite3.Row) -> ScheduleInfo:
        return ScheduleInfo(
            task_id=row["task_id"],
            cron=row["cron"],
            enabled=bool(row["enabled"]),
            catch_up=bool(row["catch_up"]),
            acknowledge_high_risk=bool(row["acknowledge_high_risk"]),
            reason=row["reason"] or "",
            next_run_at=datetime.fromisoformat(row["next_run_at"]) if row["next_run_at"] else None,
            last_run_at=datetime.fromisoformat(row["last_run_at"]) if row["last_run_at"] else None,
            last_run_id=row["last_run_id"],
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )

    def get_schedule(self, task_id: str) -> ScheduleInfo | None:
        row = self._one("SELECT * FROM schedules WHERE task_id = ?", (task_id,))
        return self._to_schedule(row) if row else None

    def list_schedules(self) -> list[ScheduleInfo]:
        return [
            self._to_schedule(row)
            for row in self._all("SELECT * FROM schedules ORDER BY updated_at DESC")
        ]

    def list_due_schedules(self, now_iso: str) -> list[ScheduleInfo]:
        return [
            self._to_schedule(row)
            for row in self._all(
                "SELECT * FROM schedules"
                " WHERE enabled = 1 AND next_run_at IS NOT NULL AND next_run_at <= ?"
                " ORDER BY next_run_at ASC",
                (now_iso,),
            )
        ]

    def upsert_schedule(
        self,
        task_id: str,
        *,
        cron: str,
        enabled: bool = True,
        catch_up: bool = True,
        acknowledge_high_risk: bool = False,
        reason: str = "",
        next_run_at: datetime | None = None,
    ) -> ScheduleInfo:
        now_str = iso()
        next_str = iso(next_run_at) if next_run_at is not None else None
        with self._lock:
            self._conn.execute(
                "INSERT INTO schedules(task_id, cron, enabled, catch_up, acknowledge_high_risk,"
                " reason, next_run_at, created_at, updated_at)"
                " VALUES (?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(task_id) DO UPDATE SET"
                " cron = excluded.cron,"
                " enabled = excluded.enabled,"
                " catch_up = excluded.catch_up,"
                " acknowledge_high_risk = excluded.acknowledge_high_risk,"
                " reason = excluded.reason,"
                " next_run_at = excluded.next_run_at,"
                " updated_at = excluded.updated_at",
                (
                    task_id,
                    cron,
                    int(enabled),
                    int(catch_up),
                    int(acknowledge_high_risk),
                    reason,
                    next_str,
                    now_str,
                    now_str,
                ),
            )
        sched = self.get_schedule(task_id)
        assert sched is not None
        return sched

    def mark_schedule_tick(
        self,
        task_id: str,
        *,
        next_run_at: datetime | None,
        last_run_at: datetime | None = None,
        last_run_id: str | None = None,
    ) -> ScheduleInfo | None:
        next_str = iso(next_run_at) if next_run_at is not None else None
        last_str = iso(last_run_at) if last_run_at is not None else None
        with self._lock:
            self._conn.execute(
                "UPDATE schedules SET next_run_at = ?,"
                " last_run_at = COALESCE(?, last_run_at),"
                " last_run_id = COALESCE(?, last_run_id),"
                " updated_at = ?"
                " WHERE task_id = ?",
                (next_str, last_str, last_run_id, iso(), task_id),
            )
        return self.get_schedule(task_id)

    def delete_schedule(self, task_id: str) -> bool:
        with self._lock:
            cur = self._conn.execute("DELETE FROM schedules WHERE task_id = ?", (task_id,))
        return cur.rowcount > 0

    # -- subscriptions & delivery log (D-01, 013) --------------------------
    def _to_subscription(self, row: sqlite3.Row) -> SubscriptionInfo:
        filters_raw = json_loads(row["filters_json"], {})
        row_keys = set(row.keys())
        next_digest = (
            datetime.fromisoformat(row["next_digest_at"])
            if "next_digest_at" in row_keys and row["next_digest_at"]
            else None
        )
        last_digest = (
            datetime.fromisoformat(row["last_digest_at"])
            if "last_digest_at" in row_keys and row["last_digest_at"]
            else None
        )
        pending_count = (
            int(row["pending_digest_count"] or 0) if "pending_digest_count" in row_keys else 0
        )
        return SubscriptionInfo(
            id=row["id"],
            task_id=row["task_id"],
            name=row["name"] or "",
            channel=row["channel"],
            target=row["target"],
            secret=row["secret"] or "",
            filters=DeliveryFilters.model_validate(filters_raw),
            enabled=bool(row["enabled"]),
            next_digest_at=next_digest,
            last_digest_at=last_digest,
            pending_digest_count=pending_count,
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )

    def get_subscription(self, sub_id: str) -> SubscriptionInfo | None:
        row = self._one(
            "SELECT s.*, "
            "(SELECT COUNT(*) FROM delivery_digest_queue q "
            " WHERE q.subscription_id = s.id AND q.flushed_at IS NULL) AS pending_digest_count "
            "FROM subscriptions s WHERE s.id = ?",
            (sub_id,),
        )
        return self._to_subscription(row) if row else None

    def list_subscriptions(self, task_id: str) -> list[SubscriptionInfo]:
        return [
            self._to_subscription(row)
            for row in self._all(
                "SELECT s.*, "
                "(SELECT COUNT(*) FROM delivery_digest_queue q "
                " WHERE q.subscription_id = s.id AND q.flushed_at IS NULL) AS pending_digest_count "
                "FROM subscriptions s WHERE s.task_id = ? "
                "ORDER BY s.created_at ASC, s.rowid ASC",
                (task_id,),
            )
        ]

    def create_subscription(
        self,
        task_id: str,
        *,
        name: str = "",
        channel: str,
        target: str,
        secret: str = "",
        filters: DeliveryFilters | None = None,
        enabled: bool = True,
        sub_id: str | None = None,
        next_digest_at: datetime | None = None,
    ) -> SubscriptionInfo:
        sub_id = sub_id or uuid.uuid4().hex[:12]
        now_str = iso()
        filters_json = (filters or DeliveryFilters()).model_dump_json()
        next_digest_str = iso(next_digest_at) if next_digest_at is not None else None
        with self._lock:
            self._conn.execute(
                "INSERT INTO subscriptions(id, task_id, name, channel, target, secret,"
                " filters_json, enabled, next_digest_at, created_at, updated_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    sub_id,
                    task_id,
                    name,
                    channel,
                    target,
                    secret,
                    filters_json,
                    int(enabled),
                    next_digest_str,
                    now_str,
                    now_str,
                ),
            )
        sub = self.get_subscription(sub_id)
        assert sub is not None
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
        next_digest_at: Any = _UNSET,
    ) -> SubscriptionInfo | None:
        existing = self._one(
            "SELECT * FROM subscriptions WHERE id = ? AND task_id = ?", (sub_id, task_id)
        )
        if existing is None:
            return None
        fields: list[str] = []
        values: list[Any] = []
        if name is not None:
            fields.append("name = ?")
            values.append(name)
        if target is not None:
            fields.append("target = ?")
            values.append(target)
        if secret is not None:
            fields.append("secret = ?")
            values.append(secret)
        if filters is not None:
            fields.append("filters_json = ?")
            values.append(filters.model_dump_json())
        if enabled is not None:
            fields.append("enabled = ?")
            values.append(int(enabled))
        if next_digest_at is not _UNSET:
            fields.append("next_digest_at = ?")
            values.append(iso(next_digest_at) if next_digest_at is not None else None)
        if not fields:
            return self.get_subscription(sub_id)
        fields.append("updated_at = ?")
        values.append(iso())
        values.extend([sub_id, task_id])
        with self._lock:
            self._conn.execute(
                f"UPDATE subscriptions SET {', '.join(fields)} WHERE id = ? AND task_id = ?",
                values,
            )
        return self.get_subscription(sub_id)

    def delete_subscription(self, task_id: str, sub_id: str) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM subscriptions WHERE id = ? AND task_id = ?", (sub_id, task_id)
            )
        return cur.rowcount > 0

    def is_delivered(self, subscription_id: str, dedup_key: str) -> bool:
        row = self._one(
            "SELECT 1 FROM delivery_log WHERE subscription_id = ? AND dedup_key = ?"
            " AND status = 'sent' LIMIT 1",
            (subscription_id, dedup_key),
        )
        return row is not None

    def record_delivery(
        self,
        subscription_id: str,
        dedup_key: str,
        *,
        run_id: str | None = None,
        status: str = "sent",
        attempts: int = 1,
        error: str | None = None,
    ) -> None:
        now_str = iso()
        delivered_str = now_str if status == "sent" else None
        with self._lock:
            self._conn.execute(
                "INSERT INTO delivery_log(subscription_id, dedup_key, run_id, status, attempts,"
                " error, created_at, delivered_at) VALUES (?,?,?,?,?,?,?,?)"
                " ON CONFLICT(subscription_id, dedup_key) DO UPDATE SET"
                " run_id = excluded.run_id, status = excluded.status,"
                " attempts = excluded.attempts, error = excluded.error,"
                " delivered_at = COALESCE(excluded.delivered_at, delivery_log.delivered_at)",
                (subscription_id, dedup_key, run_id, status, attempts, error, now_str, delivered_str),
            )

    def enqueue_digest_items(
        self,
        subscription_id: str,
        task_id: str,
        dataset_version: int,
        run_id: str | None,
        *,
        items: list[tuple[str, dict[str, Any], dict[str, Any]]],
    ) -> int:
        if not items:
            return 0
        now_str = iso()
        added = 0
        with self._lock:
            for dedup_key, change_dict, record_dict in items:
                cur = self._conn.execute(
                    "INSERT OR IGNORE INTO delivery_digest_queue("
                    "subscription_id, task_id, dataset_version, run_id, dedup_key,"
                    " change_json, record_json, created_at"
                    ") VALUES (?,?,?,?,?,?,?,?)",
                    (
                        subscription_id,
                        task_id,
                        dataset_version,
                        run_id,
                        dedup_key,
                        json_dumps(change_dict),
                        json_dumps(record_dict),
                        now_str,
                    ),
                )
                added += cur.rowcount
        return added

    def list_pending_digest(self, subscription_id: str) -> list[dict[str, Any]]:
        rows = self._all(
            "SELECT id, subscription_id, task_id, dataset_version, run_id, dedup_key,"
            " change_json, record_json, created_at "
            "FROM delivery_digest_queue "
            "WHERE subscription_id = ? AND flushed_at IS NULL "
            "ORDER BY id ASC",
            (subscription_id,),
        )
        return [
            {
                "id": int(row["id"]),
                "subscription_id": str(row["subscription_id"]),
                "task_id": str(row["task_id"]),
                "dataset_version": int(row["dataset_version"]),
                "run_id": row["run_id"],
                "dedup_key": str(row["dedup_key"]),
                "change": json_loads(row["change_json"], {}),
                "record": json_loads(row["record_json"], {}),
                "created_at": str(row["created_at"]),
            }
            for row in rows
        ]

    def mark_digest_flushed(self, subscription_id: str, queue_ids: list[int]) -> None:
        if not queue_ids:
            return
        now_str = iso()
        with self._lock:
            for start in range(0, len(queue_ids), 200):
                batch = queue_ids[start : start + 200]
                placeholders = ",".join("?" for _ in batch)
                self._conn.execute(
                    f"UPDATE delivery_digest_queue SET flushed_at = ? "
                    f"WHERE subscription_id = ? AND flushed_at IS NULL AND id IN ({placeholders})",
                    [now_str, subscription_id, *batch],
                )

    def list_due_digest_subscriptions(self, now_iso: str) -> list[SubscriptionInfo]:
        rows = self._all(
            "SELECT s.*, "
            "(SELECT COUNT(*) FROM delivery_digest_queue q "
            " WHERE q.subscription_id = s.id AND q.flushed_at IS NULL) AS pending_digest_count "
            "FROM subscriptions s "
            "WHERE s.enabled = 1 AND s.next_digest_at IS NOT NULL AND s.next_digest_at <= ? "
            "ORDER BY s.next_digest_at ASC",
            (now_iso,),
        )
        due = [self._to_subscription(row) for row in rows]
        return [sub for sub in due if sub.filters.delivery_mode == "digest_daily"]

    def advance_subscription_digest(
        self,
        subscription_id: str,
        *,
        next_digest_at: datetime | None,
        last_digest_at: datetime | None = None,
    ) -> None:
        now_str = iso()
        fields = ["next_digest_at = ?", "updated_at = ?"]
        values: list[Any] = [iso(next_digest_at) if next_digest_at is not None else None, now_str]
        if last_digest_at is not None:
            fields.append("last_digest_at = ?")
            values.append(iso(last_digest_at))
        values.append(subscription_id)
        with self._lock:
            self._conn.execute(
                f"UPDATE subscriptions SET {', '.join(fields)} WHERE id = ?",
                values,
            )

    # -- entity resolution links (P1-02) ----------------------------------
    @staticmethod
    def _to_entity_link(row: sqlite3.Row) -> EntityLinkInfo:
        unmerged_raw = row["unmerged_at"]
        return EntityLinkInfo(
            id=str(row["id"]),
            task_id=str(row["task_id"]),
            canonical_key=str(row["canonical_key"]),
            merged_key=str(row["merged_key"]),
            status=str(row["status"]),  # type: ignore[arg-type]
            score=float(row["score"] or 0.0),
            rule=str(row["rule"] or "manual"),
            policy=str(row["policy"] or "confidence"),  # type: ignore[arg-type]
            reviewer=str(row["reviewer"] or "user"),
            canonical_before_rev=(
                int(row["canonical_before_rev"])
                if row["canonical_before_rev"] is not None
                else None
            ),
            merged_before_rev=(
                int(row["merged_before_rev"]) if row["merged_before_rev"] is not None else None
            ),
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
            unmerged_at=datetime.fromisoformat(unmerged_raw) if unmerged_raw else None,
        )

    def create_entity_link(
        self,
        task_id: str,
        canonical_key: str,
        merged_key: str,
        *,
        status: EntityLinkStatus,
        score: float = 0.0,
        rule: str = "manual",
        policy: MergePolicy = "confidence",
        reviewer: str = "user",
        canonical_before_rev: int | None = None,
        merged_before_rev: int | None = None,
    ) -> EntityLinkInfo:
        link_id = uuid.uuid4().hex[:16]
        now_str = iso()
        with self._lock:
            self._conn.execute(
                "INSERT INTO entity_links(id, task_id, canonical_key, merged_key, status, score,"
                " rule, policy, reviewer, canonical_before_rev, merged_before_rev, created_at,"
                " updated_at, unmerged_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,NULL)",
                (
                    link_id,
                    task_id,
                    canonical_key,
                    merged_key,
                    status,
                    float(score),
                    rule,
                    policy,
                    reviewer,
                    canonical_before_rev,
                    merged_before_rev,
                    now_str,
                    now_str,
                ),
            )
        link = self.get_entity_link(task_id, link_id)
        assert link is not None
        return link

    def get_entity_link(self, task_id: str, link_id: str) -> EntityLinkInfo | None:
        row = self._one(
            "SELECT * FROM entity_links WHERE id = ? AND task_id = ?", (link_id, task_id)
        )
        return self._to_entity_link(row) if row else None

    def list_entity_links(
        self, task_id: str, *, status: EntityLinkStatus | None = None
    ) -> list[EntityLinkInfo]:
        if status is not None:
            rows = self._all(
                "SELECT * FROM entity_links WHERE task_id = ? AND status = ?"
                " ORDER BY created_at DESC, rowid DESC",
                (task_id, status),
            )
        else:
            rows = self._all(
                "SELECT * FROM entity_links WHERE task_id = ?"
                " ORDER BY created_at DESC, rowid DESC",
                (task_id,),
            )
        return [self._to_entity_link(row) for row in rows]

    def list_active_merges_for_canonical(
        self, task_id: str, canonical_key: str
    ) -> list[EntityLinkInfo]:
        rows = self._all(
            "SELECT * FROM entity_links WHERE task_id = ? AND canonical_key = ?"
            " AND status = 'merged' ORDER BY created_at ASC, rowid ASC",
            (task_id, canonical_key),
        )
        return [self._to_entity_link(row) for row in rows]

    def find_active_pair_link(
        self, task_id: str, key_a: str, key_b: str
    ) -> EntityLinkInfo | None:
        row = self._one(
            "SELECT * FROM entity_links WHERE task_id = ? AND status IN ('merged', 'dismissed')"
            " AND ((canonical_key = ? AND merged_key = ?) OR (canonical_key = ? AND merged_key = ?))"
            " ORDER BY created_at DESC, rowid DESC LIMIT 1",
            (task_id, key_a, key_b, key_b, key_a),
        )
        return self._to_entity_link(row) if row else None

    def active_merged_key_map(self, task_id: str) -> dict[str, str]:
        rows = self._all(
            "SELECT canonical_key, merged_key FROM entity_links"
            " WHERE task_id = ? AND status = 'merged' ORDER BY rowid ASC",
            (task_id,),
        )
        direct = {str(r["merged_key"]): str(r["canonical_key"]) for r in rows}
        resolved: dict[str, str] = {}
        for mkey in direct:
            curr = mkey
            seen_chain: set[str] = set()
            while curr in direct and curr not in seen_chain:
                seen_chain.add(curr)
                curr = direct[curr]
            resolved[mkey] = curr
        return resolved

    def excluded_duplicate_pairs(self, task_id: str) -> set[tuple[str, str]]:
        rows = self._all(
            "SELECT canonical_key, merged_key FROM entity_links"
            " WHERE task_id = ? AND status IN ('merged', 'dismissed')",
            (task_id,),
        )
        return {
            tuple(sorted((str(r["canonical_key"]), str(r["merged_key"]))))
            for r in rows
        }

    def mark_entity_link_unmerged(self, task_id: str, link_id: str) -> EntityLinkInfo | None:
        now_str = iso()
        with self._lock:
            self._conn.execute(
                "UPDATE entity_links SET status = 'unmerged', updated_at = ?, unmerged_at = ?"
                " WHERE id = ? AND task_id = ? AND status = 'merged'",
                (now_str, now_str, link_id, task_id),
            )
        return self.get_entity_link(task_id, link_id)

    # -- aggregates -------------------------------------------------------
    def overview(self) -> dict[str, Any]:
        tasks = self._one("SELECT COUNT(*) AS c FROM tasks")
        running = self._one("SELECT COUNT(*) AS c FROM runs WHERE status = 'running'")
        spend = self._one("SELECT COALESCE(SUM(usd), 0) AS s FROM llm_calls")
        queued = self._one("SELECT COUNT(*) AS c FROM jobs WHERE status = 'queued'")
        failed = self._one("SELECT COUNT(*) AS c FROM jobs WHERE status = 'failed'")
        return {
            "tasks": tasks["c"] if tasks else 0,
            "running_runs": running["c"] if running else 0,
            "queued_jobs": queued["c"] if queued else 0,
            "failed_jobs": failed["c"] if failed else 0,
            "llm_spend_usd": round(float(spend["s"]), 4) if spend else 0.0,
        }

    def metrics_snapshot(self) -> dict[str, Any]:
        tasks_by_status = {"draft": 0, "ready": 0, "running": 0, "done": 0, "failed": 0}
        runs_by_status = {
            "queued": 0,
            "running": 0,
            "succeeded": 0,
            "failed": 0,
            "partial": 0,
            "cancelled": 0,
        }
        jobs_by_status = {
            "queued": 0,
            "leased": 0,
            "done": 0,
            "failed": 0,
            "skipped": 0,
            "cancelled": 0,
        }
        pages_fetched = 0
        pages_cached = 0
        pages_failed = 0
        robots_blocked = 0
        llm_by_purpose: dict[str, dict[str, Any]] = {}
        llm_spend_usd_total = 0.0
        llm_calls_total = 0

        with self._lock:
            for row in self._conn.execute("SELECT status, COUNT(*) AS c FROM tasks GROUP BY status"):
                tasks_by_status[str(row["status"])] = int(row["c"])
            for row in self._conn.execute("SELECT status, COUNT(*) AS c FROM runs GROUP BY status"):
                runs_by_status[str(row["status"])] = int(row["c"])
            for row in self._conn.execute("SELECT status, COUNT(*) AS c FROM jobs GROUP BY status"):
                jobs_by_status[str(row["status"])] = int(row["c"])
            for row in self._conn.execute("SELECT stats_json FROM runs"):
                stats = json_loads(row["stats_json"], {})
                if isinstance(stats, dict):
                    pages_fetched += int(stats.get("pages_fetched") or 0)
                    pages_cached += int(stats.get("pages_cached") or 0)
                    pages_failed += int(stats.get("pages_failed") or 0)
                    robots_blocked += int(stats.get("robots_blocked") or 0)
            comp_row = self._conn.execute(
                "SELECT COUNT(*) AS c FROM events WHERE type = 'compliance_blocked'"
            ).fetchone()
            compliance_blocked = int(comp_row["c"]) if comp_row else 0
            for row in self._conn.execute(
                "SELECT purpose, COUNT(*) AS calls, COALESCE(SUM(usd), 0) AS usd,"
                " COALESCE(SUM(tokens_in), 0) AS tokens_in,"
                " COALESCE(SUM(tokens_out), 0) AS tokens_out"
                " FROM llm_calls GROUP BY purpose"
            ):
                purpose = str(row["purpose"])
                calls = int(row["calls"])
                usd = float(row["usd"])
                llm_by_purpose[purpose] = {
                    "calls": calls,
                    "usd": usd,
                    "tokens_in": int(row["tokens_in"]),
                    "tokens_out": int(row["tokens_out"]),
                }
                llm_calls_total += calls
                llm_spend_usd_total += usd

        return {
            "tasks_by_status": tasks_by_status,
            "runs_by_status": runs_by_status,
            "jobs_by_status": jobs_by_status,
            "pages_fetched": pages_fetched,
            "pages_cached": pages_cached,
            "pages_failed": pages_failed,
            "robots_blocked": robots_blocked,
            "compliance_blocked": compliance_blocked,
            "llm_spend_usd_total": round(llm_spend_usd_total, 6),
            "llm_calls_total": llm_calls_total,
            "llm_by_purpose": llm_by_purpose,
        }

    def dataset_manifest(self, dataset_id: str) -> dict[str, Any]:
        row = self._one(
            "SELECT manifest_json FROM dataset_versions WHERE dataset_id = ?"
            " ORDER BY version DESC LIMIT 1",
            (dataset_id,),
        )
        return json_loads(row["manifest_json"], {}) if row else {}


def infer_columns(task: TaskSpec) -> list[FieldSpec]:
    return task.columns
