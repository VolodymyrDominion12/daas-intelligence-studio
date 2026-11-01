"""Local-first backup: one zip with SQLite, DuckDB, snapshots and a manifest (docs/02 §6).

* SQLite is copied with the online backup API — consistent even while WAL is active.
* DuckDB is checkpointed first so the ``.duckdb`` file on disk is complete, then copied.
* Parquet snapshots are included; the raw page cache only on request (it can be large and
  is re-creatable by re-crawling).
"""

from __future__ import annotations

import json
import shutil
import tempfile
import time
import zipfile
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .. import __version__
from ..utils import utcnow

if TYPE_CHECKING:  # pragma: no cover
    from ..config import Paths
    from ..db.duckdb_store import AnalyticsStore
    from ..db.sqlite_store import OperationalStore

MANIFEST_NAME = "backup-manifest.json"


def default_backup_path(paths: Paths) -> Path:
    return paths.root / "backups" / f"daas-backup-{int(time.time())}.zip"


def create_backup(
    store: OperationalStore,
    analytics: AnalyticsStore,
    paths: Paths,
    *,
    target: Path | None = None,
    include_cache: bool = False,
) -> Path:
    target = Path(target) if target else default_backup_path(paths)
    target.parent.mkdir(parents=True, exist_ok=True)
    files: list[dict[str, Any]] = []

    with tempfile.TemporaryDirectory(prefix="daas-backup-") as scratch:
        scratch_dir = Path(scratch)

        sqlite_copy = store.backup_to(scratch_dir / "operational.sqlite")
        analytics.checkpoint()
        duck_copy = scratch_dir / "analytics.duckdb"
        shutil.copy2(paths.duckdb, duck_copy)

        tmp_target = target.with_name(f".{target.name}.tmp")
        try:
            with zipfile.ZipFile(tmp_target, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
                for source, arcname in ((sqlite_copy, "operational.sqlite"),
                                        (duck_copy, "analytics.duckdb")):
                    bundle.write(source, arcname)
                    files.append({"name": arcname, "bytes": source.stat().st_size})

                for folder, prefix, enabled in (
                    (paths.snapshots, "snapshots", True),
                    (paths.raw_cache, "raw_cache", include_cache),
                ):
                    if not enabled or not folder.exists():
                        continue
                    count, size = 0, 0
                    for path in sorted(folder.rglob("*")):
                        if path.is_file() and not path.name.endswith(".tmp"):
                            bundle.write(path, f"{prefix}/{path.relative_to(folder).as_posix()}")
                            count += 1
                            size += path.stat().st_size
                    files.append({"name": f"{prefix}/", "files": count, "bytes": size})

                manifest = {
                    "created_at": utcnow().isoformat(),
                    "generator": f"daas-studio {__version__}",
                    "storage_root": str(paths.root),
                    "include_cache": include_cache,
                    "files": files,
                    "restore": (
                        "Зупиніть студію, розпакуйте архів у каталог сховища "
                        "(operational.sqlite, analytics.duckdb, snapshots/, raw_cache/)."
                    ),
                }
                bundle.writestr(MANIFEST_NAME, json.dumps(manifest, ensure_ascii=False, indent=2))
            tmp_target.replace(target)
        finally:
            tmp_target.unlink(missing_ok=True)
    return target
