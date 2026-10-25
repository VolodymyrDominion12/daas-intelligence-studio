"""Utility hardening tests: SSRF, PII masking, record keys, coercion."""

from __future__ import annotations

import pytest

from app.enrichment.dedupe import find_duplicates, identity_quality
from app.utils import (
    UnsafeUrlError,
    assert_public_url,
    canonical_url,
    coerce_value,
    is_plausible_content_url,
    mask_email,
    mask_phone,
    normalize_company_name,
    normalize_domain,
    parse_bool,
    parse_number,
    record_key_for,
    redact_pii,
    slugify,
)


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("12 500 ₴", 12500.0),
        ("1 234,50 грн", 1234.5),
        ("1,234.56", 1234.56),
        ("від 3500", 3500.0),
        ("—", None),
        (None, None),
        ("$49.99", 49.99),
    ],
)
def test_parse_number(raw, expected) -> None:
    assert parse_number(raw) == expected


@pytest.mark.parametrize(
    "raw,expected",
    [("так", True), ("ні", False), ("In stock", True), ("sold out", False), ("?", None)],
)
def test_parse_bool(raw, expected) -> None:
    assert parse_bool(raw) is expected


def test_coerce_value_respects_dtype() -> None:
    assert coerce_value("+380 44 111 22 33", "phone") == "+380441112233"
    assert coerce_value("info@clinic.example", "email") == "info@clinic.example"
    assert coerce_value("https://x.example/a", "url") == "https://x.example/a"
    assert coerce_value("not a url", "url") is None
    assert coerce_value("4,8", "number") == 4.8
    assert coerce_value("128", "integer") == 128
    assert coerce_value("a, b; c", "array") == ["a", "b", "c"]
    assert coerce_value("", "string") is None


def test_record_keys_are_stable_and_domain_first() -> None:
    key = record_key_for({"name": "ТОВ «Альфа-Буд»", "website": "https://www.alpha-bud.com.ua/x"})
    assert key == "d:alpha-bud.com.ua"
    anonymous = record_key_for({"name": "Альфа Буд", "city": "Київ"})
    assert anonymous.startswith("n:") and len(anonymous) > 5
    assert anonymous == record_key_for({"name": "Альфа-Буд", "city": "Київ"})
    # Loopback hosts are not real company domains -> name-based identity.
    assert record_key_for(
        {"name": "Альфа Стоматологія", "city": "Київ", "website": "http://127.0.0.1:8765/site.html"}
    ).startswith("n:")


def test_transliteration_and_normalization() -> None:
    assert slugify("ТОВ Альфа-Буд") == "tov-alfa-bud"
    assert normalize_domain("https://WWW.Example.com/a?b=1") == "example.com"
    assert normalize_company_name('ТОВ "Альфа-Буд", LLC') == "альфа буд"


def test_pii_masking() -> None:
    assert mask_email("boss@alpha.example") == "b***@alpha.example"
    assert mask_phone("+380441112233") == "+380***33"
    text = redact_pii("Дзвоніть +380 44 111 22 33 або пишіть boss@alpha.example")
    assert "+380" not in text
    assert "boss@" not in text


@pytest.mark.parametrize(
    "url",
    ["http://127.0.0.1:8000/x", "http://localhost/", "http://10.0.0.5/", "http://169.254.169.254/"],
)
def test_ssrf_guard_blocks_private_targets(url) -> None:
    with pytest.raises(UnsafeUrlError):
        assert_public_url(url)


def test_ssrf_guard_blocks_non_http_schemes() -> None:
    with pytest.raises(UnsafeUrlError):
        assert_public_url("file:///etc/passwd")
    with pytest.raises(UnsafeUrlError):
        assert_public_url("ftp://example.com/file")


def test_ssrf_guard_allows_public_urls() -> None:
    assert assert_public_url("https://example.com/path") == "https://example.com/path"
    assert assert_public_url("http://127.0.0.1:8000/x", allow_private=True)


def test_url_helpers() -> None:
    assert canonical_url("https://www.Example.com/a/?q=1") == "https://example.com/a"
    assert canonical_url("http://127.0.0.1:8765/catalog/") == "http://127.0.0.1:8765/catalog"
    assert canonical_url("https://example.com:8443") == "https://example.com:8443/"
    assert is_plausible_content_url("https://example.com/clinics")
    assert not is_plausible_content_url("https://example.com/privacy")
    assert not is_plausible_content_url("https://example.com/logo.png")


def test_duplicate_candidates() -> None:
    rows = [
        {"record_key": "n:alpha-1", "payload": {"name": "Альфа Стоматологія", "city": "Київ"}},
        {"record_key": "n:alpha-2", "payload": {"name": "Альфа Стоматологія", "city": "Київ"}},
        {"record_key": "d:beta.example", "payload": {"name": "Бета Дентал", "website": "https://beta.example"}},
        {"record_key": "d:gamma.example", "payload": {"name": "Гама Смайл", "website": "https://gamma.example"}},
    ]
    candidates = find_duplicates(rows, threshold=90)
    assert candidates
    assert {candidates[0].left_key, candidates[0].right_key} == {"n:alpha-1", "n:alpha-2"}
    assert identity_quality({"website": "https://x.example"}) == 1.0
    assert identity_quality({}) == 0.2
