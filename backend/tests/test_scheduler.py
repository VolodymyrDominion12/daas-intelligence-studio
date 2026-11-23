"""Offline tests for P1-05: In-process cron scheduler, catch-up and compliance guard."""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from conftest import make_settings, read_fixture
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from app.cli import app as cli_app
from app.db.sqlite_store import OperationalStore
from app.main import create_app
from app.models import ApiSourceSpec, PageContent, SourcePolicy
from app.pipeline.scheduler import CronError, next_cron_after, parse_cron, validate_cron
from app.runtime import Studio
from app.scraping.fetcher import FetchResult
from app.utils import hash_text, iso

API_BASE = "http://api.openprocurement.local/api/2.5"


class _JsonFixtureFetcher:
    """Offline fixture fetcher serving JSON payloads for scheduled API tasks."""

    name = "fixture"

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def fetch(
        self, url: str, *, force_refresh: bool = False, allow_json: bool = False
    ) -> FetchResult:
        self.calls.append(url)
        if url.startswith(f"{API_BASE}/tenders?"):
            raw = read_fixture("prozorro_list_page1.json")
        elif url == f"{API_BASE}/tenders/tender-001":
            raw = read_fixture("prozorro_tender_001.json")
        elif url == f"{API_BASE}/tenders/tender-002":
            raw = read_fixture("prozorro_tender_002.json")
        else:
            return FetchResult(kind="permanent", status=404, error=f"no fixture for {url}")
        return FetchResult(
            page=PageContent(
                url=url,
                final_url=url,
                status_code=200,
                html=raw,
                text=raw,
                content_hash=hash_text(raw),
                engine=self.name,
            ),
            status=200,
        )

    async def aclose(self) -> None:
        return None


def _api_task_payload() -> dict:
    return {
        "prompt": "Тендери Prozorro API за розкладом",
        "seeds": [f"{API_BASE}/tenders?limit=10"],
        "api_source": {
            "items_path": "data",
            "id_path": "id",
            "detail_url_template": f"{API_BASE}/tenders/{{id}}",
            "incremental": True,
            "field_map": {
                "tender_id": "tenderID",
                "title": "title",
                "name": "procuringEntity.name",
                "amount": "value.amount",
                "currency": "value.currency",
                "status": "status",
            },
        },
    }


def test_cron_validation_and_schedule_crud_api(tmp_path: Path) -> None:
    """REQ-RUN-040: cron parser handles macros/ranges/weekdays, rejects invalid dates, CRUD API emits events."""
    assert validate_cron(" */15  * * * * ") == "*/15 * * * *"
    assert validate_cron("@daily") == "@daily"

    parsed = parse_cron("30 9 * * 1-5")
    assert 30 in parsed.minutes
    assert 9 in parsed.hours
    assert parsed.days_of_week == {1, 2, 3, 4, 5}

    sunday_cron = parse_cron("0 8 * * 7")
    assert 0 in sunday_cron.days_of_week

    # Friday 2026-11-20 10:00 UTC -> next 09:30 Mon-Fri is Monday 2026-11-23 09:30 UTC
    after_fri = datetime(2026, 11, 20, 10, 0, tzinfo=UTC)
    assert next_cron_after("30 9 * * 1-5", after_fri) == datetime(2026, 11, 23, 9, 30, tzinfo=UTC)
    assert next_cron_after("@hourly", datetime(2026, 11, 20, 10, 0, tzinfo=UTC)) == datetime(
        2026, 11, 20, 11, 0, tzinfo=UTC
    )

    for bad in ("99 * * * *", "0 0 31 2 *", "0 0 31 4 *", "* * *", "*/0 * * * *", "5-2 * * * *"):
        with pytest.raises(CronError):
            validate_cron(bad)

    settings = make_settings(tmp_path, scheduler_enabled=False)
    app = create_app(settings, studio_factory=lambda s: Studio(s, fetcher=_JsonFixtureFetcher()))
    with TestClient(app) as client:
        created = client.post("/api/tasks", json=_api_task_payload())
        assert created.status_code == 201, created.text
        task_id = created.json()["task"]["id"]

        # Initially empty
        assert client.get(f"/api/tasks/{task_id}/schedule").json() == {"schedule": None}

        # Invalid cron -> 422
        bad_cron = client.put(
            f"/api/tasks/{task_id}/schedule",
            json={"cron": "99 * * * *"},
        )
        assert bad_cron.status_code == 422

        # Impossible calendar date -> 422
        feb_31 = client.put(
            f"/api/tasks/{task_id}/schedule",
            json={"cron": "0 0 31 2 *"},
        )
        assert feb_31.status_code == 422

        # acknowledge_high_risk=True without 10-char reason -> 400
        bad_ack = client.put(
            f"/api/tasks/{task_id}/schedule",
            json={"cron": "*/15 * * * *", "acknowledge_high_risk": True, "reason": "short"},
        )
        assert bad_ack.status_code == 400

        # Valid schedule
        saved = client.put(
            f"/api/tasks/{task_id}/schedule",
            json={"cron": "*/15 * * * *", "enabled": True, "catch_up": True},
        )
        assert saved.status_code == 200, saved.text
        sched = saved.json()["schedule"]
        assert sched["task_id"] == task_id
        assert sched["cron"] == "*/15 * * * *"
        assert sched["next_run_at"] is not None

        listed = client.get("/api/schedules")
        assert listed.status_code == 200
        assert len(listed.json()["items"]) == 1

        got = client.get(f"/api/tasks/{task_id}/schedule")
        assert got.status_code == 200
        assert got.json()["schedule"]["cron"] == "*/15 * * * *"

        deleted = client.delete(f"/api/tasks/{task_id}/schedule")
        assert deleted.status_code == 200
        assert deleted.json() == {"deleted": True}
        assert client.get(f"/api/tasks/{task_id}/schedule").json() == {"schedule": None}

        events = [ev["type"] for ev in client.get(f"/api/tasks/{task_id}/events").json()["items"]]
        assert "schedule_updated" in events
        assert "schedule_deleted" in events


@pytest.mark.asyncio
async def test_scheduler_triggers_skips_active_and_respects_compliance(tmp_path: Path) -> None:
    """REQ-RUN-041: tick launches due collect, skips active run conflict, blocks forbidden without loop."""
    fetcher = _JsonFixtureFetcher()
    settings = make_settings(tmp_path, scheduler_enabled=False)
    studio = Studio(settings, fetcher=fetcher)
    try:
        task = await studio.create_task(
            "Тендери Prozorro API за розкладом",
            [f"{API_BASE}/tenders?limit=10"],
            api_source=ApiSourceSpec.model_validate(_api_task_payload()["api_source"]),
        )
        t0 = datetime(2026, 11, 23, 10, 0, tzinfo=UTC)
        sched = studio.set_schedule(task.id, cron="*/15 * * * *", now=t0)
        assert sched.next_run_at == datetime(2026, 11, 23, 10, 15, tzinfo=UTC)

        # Before due time -> nothing runs
        assert await studio.scheduler.tick(now=datetime(2026, 11, 23, 10, 14, tzinfo=UTC)) == []

        # At due time -> triggers collect run
        t1 = datetime(2026, 11, 23, 10, 15, 5, tzinfo=UTC)
        outcomes = await studio.scheduler.tick(now=t1)
        assert len(outcomes) == 1
        assert outcomes[0]["action"] == "triggered"
        run_id = outcomes[0]["run_id"]
        finished = await studio.wait(run_id, timeout=10.0)
        assert finished is not None and finished.status == "succeeded"

        updated = studio.store.get_schedule(task.id)
        assert updated is not None
        assert updated.last_run_id == run_id
        assert updated.next_run_at == datetime(2026, 11, 23, 10, 30, tzinfo=UTC)

        # Simulate active run at next due slot -> must skip and advance next_run_at
        active = studio.store.create_run(task.id, "collect", exclusive=True)
        t2 = datetime(2026, 11, 23, 10, 30, 5, tzinfo=UTC)
        skip_outcomes = await studio.scheduler.tick(now=t2)
        assert len(skip_outcomes) == 1
        assert skip_outcomes[0]["action"] == "skipped"
        assert skip_outcomes[0]["reason"] == "active_run"
        after_skip = studio.store.get_schedule(task.id)
        assert after_skip is not None
        assert after_skip.next_run_at == datetime(2026, 11, 23, 10, 45, tzinfo=UTC)
        studio.store.update_run(active.id, status="cancelled", finished=True)

        # Mark source domain as forbidden -> compliance blocks without spinning at same slot
        studio.store.upsert_source_policy(
            SourcePolicy(domain="api.openprocurement.local", tos_risk="forbidden")
        )
        t3 = datetime(2026, 11, 23, 10, 45, 5, tzinfo=UTC)
        block_outcomes = await studio.scheduler.tick(now=t3)
        assert len(block_outcomes) == 1
        assert block_outcomes[0]["action"] == "blocked"
        after_block = studio.store.get_schedule(task.id)
        assert after_block is not None
        assert after_block.next_run_at == datetime(2026, 11, 23, 11, 0, tzinfo=UTC)

        events = studio.store.list_events(task.id, limit=200)
        by_type = {ev.type: ev for ev in events}
        assert "schedule_triggered" in by_type
        assert "schedule_skipped" in by_type
        assert by_type["schedule_skipped"].data.get("reason") == "active_run"
        assert "schedule_blocked" in by_type
        assert by_type["schedule_blocked"].level == "warning"

        # Uncalibrated web task must also produce schedule_blocked (not schedule_skipped)
        web_task = await studio.create_task(
            "Клініки Києва",
            ["http://demo.local/catalog.html"],
        )
        studio.set_schedule(web_task.id, cron="*/15 * * * *", now=t3)
        web_outcomes = {
            item["task_id"]: item
            for item in await studio.scheduler.tick(now=t3 + timedelta(minutes=16))
        }
        assert web_outcomes[web_task.id]["action"] == "blocked"
        assert web_outcomes[web_task.id]["reason"] == "uncalibrated"
        web_events = studio.store.list_events(web_task.id, limit=20)
        assert any(ev.type == "schedule_blocked" and ev.level == "warning" for ev in web_events)
    finally:
        await studio.aclose()

    # Verify background scheduler loop starts and stops cleanly via FastAPI lifespan
    live_settings = make_settings(tmp_path / "live", scheduler_enabled=True, scheduler_poll_seconds=60.0)
    live_app = create_app(live_settings, studio_factory=lambda s: Studio(s, fetcher=fetcher))
    with TestClient(live_app) as client:
        caps = client.get("/api/config").json()["capabilities"]
        assert caps["scheduler_enabled"] is True
        assert live_app.state.studio.scheduler.running is True
    assert live_app.state.studio.scheduler.running is False


@pytest.mark.asyncio
async def test_scheduler_catch_up_and_missed_slot_policy(tmp_path: Path) -> None:
    """REQ-RUN-042: missed slots execute at most 1 catch-up run when catch_up=True, or skip if False."""
    settings = make_settings(tmp_path, scheduler_enabled=False)
    studio = Studio(settings, fetcher=_JsonFixtureFetcher())
    try:
        api_source = ApiSourceSpec.model_validate(_api_task_payload()["api_source"])
        task_catch = await studio.create_task(
            "Catchup task", [f"{API_BASE}/tenders?limit=10"], api_source=api_source
        )
        task_skip = await studio.create_task(
            "No-catchup task", [f"{API_BASE}/tenders?limit=10"], api_source=api_source
        )

        t0 = datetime(2026, 11, 23, 8, 0, tzinfo=UTC)
        studio.set_schedule(task_catch.id, cron="0 * * * *", catch_up=True, now=t0)
        studio.set_schedule(task_skip.id, cron="0 * * * *", catch_up=False, now=t0)

        # Fast-forward 3 hours: 09:00 slot is > 2h overdue
        t_late = datetime(2026, 11, 23, 11, 5, tzinfo=UTC)
        outcomes = await studio.scheduler.tick(now=t_late)
        by_task = {item["task_id"]: item for item in outcomes}

        assert by_task[task_catch.id]["action"] == "triggered"
        assert by_task[task_catch.id]["catch_up"] is True
        await studio.wait(by_task[task_catch.id]["run_id"], timeout=10.0)
        assert len(studio.store.list_runs(task_catch.id, limit=10)) == 1
        catch_sched = studio.store.get_schedule(task_catch.id)
        assert catch_sched is not None
        assert catch_sched.next_run_at == datetime(2026, 11, 23, 12, 0, tzinfo=UTC)

        # Second immediate tick must NOT fire again (no storm of 09:00, 10:00, 11:00)
        assert await studio.scheduler.tick(now=t_late + timedelta(seconds=10)) == []

        assert by_task[task_skip.id]["action"] == "skipped"
        assert by_task[task_skip.id]["reason"] == "missed_no_catchup"
        assert len(studio.store.list_runs(task_skip.id, limit=10)) == 0
        skip_sched = studio.store.get_schedule(task_skip.id)
        assert skip_sched is not None
        assert skip_sched.next_run_at == datetime(2026, 11, 23, 12, 0, tzinfo=UTC)

        events = studio.store.list_events(task_skip.id, limit=50)
        assert any(
            ev.type == "schedule_skipped" and ev.data.get("reason") == "missed_no_catchup"
            for ev in events
        )
    finally:
        await studio.aclose()


def test_sqlite_migration_v5_opens_v4_database(tmp_path: Path) -> None:
    """REQ-OPS-010: migration v5 adds schedules table cleanly on an existing user_version=4 database."""
    settings = make_settings(tmp_path)
    settings.paths.ensure()
    conn = sqlite3.connect(settings.paths.sqlite)
    conn.executescript(
        f"""
        CREATE TABLE tasks (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            prompt TEXT NOT NULL,
            spec_json TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'draft',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE api_state (
            task_id TEXT PRIMARY KEY REFERENCES tasks(id) ON DELETE CASCADE,
            next_cursor TEXT,
            next_url TEXT,
            last_modified TEXT,
            updated_at TEXT NOT NULL
        );
        INSERT INTO tasks (id, title, prompt, spec_json, status, created_at, updated_at)
        VALUES ('t_v4', 'V4 Task', 'Old prompt', '{{}}', 'draft', '{iso()}', '{iso()}');
        PRAGMA user_version = 4;
        """
    )
    conn.close()

    store = OperationalStore(settings.paths)
    try:
        assert store.get_schedule("t_v4") is None
        due_time = datetime(2026, 11, 23, 12, 0, tzinfo=UTC)
        sched = store.upsert_schedule("t_v4", cron="*/30 * * * *", next_run_at=due_time)
        assert sched.task_id == "t_v4"
        assert sched.cron == "*/30 * * * *"
        assert len(store.list_due_schedules(iso(due_time))) == 1
        assert store.delete_schedule("t_v4") is True
    finally:
        store.close()


def test_cli_schedule_commands(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """REQ-OPS-030: `daas schedule set | show | list | delete` works via Typer CLI."""
    settings = make_settings(tmp_path, scheduler_enabled=False)
    monkeypatch.setattr("app.cli.get_settings", lambda: settings)

    # Create a task directly in store so CLI tests stay fast & offline
    setup_studio = Studio(settings, fetcher=_JsonFixtureFetcher())
    task = asyncio.run(
        setup_studio.create_task(
            "CLI schedule task",
            [f"{API_BASE}/tenders?limit=10"],
            api_source=ApiSourceSpec.model_validate(_api_task_payload()["api_source"]),
        )
    )
    asyncio.run(setup_studio.aclose())

    runner = CliRunner()
    res_set = runner.invoke(cli_app, ["schedule", "set", task.id, "--cron", "*/15 * * * *"])
    assert res_set.exit_code == 0, res_set.output
    assert "*/15 * * * *" in res_set.output

    res_show = runner.invoke(cli_app, ["schedule", "show", task.id])
    assert res_show.exit_code == 0, res_show.output
    assert task.id in res_show.output

    res_list = runner.invoke(cli_app, ["schedule", "list"])
    assert res_list.exit_code == 0, res_list.output
    assert task.id in res_list.output

    res_del = runner.invoke(cli_app, ["schedule", "delete", task.id])
    assert res_del.exit_code == 0, res_del.output

    res_after = runner.invoke(cli_app, ["schedule", "show", task.id])
    assert res_after.exit_code == 0, res_after.output
