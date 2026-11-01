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
    EventInfo,
    FieldSpec,
    RunInfo,
    RunStats,
    SourcePolicy,
    TaskInfo,
    TaskSpec,
)
from ..utils import iso, json_dumps, json_loads, utcnow

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
]


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
    ) -> RunInfo:
        run_id = run_id or uuid.uuid4().hex[:12]
        now = iso()
        with self._lock:
            self._conn.execute(
                "INSERT INTO runs(id, task_id, kind, status, stats_json, budget_usd, created_at)"
                " VALUES (?,?,?,?,?,?,?)",
                (run_id, task_id, kind, "queued", (stats or RunStats()).model_dump_json(), budget_usd, now),
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
        self, worker: str, *, limit: int = 8, lease_seconds: int = 120
    ) -> list[dict[str, Any]]:
        now = utcnow()
        lease_until = iso(now + timedelta(seconds=lease_seconds))
        with self._lock:
            cursor = self._conn.execute(
                """
                UPDATE jobs SET status = 'leased', worker = ?, lease_until = ?,
                       attempts = attempts + 1, updated_at = ?
                WHERE id IN (
                    SELECT id FROM jobs
                    WHERE status = 'queued'
                    ORDER BY id
                    LIMIT ?
                )
                RETURNING id, run_id, task_id, kind, url, depth, parent_url, attempts, max_attempts
                """,
                (worker, lease_until, iso(now), limit),
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
    def save_calibration(self, task_id: str, run_id: str | None, report_json: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO calibrations(task_id, run_id, report_json, created_at)"
                " VALUES (?,?,?,?) ON CONFLICT(task_id) DO UPDATE SET"
                " run_id = excluded.run_id, report_json = excluded.report_json,"
                " created_at = excluded.created_at",
                (task_id, run_id, report_json, iso()),
            )

    def get_calibration(self, task_id: str) -> str | None:
        row = self._one("SELECT report_json FROM calibrations WHERE task_id = ?", (task_id,))
        return row["report_json"] if row else None

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

    # -- source registry --------------------------------------------------
    def get_source_policy(self, domain: str) -> SourcePolicy | None:
        row = self._one("SELECT payload_json FROM source_registry WHERE domain = ?", (domain,))
        if not row:
            return None
        return SourcePolicy.model_validate_json(row["payload_json"])

    def upsert_source_policy(self, policy: SourcePolicy) -> None:
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

    def dataset_manifest(self, dataset_id: str) -> dict[str, Any]:
        row = self._one(
            "SELECT manifest_json FROM dataset_versions WHERE dataset_id = ?"
            " ORDER BY version DESC LIMIT 1",
            (dataset_id,),
        )
        return json_loads(row["manifest_json"], {}) if row else {}


def infer_columns(task: TaskSpec) -> list[FieldSpec]:
    return task.columns
