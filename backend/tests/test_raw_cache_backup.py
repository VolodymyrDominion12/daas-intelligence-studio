"""Raw page cache (zstd) and storage backup."""

from __future__ import annotations

import json
import os
import time
import zipfile
from pathlib import Path

from conftest import make_settings

from app.db.duckdb_store import AnalyticsStore
from app.db.sqlite_store import OperationalStore
from app.models import PageContent
from app.storage.backup import create_backup
from app.storage.raw_cache import RawCache
from app.utils import hash_text


def _page(url: str, html: str = "<html><body><h1>Альфа</h1></body></html>",
          final_url: str | None = None) -> PageContent:
    return PageContent(
        url=url,
        final_url=final_url or url,
        html=html,
        text="Альфа",
        content_hash=hash_text(html),
    )


def test_put_get_roundtrip_and_index(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    store = OperationalStore(settings.paths)
    cache = RawCache(settings.paths.raw_cache, store)
    try:
        page = _page("https://a.example/1", final_url="https://a.example/1?ref=x")
        cache.put(page)

        cached = cache.get("https://a.example/1")
        assert cached is not None
        assert cached.from_cache is True
        assert cached.html == page.html and cached.text == "Альфа"
        # the final URL is an alias of the same page
        assert cache.get("https://a.example/1?ref=x") is not None
        assert cache.get("https://a.example/missing") is None

        meta = store.page_meta("https://a.example/1")
        assert meta is not None and meta["content_hash"] == page.content_hash

        stats = cache.stats()
        assert stats["pages"] == 2 and stats["bytes"] > 0
        assert {"pages", "compressed", "path"} <= stats.keys()
    finally:
        store.close()


def test_max_age_and_purge(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    store = OperationalStore(settings.paths)
    cache = RawCache(settings.paths.raw_cache, store)
    try:
        cache.put(_page("https://a.example/old"))
        cache.put(_page("https://a.example/new"))
        old_path = cache.path_for("https://a.example/old")
        ten_days_ago = time.time() - 10 * 86400
        os.utime(old_path, (ten_days_ago, ten_days_ago))

        assert cache.get("https://a.example/old", max_age_seconds=3600) is None
        assert cache.get("https://a.example/old") is not None  # no TTL → still served

        assert cache.purge(older_than_days=5) == 1
        assert not cache.has("https://a.example/old")
        assert cache.has("https://a.example/new")
        assert store.page_meta("https://a.example/old") is None

        assert cache.purge(older_than_days=None) == 1
        assert cache.stats()["pages"] == 0
    finally:
        store.close()


def test_corrupt_entry_is_a_miss(tmp_path: Path) -> None:
    cache = RawCache(tmp_path / "cache")
    cache.put(_page("https://a.example/x"))
    cache.path_for("https://a.example/x").write_bytes(b"not zstd")
    assert cache.get("https://a.example/x") is None
    assert not cache.has("https://a.example/x")


def test_backup_bundle_contents(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    paths = settings.paths
    store = OperationalStore(paths)
    analytics = AnalyticsStore(paths)
    cache = RawCache(paths.raw_cache, store)
    try:
        cache.put(_page("https://a.example/1"))
        (paths.snapshots / "ds1").mkdir(parents=True, exist_ok=True)
        (paths.snapshots / "ds1" / "v1.parquet").write_bytes(b"PAR1")

        plain = create_backup(store, analytics, paths, target=tmp_path / "b1.zip")
        with zipfile.ZipFile(plain) as bundle:
            names = set(bundle.namelist())
            manifest = json.loads(bundle.read("backup-manifest.json"))
        assert {"operational.sqlite", "analytics.duckdb", "snapshots/ds1/v1.parquet"} <= names
        assert not any(name.startswith("raw_cache/") for name in names)
        assert manifest["include_cache"] is False

        full = create_backup(store, analytics, paths, target=tmp_path / "b2.zip",
                             include_cache=True)
        with zipfile.ZipFile(full) as bundle:
            assert any(name.startswith("raw_cache/") for name in bundle.namelist())
    finally:
        analytics.close()
        store.close()
