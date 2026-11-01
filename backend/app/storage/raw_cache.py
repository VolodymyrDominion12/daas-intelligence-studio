"""Compressed raw page cache (bronze layer, docs/01 §2).

Every fetched page is stored as zstd-compressed JSON keyed by a hash of its URL. A new field a
month later is then a local re-extraction, not a new crawl. The SQLite ``raw_pages`` table keeps a
lightweight index (hash, status, size, engine) for dashboards and freshness checks.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import zstandard

from ..models import PageContent
from ..utils import human_bytes

if TYPE_CHECKING:  # pragma: no cover
    from ..db.sqlite_store import OperationalStore

logger = logging.getLogger(__name__)

SUFFIX = ".json.zst"
COMPRESSION_LEVEL = 6


def url_digest(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8")).hexdigest()


class RawCache:
    def __init__(self, root: Path, store: OperationalStore | None = None) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.store = store
        self._lock = threading.Lock()
        self._compressor = zstandard.ZstdCompressor(level=COMPRESSION_LEVEL)
        self._decompressor = zstandard.ZstdDecompressor()

    # -- paths ------------------------------------------------------------
    def path_for(self, url: str) -> Path:
        digest = url_digest(url)
        return self.root / digest[:2] / f"{digest}{SUFFIX}"

    # -- read/write -------------------------------------------------------
    def put(self, page: PageContent) -> None:
        """Store a page under its requested URL and, if different, its final URL."""
        payload = page.model_dump(mode="json")
        payload["from_cache"] = False
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        with self._lock:
            blob = self._compressor.compress(raw)
        for url in {page.url, page.final_url or page.url}:
            if not url:
                continue
            self._write_atomic(self.path_for(url), blob)
            if self.store is not None:
                try:
                    self.store.remember_page(
                        url, page.content_hash, page.status_code, len(blob), page.engine
                    )
                except Exception:  # noqa: BLE001 - the index is best-effort
                    logger.debug("raw_pages index update failed for %s", url, exc_info=True)

    def get(self, url: str, *, max_age_seconds: float | None = None) -> PageContent | None:
        path = self.path_for(url)
        try:
            stat = path.stat()
        except FileNotFoundError:
            return None
        if max_age_seconds is not None and max_age_seconds >= 0:
            if time.time() - stat.st_mtime > max_age_seconds:
                return None
        try:
            blob = path.read_bytes()
            with self._lock:
                raw = self._decompressor.decompress(blob)
            page = PageContent.model_validate_json(raw)
        except Exception:  # noqa: BLE001 - a corrupt entry is a cache miss, not a crash
            logger.warning("corrupt raw cache entry %s — dropping it", path)
            with contextlib.suppress(OSError):
                path.unlink()
            return None
        page.from_cache = True
        return page

    def has(self, url: str) -> bool:
        return self.path_for(url).exists()

    def delete(self, url: str) -> bool:
        path = self.path_for(url)
        try:
            path.unlink()
        except FileNotFoundError:
            return False
        if self.store is not None:
            self.store.forget_pages([url])
        return True

    # -- maintenance ------------------------------------------------------
    def _entries(self) -> list[Path]:
        return [path for path in self.root.glob(f"*/*{SUFFIX}") if path.is_file()]

    def purge(self, *, older_than_days: float | None = None) -> int:
        """Delete cache entries older than N days; ``None`` wipes the whole cache.

        Retention for raw pages (docs/02 §2.1 «мінімізація та строк зберігання»).
        """
        cutoff = None if older_than_days is None else time.time() - older_than_days * 86400
        removed = 0
        removed_urls: list[str] = []
        for path in self._entries():
            try:
                if cutoff is not None and path.stat().st_mtime >= cutoff:
                    continue
                url = self._url_of(path)
                path.unlink()
            except OSError:
                continue
            removed += 1
            if url:
                removed_urls.append(url)
        for folder in self.root.iterdir() if self.root.exists() else []:
            if folder.is_dir():
                with contextlib.suppress(OSError):
                    folder.rmdir()  # only succeeds when empty
        if self.store is not None and removed_urls:
            self.store.forget_pages(removed_urls)
        return removed

    def stats(self) -> dict[str, Any]:
        entries = self._entries()
        size = 0
        newest: float | None = None
        for path in entries:
            try:
                stat = path.stat()
            except OSError:
                continue
            size += stat.st_size
            newest = stat.st_mtime if newest is None else max(newest, stat.st_mtime)
        return {
            "pages": len(entries),
            "bytes": size,
            "compressed": human_bytes(size),
            "path": str(self.root),
            "newest": datetime.fromtimestamp(newest).isoformat() if newest else None,
        }

    # -- internals --------------------------------------------------------
    def _url_of(self, path: Path) -> str | None:
        try:
            with self._lock:
                raw = self._decompressor.decompress(path.read_bytes())
            return json.loads(raw).get("url")
        except Exception:  # noqa: BLE001
            return None

    @staticmethod
    def _write_atomic(path: Path, blob: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        tmp.write_bytes(blob)
        tmp.replace(path)
