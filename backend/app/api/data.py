"""Dataset, record, export and governance endpoints."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse

from ..enrichment.dedupe import find_duplicates
from ..models import DuplicateCandidate, SourcePolicy
from ..storage.backup import create_backup
from ..storage.exports import SUPPORTED_FORMATS, export_dataset
from ..utils import normalize_domain, utcnow
from .deps import StudioDep
from .schemas import ExportRequest, SourcePolicyRequest, policy_view

router = APIRouter(prefix="/api", tags=["data"])


@router.get("/tasks/{task_id}/dataset")
def get_dataset(task_id: str, studio: StudioDep) -> dict[str, Any]:
    task = _task_or_404(studio, task_id)
    dataset_id = task.dataset_id or task.id
    dataset = studio.store.dataset(dataset_id)
    if dataset is None:
        raise HTTPException(status_code=404, detail="Датасет не знайдено")
    dataset.freshness = studio.analytics.freshness(dataset_id)
    columns = [column for column in task.spec.columns if not column.is_ai]
    return {
        "dataset": dataset.model_dump(mode="json"),
        "quality": studio.analytics.field_fill_rates(dataset_id, columns),
        "versions": studio.store.dataset_versions(dataset_id),
        "freshness": dataset.freshness.isoformat() if dataset.freshness else None,
    }


@router.get("/tasks/{task_id}/records")
def list_records(
    task_id: str,
    request: Request,
    studio: StudioDep,
    q: str | None = None,
    sort: str | None = None,
    dir: str = "asc",
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    task = _task_or_404(studio, task_id)
    filters = {
        key[2:]: value
        for key, value in request.query_params.items()
        if key.startswith("f_") and value
    }
    result = studio.analytics.query_records(
        task.dataset_id or task.id,
        columns=task.spec.columns,
        query=q,
        filters=filters,
        sort=sort,
        direction=dir,
        limit=limit,
        offset=offset,
    )
    return {
        "items": result["items"],
        "total": result["total"],
        "limit": limit,
        "offset": offset,
        "columns": [column.model_dump(mode="json") for column in task.spec.columns],
    }


@router.get("/tasks/{task_id}/records/{record_key}")
def get_record(task_id: str, record_key: str, studio: StudioDep) -> dict[str, Any]:
    task = _task_or_404(studio, task_id)
    dataset_id = task.dataset_id or task.id
    record = studio.analytics.get_record(dataset_id, record_key)
    if record is None:
        raise HTTPException(status_code=404, detail="Запис не знайдено")
    return {
        "record": record,
        "history": studio.analytics.record_history(dataset_id, record_key, limit=30),
    }


@router.get("/tasks/{task_id}/duplicates")
def duplicates(
    task_id: str, studio: StudioDep, threshold: float = Query(default=88, ge=50, le=100)
) -> dict[str, Any]:
    task = _task_or_404(studio, task_id)
    rows = studio.analytics.all_current(task.dataset_id or task.id)
    candidates: list[DuplicateCandidate] = find_duplicates(rows, threshold=threshold)
    return {
        "items": [candidate.model_dump(mode="json") for candidate in candidates],
        "scanned": len(rows),
        "note": "Кандидати на злиття. Автоматичний merge/unmerge — наступний етап (P1).",
    }


@router.post("/tasks/{task_id}/exports")
def create_export(task_id: str, payload: ExportRequest, studio: StudioDep) -> dict[str, Any]:
    task = _task_or_404(studio, task_id)
    dataset_id = task.dataset_id or task.id
    dataset = studio.store.dataset(dataset_id)
    if dataset is None:
        raise HTTPException(status_code=404, detail="Датасет не знайдено")
    dataset.freshness = studio.analytics.freshness(dataset_id)
    dataset.quality = studio.analytics.quality(
        dataset_id, [column for column in task.spec.columns if not column.is_ai]
    )
    if payload.format not in SUPPORTED_FORMATS:
        raise HTTPException(status_code=400, detail=f"Непідтримуваний формат: {payload.format}")
    try:
        manifest = export_dataset(
            studio.analytics,
            dataset,
            fmt=payload.format,
            exports_dir=studio.paths.exports,
            mask_pii=payload.mask_pii,
            limit=payload.limit,
            source_note="; ".join(
                f"{policy.domain} ({policy.tos_risk}/{policy.license})"
                for policy in task.spec.source_policy.values()
            ),
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"Експорт не вдався: {exc}") from exc
    studio.store.add_event(
        task_id,
        "export_created",
        f"Експорт {payload.format.upper()}: {manifest['rows']} рядків"
        + (" (PII замасковано)" if payload.mask_pii else ""),
        data={"file": manifest["file"], "rows": manifest["rows"]},
    )
    return {
        "file": manifest["file"],
        "download_url": f"/api/exports/{manifest['file']}",
        "manifest": manifest["manifest_data"],
        "rows": manifest["rows"],
        "bytes": manifest["bytes"],
        "masked_pii": manifest["masked_pii"],
    }


@router.get("/exports/{filename}")
def download_export(filename: str, studio: StudioDep) -> FileResponse:
    exports_dir = studio.paths.exports.resolve()
    if Path(filename).name != filename:
        raise HTTPException(status_code=400, detail="Некоректне ім'я файлу")
    path = (exports_dir / filename).resolve()
    if not path.is_file() or not path.is_relative_to(exports_dir):
        raise HTTPException(status_code=404, detail="Файл не знайдено")
    return FileResponse(path, filename=filename)


@router.get("/exports")
def list_exports(studio: StudioDep) -> dict[str, Any]:
    exports_dir = studio.paths.exports
    items = []
    if exports_dir.exists():
        for path in sorted(exports_dir.glob("*"), key=lambda item: item.stat().st_mtime, reverse=True):
            if path.is_file() and not path.name.endswith(".manifest.json"):
                items.append(
                    {
                        "file": path.name,
                        "bytes": path.stat().st_size,
                        "modified": path.stat().st_mtime,
                        "download_url": f"/api/exports/{path.name}",
                    }
                )
    return {"items": items[:100]}


@router.get("/dashboard")
def dashboard(studio: StudioDep) -> dict[str, Any]:
    tasks = studio.store.list_tasks(limit=20)
    items = []
    for task in tasks:
        dataset = studio.store.dataset(task.dataset_id or task.id)
        items.append(
            {
                "id": task.id,
                "title": task.title,
                "status": task.status,
                "updated_at": task.updated_at.isoformat(),
                "rows": dataset.row_count if dataset else 0,
                "version": dataset.version if dataset else 0,
                "last_run": task.last_run.model_dump(mode="json") if task.last_run else None,
            }
        )
    return {
        "overview": studio.overview(),
        "tasks": items,
        "capabilities": studio.capabilities(),
    }


@router.get("/sources")
def list_sources(studio: StudioDep) -> dict[str, Any]:
    policies = {policy.domain: policy for policy in studio.store.list_source_policies()}
    for task in studio.store.list_tasks(limit=200):
        for seed in task.spec.seeds:
            domain = normalize_domain(seed)
            if domain and domain not in policies:
                policies[domain] = SourcePolicy(domain=domain)
    return {"items": [policy_view(policy).model_dump() for policy in policies.values()]}


@router.put("/sources/{domain}")
def update_source(domain: str, payload: SourcePolicyRequest, studio: StudioDep) -> dict[str, Any]:
    policy = SourcePolicy(
        domain=domain,
        tos_risk=payload.tos_risk,  # type: ignore[arg-type]
        license=payload.license,
        notes=payload.notes,
        allowed_fields=payload.allowed_fields,
        reviewed_at=utcnow() if payload.reviewed else None,
    )
    studio.store.upsert_source_policy(policy)
    return {"item": policy_view(policy).model_dump()}


@router.post("/maintenance/backup")
def create_backup_endpoint(
    studio: StudioDep, include_cache: bool = Query(default=False)
) -> dict[str, Any]:
    try:
        path = create_backup(
            studio.store,
            studio.analytics,
            studio.paths,
            include_cache=include_cache,
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"Бекап не вдався: {exc}") from exc
    studio.store.add_event(
        "system", "backup_created", f"Бекап сховища: {path.name}",
        data={"file": path.name, "bytes": path.stat().st_size},
    )
    return {
        "file": path.name,
        "path": str(path),
        "bytes": path.stat().st_size,
        "include_cache": include_cache,
    }


@router.post("/maintenance/purge-cache")
def purge_cache(
    studio: StudioDep, older_than_days: int = Query(default=30, ge=0, le=3650)
) -> dict[str, Any]:
    removed = studio.cache.purge(older_than_days=older_than_days or None)
    studio.store.add_event(
        "system",
        "cache_purged",
        f"Очищено {removed} сторінок raw-кешу",
        data={"removed": removed, "older_than_days": older_than_days},
    )
    return {"removed": removed, "cache": studio.cache.stats()}


# -- helpers -------------------------------------------------------------


def _task_or_404(studio, task_id: str):
    task = studio.store.get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Задачу не знайдено")
    return task
