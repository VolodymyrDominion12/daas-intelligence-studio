"""Tombstones (P1-03): records missing from a full crawl become ``removed_entity``."""

from __future__ import annotations

import re
from pathlib import Path

import duckdb
from conftest import DEMO_BASE, FixtureFetcher, make_settings, make_spec, read_fixture
from test_storage import _record

from app.db.duckdb_store import AnalyticsStore
from app.models import FieldSpec, PageContent
from app.runtime import Studio
from app.scraping.fetcher import FetchResult
from app.scraping.page import html_to_markdown, html_to_text
from app.utils import hash_text

SEED = f"{DEMO_BASE}/catalog.html"
ABOUT = f"{DEMO_BASE}/about"
COLUMNS = [FieldSpec(key="name", title="Назва")]


# ---------------------------------------------------------------------------
# storage
# ---------------------------------------------------------------------------


def test_tombstone_hides_record_but_keeps_history(tmp_path: Path) -> None:
    analytics = AnalyticsStore(make_settings(tmp_path).paths)
    try:
        analytics.append_records("ds", 1, "r1", [_record("a", {"name": "A"}),
                                                  _record("b", {"name": "B"})])
        assert analytics.tombstone("ds", 2, "r2", ["b", "b", "missing"]) == 1

        assert {row["record_key"] for row in analytics.all_current("ds")} == {"a"}
        assert analytics.count_records("ds") == 1
        assert analytics.get_record("ds", "b") is None
        history = analytics.record_history("ds", "b")
        assert history[0]["deleted"] is True and history[0]["payload"] == {"name": "B"}
        assert history[1]["deleted"] is False

        changes = analytics.diff_versions("ds", from_version=1, to_version=2, columns=COLUMNS)
        assert [(c.type, c.record_key) for c in changes] == [("removed_entity", "b")]

        # the record comes back → new version, visible again, reported as new
        analytics.append_records("ds", 3, "r3", [_record("b", {"name": "B"})])
        assert analytics.count_records("ds") == 2
        back = analytics.diff_versions("ds", from_version=2, to_version=3, columns=COLUMNS)
        assert [(c.type, c.record_key) for c in back] == [("new_entity", "b")]
    finally:
        analytics.close()


def test_old_database_is_migrated(tmp_path: Path) -> None:
    """A file created before the ``deleted`` column (with its index) opens and works."""
    paths = make_settings(tmp_path).paths
    paths.ensure()
    con = duckdb.connect(str(paths.duckdb))
    con.execute("CREATE SEQUENCE record_revision_seq")
    con.execute(
        """
        CREATE TABLE record_versions (
            dataset_id VARCHAR NOT NULL, record_key VARCHAR NOT NULL,
            dataset_version INTEGER NOT NULL,
            revision BIGINT DEFAULT nextval('record_revision_seq'),
            run_id VARCHAR, source_url VARCHAR, content_hash VARCHAR,
            payload JSON, provenance JSON, observed_at TIMESTAMP NOT NULL,
            PRIMARY KEY (dataset_id, record_key, revision)
        )
        """
    )
    con.execute(
        "CREATE INDEX idx_record_versions_lookup ON record_versions(dataset_id, record_key, revision)"
    )
    con.execute(
        "INSERT INTO record_versions (dataset_id, record_key, dataset_version, payload, provenance,"
        " observed_at) VALUES ('ds', 'old', 1, '{\"name\": \"Old\"}', '{}', now())"
    )
    con.close()

    analytics = AnalyticsStore(paths)
    try:
        assert analytics.count_records("ds") == 1
        assert analytics.tombstone("ds", 2, "r2", ["old"]) == 1
        assert analytics.count_records("ds") == 0
    finally:
        analytics.close()


# ---------------------------------------------------------------------------
# pipeline
# ---------------------------------------------------------------------------


class MutableFetcher(FixtureFetcher):
    """FixtureFetcher whose pages can be replaced or broken between runs."""

    def __init__(self) -> None:
        super().__init__()
        self.html: dict[str, str] = {}
        self.broken: set[str] = set()

    async def fetch(self, url: str, *, force_refresh: bool = False) -> FetchResult:
        if url in self.broken:
            self.calls.append(url)
            return FetchResult(kind="permanent", error="HTTP 404", status=404)
        if url in self.html:
            self.calls.append(url)
            html = self.html[url]
            page = PageContent(
                url=url, final_url=url, html=html, text=html_to_text(html),
                markdown=html_to_markdown(html, url), content_hash=hash_text(html),
                engine=self.name,
            )
            return FetchResult(page=page, status=200)
        return await super().fetch(url, force_refresh=force_refresh)


def catalog_without(*names: str) -> str:
    html = read_fixture("catalog.html")
    for name in names:
        html = re.sub(
            r'<article class="clinic-card">(?:(?!</article>).)*?' + re.escape(name)
            + r".*?</article>",
            "",
            html,
            flags=re.S,
        )
    return html


async def _run(studio: Studio, task_id: str, kind: str):
    if kind == "calibrate":
        run = await studio.calibrate(task_id)
    else:
        run = await studio.collect(task_id)
    finished = await studio.wait(run.id, timeout=30)
    assert finished is not None and finished.status in {"succeeded", "partial"}, finished
    return finished


async def _studio(tmp_path: Path, fetcher: MutableFetcher, seeds: list[str], **settings):
    studio = Studio(make_settings(tmp_path, **settings), fetcher=fetcher)
    spec = make_spec(seeds=seeds)
    task = studio.store.create_task(spec)
    await _run(studio, task.id, "calibrate")
    first = await _run(studio, task.id, "collect")
    assert first.stats.records_new == 3
    return studio, task.dataset_id or task.id, task.id


def _event_types(studio: Studio, task_id: str) -> list[str]:
    return [event.type for event in studio.store.list_events(task_id, limit=500)]


async def test_full_crawl_marks_missing_record_removed(tmp_path: Path) -> None:
    fetcher = MutableFetcher()
    studio, dataset_id, task_id = await _studio(tmp_path, fetcher, [SEED])
    try:
        fetcher.html[SEED] = catalog_without("Гама Смайл")
        second = await _run(studio, task_id, "collect")

        assert second.stats.records_removed == 1
        assert studio.analytics.count_records(dataset_id) == 2
        types = _event_types(studio, task_id)
        assert "records_removed" in types and "removed_entity" in types
        manifest = studio.store.dataset_manifest(dataset_id)
        assert manifest["full_crawl"] is True and manifest["removed"] == 1

        # the clinic is back → a new record again
        fetcher.html.pop(SEED)
        third = await _run(studio, task_id, "collect")
        assert third.stats.records_new == 1
        assert studio.analytics.count_records(dataset_id) == 3
    finally:
        await studio.aclose()


async def test_partial_crawl_never_removes(tmp_path: Path) -> None:
    fetcher = MutableFetcher()
    studio, dataset_id, task_id = await _studio(tmp_path, fetcher, [SEED, ABOUT])
    try:
        fetcher.html[SEED] = catalog_without("Гама Смайл")
        fetcher.broken.add(ABOUT)  # one page failed → the crawl is not complete
        second = await _run(studio, task_id, "collect")

        assert second.stats.records_removed == 0
        assert studio.analytics.count_records(dataset_id) == 3
        assert "tombstones_skipped" in _event_types(studio, task_id)
        assert studio.store.dataset_manifest(dataset_id)["full_crawl"] is False
    finally:
        await studio.aclose()


async def test_mass_removal_is_refused(tmp_path: Path) -> None:
    fetcher = MutableFetcher()
    studio, dataset_id, task_id = await _studio(tmp_path, fetcher, [SEED])
    try:
        fetcher.html[SEED] = catalog_without("Бета Дентал", "Гама Смайл")  # 2 of 3 gone
        await _run(studio, task_id, "collect")

        assert studio.analytics.count_records(dataset_id) == 3
        assert "tombstones_suspicious" in _event_types(studio, task_id)
    finally:
        await studio.aclose()


async def test_grace_period_needs_consecutive_misses(tmp_path: Path) -> None:
    fetcher = MutableFetcher()
    studio, dataset_id, task_id = await _studio(
        tmp_path, fetcher, [SEED], tombstone_after_misses=2
    )
    try:
        fetcher.html[SEED] = catalog_without("Гама Смайл")
        first_miss = await _run(studio, task_id, "collect")
        assert first_miss.stats.records_removed == 0
        assert studio.analytics.count_records(dataset_id) == 3

        second_miss = await _run(studio, task_id, "collect")
        assert second_miss.stats.records_removed == 1
        assert studio.analytics.count_records(dataset_id) == 2
    finally:
        await studio.aclose()
