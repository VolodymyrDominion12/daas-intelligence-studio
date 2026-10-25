"""Task, run and event endpoints."""

from __future__ import annotations

import asyncio
import json
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from ..enrichment.registry import enricher_catalog
from ..models import EventInfo
from ..runtime import RunConflict as RunConflictError
from ..utils import iso
from .deps import StudioDep
from .schemas import (
    AddColumnRequest,
    CreateTaskRequest,
    EnrichRequest,
    LimitsRequest,
    RunRequest,
    UpdateTaskRequest,
)

router = APIRouter(prefix="/api", tags=["tasks"])

CHANGE_TYPES = {"new_entity", "removed_entity", "field_changed", "price_drop"}


@router.get("/config")
async def config(studio: StudioDep) -> dict[str, Any]:
    await studio.router.probe_if_stale()
    return {
        "capabilities": studio.capabilities(),
        "overview": studio.overview(),
        "enrichers": enricher_catalog(),
    }


@router.get("/tasks")
def list_tasks(studio: StudioDep) -> dict[str, Any]:
    tasks = []
    for task in studio.store.list_tasks(limit=200):
        dataset_id = task.dataset_id or task.id
        tasks.append(
            task.model_copy(
                update={
                    "record_count": studio.analytics.count_records(dataset_id),
                }
            ).model_dump(mode="json")
        )
    return {"items": tasks}


@router.post("/tasks", status_code=201)
async def create_task(payload: CreateTaskRequest, studio: StudioDep) -> dict[str, Any]:
    try:
        task = await studio.create_task(payload.prompt, payload.seeds, title=payload.title)
    except Exception as exc:  # noqa: BLE001 - planner errors are user-facing
        raise HTTPException(status_code=400, detail=f"Не вдалося побудувати план: {exc}") from exc
    return {"task": task.model_dump(mode="json"), "spec": task.spec.model_dump(mode="json")}


@router.get("/tasks/{task_id}")
def get_task(task_id: str, studio: StudioDep) -> dict[str, Any]:
    task = _task_or_404(studio, task_id)
    dataset_id = task.dataset_id or task.id
    calibration = studio.store.get_calibration(task_id)
    return {
        "task": task.model_dump(mode="json"),
        "calibration": json.loads(calibration) if calibration else None,
        "dataset": _dataset_payload(studio, dataset_id, task.spec),
        "runs": [run.model_dump(mode="json") for run in studio.store.list_runs(task_id, limit=8)],
        "active_run": (
            studio.active_run(task_id).model_dump(mode="json") if studio.active_run(task_id) else None
        ),
    }


@router.patch("/tasks/{task_id}")
def update_task(task_id: str, payload: UpdateTaskRequest, studio: StudioDep) -> dict[str, Any]:
    _task_or_404(studio, task_id)
    spec = payload.spec
    if spec is not None and payload.title:
        spec.title = payload.title
    task = studio.update_task(task_id, spec=spec, title=payload.title)
    assert task is not None
    studio.store.add_event(task_id, "task_updated", "Параметри задачі оновлено")
    return {"task": task.model_dump(mode="json")}


@router.delete("/tasks/{task_id}")
def delete_task(task_id: str, studio: StudioDep) -> dict[str, bool]:
    _task_or_404(studio, task_id)
    studio.analytics.delete_dataset(task_id)
    studio.store.delete_task(task_id)
    return {"deleted": True}


@router.patch("/tasks/{task_id}/limits")
def update_limits(task_id: str, payload: LimitsRequest, studio: StudioDep) -> dict[str, Any]:
    task = _task_or_404(studio, task_id)
    spec = task.spec.model_copy(deep=True)
    spec.limits = payload.limits
    updated = studio.update_task(task_id, spec=spec)
    assert updated is not None
    return {"task": updated.model_dump(mode="json")}


@router.post("/tasks/{task_id}/columns")
def add_column(task_id: str, payload: AddColumnRequest, studio: StudioDep) -> dict[str, Any]:
    task = _task_or_404(studio, task_id)
    spec = task.spec.model_copy(deep=True)
    if any(column.key == payload.field.key for column in spec.columns):
        raise HTTPException(status_code=409, detail=f"Колонка {payload.field.key} вже існує")
    spec.columns.append(payload.field)
    _attach_enricher(spec, payload.field)
    updated = studio.update_task(task_id, spec=spec)
    assert updated is not None
    studio.store.add_event(
        task_id,
        "column_added",
        f"Додано колонку {payload.field.title}",
        data={"key": payload.field.key, "method": payload.field.method},
    )
    return {"task": updated.model_dump(mode="json")}


@router.delete("/tasks/{task_id}/columns/{key}")
def delete_column(task_id: str, key: str, studio: StudioDep) -> dict[str, Any]:
    task = _task_or_404(studio, task_id)
    spec = task.spec.model_copy(deep=True)
    if len(spec.columns) <= 1:
        raise HTTPException(status_code=400, detail="Не можна видалити останню колонку")
    spec.columns = [column for column in spec.columns if column.key != key]
    updated = studio.update_task(task_id, spec=spec)
    assert updated is not None
    return {"task": updated.model_dump(mode="json")}


@router.post("/tasks/{task_id}/calibrate")
async def calibrate(task_id: str, studio: StudioDep, force: bool = False) -> dict[str, Any]:
    _task_or_404(studio, task_id)
    try:
        run = await studio.calibrate(task_id, force=force)
    except (ValueError, KeyError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"run": run.model_dump(mode="json")}


@router.post("/tasks/{task_id}/run")
async def run_collect(task_id: str, payload: RunRequest, studio: StudioDep) -> dict[str, Any]:
    _task_or_404(studio, task_id)
    try:
        run = await studio.collect(
            task_id, force=payload.force, skip_calibration=payload.skip_calibration
        )
    except RunConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except (ValueError, KeyError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"run": run.model_dump(mode="json")}


@router.post("/tasks/{task_id}/enrich")
async def run_enrich(task_id: str, payload: EnrichRequest, studio: StudioDep) -> dict[str, Any]:
    _task_or_404(studio, task_id)
    try:
        run = await studio.enrich(task_id, column_keys=payload.column_keys)
    except RunConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except (ValueError, KeyError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"run": run.model_dump(mode="json")}


@router.post("/tasks/{task_id}/snapshot")
async def run_snapshot(task_id: str, studio: StudioDep) -> dict[str, Any]:
    _task_or_404(studio, task_id)
    try:
        run = await studio.snapshot(task_id)
    except RunConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"run": run.model_dump(mode="json")}


@router.get("/tasks/{task_id}/calibration")
def get_calibration(task_id: str, studio: StudioDep) -> dict[str, Any]:
    _task_or_404(studio, task_id)
    raw = studio.store.get_calibration(task_id)
    return {"calibration": json.loads(raw) if raw else None}


@router.get("/tasks/{task_id}/runs")
def list_runs(task_id: str, studio: StudioDep) -> dict[str, Any]:
    _task_or_404(studio, task_id)
    return {
        "items": [run.model_dump(mode="json") for run in studio.store.list_runs(task_id, limit=50)]
    }


@router.get("/tasks/{task_id}/events")
def list_events(
    task_id: str,
    studio: StudioDep,
    after: int = 0,
    limit: int = Query(default=200, ge=1, le=1000),
) -> dict[str, Any]:
    _task_or_404(studio, task_id)
    events = studio.store.list_events(task_id, after_id=after, limit=limit)
    return {"items": [event.model_dump(mode="json") for event in events]}


@router.get("/tasks/{task_id}/events/stream")
async def stream_events(
    task_id: str, request: Request, studio: StudioDep, after: int = 0
) -> StreamingResponse:
    _task_or_404(studio, task_id)

    async def generator():
        last = after
        idle = 0
        while not await request.is_disconnected():
            events = studio.store.list_events(task_id, after_id=last, limit=100)
            if events:
                idle = 0
                for event in events:
                    last = event.id
                    yield f"id: {event.id}\ndata: {event.model_dump_json()}\n\n"
            else:
                idle += 1
                if idle % 20 == 0:
                    yield f": keep-alive {iso()}\n\n"
            await asyncio.sleep(0.6)

    return StreamingResponse(
        generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@router.get("/tasks/{task_id}/changes")
def list_changes(
    task_id: str, studio: StudioDep, limit: int = Query(default=200, ge=1, le=1000)
) -> dict[str, Any]:
    _task_or_404(studio, task_id)
    events: list[EventInfo] = studio.store.list_events(task_id, limit=1000)
    changes = [event for event in events if event.type in CHANGE_TYPES]
    return {"items": [event.model_dump(mode="json") for event in changes[-limit:]]}


@router.get("/runs/{run_id}")
def get_run(run_id: str, studio: StudioDep) -> dict[str, Any]:
    run = studio.store.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Запуск не знайдено")
    counts = studio.store.job_counts(run_id)
    return {"run": run.model_dump(mode="json"), "jobs": counts}


@router.post("/runs/{run_id}/cancel")
async def cancel_run(run_id: str, studio: StudioDep) -> dict[str, Any]:
    run = await studio.cancel(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Запуск не знайдено")
    return {"run": run.model_dump(mode="json")}


# -- helpers -------------------------------------------------------------


def _task_or_404(studio, task_id: str):
    task = studio.store.get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Задачу не знайдено")
    return task


def _dataset_payload(studio, dataset_id: str, spec) -> dict[str, Any]:
    dataset = studio.store.dataset(dataset_id)
    if dataset is None:
        return {"dataset": None, "quality": {}, "versions": [], "freshness": None}
    dataset.freshness = studio.analytics.freshness(dataset_id)
    columns = [column for column in spec.columns if not column.is_ai]
    return {
        "dataset": dataset.model_dump(mode="json"),
        "quality": studio.analytics.field_fill_rates(dataset_id, columns),
        "versions": studio.store.dataset_versions(dataset_id),
        "freshness": dataset.freshness.isoformat() if dataset.freshness else None,
    }


def _attach_enricher(spec, field) -> None:
    from ..models import EnrichmentSpec

    mapping = {"email": "site_audit", "phone": "site_audit", "tech_stack": "site_audit",
               "has_online_booking": "site_audit", "has_chat": "site_audit", "socials": "site_audit"}
    if field.method != "enrichment" and field.key not in mapping:
        return
    enricher_key = mapping.get(field.key, "site_audit")
    for item in spec.enrichment:
        if item.enricher == enricher_key:
            if field.key not in item.fields:
                item.fields.append(field.key)
            return
    spec.enrichment.append(EnrichmentSpec(enricher=enricher_key, fields=[field.key]))
