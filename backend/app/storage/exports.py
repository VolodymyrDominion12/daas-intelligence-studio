"""Dataset exports: CSV / XLSX / Parquet / JSON, each with a manifest (docs/02 §5.4).

Rules that make an export a *data product* rather than a dump:

* every file ships with ``<file>.manifest.json`` — schema, row count, quality, freshness,
  sources, license and a sha256 of the file;
* ``personal`` PII columns are **always** masked; ``business_contact`` columns are masked
  when ``mask_pii`` is on (default) — e.g. for demo samples;
* each row keeps provenance columns ``_source_url`` and ``_observed_at``.
"""

from __future__ import annotations

import csv
import hashlib
import json
import uuid
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .. import __version__
from ..models import DatasetInfo, FieldSpec
from ..utils import json_dumps, mask_value, safe_filename, utcnow

if TYPE_CHECKING:  # pragma: no cover
    from ..db.duckdb_store import AnalyticsStore

SUPPORTED_FORMATS: tuple[str, ...] = ("csv", "xlsx", "parquet", "json")

DEFAULT_LICENSE = (
    "Proprietary dataset. Internal use by the licensee only; no resale or redistribution of "
    "personal data. Facts were collected from publicly available, logged-out pages; check the "
    "listed sources' terms before any commercial reuse."
)

PII_DTYPES = {"email", "phone"}
META_COLUMNS = ("_source_url", "_observed_at")


def export_dataset(
    analytics: AnalyticsStore,
    dataset: DatasetInfo,
    *,
    fmt: str,
    exports_dir: Path,
    mask_pii: bool = True,
    limit: int | None = None,
    source_note: str = "",
    license_text: str | None = None,
) -> dict[str, Any]:
    """Write the current state of a dataset to ``exports_dir`` and return its manifest info."""
    fmt = fmt.lower().strip()
    if fmt not in SUPPORTED_FORMATS:
        raise ValueError(f"unsupported export format: {fmt}")

    columns = list(dataset.columns)
    rows = analytics.export_rows(dataset.id, columns, limit=limit)
    masked_columns = columns_to_mask(columns, mask_pii=mask_pii)
    rows = [mask_row(row, columns, masked_columns) for row in rows]

    exports_dir = Path(exports_dir)
    exports_dir.mkdir(parents=True, exist_ok=True)
    stamp = utcnow().strftime("%Y%m%dT%H%M%SZ")
    stem = f"{safe_filename(dataset.name)}-v{dataset.version}-{stamp}-{uuid.uuid4().hex[:6]}"
    path = exports_dir / f"{stem}.{fmt}"

    manifest = build_manifest(
        dataset,
        columns,
        fmt=fmt,
        file=path.name,
        rows=len(rows),
        mask_pii=mask_pii,
        masked_columns=masked_columns,
        source_note=source_note,
        license_text=license_text or DEFAULT_LICENSE,
    )

    tmp = path.with_name(f".{path.name}.tmp")
    try:
        if fmt == "csv":
            _write_csv(rows, columns, tmp)
        elif fmt == "json":
            _write_json(rows, tmp)
        elif fmt == "xlsx":
            _write_xlsx(rows, columns, manifest, tmp)
        else:  # parquet — DuckDB gives typed columns and ZSTD compression
            analytics.write_tabular_export(rows, columns, tmp, "parquet")
        tmp.replace(path)
    finally:
        tmp.unlink(missing_ok=True)

    manifest["bytes"] = path.stat().st_size
    manifest["sha256"] = _sha256(path)
    manifest_path = Path(f"{path}.manifest.json")
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    return {
        "path": str(path),
        "file": path.name,
        "manifest": str(manifest_path),
        "manifest_data": manifest,
        "rows": len(rows),
        "bytes": manifest["bytes"],
        "masked_pii": bool(mask_pii),
        "format": fmt,
    }


# ---------------------------------------------------------------------------
# masking
# ---------------------------------------------------------------------------


def columns_to_mask(columns: list[FieldSpec], *, mask_pii: bool) -> list[str]:
    keys: list[str] = []
    for column in columns:
        pii = column.pii
        if pii == "none" and column.dtype in PII_DTYPES:
            pii = "business_contact"  # an untagged email/phone column is still contact data
        if pii == "personal" or (mask_pii and pii == "business_contact"):
            keys.append(column.key)
    return keys


def mask_row(row: dict[str, Any], columns: list[FieldSpec], masked: list[str]) -> dict[str, Any]:
    if not masked:
        return row
    dtypes = {column.key: column.dtype for column in columns}
    result = dict(row)
    for key in masked:
        value = result.get(key)
        if isinstance(value, list):
            result[key] = [mask_value(item, dtypes.get(key, "string")) for item in value]
        else:
            result[key] = mask_value(value, dtypes.get(key, "string"))
    return result


# ---------------------------------------------------------------------------
# manifest
# ---------------------------------------------------------------------------


def build_manifest(
    dataset: DatasetInfo,
    columns: list[FieldSpec],
    *,
    fmt: str,
    file: str,
    rows: int,
    mask_pii: bool,
    masked_columns: list[str],
    source_note: str,
    license_text: str,
) -> dict[str, Any]:
    return {
        "dataset_id": dataset.id,
        "dataset_name": dataset.name,
        "task_id": dataset.task_id,
        "version": dataset.version,
        "format": fmt,
        "file": file,
        "rows": rows,
        "generated_at": utcnow().isoformat(),
        "generator": f"daas-studio {__version__}",
        "freshness": dataset.freshness.isoformat() if dataset.freshness else None,
        "quality": dataset.quality,
        "columns": [
            {
                "key": column.key,
                "title": column.title,
                "dtype": column.dtype,
                "pii": column.pii,
                "method": column.method,
            }
            for column in columns
        ],
        "provenance_columns": list(META_COLUMNS),
        "masked_pii": bool(mask_pii),
        "masked_columns": masked_columns,
        "sources": source_note,
        "license": license_text,
    }


# ---------------------------------------------------------------------------
# writers
# ---------------------------------------------------------------------------


def _header(columns: list[FieldSpec]) -> list[str]:
    return ["record_key", *[column.key for column in columns], *META_COLUMNS]


def _cell(value: Any) -> Any:
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return json_dumps(value)
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def _write_csv(rows: list[dict[str, Any]], columns: list[FieldSpec], path: Path) -> None:
    header = _header(columns)
    dtypes = {column.key: column.dtype for column in columns}
    # UTF-8 with BOM so Excel opens Cyrillic correctly.
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(header)
        for row in rows:
            writer.writerow(
                [_defuse(_cell(row.get(name)), dtypes.get(name, "string")) for name in header]
            )


def _defuse(value: Any, dtype: str) -> Any:
    """Neutralise spreadsheet formula injection coming from scraped text."""
    if (
        isinstance(value, str)
        and value[:1] in {"=", "+", "-", "@"}
        and dtype not in {"phone", "number", "integer"}
    ):
        return "'" + value
    return value


def _write_json(rows: list[dict[str, Any]], path: Path) -> None:
    path.write_text(
        json.dumps(rows, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )


def _write_xlsx(
    rows: list[dict[str, Any]],
    columns: list[FieldSpec],
    manifest: dict[str, Any],
    path: Path,
) -> None:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    header = _header(columns)
    titles = {column.key: column.title for column in columns}
    dtypes = {column.key: column.dtype for column in columns}

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Дані"
    sheet.append([titles.get(name, name) for name in header])
    sheet.append(header)  # machine-readable keys under the human titles
    for row in rows:
        sheet.append([_xlsx_cell(row.get(name), dtypes.get(name, "string")) for name in header])

    bold = Font(bold=True, color="FFFFFF")
    fill = PatternFill("solid", fgColor="1F3A5F")
    key_font = Font(italic=True, color="666666", size=9)
    for cell in sheet[1]:
        cell.font = bold
        cell.fill = fill
        cell.alignment = Alignment(vertical="center", wrap_text=True)
    for cell in sheet[2]:
        cell.font = key_font
    sheet.freeze_panes = "B3"
    if rows:
        sheet.auto_filter.ref = f"A2:{get_column_letter(len(header))}{len(rows) + 2}"
    for index, name in enumerate(header, start=1):
        sample = [str(titles.get(name, name))] + [
            str(row.get(name) or "") for row in rows[:200]
        ]
        width = min(max(len(item) for item in sample) + 2, 60)
        sheet.column_dimensions[get_column_letter(index)].width = max(width, 10)
        if dtypes.get(name) == "number":
            for (cell,) in sheet.iter_rows(min_row=3, min_col=index, max_col=index):
                cell.number_format = "#,##0.00"

    meta = workbook.create_sheet("Маніфест")
    meta.append(["Поле", "Значення"])
    for key, value in manifest.items():
        if key == "columns":
            continue
        meta.append([key, _cell(value) if not isinstance(value, str) else value])
    for cell in meta[1]:
        cell.font = bold
        cell.fill = fill
    meta.column_dimensions["A"].width = 22
    meta.column_dimensions["B"].width = 100

    spec = workbook.create_sheet("Колонки")
    spec.append(["key", "Назва", "Тип", "PII", "Метод"])
    for column in manifest["columns"]:
        spec.append([column["key"], column["title"], column["dtype"], column["pii"],
                     column["method"]])
    for cell in spec[1]:
        cell.font = bold
        cell.fill = fill
    for letter, width in zip("ABCDE", (22, 32, 12, 18, 14), strict=True):
        spec.column_dimensions[letter].width = width

    workbook.save(path)


def _xlsx_cell(value: Any, dtype: str) -> Any:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if dtype in {"number", "integer"} and isinstance(value, (int, float)):
        return value
    if isinstance(value, (dict, list)):
        return json_dumps(value)
    if isinstance(value, datetime):
        return value.replace(tzinfo=None) if value.tzinfo else value
    return _defuse(str(value), dtype)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()
