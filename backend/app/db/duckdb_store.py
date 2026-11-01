"""Analytical store (DuckDB): append-only record versions, snapshots, exports.

Design notes (ADR-02/ADR-05):
* ``record_versions`` is append-only — history is never overwritten, only extended.
* The API and the worker share one process-local connection guarded by a lock. When the
  studio grows, move the worker into its own process and open this file read-only there.
* Every write goes through a small number of explicit methods; no ad-hoc SQL in routes.
"""

from __future__ import annotations

import threading
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import duckdb

from ..config import Paths
from ..models import ChangeEvent, ExtractedRecord, FieldSpec
from ..utils import json_dumps, json_loads, normalize_domain, safe_filename

SCHEMA = """
CREATE SEQUENCE IF NOT EXISTS record_revision_seq;

CREATE TABLE IF NOT EXISTS record_versions (
    dataset_id      VARCHAR NOT NULL,
    record_key      VARCHAR NOT NULL,
    dataset_version INTEGER NOT NULL,
    revision        BIGINT DEFAULT nextval('record_revision_seq'),
    run_id          VARCHAR,
    source_url      VARCHAR,
    content_hash    VARCHAR,
    payload         JSON,
    provenance      JSON,
    observed_at     TIMESTAMP NOT NULL,
    PRIMARY KEY (dataset_id, record_key, revision)
);

CREATE INDEX IF NOT EXISTS idx_record_versions_lookup
    ON record_versions(dataset_id, record_key, revision);

CREATE TABLE IF NOT EXISTS quarantine (
    dataset_id  VARCHAR NOT NULL,
    run_id      VARCHAR,
    source_url  VARCHAR,
    reason      VARCHAR,
    payload     JSON,
    created_at  TIMESTAMP NOT NULL
);
"""

CURRENT_VIEW = """
CREATE OR REPLACE VIEW current_records AS
SELECT dataset_id, record_key, dataset_version, payload, provenance, source_url, observed_at
FROM (
    SELECT *, row_number() OVER (
        PARTITION BY dataset_id, record_key ORDER BY revision DESC
    ) AS rn
    FROM record_versions
)
WHERE rn = 1;
"""

NUMERIC_DTYPES = {"number", "integer"}
_JSON_PATH = "$.\"{key}\""


class AnalyticsStore:
    def __init__(self, paths: Paths, *, read_only: bool = False) -> None:
        paths.ensure()
        self.path: Path = paths.duckdb
        self.read_only = read_only
        self._lock = threading.RLock()
        self._con = duckdb.connect(str(self.path), read_only=read_only)
        with self._lock:
            self._con.execute("SET TimeZone='UTC'")
            self._con.execute("SET preserve_insertion_order=true")
            if not read_only:
                self._con.execute(SCHEMA)
                self._con.execute(CURRENT_VIEW)

    # -- infrastructure ---------------------------------------------------
    def close(self) -> None:
        with self._lock:
            self._con.close()

    def ping(self) -> bool:
        with self._lock:
            return self._con.execute("SELECT 1").fetchone()[0] == 1

    def checkpoint(self) -> None:
        """Flush the WAL so the .duckdb file on disk is complete before copying."""
        with self._lock:
            self._con.execute("CHECKPOINT")

    # -- writes -----------------------------------------------------------
    def append_records(
        self,
        dataset_id: str,
        dataset_version: int,
        run_id: str | None,
        records: list[ExtractedRecord],
    ) -> int:
        if not records:
            return 0
        rows = []
        for record in records:
            observed = record.provenance and next(iter(record.provenance.values())).observed_at
            observed_at = (observed or datetime.now(UTC))
            if observed_at.tzinfo is not None:
                observed_at = observed_at.astimezone(UTC).replace(tzinfo=None)
            rows.append(
                (
                    dataset_id,
                    record.record_key,
                    dataset_version,
                    run_id,
                    record.source_url,
                    record.content_hash,
                    json_dumps(record.payload),
                    json_dumps({k: v.model_dump(mode="json") for k, v in record.provenance.items()}),
                    observed_at,
                )
            )
        with self._lock:
            self._con.executemany(
                """
                INSERT INTO record_versions
                    (dataset_id, record_key, dataset_version, run_id, source_url,
                     content_hash, payload, provenance, observed_at)
                VALUES (?, ?, ?, ?, ?, ?, ?::JSON, ?::JSON, ?)
                """,
                rows,
            )
        return len(rows)

    def quarantine(
        self, dataset_id: str, run_id: str | None, source_url: str, reason: str, payload: dict
    ) -> None:
        with self._lock:
            self._con.execute(
                "INSERT INTO quarantine VALUES (?, ?, ?, ?, ?::JSON, ?)",
                [dataset_id, run_id, source_url, reason, json_dumps(payload),
                 datetime.now(UTC).replace(tzinfo=None)],
            )

    def delete_dataset(self, dataset_id: str) -> None:
        with self._lock:
            self._con.execute("DELETE FROM record_versions WHERE dataset_id = ?", [dataset_id])
            self._con.execute("DELETE FROM quarantine WHERE dataset_id = ?", [dataset_id])

    # -- reads ------------------------------------------------------------
    def count_records(self, dataset_id: str) -> int:
        with self._lock:
            row = self._con.execute(
                "SELECT COUNT(*) FROM current_records WHERE dataset_id = ?", [dataset_id]
            ).fetchone()
        return int(row[0]) if row else 0

    def freshness(self, dataset_id: str) -> datetime | None:
        with self._lock:
            row = self._con.execute(
                "SELECT MAX(observed_at) FROM current_records WHERE dataset_id = ?", [dataset_id]
            ).fetchone()
        value = row[0] if row else None
        return value.replace(tzinfo=UTC) if isinstance(value, datetime) else None

    def query_records(
        self,
        dataset_id: str,
        *,
        columns: list[FieldSpec],
        query: str | None = None,
        filters: dict[str, str] | None = None,
        sort: str | None = None,
        direction: str = "asc",
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        type_map = {column.key: column.dtype for column in columns}
        where = ["dataset_id = ?"]
        params: list[Any] = [dataset_id]
        if query:
            where.append("payload::VARCHAR ILIKE ?")
            params.append(f"%{query}%")
        for key, value in (filters or {}).items():
            if value in (None, ""):
                continue
            where.append(f"json_extract_string(payload, '{_JSON_PATH.format(key=_safe_key(key))}') ILIKE ?")
            params.append(f"%{value}%")
        where_sql = " AND ".join(where)

        order_sql = "record_key"
        if sort and _safe_key(sort) in type_map:
            key = _safe_key(sort)
            expression = f"json_extract_string(payload, '{_JSON_PATH.format(key=key)}')"
            if type_map.get(key) in NUMERIC_DTYPES:
                order_sql = f"TRY_CAST({expression} AS DOUBLE)"
            elif type_map.get(key) == "datetime":
                order_sql = f"TRY_CAST({expression} AS TIMESTAMP)"
            else:
                order_sql = f"lower({expression})"
            order_sql += " NULLS LAST, record_key"
        order_sql += " DESC" if direction.lower() == "desc" else " ASC"

        with self._lock:
            total = self._con.execute(
                f"SELECT COUNT(*) FROM current_records WHERE {where_sql}", params
            ).fetchone()[0]
            rows = self._con.execute(
                f"SELECT record_key, payload, provenance, source_url, observed_at "
                f"FROM current_records WHERE {where_sql} ORDER BY {order_sql} LIMIT ? OFFSET ?",
                [*params, limit, offset],
            ).fetchall()
        items = [self._row_to_item(row) for row in rows]
        return {"items": items, "total": int(total), "limit": limit, "offset": offset}

    def get_record(self, dataset_id: str, record_key: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._con.execute(
                "SELECT record_key, payload, provenance, source_url, observed_at "
                "FROM current_records WHERE dataset_id = ? AND record_key = ?",
                [dataset_id, record_key],
            ).fetchone()
        return self._row_to_item(row) if row else None

    def record_history(self, dataset_id: str, record_key: str, limit: int = 50) -> list[dict]:
        with self._lock:
            rows = self._con.execute(
                "SELECT dataset_version, revision, payload, provenance, source_url, observed_at "
                "FROM record_versions WHERE dataset_id = ? AND record_key = ? "
                "ORDER BY revision DESC LIMIT ?",
                [dataset_id, record_key, limit],
            ).fetchall()
        return [
            {
                "dataset_version": row[0],
                "revision": row[1],
                "payload": json_loads(row[2], {}),
                "provenance": json_loads(row[3], {}),
                "source_url": row[4],
                "observed_at": row[5],
            }
            for row in rows
        ]

    def all_current(self, dataset_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._con.execute(
                "SELECT record_key, payload, provenance, source_url, observed_at "
                "FROM current_records WHERE dataset_id = ? ORDER BY record_key",
                [dataset_id],
            ).fetchall()
        return [self._row_to_item(row) for row in rows]

    def field_fill_rates(self, dataset_id: str, columns: list[FieldSpec]) -> dict[str, float]:
        if not columns:
            return {}
        selects = ", ".join(
            f"AVG(CASE WHEN json_extract_string(payload, '{_JSON_PATH.format(key=_safe_key(c.key))}')"
            f" IS NOT NULL THEN 1.0 ELSE 0.0 END) AS \"{_safe_key(c.key)}\""
            for c in columns
        )
        with self._lock:
            row = self._con.execute(
                f"SELECT {selects}, COUNT(*) AS total FROM current_records WHERE dataset_id = ?",
                [dataset_id],
            ).fetchone()
        total = row[-1] if row else 0
        if not total:
            return {c.key: 0.0 for c in columns}
        return {
            c.key: round(float(row[index] or 0.0) * 100, 1) for index, c in enumerate(columns)
        }

    def quality(self, dataset_id: str, columns: list[FieldSpec]) -> dict[str, float]:
        rates = self.field_fill_rates(dataset_id, columns)
        if not rates:
            return {}
        metrics = {f"fill_{key}": value for key, value in rates.items()}
        metrics["fill_avg"] = round(sum(rates.values()) / len(rates), 1)
        return metrics

    # -- history / change detection --------------------------------------
    def snapshot(self, dataset_id: str, version: int, destination: Path) -> Path:
        destination.parent.mkdir(parents=True, exist_ok=True)
        tmp = destination.with_suffix(".tmp.parquet")
        with self._lock:
            self._con.execute(
                f"COPY (SELECT * FROM record_versions WHERE dataset_id = ? AND dataset_version <= ?) "
                f"TO '{_escape_path(tmp)}' (FORMAT PARQUET, COMPRESSION ZSTD)",
                [dataset_id, version],
            )
        tmp.replace(destination)
        return destination

    def snapshot_rows(self, dataset_id: str, version: int) -> dict[str, dict[str, Any]]:
        with self._lock:
            rows = self._con.execute(
                """
                SELECT record_key, payload FROM (
                    SELECT record_key, payload,
                           row_number() OVER (PARTITION BY record_key ORDER BY revision DESC) AS rn
                    FROM record_versions WHERE dataset_id = ? AND dataset_version <= ?
                ) WHERE rn = 1
                """,
                [dataset_id, version],
            ).fetchall()
        return {row[0]: json_loads(row[1], {}) for row in rows}

    def diff_versions(
        self,
        dataset_id: str,
        *,
        from_version: int,
        to_version: int,
        columns: list[FieldSpec],
        price_fields: set[str] | None = None,
        price_drop_pct: float = 5.0,
    ) -> list[ChangeEvent]:
        before = self.snapshot_rows(dataset_id, from_version)
        after = self.snapshot_rows(dataset_id, to_version)
        price_fields = price_fields or set()
        events: list[ChangeEvent] = []

        for key in after.keys() - before.keys():
            events.append(ChangeEvent(type="new_entity", record_key=key))
        for key in before.keys() - after.keys():
            events.append(ChangeEvent(type="removed_entity", record_key=key))

        for key in before.keys() & after.keys():
            old, new = before[key], after[key]
            for column in columns:
                if column.method == "enrichment" and column.key not in old and column.key not in new:
                    continue
                old_value, new_value = old.get(column.key), new.get(column.key)
                if _same_value(old_value, new_value):
                    continue
                delta_pct: float | None = None
                if (
                    column.key in price_fields
                    and isinstance(old_value, (int, float))
                    and old_value
                    and isinstance(new_value, (int, float))
                ):
                    delta_pct = round((new_value - old_value) / old_value * 100, 2)
                events.append(
                    ChangeEvent(
                        type="field_changed",
                        record_key=key,
                        field=column.key,
                        old=old_value,
                        new=new_value,
                        delta_pct=delta_pct,
                    )
                )
                if (
                    column.key in price_fields
                    and delta_pct is not None
                    and delta_pct <= -abs(price_drop_pct)
                ):
                    events.append(
                        ChangeEvent(
                            type="price_drop",
                            record_key=key,
                            field=column.key,
                            old=old_value,
                            new=new_value,
                            delta_pct=delta_pct,
                        )
                    )
        return events

    # -- exports ----------------------------------------------------------
    def export_rows(
        self,
        dataset_id: str,
        columns: list[FieldSpec],
        *,
        limit: int | None = None,
        mask: dict[str, bool] | None = None,
    ) -> list[dict[str, Any]]:
        rows = self.all_current(dataset_id)
        if limit:
            rows = rows[:limit]
        result = []
        for item in rows:
            payload = item["payload"]
            record = {"record_key": item["record_key"]}
            for column in columns:
                record[column.key] = payload.get(column.key)
            record["_source_url"] = item["source_url"]
            record["_observed_at"] = item["observed_at"]
            result.append(record)
        return result

    def write_tabular_export(
        self,
        rows: list[dict[str, Any]],
        columns: list[FieldSpec],
        destination: Path,
        fmt: str,
    ) -> Path:
        destination.parent.mkdir(parents=True, exist_ok=True)
        names = ["record_key", *[column.key for column in columns], "_source_url", "_observed_at"]
        types = {"number": "DOUBLE", "integer": "BIGINT", "boolean": "BOOLEAN",
                 "datetime": "VARCHAR"}
        definition = ", ".join(
            f'"{name}" {types.get(_dtype_of(name, columns), "VARCHAR")}' for name in names
        )
        temp_table = f"_export_{uuid.uuid4().hex[:8]}"
        values = [
            (
                *[
                    _coerce_for_duck(name, row.get(name), _dtype_of(name, columns))
                    for name in names
                ],
            )
            for row in rows
        ]
        placeholders = ", ".join(["?"] * len(names))
        with self._lock:
            self._con.execute(f"CREATE TEMP TABLE {temp_table} ({definition})")
            try:
                if values:
                    self._con.executemany(
                        f"INSERT INTO {temp_table} VALUES ({placeholders})", values
                    )
                options = "FORMAT PARQUET, COMPRESSION ZSTD" if fmt == "parquet" else "HEADER, DELIMITER ','"
                self._con.execute(
                    f"COPY (SELECT * FROM {temp_table}) TO '{_escape_path(destination)}' "
                    f"({options})"
                )
            finally:
                self._con.execute(f"DROP TABLE IF EXISTS {temp_table}")
        return destination

    # -- misc -------------------------------------------------------------
    def _row_to_item(self, row: tuple) -> dict[str, Any]:
        payload = json_loads(row[1], {})
        provenance = json_loads(row[2], {})
        confidence = [
            item.get("confidence", 0.0) for item in provenance.values() if isinstance(item, dict)
        ]
        return {
            "record_key": row[0],
            "payload": payload,
            "provenance": provenance,
            "source_url": row[3],
            "observed_at": row[4],
            "confidence": round(sum(confidence) / len(confidence), 3) if confidence else 0.0,
        }


def _safe_key(key: str) -> str:
    if not key.replace("_", "").isalnum():
        raise ValueError(f"unsafe column key: {key!r}")
    return key


def _escape_path(path: Path) -> str:
    return str(path).replace("'", "''")


def _dtype_of(name: str, columns: list[FieldSpec]) -> str:
    for column in columns:
        if column.key == name:
            return column.dtype
    return "string"


def _coerce_for_duck(name: str, value: Any, dtype: str) -> Any:
    if value is None:
        return None
    if dtype in NUMERIC_DTYPES:
        try:
            return float(value) if dtype == "number" else int(float(value))
        except (TypeError, ValueError):
            return None
    if dtype == "boolean":
        return bool(value) if isinstance(value, bool) else None
    if isinstance(value, (dict, list)):
        return json_dumps(value)
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _same_value(left: Any, right: Any) -> bool:
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        return abs(float(left) - float(right)) < 1e-9
    return left == right


def dataset_slug(name: str) -> str:
    return safe_filename(name)


def detect_price_fields(columns: list[FieldSpec]) -> set[str]:
    return {
        column.key
        for column in columns
        if column.dtype in NUMERIC_DTYPES
        and any(token in column.key for token in ("price", "cost", "amount", "salary", "rate"))
    }


def domain_of(url: str) -> str:
    return normalize_domain(url)
