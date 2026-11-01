"""SQL-safety regressions for the analytics store: no user input is spliced into SQL text."""

from __future__ import annotations

from pathlib import Path

import duckdb
import pytest
from conftest import make_settings
from test_storage import _record

from app.db.duckdb_store import AnalyticsStore
from app.models import FieldSpec

COLUMNS = [
    FieldSpec(key="name", title="Назва"),
    FieldSpec(key="city", title="Місто"),
    FieldSpec(key="price", title="Ціна", dtype="number"),
]


def _store(tmp_path: Path) -> AnalyticsStore:
    analytics = AnalyticsStore(make_settings(tmp_path).paths)
    analytics.append_records(
        "ds", 1, "r1",
        [
            _record("a", {"name": "O'Brien Dental", "city": "Київ", "price": 10.0}),
            _record("b", {"name": "Beta", "city": "Львів'; DROP TABLE record_versions; --",
                          "price": 5.0}),
        ],
    )
    return analytics


def test_quotes_in_filter_values_are_data(tmp_path: Path) -> None:
    analytics = _store(tmp_path)
    try:
        hit = analytics.query_records("ds", columns=COLUMNS, filters={"name": "O'Brien"})
        assert [row["record_key"] for row in hit["items"]] == ["a"]
        injected = analytics.query_records(
            "ds", columns=COLUMNS, filters={"city": "'; DROP TABLE record_versions; --"}
        )
        assert [row["record_key"] for row in injected["items"]] == ["b"]
        assert analytics.count_records("ds") == 2  # table still there
    finally:
        analytics.close()


def test_unsafe_column_keys_are_rejected(tmp_path: Path) -> None:
    analytics = _store(tmp_path)
    try:
        with pytest.raises(ValueError):
            analytics.query_records("ds", columns=COLUMNS, filters={"name') OR 1=1 --": "x"})
        # an unknown or unsafe sort key falls back to the default order, never reaches SQL
        page = analytics.query_records("ds", columns=COLUMNS, sort="price", direction="desc")
        assert [row["record_key"] for row in page["items"]] == ["a", "b"]
    finally:
        analytics.close()


def test_fill_rates_with_bound_paths(tmp_path: Path) -> None:
    analytics = _store(tmp_path)
    try:
        rates = analytics.field_fill_rates("ds", [*COLUMNS, FieldSpec(key="email", title="E")])
        assert rates == {"name": 100.0, "city": 100.0, "price": 100.0, "email": 0.0}
    finally:
        analytics.close()


def test_files_with_quotes_in_path(tmp_path: Path) -> None:
    """Paths go to DuckDB as arguments, so a quote in a directory name is just a character."""
    analytics = _store(tmp_path)
    folder = tmp_path / "it's a \"dir\""
    try:
        snapshot = analytics.snapshot("ds", 1, folder / "part.parquet")
        rows = analytics.export_rows("ds", COLUMNS)
        parquet = analytics.write_tabular_export(rows, COLUMNS, folder / "x.parquet", "parquet")
        csv = analytics.write_tabular_export(rows, COLUMNS, folder / "x.csv", "csv")
        con = duckdb.connect()
        try:
            assert con.execute("SELECT COUNT(*) FROM read_parquet(?)", [str(snapshot)]).fetchone()[0] == 2
            assert con.execute("SELECT MAX(price) FROM read_parquet(?)", [str(parquet)]).fetchone()[0] == 10.0
        finally:
            con.close()
        assert "O'Brien Dental" in csv.read_text("utf-8")
    finally:
        analytics.close()
