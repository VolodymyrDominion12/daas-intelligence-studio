"""Tombstones (P1-03): records missing from a full crawl become ``removed_entity``."""

from __future__ import annotations

import re
from pathlib import Path

import duckdb
from conftest import DEMO_BASE, FixtureFetcher, make_settings, make_spec, read_fixture
from test_storage import _record

from app.db.duckdb_store import AnalyticsStore
from app.db.sqlite_store import OperationalStore
from app.models import FieldSpec, PageContent
from app.runtime import Studio
from app.scraping.fetcher import FetchResult
from app.scraping.page import html_to_markdown, html_to_text
from app.utils import hash_text

SEED = f"{DEMO_BASE}/catalog.html"
ABOUT = f"{DEMO_BASE}/about"
COLUMNS = [FieldSpec(key="name", title="Назва")]
ALL = {"Альфа Стоматологія", "Бета Дентал", "Гама Смайл"}
GAMMA = "Гама Смайл"


# ---------------------------------------------------------------------------
# storage
# ---------------------------------------------------------------------------


def _revisions(analytics: AnalyticsStore, dataset_id: str) -> dict[str, int]:
    return {key: revision for key, (revision, _v) in analytics.current_state(dataset_id).items()}


def test_tombstone_hides_record_but_keeps_history(tmp_path: Path) -> None:
    analytics = AnalyticsStore(make_settings(tmp_path).paths)
    try:
        analytics.append_records("ds", 1, "r1", [_record("a", {"name": "A"}),
                                                  _record("b", {"name": "B"})])
        revisions = _revisions(analytics, "ds")
        assert analytics.tombstone("ds", 2, "r2", {"b": revisions["b"], "missing": 1}) == 1

        assert {row["record_key"] for row in analytics.all_current("ds")} == {"a"}
        assert analytics.get_record("ds", "b") is None
        history = analytics.record_history("ds", "b")
        assert history[0]["deleted"] is True and history[0]["payload"] == {"name": "B"}
        assert history[1]["deleted"] is False

        changes = analytics.diff_versions("ds", from_version=1, to_version=2, columns=COLUMNS)
        assert [(c.type, c.record_key) for c in changes] == [("removed_entity", "b")]

        # the record comes back → new version, visible again, reported as new
        analytics.append_records("ds", 3, "r3", [_record("b", {"name": "B"})])
        assert {row["record_key"] for row in analytics.all_current("ds")} == {"a", "b"}
        back = analytics.diff_versions("ds", from_version=2, to_version=3, columns=COLUMNS)
        assert [(c.type, c.record_key) for c in back] == [("new_entity", "b")]
    finally:
        analytics.close()


def test_tombstone_skips_records_reobserved_meanwhile(tmp_path: Path) -> None:
    """A stale revision (record re-observed after selection) must not be hidden."""
    analytics = AnalyticsStore(make_settings(tmp_path).paths)
    try:
        analytics.append_records("ds", 1, "r1", [_record("b", {"name": "B"})])
        stale = _revisions(analytics, "ds")["b"]
        analytics.append_records("ds", 2, "r-concurrent", [_record("b", {"name": "B"})])
        assert analytics.tombstone("ds", 2, "r2", {"b": stale}) == 0
        assert analytics.count_records("ds") == 1
    finally:
        analytics.close()


def test_rollback_tombstones_of_unfinished_run(tmp_path: Path) -> None:
    analytics = AnalyticsStore(make_settings(tmp_path).paths)
    try:
        analytics.append_records("ds", 1, "r1", [_record("b", {"name": "B"})])
        analytics.tombstone("ds", 2, "r2", _revisions(analytics, "ds"))
        assert analytics.count_records("ds") == 0
        assert analytics.rollback_tombstones("r2") == 1
        assert analytics.count_records("ds") == 1
    finally:
        analytics.close()


def test_snapshot_contains_current_state_only(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    analytics = AnalyticsStore(settings.paths)
    try:
        analytics.append_records("ds", 1, "r1", [_record("a", {"name": "A1"}),
                                                  _record("b", {"name": "B"})])
        analytics.append_records("ds", 2, "r2", [_record("a", {"name": "A2"})])
        analytics.tombstone("ds", 2, "r2", {"b": _revisions(analytics, "ds")["b"]})
        target = analytics.snapshot("ds", 2, settings.paths.snapshots / "ds" / "part.parquet")
        con = duckdb.connect()
        try:
            rows = con.execute(
                "SELECT record_key, payload FROM read_parquet(?) ORDER BY record_key",
                [str(target)],
            ).fetchall()
        finally:
            con.close()
        assert [row[0] for row in rows] == ["a"]  # no removed record, no stale revision
        assert "A2" in rows[0][1]
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
        assert analytics.tombstone("ds", 2, "r2", _revisions(analytics, "ds")) == 1
        assert analytics.count_records("ds") == 0
    finally:
        analytics.close()


def test_claim_jobs_is_scoped_to_the_run(tmp_path: Path) -> None:
    store = OperationalStore(make_settings(tmp_path).paths)
    try:
        task_a = store.create_task(make_spec())
        task_b = store.create_task(make_spec())
        run_a = store.create_run(task_a.id, "collect")
        run_b = store.create_run(task_b.id, "collect")
        store.enqueue_urls(run_b.id, task_b.id, [("https://b.example/1", 0, None)])
        store.enqueue_urls(run_a.id, task_a.id, [("https://a.example/1", 0, None)])

        claimed = store.claim_jobs("worker-a", run_id=run_a.id, limit=10)
        assert [job["run_id"] for job in claimed] == [run_a.id]
        assert store.job_counts(run_b.id)["queued"] == 1
    finally:
        store.close()


def test_misses_count_once_per_run(tmp_path: Path) -> None:
    store = OperationalStore(make_settings(tmp_path).paths)
    try:
        assert store.bump_misses("ds", ["k"], "r1") == {"k": 1}
        assert store.bump_misses("ds", ["k"], "r1") == {"k": 1}  # same run again
        assert store.bump_misses("ds", ["k"], "r2") == {"k": 2}
        store.rollback_misses("r2")
        assert store.bump_misses("ds", ["k"], "r3") == {"k": 2}
        store.reset_misses("ds", ["k"])
        assert store.bump_misses("ds", ["k"], "r4") == {"k": 1}
    finally:
        store.close()


# ---------------------------------------------------------------------------
# pipeline
# ---------------------------------------------------------------------------


class MutableFetcher(FixtureFetcher):
    """FixtureFetcher whose pages can be replaced, broken or served «from cache»."""

    def __init__(self) -> None:
        super().__init__()
        self.html: dict[str, str] = {}
        self.broken: set[str] = set()
        self.cached: set[str] = set()

    async def fetch(self, url: str, *, force_refresh: bool = False) -> FetchResult:
        self.calls.append(url)
        if url in self.broken:
            return FetchResult(kind="permanent", error="HTTP 404", status=404)
        if url in self.html:
            html = self.html[url]
        else:
            fixture = self._resolve(url)
            if fixture is None:
                return FetchResult(kind="permanent", error=f"no fixture for {url}", status=404)
            html = read_fixture(fixture)
        page = PageContent(
            url=url, final_url=url, html=html, text=html_to_text(html),
            markdown=html_to_markdown(html, url), content_hash=hash_text(html),
            engine=self.name, from_cache=url in self.cached,
        )
        return FetchResult(page=page, status=200)


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


async def _run(studio: Studio, task_id: str, kind: str = "collect"):
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
    first = await _run(studio, task.id)
    assert first.stats.records_new == 3
    return studio, task.dataset_id or task.id, task.id


def _names(studio: Studio, dataset_id: str) -> set[str]:
    return {row["payload"].get("name") for row in studio.analytics.all_current(dataset_id)}


def _key_of(studio: Studio, dataset_id: str, name: str) -> str:
    return next(
        row["record_key"] for row in studio.analytics.all_current(dataset_id)
        if row["payload"].get("name") == name
    )


def _event_types(studio: Studio, task_id: str) -> list[str]:
    return [event.type for event in studio.store.list_events(task_id, limit=500)]


async def test_full_crawl_marks_missing_record_removed(tmp_path: Path) -> None:
    fetcher = MutableFetcher()
    studio, dataset_id, task_id = await _studio(tmp_path, fetcher, [SEED])
    try:
        gamma_key = _key_of(studio, dataset_id, GAMMA)
        fetcher.html[SEED] = catalog_without(GAMMA)
        second = await _run(studio, task_id)

        assert second.stats.records_removed == 1
        assert _names(studio, dataset_id) == ALL - {GAMMA}
        assert studio.analytics.get_record(dataset_id, gamma_key) is None
        removed = [e for e in studio.store.list_events(task_id, limit=500)
                   if e.type == "records_removed"]
        assert removed and removed[-1].data["keys"] == [gamma_key]
        assert "removed_entity" in _event_types(studio, task_id)
        manifest = studio.store.dataset_manifest(dataset_id)
        assert manifest["full_crawl"] is True and manifest["removed"] == 1

        # the clinic is back → a new record again, same key
        fetcher.html.pop(SEED)
        third = await _run(studio, task_id)
        assert third.stats.records_new == 1
        assert _names(studio, dataset_id) == ALL
        assert studio.analytics.get_record(dataset_id, gamma_key) is not None
    finally:
        await studio.aclose()


async def test_partial_crawl_never_removes(tmp_path: Path) -> None:
    fetcher = MutableFetcher()
    studio, dataset_id, task_id = await _studio(tmp_path, fetcher, [SEED, ABOUT])
    try:
        fetcher.html[SEED] = catalog_without(GAMMA)
        fetcher.broken.add(ABOUT)  # one page failed → the crawl is not complete
        second = await _run(studio, task_id)

        assert second.stats.records_removed == 0
        assert _names(studio, dataset_id) == ALL
        assert "tombstones_skipped" in _event_types(studio, task_id)
        assert studio.store.dataset_manifest(dataset_id)["full_crawl"] is False
    finally:
        await studio.aclose()


async def test_unchecked_known_source_blocks_removal(tmp_path: Path) -> None:
    """A record seen on two pages is not removed when only one of them is re-checked."""
    fetcher = MutableFetcher()
    studio, dataset_id, task_id = await _studio(tmp_path, fetcher, [SEED, ABOUT])
    try:
        task = studio.store.get_task(task_id)
        spec = task.spec.model_copy(deep=True)
        spec.seeds = [SEED]  # ABOUT is still a known source of every record
        studio.update_task(task_id, spec=spec)
        fetcher.html[SEED] = catalog_without(GAMMA)
        second = await _run(studio, task_id)

        assert second.stats.records_removed == 0
        assert _names(studio, dataset_id) == ALL
    finally:
        await studio.aclose()


async def test_cached_page_is_not_evidence(tmp_path: Path) -> None:
    fetcher = MutableFetcher()
    studio, dataset_id, task_id = await _studio(tmp_path, fetcher, [SEED])
    try:
        fetcher.html[SEED] = catalog_without(GAMMA)
        fetcher.cached.add(SEED)
        second = await _run(studio, task_id)

        assert second.stats.records_removed == 0
        assert _names(studio, dataset_id) == ALL
    finally:
        await studio.aclose()


async def test_mass_removal_is_refused(tmp_path: Path) -> None:
    # 1 of 3 records (33%) disappears, above a 20% safety threshold.
    fetcher = MutableFetcher()
    studio, dataset_id, task_id = await _studio(
        tmp_path, fetcher, [SEED], tombstone_max_ratio=0.2
    )
    try:
        fetcher.html[SEED] = catalog_without(GAMMA)
        await _run(studio, task_id)

        assert _names(studio, dataset_id) == ALL
        assert "tombstones_suspicious" in _event_types(studio, task_id)
    finally:
        await studio.aclose()


async def test_page_limit_without_remaining_work_is_a_full_crawl(tmp_path: Path) -> None:
    fetcher = MutableFetcher()
    studio, dataset_id, task_id = await _studio(tmp_path, fetcher, [SEED])
    try:
        task = studio.store.get_task(task_id)
        spec = task.spec.model_copy(deep=True)
        spec.limits.max_pages = 1  # the only page is processed, then the limit note fires
        studio.update_task(task_id, spec=spec)
        fetcher.html[SEED] = catalog_without(GAMMA)
        second = await _run(studio, task_id)

        assert second.stats.records_removed == 1
        assert _names(studio, dataset_id) == ALL - {GAMMA}
    finally:
        await studio.aclose()


async def test_grace_period_counts_only_eligible_misses(tmp_path: Path) -> None:
    fetcher = MutableFetcher()
    studio, dataset_id, task_id = await _studio(
        tmp_path, fetcher, [SEED], tombstone_after_misses=2
    )
    try:
        fetcher.html[SEED] = catalog_without(GAMMA)
        first_miss = await _run(studio, task_id)  # eligible miss #1
        assert first_miss.stats.records_removed == 0
        assert _names(studio, dataset_id) == ALL

        fetcher.cached.add(SEED)  # full crawl, but from cache → not a miss
        await _run(studio, task_id)
        assert _names(studio, dataset_id) == ALL

        fetcher.cached.clear()
        second_miss = await _run(studio, task_id)  # eligible miss #2 → removed
        assert second_miss.stats.records_removed == 1
        assert _names(studio, dataset_id) == ALL - {GAMMA}
    finally:
        await studio.aclose()


async def test_failed_finish_rolls_back_removals(tmp_path: Path) -> None:
    fetcher = MutableFetcher()
    studio, dataset_id, task_id = await _studio(tmp_path, fetcher, [SEED])
    try:
        fetcher.html[SEED] = catalog_without(GAMMA)

        async def broken_finish(*_args, **_kwargs):
            raise RuntimeError("disk full")

        studio.runner._finish_dataset = broken_finish  # type: ignore[method-assign]
        run = await studio.collect(task_id)
        finished = await studio.wait(run.id, timeout=30)
        assert finished is not None and finished.status == "failed"
        assert _names(studio, dataset_id) == ALL  # the tombstone did not stick
    finally:
        await studio.aclose()
