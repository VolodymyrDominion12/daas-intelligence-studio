"""Offline tests for P1-02: Entity Resolution v2 (entity_links, merge policies, reversible unmerge, UI queue & CLI)."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from conftest import make_settings, make_spec
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from app.cli import app as cli_app
from app.db.duckdb_store import AnalyticsStore
from app.db.sqlite_store import OperationalStore
from app.enrichment.dedupe import find_incremental_duplicates, merge_records
from app.main import create_app
from app.models import (
    CssField,
    CssSchema,
    ExtractedRecord,
    FieldSpec,
    PageContent,
    Provenance,
    TaskSpec,
)
from app.runtime import Studio
from app.scraping.fetcher import FetchResult
from app.scraping.page import html_to_markdown, html_to_text
from app.utils import hash_text, iso


def _record(
    key: str,
    payload: dict,
    *,
    method: str = "css",
    confidence: float = 0.8,
    observed_at: datetime | None = None,
    source_url: str = "https://catalog.local/p1",
    per_field_conf: dict[str, float] | None = None,
    per_field_ts: dict[str, datetime] | None = None,
) -> ExtractedRecord:
    ts = observed_at or datetime.now(UTC)
    provenance = {
        field: Provenance(
            method=method,
            confidence=(per_field_conf or {}).get(field, confidence),
            source_url=source_url,
            observed_at=(per_field_ts or {}).get(field, ts),
            verified=True,
        )
        for field in payload
        if payload[field] not in (None, "", [])
    }
    return ExtractedRecord(
        record_key=key,
        payload=payload,
        provenance=provenance,
        source_url=source_url,
        content_hash=hash_text(json.dumps(payload, sort_keys=True, ensure_ascii=False)),
    )


class _MappedFetcher:
    name = "mapped-fixture"

    def __init__(self, responses: dict[str, tuple[int, str]]) -> None:
        self.responses = responses

    async def fetch(
        self, url: str, *, force_refresh: bool = False, allow_json: bool = False
    ) -> FetchResult:
        entry = self.responses.get(url)
        if entry is None:
            return FetchResult(kind="permanent", status=404, error=f"no fixture for {url}")
        status, body = entry
        is_html = body.lstrip().startswith("<")
        return FetchResult(
            status=status,
            kind="ok",
            page=PageContent(
                url=url,
                final_url=url,
                status_code=status,
                html=body,
                text=html_to_text(body) if is_html else body,
                markdown=html_to_markdown(body, url) if is_html else "",
                content_hash=hash_text(body),
                engine=self.name,
            ),
        )

    async def aclose(self) -> None:
        return None


def test_old_sqlite_migrates_to_entity_links(tmp_path: Path) -> None:
    """REQ-ENR-011: existing SQLite v6 database migrates forward-only to v7 with entity_links."""
    settings = make_settings(tmp_path)
    settings.paths.ensure()

    # Build a legacy v6 SQLite database without entity_links table.
    con = sqlite3.connect(str(settings.paths.sqlite))
    con.executescript(
        """
        PRAGMA user_version = 6;
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
        """
    )
    now = iso()
    spec_json = make_spec().model_dump_json()
    con.execute(
        "INSERT INTO tasks (id, title, prompt, spec_json, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("legacy-task", "Legacy", "Legacy prompt", spec_json, "done", now, now),
    )
    con.commit()
    con.close()

    store = OperationalStore(settings.paths)
    try:
        task = store.get_task("legacy-task")
        assert task is not None
        assert store.list_entity_links("legacy-task") == []
        user_ver = store._conn.execute("PRAGMA user_version").fetchone()[0]
        assert user_ver == 7
    finally:
        store.close()


def test_deterministic_merge_policies_and_incremental_dedupe() -> None:
    """REQ-ENR-010: merge_records respects policies + provenance(merge) and incremental dedupe scans new vs existing."""
    older = datetime.now(UTC) - timedelta(days=5)
    newer = datetime.now(UTC) - timedelta(hours=1)

    left = _record(
        "n:alpha-1",
        {
            "name": "Альфа Стоматологія",
            "phone": "+380441112233",
            "city": "Київ",
            "tech_stack": ["WordPress"],
        },
        confidence=0.9,
        observed_at=older,
        per_field_conf={"phone": 0.95, "city": 0.6},
        per_field_ts={"phone": older, "city": older},
        source_url="https://dir.example/1",
    )
    right = _record(
        "n:alpha-2",
        {
            "name": "Клініка Альфа Стоматологія",
            "phone": "+380449998877",
            "email": "info@alpha.example",
            "website": "https://alpha.example",
            "city": "Київ, центр",
            "tech_stack": ["WordPress", "Stripe"],
        },
        confidence=0.85,
        observed_at=newer,
        per_field_conf={"phone": 0.7, "city": 0.9, "email": 0.9, "website": 0.95},
        per_field_ts={"phone": newer, "city": newer, "email": newer, "website": newer},
        source_url="https://maps.example/2",
    )

    # 1) confidence policy: phone comes from left (0.95 > 0.7), city comes from right (0.9 > 0.6), email filled from right
    merged_payload, merged_prov = merge_records(left, right, policy="confidence")
    assert merged_payload["phone"] == "+380441112233"
    assert merged_payload["city"] == "Київ, центр"
    assert merged_payload["email"] == "info@alpha.example"
    assert merged_payload["website"] == "https://alpha.example"
    assert merged_payload["tech_stack"] == ["WordPress", "Stripe"]
    assert merged_prov["email"].method == "merge"
    assert merged_prov["email"].source_url == "https://maps.example/2"
    assert merged_prov["city"].method == "merge"
    assert merged_prov["phone"].method == "merge"

    # 2) freshness policy: newer phone wins (+380449998877)
    fresh_payload, _ = merge_records(left, right, policy="freshness")
    assert fresh_payload["phone"] == "+380449998877"

    # 3) completeness policy: right has more populated fields (6 vs 4) so right wins conflicts
    comp_payload, _ = merge_records(left, right, policy="completeness")
    assert comp_payload["phone"] == "+380449998877"
    assert comp_payload["name"] == "Клініка Альфа Стоматологія"

    # 4) prefer_left & prefer_right & explicit field overrides
    pref_left, _ = merge_records(left, right, policy="prefer_left")
    assert pref_left["phone"] == "+380441112233"
    assert pref_left["email"] == "info@alpha.example"  # missing on left still filled from right

    pref_right, _ = merge_records(
        left, right, policy="prefer_right", field_overrides={"phone": "left"}
    )
    assert pref_right["name"] == "Клініка Альфа Стоматологія"
    assert pref_right["phone"] == "+380441112233"

    # 5) Invalid policy raises ValueError
    with pytest.raises(ValueError):
        merge_records(left, right, policy="unknown")

    # 6) Incremental discovery compares new_rows against existing_rows (including phone blocking), NOT existing vs existing
    existing = [
        {"record_key": "n:old-1", "payload": {"name": "Альфа Дент", "phone": "+380441112233"}},
        {"record_key": "n:old-2", "payload": {"name": "Альфа Дент", "phone": "+380441112233"}},
        {"record_key": "d:beta.example", "payload": {"name": "Бета Клініка", "website": "https://beta.example"}},
    ]
    incoming = [
        {"record_key": "n:new-1", "payload": {"name": "Стоматологія Альфа Дент", "phone": "+380441112233"}},
    ]
    inc = find_incremental_duplicates(incoming, existing, threshold=80)
    assert inc
    # Never emits (old-1, old-2) since neither is new
    assert all("n:new-1" in {c.left_key, c.right_key} for c in inc)


def test_merge_dismiss_and_reversible_unmerge_api(tmp_path: Path) -> None:
    """REQ-ENR-011 + REQ-API-010: duplicates queue excludes dismissed/merged, merges records, unmerges with full append-only history, and enforces 400/404/409."""
    settings = make_settings(tmp_path)
    app = create_app(settings)
    with TestClient(app) as client:
        studio: Studio = client.app.state.studio  # type: ignore[attr-defined]
        spec = TaskSpec(
            title="Клініки для дедуплікації",
            prompt="Збери клініки: назва, місто, телефон, email, сайт",
            entity="clinic",
            seeds=["https://catalog.local/list"],
            columns=[
                FieldSpec(key="name", title="Назва"),
                FieldSpec(key="city", title="Місто"),
                FieldSpec(key="phone", title="Телефон", dtype="phone", pii="business_contact"),
                FieldSpec(key="email", title="Email", dtype="email", pii="personal"),
                FieldSpec(key="website", title="Сайт", dtype="url"),
            ],
        )
        task = studio.store.create_task(spec)
        dataset_id = task.dataset_id or task.id
        now = datetime.now(UTC)

        studio.analytics.append_records(
            dataset_id,
            1,
            None,
            [
                _record(
                    "n:alpha-a",
                    {"name": "Альфа Дент", "city": "Київ", "phone": "+380441112233"},
                    confidence=0.92,
                    observed_at=now - timedelta(days=2),
                    source_url="https://a.example/1",
                ),
                _record(
                    "n:alpha-b",
                    {
                        "name": "Альфа Дент",
                        "city": "Київ",
                        "email": "boss@alpha.example",
                        "website": "https://alpha.example",
                    },
                    confidence=0.80,
                    observed_at=now,
                    source_url="https://b.example/2",
                ),
                _record(
                    "n:beta-a",
                    {"name": "Бета Мед", "city": "Львів"},
                    confidence=0.85,
                    observed_at=now,
                    source_url="https://a.example/3",
                ),
                _record(
                    "n:beta-b",
                    {"name": "Бета Мед", "city": "Одеса"},
                    confidence=0.85,
                    observed_at=now,
                    source_url="https://b.example/4",
                ),
            ],
        )
        studio.store.add_dataset_version(dataset_id, 1, None, 4, {"rows": 4})
        studio.store.update_task(task.id, status="done")

        # 1) Candidate list sees both pairs
        dups = client.get(f"/api/tasks/{task.id}/duplicates", params={"threshold": 85})
        assert dups.status_code == 200, dups.text
        dup_body = dups.json()
        assert dup_body["scanned"] == 4
        assert dup_body["links"] == []
        pair_sets = [{c["left_key"], c["right_key"]} for c in dup_body["items"]]
        assert {"n:alpha-a", "n:alpha-b"} in pair_sets
        assert {"n:beta-a", "n:beta-b"} in pair_sets

        # 2) Dismiss beta pair -> excluded from candidate queue and recorded in links + events
        dismiss_resp = client.post(
            f"/api/tasks/{task.id}/duplicates/dismiss",
            json={"left_key": "n:beta-a", "right_key": "n:beta-b", "score": 1.0, "rule": "manual"},
        )
        assert dismiss_resp.status_code == 200, dismiss_resp.text
        assert dismiss_resp.json()["link"]["status"] == "dismissed"

        dups_after_dismiss = client.get(f"/api/tasks/{task.id}/duplicates", params={"threshold": 85}).json()
        remaining_pairs = [{c["left_key"], c["right_key"]} for c in dups_after_dismiss["items"]]
        assert {"n:beta-a", "n:beta-b"} not in remaining_pairs
        assert {"n:alpha-a", "n:alpha-b"} in remaining_pairs
        assert len(dups_after_dismiss["links"]) == 1

        # 3) Merge alpha-b into alpha-a -> row_count drops 4 -> 3, merged payload & provenance preserved, alpha-b tombstoned
        merge_resp = client.post(
            f"/api/tasks/{task.id}/duplicates/merge",
            json={
                "canonical_key": "n:alpha-a",
                "merged_key": "n:alpha-b",
                "policy": "confidence",
                "score": 1.0,
                "rule": "manual",
            },
        )
        assert merge_resp.status_code == 200, merge_resp.text
        merged_body = merge_resp.json()
        link = merged_body["link"]
        assert link["status"] == "merged"
        assert link["canonical_key"] == "n:alpha-a"
        assert link["merged_key"] == "n:alpha-b"
        assert merged_body["row_count"] == 3
        assert merged_body["record"]["payload"]["phone"] == "+380441112233"
        assert merged_body["record"]["payload"]["email"] == "boss@alpha.example"
        assert merged_body["record"]["provenance"]["email"]["method"] == "merge"

        # Current records has 3 rows; alpha-b is absent from current_records, but history keeps tombstone
        assert client.get(f"/api/tasks/{task.id}/records").json()["total"] == 3
        assert client.get(f"/api/tasks/{task.id}/records/n:alpha-b").status_code == 404
        b_hist_during_merge = studio.analytics.record_history(dataset_id, "n:alpha-b")
        assert len(b_hist_during_merge) == 2
        assert b_hist_during_merge[0]["deleted"] is True

        # Export still respects PII masking on personal fields after merge
        exp = client.post(
            f"/api/tasks/{task.id}/exports", json={"format": "csv", "mask_pii": False}
        ).json()
        csv_text = client.get(exp["download_url"]).content.decode("utf-8-sig")
        assert "boss@alpha.example" not in csv_text  # pii="personal" never exported openly

        # Error boundaries: 400 self-merge, 404 missing record/link, 409 already merged
        assert (
            client.post(
                f"/api/tasks/{task.id}/duplicates/merge",
                json={"canonical_key": "n:alpha-a", "merged_key": "n:alpha-a"},
            ).status_code
            == 400
        )
        assert (
            client.post(
                f"/api/tasks/{task.id}/duplicates/merge",
                json={"canonical_key": "n:alpha-a", "merged_key": "n:missing"},
            ).status_code
            == 404
        )
        assert (
            client.post(
                f"/api/tasks/{task.id}/duplicates/merge",
                json={"canonical_key": "n:beta-a", "merged_key": "n:alpha-b"},
            ).status_code
            == 409
        )
        assert (
            client.post(f"/api/tasks/{task.id}/duplicates/links/missing/unmerge").status_code == 404
        )

        # 4) Unmerge restores BOTH records cleanly without deleting append-only history
        unmerge_resp = client.post(
            f"/api/tasks/{task.id}/duplicates/links/{link['id']}/unmerge"
        )
        assert unmerge_resp.status_code == 200, unmerge_resp.text
        unmerged_body = unmerge_resp.json()
        assert unmerged_body["link"]["status"] == "unmerged"
        assert unmerged_body["row_count"] == 4
        assert "email" not in unmerged_body["canonical_record"]["payload"]
        assert unmerged_body["canonical_record"]["payload"]["phone"] == "+380441112233"
        assert unmerged_body["restored_record"]["payload"]["email"] == "boss@alpha.example"

        # History is strictly append-only (nothing deleted)
        a_hist = studio.analytics.record_history(dataset_id, "n:alpha-a")
        b_hist = studio.analytics.record_history(dataset_id, "n:alpha-b")
        assert len(a_hist) == 3  # original + merged + restored
        assert len(b_hist) == 3  # original + tombstone + restored
        assert b_hist[0]["deleted"] is False
        assert b_hist[1]["deleted"] is True

        # Repeating unmerge returns 409 conflict
        assert (
            client.post(
                f"/api/tasks/{task.id}/duplicates/links/{link['id']}/unmerge"
            ).status_code
            == 409
        )

        # Audit events recorded
        event_types = [e["type"] for e in client.get(f"/api/tasks/{task.id}/events").json()["items"]]
        assert "duplicate_dismissed" in event_types
        assert "entity_merged" in event_types
        assert "entity_unmerged" in event_types


@pytest.mark.asyncio
async def test_incremental_collect_redirects_merged_key_to_canonical(tmp_path: Path) -> None:
    """REQ-ENR-010: subsequent collection redirects previously merged record_key to its canonical_key."""
    page1_html = """<!doctype html><html><body>
      <article class="row"><h3 class="nm">Альфа Дент</h3><span class="ct">Київ</span><span class="ph">+380441112233</span></article>
      <article class="row"><h3 class="nm">Альфа Дент Плюс</h3><span class="ct">Київ</span><span class="ph">+380449990000</span></article>
    </body></html>"""
    page2_html = """<!doctype html><html><body>
      <article class="row"><h3 class="nm">Альфа Дент</h3><span class="ct">Київ</span><span class="ph">+380441112233</span></article>
      <article class="row"><h3 class="nm">Альфа Дент Плюс</h3><span class="ct">Київ</span><span class="ph">+380447776655</span></article>
    </body></html>"""
    url = "http://catalog.local/clinics"
    fetcher = _MappedFetcher({url: (200, page1_html)})
    settings = make_settings(tmp_path)
    studio = Studio(settings, fetcher=fetcher)
    try:
        spec = TaskSpec(
            title="Клініки",
            prompt="Назва, місто, телефон клінік",
            entity="clinic",
            extraction="css",
            seeds=[url],
            columns=[
                FieldSpec(key="name", title="Назва"),
                FieldSpec(key="city", title="Місто"),
                FieldSpec(key="phone", title="Телефон", dtype="phone"),
            ],
        )
        task = studio.store.create_task(spec)
        studio.store.save_selector(
            "catalog.local",
            "auto:clinic",
            CssSchema(
                container="article.row",
                item_scope=True,
                fields=[
                    CssField(key="name", selector="h3.nm"),
                    CssField(key="city", selector="span.ct"),
                    CssField(key="phone", selector="span.ph", dtype="phone"),
                ],
            ),
        )

        run1 = await studio.collect(task.id, skip_calibration=True)
        done1 = await studio.wait(run1.id, timeout=20)
        assert done1 is not None and done1.status == "succeeded"
        rows1 = studio.analytics.all_current(task.id)
        assert len(rows1) == 2

        left = next(r for r in rows1 if r["payload"]["name"] == "Альфа Дент")
        right = next(r for r in rows1 if r["payload"]["name"] == "Альфа Дент Плюс")

        studio.merge_duplicates(
            task.id,
            canonical_key=left["record_key"],
            merged_key=right["record_key"],
            policy="confidence",
        )
        assert studio.analytics.count_records(task.id) == 1

        # Re-crawl sees updated phone for 'Альфа Дент Плюс' (which generates right['record_key'])
        fetcher.responses[url] = (200, page2_html)
        run2 = await studio.collect(task.id, skip_calibration=True, force=True)
        done2 = await studio.wait(run2.id, timeout=20)
        assert done2 is not None and done2.status == "succeeded"

        # Merged duplicate must NOT resurrect as a 2nd active row
        rows2 = studio.analytics.all_current(task.id)
        assert len(rows2) == 1
        assert rows2[0]["record_key"] == left["record_key"]
        assert rows2[0]["payload"]["phone"] == "+380447776655"
    finally:
        await studio.aclose()


def test_cli_merge_and_unmerge(tmp_path: Path, monkeypatch) -> None:
    """REQ-API-010: CLI daas merge / daas unmerge / daas duplicates work offline and exit non-zero on error."""
    settings = make_settings(tmp_path)
    monkeypatch.setattr("app.cli.get_settings", lambda: settings)

    store = OperationalStore(settings.paths)
    analytics = AnalyticsStore(settings.paths)
    try:
        spec = TaskSpec(
            title="CLI дублікати",
            prompt="Компанії",
            entity="company",
            seeds=["https://catalog.local"],
            columns=[
                FieldSpec(key="name", title="Назва"),
                FieldSpec(key="email", title="Email", dtype="email"),
            ],
        )
        task = store.create_task(spec)
        analytics.append_records(
            task.id,
            1,
            None,
            [
                _record("n:one", {"name": "Омега Груп"}),
                _record("n:two", {"name": "Омега Груп", "email": "hi@omega.example"}),
            ],
        )
        store.add_dataset_version(task.id, 1, None, 2, {"rows": 2})
        store.update_task(task.id, status="done")
    finally:
        analytics.close()
        store.close()

    runner = CliRunner()
    dup_res = runner.invoke(cli_app, ["duplicates", task.id, "--threshold", "85"])
    assert dup_res.exit_code == 0, dup_res.output
    assert "n:one" in dup_res.output and "n:two" in dup_res.output

    merge_res = runner.invoke(
        cli_app, ["merge", task.id, "n:one", "n:two", "--policy", "confidence"]
    )
    assert merge_res.exit_code == 0, merge_res.output
    assert "hi@omega.example" in merge_res.output or "n:one" in merge_res.output

    # Extract link id via OperationalStore
    store2 = OperationalStore(settings.paths)
    try:
        links = store2.list_entity_links(task.id)
        assert len(links) == 1
        link_id = links[0].id
    finally:
        store2.close()

    unmerge_res = runner.invoke(cli_app, ["unmerge", task.id, link_id])
    assert unmerge_res.exit_code == 0, unmerge_res.output

    # Unmerging again fails with exit code 1
    bad_res = runner.invoke(cli_app, ["unmerge", task.id, link_id])
    assert bad_res.exit_code != 0


def test_out_of_order_unmerge_replays_later_merges(tmp_path: Path) -> None:
    """REQ-ENR-011: unmerging earlier merge preserves subsequent active merges on the same canonical key."""
    settings = make_settings(tmp_path)
    app = create_app(settings)
    with TestClient(app) as client:
        studio: Studio = client.app.state.studio  # type: ignore[attr-defined]
        spec = TaskSpec(
            title="Ланцюжок злиттів",
            prompt="Компанії",
            entity="company",
            seeds=["https://catalog.local"],
            columns=[
                FieldSpec(key="name", title="Назва"),
                FieldSpec(key="phone", title="Телефон", dtype="phone"),
                FieldSpec(key="website", title="Сайт", dtype="url"),
            ],
        )
        task = studio.store.create_task(spec)
        studio.analytics.append_records(
            task.id,
            1,
            None,
            [
                _record("n:a", {"name": "Сігма"}),
                _record("n:b", {"name": "Сігма", "phone": "+380441234567"}),
                _record("n:c", {"name": "Сігма", "website": "https://sigma.example"}),
            ],
        )
        studio.store.add_dataset_version(task.id, 1, None, 3, {"rows": 3})
        studio.store.update_task(task.id, status="done")

        m1 = studio.merge_duplicates(task.id, canonical_key="n:a", merged_key="n:b", policy="confidence")
        studio.merge_duplicates(task.id, canonical_key="n:a", merged_key="n:c", policy="confidence")

        combined = studio.analytics.get_record(task.id, "n:a")
        assert combined is not None
        assert combined["payload"]["phone"] == "+380441234567"
        assert combined["payload"]["website"] == "https://sigma.example"

        # Unmerge earlier link (n:a + n:b): n:b is restored, while n:a retains n:c's website!
        unmerged = studio.unmerge_link(task.id, m1["link"]["id"])
        assert unmerged["row_count"] == 2
        assert "phone" not in unmerged["canonical_record"]["payload"]
        assert unmerged["canonical_record"]["payload"]["website"] == "https://sigma.example"
        assert unmerged["restored_record"]["payload"]["phone"] == "+380441234567"

