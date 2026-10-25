"""Storage tests: queue leases, append-only versions, diff, exports."""

from __future__ import annotations

from pathlib import Path

import duckdb
import pytest
from conftest import make_settings, make_spec

from app.db.duckdb_store import AnalyticsStore, detect_price_fields
from app.db.sqlite_store import OperationalStore
from app.models import DatasetInfo, ExtractedRecord, FieldSpec, Provenance
from app.storage.exports import export_dataset


def test_queue_lease_retry_and_cancel(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    store = OperationalStore(settings.paths)
    try:
        task = store.create_task(make_spec())
        run = store.create_run(task.id, "collect", budget_usd=1.0)
        total = store.enqueue_urls(
            run.id,
            task.id,
            [
                ("https://a.example/1", 0, None),
                ("https://a.example/2", 0, None),
                ("https://a.example/3", 0, None),
            ],
        )
        assert total == 3

        claimed = store.claim_jobs("worker-1", limit=2)
        assert len(claimed) == 2
        assert all(job["attempts"] == 1 for job in claimed)
        assert store.job_counts(run.id)["leased"] == 2

        # Retryable failure goes back to the queue.
        assert store.fail_job(claimed[0]["id"], "timeout", retryable=True) == "queued"
        # Permanent failure is parked.
        assert store.fail_job(claimed[1]["id"], "404", retryable=False) == "failed"
        store.complete_job(claimed[0]["id"], status="done")

        counts = store.job_counts(run.id)
        assert counts["queued"] == 1
        assert counts["done"] == 1
        assert counts["failed"] == 1

        store.cancel_run_jobs(run.id)
        assert store.job_counts(run.id)["cancelled"] >= 1
        assert store.list_unfinished_runs()[0].id == run.id
    finally:
        store.close()


def test_events_and_llm_ledger(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    store = OperationalStore(settings.paths)
    try:
        task = store.create_task(make_spec())
        store.add_event(task.id, "page_fetched", "ok", level="debug", data={"n": 1})
        store.add_event(task.id, "run_finished", "done")
        events = store.list_events(task.id)
        assert [event.type for event in events] == ["page_fetched", "run_finished"]
        assert store.list_events(task.id, after_id=events[0].id)[0].type == "run_finished"

        run = store.create_run(task.id, "collect")
        latest = store.create_run(task.id, "snapshot")
        refreshed = store.get_task(task.id)
        assert refreshed is not None
        assert refreshed.last_run is not None and refreshed.last_run.id == latest.id
        store.record_llm_call(
            purpose="plan", provider="primary", model="gpt-4o-mini",
            tokens_in=100, tokens_out=50, usd=0.001, cached=False,
            task_id=task.id, run_id=run.id,
        )
        assert store.run_spend(run.id) == pytest.approx(0.001)
        assert store.task_spend(task.id) == pytest.approx(0.001)
    finally:
        store.close()


def _record(key: str, payload: dict, *, method: str = "css") -> ExtractedRecord:
    return ExtractedRecord(
        record_key=key,
        payload=payload,
        provenance={
            field: Provenance(method=method, confidence=0.9, source_url="https://a.example/x")
            for field in payload
        },
        source_url="https://a.example/x",
    )


def test_record_versions_current_view_and_diff(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    analytics = AnalyticsStore(settings.paths)
    spec = make_spec()
    try:
        analytics.append_records(
            "ds1",
            1,
            "run1",
            [
                _record("d:alpha", {"name": "Alpha", "price": 100.0, "city": "Київ"}),
                _record("d:beta", {"name": "Beta", "price": 50.0, "city": "Львів"}),
            ],
        )
        assert analytics.count_records("ds1") == 2
        assert len(analytics.all_current("ds1")) == 2

        analytics.append_records(
            "ds1",
            2,
            "run2",
            [
                _record("d:alpha", {"name": "Alpha", "price": 80.0, "city": "Київ"}),
                _record("d:gamma", {"name": "Gamma", "price": 70.0, "city": "Київ"}),
            ],
        )
        current = {row["record_key"]: row["payload"] for row in analytics.all_current("ds1")}
        assert len(current) == 3
        assert current["d:alpha"]["price"] == 80.0
        assert current["d:beta"]["price"] == 50.0

        filtered = analytics.query_records(
            "ds1", columns=spec.columns, filters={"city": "Київ"}, limit=10
        )
        assert filtered["total"] == 2  # alpha + gamma (beta is in Львів)
    finally:
        analytics.close()


def test_diff_detects_new_entities_and_price_drops(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    analytics = AnalyticsStore(settings.paths)
    columns = [
        FieldSpec(key="name", title="Назва"),
        FieldSpec(key="price", title="Ціна", dtype="number"),
    ]
    try:
        analytics.append_records(
            "ds2", 1, "run1",
            [
                _record("d:alpha", {"name": "Alpha", "price": 100.0}),
                _record("d:beta", {"name": "Beta", "price": 50.0}),
            ],
        )
        analytics.append_records(
            "ds2", 2, "run2",
            [
                _record("d:alpha", {"name": "Alpha", "price": 70.0}),
                _record("d:gamma", {"name": "Gamma", "price": 10.0}),
            ],
        )
        changes = analytics.diff_versions(
            "ds2",
            from_version=1,
            to_version=2,
            columns=columns,
            price_fields=detect_price_fields(columns),
            price_drop_pct=5.0,
        )
        types = {change.type for change in changes}
        # Snapshots are cumulative: a record absent from a run is retained until an explicit
        # tombstone exists (roadmap P1), so removal detection is not asserted here.
        assert {"new_entity", "field_changed", "price_drop"} <= types
        drop = next(change for change in changes if change.type == "price_drop")
        assert drop.old == 100.0 and drop.new == 70.0
        assert drop.delta_pct == pytest.approx(-30.0)

        snapshot = analytics.snapshot("ds2", 2, settings.paths.snapshots / "ds2" / "part.parquet")
        assert snapshot.exists()
        con = duckdb.connect()
        try:
            count = con.execute("SELECT COUNT(*) FROM read_parquet(?)", [str(snapshot)]).fetchone()[0]
        finally:
            con.close()
        assert count >= 4
    finally:
        analytics.close()


def test_exports_csv_xlsx_parquet_json_and_masking(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    analytics = AnalyticsStore(settings.paths)
    spec = make_spec()
    try:
        analytics.append_records(
            "ds3", 1, "run1",
            [
                _record(
                    "d:alpha",
                    {
                        "name": "Alpha",
                        "phone": "+380441112233",
                        "email": "boss@alpha.example",
                        "price": 120.5,
                        "tech_stack": ["WordPress", "Stripe"],
                    },
                )
            ],
        )
        columns = [*spec.columns, FieldSpec(key="price", title="Ціна", dtype="number")]
        dataset = DatasetInfo(
            id="ds3",
            task_id="t1",
            name="Клініки",
            version=1,
            row_count=1,
            columns=columns,
        )
        dataset.freshness = analytics.freshness("ds3")
        dataset.quality = analytics.quality("ds3", columns)

        for fmt in ("csv", "xlsx", "parquet", "json"):
            manifest = export_dataset(
                analytics,
                dataset,
                fmt=fmt,
                exports_dir=settings.paths.exports,
                mask_pii=True,
            )
            path = Path(manifest["path"])
            assert path.exists() and path.stat().st_size > 0
            assert Path(manifest["path"] + ".manifest.json").exists()

        csv_text = next(settings.paths.exports.glob("*.csv")).read_text("utf-8-sig")
        assert "b***@alpha.example" in csv_text
        assert "boss@alpha.example" not in csv_text
        assert "+380***33" in csv_text

        parquet = next(settings.paths.exports.glob("*.parquet"))
        con = duckdb.connect()
        try:
            rows = con.execute("SELECT COUNT(*), MAX(price) FROM read_parquet(?)", [str(parquet)]).fetchone()
        finally:
            con.close()
        assert rows[0] == 1
        assert rows[1] == pytest.approx(120.5)
    finally:
        analytics.close()
