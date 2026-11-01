"""Guards against code silently missing from a fresh clone (e.g. swallowed by .gitignore)."""

from __future__ import annotations

import importlib
import pkgutil
import subprocess
from pathlib import Path

import pytest

import app

BACKEND = Path(__file__).resolve().parents[1]
REPO = BACKEND.parent


def _modules() -> list[str]:
    return sorted(
        info.name for info in pkgutil.walk_packages(app.__path__, prefix="app.")
    )


def test_package_has_expected_subpackages() -> None:
    names = set(_modules())
    for required in (
        "app.storage.raw_cache",
        "app.storage.exports",
        "app.storage.backup",
        "app.db.sqlite_store",
        "app.db.duckdb_store",
        "app.pipeline.runner",
    ):
        assert required in names, f"{required} is missing from the package"


@pytest.mark.parametrize("module", _modules())
def test_every_module_imports(module: str) -> None:
    importlib.import_module(module)


def test_source_files_are_not_gitignored() -> None:
    """Every .py under backend/app must be committable."""
    if not (REPO / ".git").exists():
        pytest.skip("not a git checkout")
    files = [str(path.relative_to(REPO)) for path in (BACKEND / "app").rglob("*.py")]
    result = subprocess.run(
        ["git", "check-ignore", "--no-index", *files],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=False,
    )
    ignored = [line for line in result.stdout.splitlines() if line.strip()]
    assert not ignored, f".gitignore hides source files: {ignored}"
