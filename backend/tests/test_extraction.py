"""Extraction engine tests: JSON-LD, CSS strategy, quarantine-worthy output."""

from __future__ import annotations

import pytest
from conftest import DEMO_BASE, make_settings, make_spec, read_fixture

from app.extraction.engine import ExtractionEngine
from app.extraction.jsonld import extract_jsonld
from app.extraction.learner import learn_schema
from app.llm.router import LLMRouter
from app.models import PageContent
from app.scraping.page import extract_json_ld, extract_links, html_to_markdown, html_to_text
from app.utils import hash_text

PRODUCT_HTML = """<!doctype html><html><head>
<script type="application/ld+json">
{"@context":"https://schema.org","@type":"Product","name":"Крісло Ерго",
 "sku":"ERG-01","url":"https://shop.example/ergo","description":"Ортопедичне крісло",
 "offers":{"@type":"Offer","price":"3499","priceCurrency":"UAH","availability":"InStock"},
 "aggregateRating":{"@type":"AggregateRating","ratingValue":"4.6","reviewCount":"87"}}
</script></head><body><h1>Крісло Ерго</h1><p>Ціна: 3 499 ₴</p></body></html>"""


def _page(url: str, html: str) -> PageContent:
    return PageContent(
        url=url,
        final_url=url,
        html=html,
        text=html_to_text(html),
        markdown=html_to_markdown(html, url),
        content_hash=hash_text(html),
    )


def test_jsonld_extraction_maps_columns() -> None:
    spec = make_spec("Збери товари магазину: назва, артикул, ціна, рейтинг, відгуки, наявність")
    outcome = extract_jsonld(PRODUCT_HTML, spec, source_url="https://shop.example/ergo")
    assert outcome.strategy == "json_ld"
    assert len(outcome.items) == 1
    item = outcome.items[0]
    assert item["name"] == "Крісло Ерго"
    assert item["price"] == 3499.0 or item["price"] == "3499"
    assert item["reviews_count"] == "87" or item["reviews_count"] == 87
    assert item["sku"] == "ERG-01"


def test_jsonld_survives_broken_script(capsys) -> None:
    html = '<script type="application/ld+json">{"@type":"Organization","name":"X"</script>'
    assert extract_json_ld(html) == []
    # And the engine must not crash on a page with no usable data.
    assert extract_jsonld(html, make_spec()) .items == []


@pytest.mark.asyncio
async def test_engine_uses_jsonld_before_css(tmp_path) -> None:
    settings = make_settings(tmp_path)
    router = LLMRouter(settings)
    engine = ExtractionEngine(settings, router)
    spec = make_spec(
        "Збери товари магазину: назва, ціна, рейтинг, відгуки, наявність, сайт"
    )
    outcome = await engine.extract(_page("https://shop.example/ergo", PRODUCT_HTML), spec, allow_llm=False)
    assert outcome.strategy == "json_ld"
    records = engine.to_records(outcome, _page("https://shop.example/ergo", PRODUCT_HTML), spec)
    assert records
    assert records[0].payload["name"] == "Крісло Ерго"
    assert records[0].record_key.startswith("d:shop.example")
    assert all(prov.method == "json_ld" for prov in records[0].provenance.values())
    await router.aclose()


@pytest.mark.asyncio
async def test_engine_uses_learned_css_schema(tmp_path) -> None:
    settings = make_settings(tmp_path)
    router = LLMRouter(settings)
    engine = ExtractionEngine(settings, router)
    spec = make_spec()
    html = read_fixture("catalog.html")
    schema = learn_schema([(f"{DEMO_BASE}/catalog.html", html)], spec)
    page = _page(f"{DEMO_BASE}/catalog.html", html)

    outcome = await engine.extract(page, spec, css_schema=schema, allow_llm=False)
    assert outcome.strategy == "css"
    assert len(outcome.items) == 3

    records = engine.to_records(outcome, page, spec)
    assert len(records) == 3
    assert len({record.record_key for record in records}) == 3
    await router.aclose()


@pytest.mark.asyncio
async def test_engine_without_llm_and_without_data_returns_empty(tmp_path) -> None:
    settings = make_settings(tmp_path)
    router = LLMRouter(settings)
    engine = ExtractionEngine(settings, router)
    page = _page("https://empty.example/", "<html><body><p>n/a</p></body></html>")
    outcome = await engine.extract(page, make_spec(), allow_llm=True)
    assert outcome.items == []
    assert outcome.llm_calls == 0
    await router.aclose()


def test_links_are_absolute_and_filtered() -> None:
    html = read_fixture("catalog.html")
    links = extract_links(html, f"{DEMO_BASE}/catalog.html")
    assert f"{DEMO_BASE}/clinic-alpha.html" in links
    assert all(link.startswith("http") for link in links)
    assert not any("mailto" in link for link in links)
