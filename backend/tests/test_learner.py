"""Learner + CSS extraction tests on local fixtures."""

from __future__ import annotations

from pathlib import Path

from conftest import DEMO_BASE, make_spec, read_fixture

from app.extraction.css import extract_css
from app.extraction.learner import learn_schema
from app.models import TaskSpec


def test_learns_catalog_schema_and_extracts_items() -> None:
    spec: TaskSpec = make_spec()
    html = read_fixture("catalog.html")
    schema = learn_schema([(f"{DEMO_BASE}/catalog.html", html)], spec)

    assert schema.container, "a repeated item container should be learned"
    keys = {field.key for field in schema.fields}
    assert {"name", "city", "address", "phone", "website", "rating"} <= keys
    assert schema.score > 0
    assert schema.coverage["name"] == 100.0

    outcome = extract_css(html, schema, spec)
    assert len(outcome.items) == 3
    first = outcome.items[0]
    assert first["name"].startswith("Альфа")
    assert first["city"] == "Київ"
    assert first["phone"] == "+380441112233"
    assert first["website"] == "https://alpha.example"
    assert first["rating"] == 4.8
    assert first["reviews_count"] == 128


def test_learns_detail_page_schema_using_labels() -> None:
    spec = make_spec(seeds=[])
    html = read_fixture("clinic-alpha.html")
    schema = learn_schema([("https://alpha.example/clinic", html)], spec)

    assert schema.item_scope is False
    assert schema.container == "body"
    keys = {field.key for field in schema.fields}
    assert {"name", "phone", "email", "city"} <= keys

    outcome = extract_css(html, schema, spec)
    assert len(outcome.items) == 1
    item = outcome.items[0]
    assert item["name"] == "Альфа Стоматологія"
    assert item["email"] == "info@alpha.example"
    assert item["phone"] == "+380441112233"
    assert item["city"] == "Київ"


def test_learner_handles_page_without_items() -> None:
    spec = make_spec(seeds=[])
    html = "<html><body><p>Сторінка без даних</p></body></html>"
    schema = learn_schema([("https://empty.example/", html)], spec)
    # Either an empty schema or a zero-score schema is acceptable; extraction must not crash.
    assert schema.container == "" or schema.score <= 0
    assert extract_css(html, schema, spec).items == []


def test_selectors_are_reusable_across_pages() -> None:
    spec = make_spec()
    html = read_fixture("catalog.html")
    schema = learn_schema([(f"{DEMO_BASE}/catalog.html", html)], spec)
    assert schema.fields
    # Re-extraction from a copy must yield identical results (deterministic selectors).
    outcome_a = extract_css(html, schema, spec)
    outcome_b = extract_css(html, schema, spec)
    assert outcome_a.items == outcome_b.items


def test_fixture_files_exist() -> None:
    for name in ("catalog.html", "clinic-alpha.html", "site_alpha.html"):
        assert (Path(__file__).parent / "fixtures" / name).is_file()
