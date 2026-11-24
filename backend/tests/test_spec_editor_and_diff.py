"""Offline tests for P1-10: spec editor, manual CSS selectors, calibration history, schema diff & rollback."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from conftest import FixtureFetcher, make_settings, make_spec
from fastapi.testclient import TestClient

from app.db.sqlite_store import OperationalStore
from app.main import create_app
from app.models import CalibrationReport, CssField, CssSchema, PageContent
from app.runtime import RunConflict, Studio
from app.scraping.fetcher import FetchResult
from app.scraping.page import html_to_markdown, html_to_text
from app.utils import hash_text, iso


class MutableHtmlFetcher:
    """Offline fetcher serving in-memory HTML pages that can mutate between calibrations."""

    name = "mutable-fixture"

    def __init__(self, pages: dict[str, str]) -> None:
        self.pages = pages
        self.calls: list[str] = []

    async def fetch(
        self, url: str, *, force_refresh: bool = False, allow_json: bool = False
    ) -> FetchResult:
        self.calls.append(url)
        html = self.pages.get(url)
        if html is None:
            return FetchResult(kind="permanent", error=f"not found: {url}", status=404)
        page = PageContent(
            url=url,
            final_url=url,
            status_code=200,
            html=html,
            text=html_to_text(html),
            markdown=html_to_markdown(html, url),
            content_hash=hash_text(html),
            engine=self.name,
        )
        return FetchResult(page=page, status=200)

    async def aclose(self) -> None:
        return None


V1_HTML = """<!doctype html>
<html>
<body>
  <main>
    <section class="clinic-grid">
      <article class="clinic-card">
        <h3 class="clinic-card__name">Клініка Альфа</h3>
        <span class="clinic-card__city">Київ</span>
        <span class="clinic-card__phone">+380441112233</span>
        <div class="custom-badge" data-tier="gold">Преміум</div>
      </article>
      <article class="clinic-card">
        <h3 class="clinic-card__name">Клініка Бета</h3>
        <span class="clinic-card__city">Львів</span>
        <span class="clinic-card__phone">+380322223344</span>
        <div class="custom-badge" data-tier="silver">Стандарт</div>
      </article>
    </section>
  </main>
</body>
</html>"""

V2_HTML = """<!doctype html>
<html>
<body>
  <main>
    <section class="clinic-list">
      <div class="clinic-row">
        <h2 class="row-title">Клініка Альфа v2</h2>
        <span class="row-city">Київ</span>
        <span class="row-rating">4.9</span>
      </div>
      <div class="clinic-row">
        <h2 class="row-title">Клініка Гамма v2</h2>
        <span class="row-city">Одеса</span>
        <span class="row-rating">4.7</span>
      </div>
    </section>
  </main>
</body>
</html>"""


def test_sqlite_migration_v8_opens_v7_and_preserves_calibration(tmp_path: Path) -> None:
    """REQ-OPS-010: migration v8 adds calibration_history and preserves legacy calibrations as v1."""
    settings = make_settings(tmp_path)
    settings.paths.ensure()

    con = sqlite3.connect(settings.paths.sqlite)
    con.executescript(
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
        CREATE TABLE source_registry (
            domain TEXT PRIMARY KEY,
            payload_json TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE calibrations (
            task_id TEXT PRIMARY KEY REFERENCES tasks(id) ON DELETE CASCADE,
            run_id TEXT,
            report_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE record_misses (
            dataset_id TEXT NOT NULL,
            record_key TEXT NOT NULL,
            misses INTEGER NOT NULL DEFAULT 0,
            last_run_id TEXT,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (dataset_id, record_key)
        );
        CREATE TABLE api_state (
            task_id TEXT PRIMARY KEY REFERENCES tasks(id) ON DELETE CASCADE,
            next_cursor TEXT,
            next_url TEXT,
            last_modified TEXT,
            updated_at TEXT NOT NULL
        );
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
        PRAGMA user_version = 7;
        """
    )
    now = iso()
    spec_json = make_spec().model_dump_json()
    legacy_report = CalibrationReport(
        strategy="css",
        schema=CssSchema(
            container="article.clinic-card",
            item_scope=True,
            fields=[CssField(key="name", selector="h3", dtype="string", confidence=0.9)],
            score=2.0,
        ),
        records_found=2,
        pages_tried=1,
        pages_ok=1,
    ).model_dump_json(by_alias=True)
    con.execute(
        "INSERT INTO tasks (id, title, prompt, spec_json, status, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("legacy-cal", "Legacy Task", "Legacy prompt", spec_json, "ready", now, now),
    )
    con.execute(
        "INSERT INTO calibrations (task_id, run_id, report_json, created_at) VALUES (?, ?, ?, ?)",
        ("legacy-cal", "run-legacy", legacy_report, now),
    )
    con.commit()
    con.close()

    store = OperationalStore(settings.paths)
    try:
        user_ver = store._conn.execute("PRAGMA user_version").fetchone()[0]
        assert user_ver >= 8
        assert store.get_calibration("legacy-cal") is not None
        history = store.list_calibration_history("legacy-cal")
        assert len(history) == 1
        assert history[0]["version"] == 1
        assert history[0]["source"] == "legacy"
        assert history[0]["run_id"] == "run-legacy"
    finally:
        store.close()


@pytest.mark.asyncio
async def test_column_update_reorder_and_selector_validation(tmp_path: Path) -> None:
    """REQ-PLAN-014: edit column metadata/type/PII/manual selector, reorder columns, reject invalid input."""
    studio = Studio(make_settings(tmp_path), fetcher=FixtureFetcher())
    try:
        task = await studio.create_task(
            "Збери стоматологічні клініки Києва",
            ["http://demo.local/catalog.html"],
        )
        original_keys = [col.key for col in task.spec.columns]
        assert "phone" in original_keys

        updated = studio.update_column(
            task.id,
            "phone",
            title="Гаряча лінія",
            dtype="phone",
            pii="personal",
            required=True,
            method="css",
            selector=".clinic-card__phone",
            attr=None,
        )
        phone_col = updated.spec.column_map["phone"]
        assert phone_col.title == "Гаряча лінія"
        assert phone_col.pii == "personal"
        assert phone_col.required is True
        assert phone_col.selector == ".clinic-card__phone"

        # Invalid CSS selector must be rejected before mutating the stored spec.
        with pytest.raises(ValueError):
            studio.update_column(task.id, "phone", selector="div[[broken")

        assert studio.store.get_task(task.id).spec.column_map["phone"].selector == ".clinic-card__phone"

        # Missing column -> KeyError
        with pytest.raises(KeyError):
            studio.update_column(task.id, "nonexistent", title="Nope")

        # Reorder columns
        reversed_keys = list(reversed(original_keys))
        reordered = studio.reorder_columns(task.id, reversed_keys)
        assert [col.key for col in reordered.spec.columns] == reversed_keys

        # Incomplete/duplicate key list must be rejected
        with pytest.raises(ValueError):
            studio.reorder_columns(task.id, original_keys[:-1])
        with pytest.raises(ValueError):
            studio.reorder_columns(task.id, [original_keys[0], *original_keys[:-1]])

        events = studio.store.list_events(task.id, limit=50)
        event_types = [e.type for e in events]
        assert "column_updated" in event_types
        assert "columns_reordered" in event_types
    finally:
        await studio.aclose()


@pytest.mark.asyncio
async def test_manual_selectors_take_precedence_in_calibration_and_collect(tmp_path: Path) -> None:
    """REQ-CAL-010: manual CSS selectors & attr override auto-learned/cached schema in calibration & collect."""
    url = "http://manual.localhost/clinics.html"
    fetcher = MutableHtmlFetcher({url: V1_HTML})
    studio = Studio(make_settings(tmp_path), fetcher=fetcher)
    try:
        task = await studio.create_task("Збери клініки: назва, місто, телефон", [url])
        # Seed auto-cache with a different selector for phone & custom column to prove manual override wins.
        studio.store.save_selector(
            "manual.localhost",
            f"auto:{task.spec.entity}",
            CssSchema(
                container="article.clinic-card",
                item_scope=True,
                fields=[
                    CssField(key="name", selector="h3.clinic-card__name", dtype="string", confidence=0.9),
                    CssField(key="city", selector=".clinic-card__city", dtype="string", confidence=0.8),
                    CssField(key="phone", selector=".wrong-cached-phone", dtype="phone", confidence=0.8),
                ],
                score=2.0,
            ),
        )
        # Add custom CSS column with attr='data-tier' and override phone with valid manual selector
        spec = studio.store.get_task(task.id).spec.model_copy(deep=True)
        from app.models import FieldSpec

        spec.columns.append(
            FieldSpec(
                key="tier",
                title="Рівень",
                dtype="string",
                method="css",
                selector=".custom-badge",
                attr="data-tier",
            )
        )
        spec.columns.append(
            FieldSpec(
                key="missing_badge",
                title="Відсутній бейдж",
                dtype="string",
                method="css",
                selector=".does-not-exist",
            )
        )
        studio.update_task(task.id, spec=spec)
        studio.update_column(task.id, "phone", selector=".clinic-card__phone")

        cal_run = await studio.calibrate(task.id)
        cal_done = await studio.wait(cal_run.id, timeout=10)
        assert cal_done is not None and cal_done.status == "succeeded"

        cal_state = studio.calibration_state(task.id)
        report = cal_state["calibration"]
        assert report is not None
        schema_fields = {f["key"]: f for f in report["schema"]["fields"]}
        assert schema_fields["tier"]["selector"] == ".custom-badge"
        assert schema_fields["tier"]["attr"] == "data-tier"
        assert schema_fields["phone"]["selector"] == ".clinic-card__phone"
        assert schema_fields["missing_badge"]["selector"] == ".does-not-exist"

        # Now mutate cached selector for `tier` in DB to a wrong one — runner collect MUST still honor spec manual selector
        cached = studio.store.get_selector("manual.localhost", f"auto:{task.spec.entity}")
        assert cached is not None
        for f in cached.fields:
            if f.key == "tier":
                f.selector = ".stale-wrong-selector"
                f.attr = None
        studio.store.save_selector("manual.localhost", f"auto:{task.spec.entity}", cached)

        col_run = await studio.collect(task.id)
        col_done = await studio.wait(col_run.id, timeout=10)
        assert col_done is not None and col_done.status == "succeeded"

        rows = studio.analytics.all_current(task.id)
        assert len(rows) == 2
        by_name = {r["payload"]["name"]: r["payload"] for r in rows}
        assert by_name["Клініка Альфа"]["tier"] == "gold"
        assert by_name["Клініка Бета"]["tier"] == "silver"
        assert by_name["Клініка Альфа"]["phone"] == "+380441112233"
        assert "missing_badge" not in by_name["Клініка Альфа"]
    finally:
        await studio.aclose()


@pytest.mark.asyncio
async def test_calibration_history_schema_diff_and_rollback(tmp_path: Path) -> None:
    """REQ-CAL-009: history tracks versions, Schema Diff surfaces changes, rollback restores schema & selector_cache."""
    url = "http://diff.local/clinics.html"
    fetcher = MutableHtmlFetcher({url: V1_HTML})
    studio = Studio(make_settings(tmp_path), fetcher=fetcher)
    try:
        task = await studio.create_task("Збери клініки: назва, місто, телефон, рейтинг", [url])

        # Refusal when no calibration exists yet
        with pytest.raises(ValueError, match="попередньої"):
            studio.rollback_calibration(task.id)

        # Calibration v1 on V1_HTML
        r1 = await studio.calibrate(task.id)
        d1 = await studio.wait(r1.id, timeout=10)
        assert d1 is not None and d1.status == "succeeded"

        state1 = studio.calibration_state(task.id)
        assert state1["version"] == 1
        assert len(state1["history"]) == 1
        assert state1["diff"]["has_previous"] is False

        # Cannot rollback with only 1 version
        with pytest.raises(ValueError, match="попередньої"):
            studio.rollback_calibration(task.id)

        # Mutate page markup and run Calibration v2
        fetcher.pages[url] = V2_HTML
        r2 = await studio.calibrate(task.id)
        d2 = await studio.wait(r2.id, timeout=10)
        assert d2 is not None and d2.status == "succeeded"

        state2 = studio.calibration_state(task.id)
        assert state2["version"] == 2
        assert len(state2["history"]) == 2
        diff = state2["diff"]
        assert diff is not None
        assert diff["has_previous"] is True
        assert diff["previous_version"] == 1
        assert diff["current_version"] == 2
        assert diff["container_changed"] is True
        assert diff["container_before"] != diff["container_after"]
        assert diff["has_changes"] is True

        # Switch HTML back to V1 and test rollback restores V1 selectors in calibrations & selector_cache
        fetcher.pages[url] = V1_HTML
        # Non-existent version -> KeyError
        with pytest.raises(KeyError):
            studio.rollback_calibration(task.id, version=999)

        rolled = studio.rollback_calibration(task.id)
        assert rolled["rolled_back_to"] == 1
        assert rolled["version"] == 3
        assert rolled["calibration"]["schema"]["container"] == state1["calibration"]["schema"]["container"]
        cached = studio.store.get_selector("diff.local", f"auto:{task.spec.entity}")
        assert cached is not None
        assert cached.container == state1["calibration"]["schema"]["container"]

        events = studio.store.list_events(task.id, limit=50)
        assert any(e.type == "calibration_rolled_back" for e in events)

        # Active run blocks rollback with RunConflict
        studio.store.create_run(task.id, "collect")
        with pytest.raises(RunConflict):
            studio.rollback_calibration(task.id)
    finally:
        await studio.aclose()


def test_spec_editor_and_diff_api_contracts(tmp_path: Path) -> None:
    """REQ-API-013: PATCH column, POST reorder, GET calibration+history+diff, POST rollback, error codes."""
    url = "http://api-diff.local/list.html"
    fetcher = MutableHtmlFetcher({url: V1_HTML})
    settings = make_settings(tmp_path, disable_auth=False, api_token="tok-spec-editor-123456")
    app = create_app(settings, studio_factory=lambda s: Studio(s, fetcher=fetcher))
    headers = {"Authorization": "Bearer tok-spec-editor-123456"}

    with TestClient(app) as client:
        # Requires auth
        assert client.post("/api/tasks/missing/columns/reorder", json={"keys": ["name"]}).status_code == 401

        created = client.post(
            "/api/tasks",
            headers=headers,
            json={"prompt": "Збери клініки: назва, місто, телефон", "seeds": [url]},
        )
        assert created.status_code == 201, created.text
        task_id = created.json()["task"]["id"]
        cols = [c["key"] for c in created.json()["spec"]["columns"]]

        # Add manual CSS column
        add_res = client.post(
            f"/api/tasks/{task_id}/columns",
            headers=headers,
            json={
                "field": {
                    "key": "badge",
                    "title": "Бейдж",
                    "dtype": "string",
                    "method": "css",
                    "selector": ".custom-badge",
                    "attr": "data-tier",
                }
            },
        )
        assert add_res.status_code == 200, add_res.text

        # PATCH column metadata & selector
        upd_res = client.patch(
            f"/api/tasks/{task_id}/columns/badge",
            headers=headers,
            json={"title": "Тариф", "pii": "business_contact", "required": True},
        )
        assert upd_res.status_code == 200, upd_res.text
        badge_col = next(c for c in upd_res.json()["task"]["spec"]["columns"] if c["key"] == "badge")
        assert badge_col["title"] == "Тариф"
        assert badge_col["pii"] == "business_contact"
        assert badge_col["required"] is True
        assert badge_col["selector"] == ".custom-badge"

        # Invalid selector -> 422 or 400
        bad_sel = client.patch(
            f"/api/tasks/{task_id}/columns/badge",
            headers=headers,
            json={"selector": "span[[bad"},
        )
        assert bad_sel.status_code in {400, 422}

        # Unknown column -> 404
        assert (
            client.patch(
                f"/api/tasks/{task_id}/columns/no_such_col",
                headers=headers,
                json={"title": "X"},
            ).status_code
            == 404
        )

        # Reorder columns
        reordered_keys = ["badge", *cols]
        reo_res = client.post(
            f"/api/tasks/{task_id}/columns/reorder",
            headers=headers,
            json={"keys": reordered_keys},
        )
        assert reo_res.status_code == 200, reo_res.text
        assert [c["key"] for c in reo_res.json()["task"]["spec"]["columns"]] == reordered_keys

        # Bad reorder -> 400
        assert (
            client.post(
                f"/api/tasks/{task_id}/columns/reorder",
                headers=headers,
                json={"keys": ["badge"]},
            ).status_code
            == 400
        )

        # Rollback before any calibration -> 400
        assert (
            client.post(f"/api/tasks/{task_id}/calibration/rollback", headers=headers, json={}).status_code
            == 400
        )

        # Run calibration v1, then mutate HTML and run v2
        studio = app.state.studio
        c1 = client.post(f"/api/tasks/{task_id}/calibrate", headers=headers)
        assert c1.status_code == 200

        # Poll via task runs endpoint until done
        while client.get(f"/api/runs/{c1.json()['run']['id']}", headers=headers).json()["run"]["status"] in {
            "queued",
            "running",
        }:
            pass

        fetcher.pages[url] = V2_HTML
        c2 = client.post(f"/api/tasks/{task_id}/calibrate", headers=headers)
        assert c2.status_code == 200
        while client.get(f"/api/runs/{c2.json()['run']['id']}", headers=headers).json()["run"]["status"] in {
            "queued",
            "running",
        }:
            pass

        cal_get = client.get(f"/api/tasks/{task_id}/calibration", headers=headers)
        assert cal_get.status_code == 200
        cal_payload = cal_get.json()
        assert cal_payload["version"] == 2
        assert len(cal_payload["history"]) == 2
        assert cal_payload["diff"]["has_previous"] is True
        assert cal_payload["diff"]["container_changed"] is True

        detail_get = client.get(f"/api/tasks/{task_id}", headers=headers).json()
        assert detail_get["calibration_version"] == 2
        assert detail_get["calibration_diff"]["container_changed"] is True

        # Rollback to v1
        rb = client.post(
            f"/api/tasks/{task_id}/calibration/rollback",
            headers=headers,
            json={"version": 1},
        )
        assert rb.status_code == 200, rb.text
        rb_payload = rb.json()
        assert rb_payload["rolled_back_to"] == 1
        assert rb_payload["version"] == 3
        assert rb_payload["calibration"]["schema"]["container"] == json.loads(
            studio.store.get_calibration_entry(task_id, 1)["report_json"]
        )["schema"]["container"]

        # Request non-existent rollback version -> 404
        assert (
            client.post(
                f"/api/tasks/{task_id}/calibration/rollback",
                headers=headers,
                json={"version": 99},
            ).status_code
            == 404
        )
