"""Spec-driven development guard rails (specs/README.md §7).

The living specs in ``specs/capabilities/`` are the source of truth for behaviour. These checks keep
them honest without any network or app import:

* every ``tests/<file>.py::<test>`` reference in a spec points to a test that exists;
* every requirement (``### REQ-…``) has a «Перевірка» line (a test, or an explicit gap);
* requirement IDs are unique;
* every module under ``backend/app`` is owned by at least one capability spec;
* the spec index in ``specs/README.md`` lists every capability file.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"
SPECS = ROOT / "specs"
CAPABILITIES = SPECS / "capabilities"

TEST_REF = re.compile(r"`(tests/[\w/]+\.py)::(\w+)`")
REQ_HEADING = re.compile(r"^#{3,4} (REQ-[A-Z]+-\d{3})\b", re.MULTILINE)


def _spec_files(*, include_archive: bool = True) -> list[Path]:
    files = sorted(CAPABILITIES.glob("*.md"))
    archive = SPECS / "changes" / "archive"
    if include_archive and archive.exists():
        files += sorted(archive.rglob("*.md"))
    return files


def _test_names(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def test_capability_specs_exist() -> None:
    assert (SPECS / "README.md").is_file(), "specs/README.md is missing"
    assert (SPECS / "constitution.md").is_file(), "specs/constitution.md is missing"
    assert _spec_files(include_archive=False), "specs/capabilities/ is empty"


def test_test_references_point_to_existing_tests() -> None:
    cache: dict[Path, set[str]] = {}
    broken: list[str] = []
    for spec in _spec_files():
        for file_ref, test_name in TEST_REF.findall(spec.read_text(encoding="utf-8")):
            path = BACKEND / file_ref
            if not path.is_file():
                broken.append(f"{spec.name}: {file_ref} does not exist")
                continue
            names = cache.setdefault(path, _test_names(path))
            if test_name not in names:
                broken.append(f"{spec.name}: {file_ref}::{test_name} does not exist")
    assert not broken, "Specs reference missing tests:\n" + "\n".join(broken)


def test_every_requirement_has_a_verification_line() -> None:
    missing: list[str] = []
    for spec in _spec_files(include_archive=False):
        text = spec.read_text(encoding="utf-8")
        headings = list(REQ_HEADING.finditer(text))
        for index, match in enumerate(headings):
            end = headings[index + 1].start() if index + 1 < len(headings) else len(text)
            block = text[match.start():end]
            if "~~скасовано~~" in block:
                continue
            if "Перевірка" not in block:
                missing.append(f"{spec.name}: {match.group(1)}")
    assert not missing, "Requirements without «Перевірка»:\n" + "\n".join(missing)


def test_requirement_ids_are_unique() -> None:
    seen: dict[str, str] = {}
    duplicates: list[str] = []
    for spec in _spec_files(include_archive=False):
        for req_id in REQ_HEADING.findall(spec.read_text(encoding="utf-8")):
            if req_id in seen:
                duplicates.append(f"{req_id}: {seen[req_id]} and {spec.name}")
            seen[req_id] = spec.name
    assert not duplicates, "Duplicate requirement IDs:\n" + "\n".join(duplicates)


def _app_modules() -> list[str]:
    app = BACKEND / "app"
    return sorted(
        path.relative_to(app).as_posix()
        for path in app.rglob("*.py")
        if path.name != "__init__.py" and "__pycache__" not in path.parts
    )


@pytest.mark.parametrize("module", _app_modules())
def test_every_app_module_is_owned_by_a_spec(module: str) -> None:
    corpus = "\n".join(spec.read_text(encoding="utf-8") for spec in _spec_files(include_archive=False))
    assert module in corpus, (
        f"backend/app/{module} is not mentioned in any specs/capabilities/*.md. "
        "Add it to the «Код» row of the spec that owns this behaviour."
    )


def test_index_lists_every_capability() -> None:
    index = (SPECS / "README.md").read_text(encoding="utf-8")
    missing = [
        spec.name for spec in _spec_files(include_archive=False) if f"capabilities/{spec.name}" not in index
    ]
    assert not missing, "specs/README.md index is missing: " + ", ".join(missing)
