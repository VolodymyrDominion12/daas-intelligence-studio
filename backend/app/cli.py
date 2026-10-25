"""Command line interface: `daas serve | init | doctor | task | calibrate | collect | ...`."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Annotated

import typer

from . import __version__
from .config import Settings, get_settings
from .db.duckdb_store import AnalyticsStore
from .db.sqlite_store import OperationalStore
from .enrichment.dedupe import find_duplicates
from .runtime import RunConflict, Studio
from .storage.exports import export_dataset

app = typer.Typer(add_completion=False, help="DaaS Intelligence Studio CLI", no_args_is_help=True)
task_app = typer.Typer(help="Керування задачами", no_args_is_help=True)
app.add_typer(task_app, name="task")


def _studio(settings: Settings | None = None) -> Studio:
    return Studio(settings or get_settings())


def _print_run(run) -> None:
    state = run.status
    stats = run.stats
    typer.echo(
        f"run {run.id} [{run.kind}] → {state} | pages={stats.pages_fetched} "
        f"new={stats.records_new} updated={stats.records_updated} "
        f"llm=${run.spent_usd:.3f}"
        + (f" | error={run.error}" if run.error else "")
    )


@app.command()
def init() -> None:
    """Створити локальне сховище та токен доступу."""
    settings = get_settings()
    settings.paths.ensure()
    studio = _studio(settings)
    try:
        asyncio.run(studio.router.probe(timeout=1.0))
        typer.echo(f"✅ storage: {studio.paths.root}")
        typer.echo(f"🔑 token:   {studio.token}")
        typer.echo(f"🧠 LLM:     {'ready' if studio.router.available else 'not configured (offline templates)'}")
        typer.echo(f"🌐 fetch:   {studio.fetcher.name} | robots={settings.respect_robots}")
        typer.echo("▶️  start:   daas serve")
    finally:
        asyncio.run(studio.aclose())


@app.command()
def serve(
    host: Annotated[str, typer.Option(help="Bind address")] = "",
    port: Annotated[int, typer.Option(help="Port")] = 0,
    reload: Annotated[bool, typer.Option("--reload", help="Auto-reload (dev)")] = False,
) -> None:
    """Запустити веб-студію (API + зібраний фронтенд)."""
    import uvicorn

    settings = get_settings()
    uvicorn.run(
        "app.main:app",
        host=host or settings.host,
        port=port or settings.port,
        reload=reload,
        log_level=settings.log_level.lower(),
    )


@app.command()
def demo(
    storage: Annotated[str, typer.Option(help="Каталог сховища для демо")] = "",
    serve: Annotated[bool, typer.Option("--serve", help="Залишити студію запущеною з живим демо-джерелом")] = False,
    port: Annotated[int, typer.Option(help="Порт, якщо --serve")] = 8000,
) -> None:
    """Офлайн-демо: локальний сайт → калібрування → збір → збагачення → XLSX."""
    from .demo import run_demo

    settings = get_settings()
    if storage:
        settings = Settings(
            **{**settings.model_dump(), "storage_dir": storage, "_env_file": None}
        )
    if not serve:
        typer.echo("▶️  генерую демо-сайт і проганяю пайплайн (мережа не потрібна)…")

    def ready(result: dict) -> None:
        typer.echo(f"✅ рядків: {result['rows']} · версія: {result['version']}")
        typer.echo(f"📦 експорт: {result['export_path']}")
        typer.echo(f"🖥  студія: http://127.0.0.1:{port}  (Ctrl+C — зупинити)")
        typer.echo(f"🔑 токен: {result['token']}")
        typer.echo("ℹ️  демо-джерело живе, поки працює процес — кнопки «Зібрати/Збагатити» працюють.")

    run_demo(settings, serve=serve, port=port, on_ready=ready)


@app.command()
def doctor() -> None:
    """Перевірити середовище, сховище та провайдерів."""
    settings = get_settings()
    studio = _studio(settings)
    try:
        asyncio.run(studio.router.probe(timeout=1.0))
        caps = studio.capabilities()
        checks = {
            "storage_writable": _writable(studio.paths.root),
            "sqlite": studio.store.overview() is not None,
            "duckdb": studio.analytics.ping(),
            "raw_cache": studio.cache.stats(),
            "llm_ready": caps["llm_ready"],
            "search": caps["search_provider"],
            "frontend_built": (Path(__file__).resolve().parents[2] / "frontend" / "dist" / "index.html").exists(),
        }
        typer.echo(json.dumps({"version": __version__, "checks": checks, "capabilities": caps},
                              ensure_ascii=False, indent=2, default=str))
        if not caps["llm_ready"]:
            typer.echo("\nℹ️  LLM не налаштований: працюють офлайн-шаблони, JSON-LD і CSS-калібрування.")
    finally:
        asyncio.run(studio.aclose())


@task_app.command("create")
def task_create(
    prompt: Annotated[str, typer.Option(help="Бізнес-запит природною мовою")],
    seed: Annotated[list[str] | None, typer.Option("--seed", help="Стартовий URL (можна кілька)")] = None,
    title: Annotated[str, typer.Option(help="Коротка назва")] = "",
) -> None:
    """Створити задачу: промпт → схема колонок + джерела."""
    async def run() -> None:
        studio = _studio()
        try:
            task = await studio.create_task(prompt, seed or [], title=title or None)
            typer.echo(f"task {task.id} | {task.title} | entity={task.spec.entity}")
            for column in task.spec.columns:
                flag = "AI" if column.is_ai else ("ENR" if column.method == "enrichment" else "CSS")
                typer.echo(f"  [{flag}] {column.key:<20} {column.dtype:<9} {column.title}")
            typer.echo(f"\nnext: daas calibrate {task.id}")
        finally:
            await studio.aclose()

    asyncio.run(run())


@task_app.command("list")
def task_list() -> None:
    """Показати задачі."""
    studio = _studio()
    try:
        for task in studio.store.list_tasks():
            dataset = studio.store.dataset(task.dataset_id or task.id)
            typer.echo(
                f"{task.id}  {task.status:<8} rows={dataset.row_count if dataset else 0:<6} "
                f"v{dataset.version if dataset else 0}  {task.title}"
            )
    finally:
        asyncio.run(studio.aclose())


@task_app.command("show")
def task_show(task_id: str) -> None:
    """Показати задачу та калібрування."""
    studio = _studio()
    try:
        task = studio.store.get_task(task_id)
        if not task:
            raise typer.Exit(f"task not found: {task_id}")
        typer.echo(task.spec.model_dump_json(indent=2))
        report = studio.store.get_calibration(task_id)
        if report:
            typer.echo("\ncalibration:\n" + report)
    finally:
        asyncio.run(studio.aclose())


@app.command()
def calibrate(
    task_id: str,
    wait: Annotated[bool, typer.Option("--wait/--no-wait")] = True,
) -> None:
    """Калібрування: тестова вибірка + навчання CSS-схеми."""
    async def run() -> None:
        studio = _studio()
        try:
            cal_run = await studio.calibrate(task_id)
            _print_run(cal_run)
            if wait:
                finished = await studio.wait(cal_run.id)
                if finished:
                    _print_run(finished)
                    report = studio.store.get_calibration(task_id)
                    if report:
                        payload = json.loads(report)
                        typer.echo(
                            f"strategy={payload.get('strategy')} records={payload.get('records_found')} "
                            f"coverage={payload.get('coverage')}"
                        )
        finally:
            await studio.aclose()

    _guard_cli(run)


@app.command()
def collect(
    task_id: str,
    pages: Annotated[int, typer.Option(help="Ліміт сторінок")] = 0,
    wait: Annotated[bool, typer.Option("--wait/--no-wait")] = True,
) -> None:
    """Запустити масовий збір даних."""
    async def run() -> None:
        studio = _studio()
        try:
            task = studio.store.get_task(task_id)
            if not task:
                raise typer.Exit(f"task not found: {task_id}")
            if pages:
                spec = task.spec.model_copy(deep=True)
                spec.limits.max_pages = pages
                studio.update_task(task_id, spec=spec)
            run_info = await studio.collect(task_id)
            _print_run(run_info)
            if wait:
                finished = await studio.wait(run_info.id)
                if finished:
                    _print_run(finished)
        finally:
            await studio.aclose()

    _guard_cli(run)


@app.command()
def enrich(
    task_id: str,
    column: Annotated[list[str] | None, typer.Option("--column", help="Колонки (за замовчуванням усі)")] = None,
) -> None:
    """Запустити waterfall-збагачення та AI-колонки."""
    async def run() -> None:
        studio = _studio()
        try:
            run_info = await studio.enrich(task_id, column_keys=column)
            _print_run(run_info)
            finished = await studio.wait(run_info.id)
            if finished:
                _print_run(finished)
        finally:
            await studio.aclose()

    _guard_cli(run)


@app.command()
def snapshot(task_id: str) -> None:
    """Створити Parquet-снапшот і порахувати зміни."""
    async def run() -> None:
        studio = _studio()
        try:
            run_info = await studio.snapshot(task_id)
            finished = await studio.wait(run_info.id)
            if finished:
                _print_run(finished)
        finally:
            await studio.aclose()

    _guard_cli(run)


@app.command()
def export(
    task_id: str,
    fmt: Annotated[str, typer.Option("--format", help="csv | xlsx | parquet | json")] = "xlsx",
    mask: Annotated[bool, typer.Option("--mask/--no-mask", help="Маскувати PII")] = True,
    limit: Annotated[int, typer.Option(help="Обмежити кількість рядків")] = 0,
) -> None:
    """Експортувати датасет з манифестом і ліцензією."""
    studio = _studio()
    try:
        task = studio.store.get_task(task_id)
        if not task:
            raise typer.Exit(f"task not found: {task_id}")
        dataset = studio.store.dataset(task.dataset_id or task.id)
        if not dataset:
            raise typer.Exit("dataset not found")
        dataset.freshness = studio.analytics.freshness(dataset.id)
        dataset.quality = studio.analytics.quality(dataset.id, task.spec.columns)
        manifest = export_dataset(
            studio.analytics,
            dataset,
            fmt=fmt,
            exports_dir=studio.paths.exports,
            mask_pii=mask,
            limit=limit or None,
        )
        typer.echo(f"✅ {manifest['file']} ({manifest['rows']} рядків, {manifest['bytes']} B)")
        typer.echo(f"   manifest: {manifest['manifest']}")
        typer.echo(f"   path:     {manifest['path']}")
    finally:
        asyncio.run(studio.aclose())


@app.command()
def duplicates(
    task_id: str,
    threshold: Annotated[float, typer.Option(help="Поріг схожості 0-100")] = 88,
) -> None:
    """Показати кандидатів на дублікати."""
    studio = _studio()
    try:
        task = studio.store.get_task(task_id)
        if not task:
            raise typer.Exit(f"task not found: {task_id}")
        rows = studio.analytics.all_current(task.dataset_id or task.id)
        candidates = find_duplicates(rows, threshold=threshold)
        if not candidates:
            typer.echo("Дублікатів не знайдено")
        for candidate in candidates[:100]:
            typer.echo(
                f"{candidate.score:.2f}  {candidate.left_name}  ⇄  {candidate.right_name}  "
                f"({candidate.left_key} / {candidate.right_key})"
            )
    finally:
        asyncio.run(studio.aclose())


@app.command()
def changes(task_id: str) -> None:
    """Показати останні виявлені зміни."""
    studio = _studio()
    try:
        events = [
            event
            for event in studio.store.list_events(task_id, limit=1000)
            if event.type in {"new_entity", "removed_entity", "field_changed", "price_drop"}
        ]
        for event in events[-50:]:
            typer.echo(f"{event.created_at.isoformat()}  {event.type:<15} {event.message}")
    finally:
        asyncio.run(studio.aclose())


@app.command()
def backup(
    out: Annotated[str, typer.Option(help="Шлях до .zip")] = "",
    include_cache: Annotated[bool, typer.Option("--cache/--no-cache")] = False,
) -> None:
    """Бекап локального сховища (SQLite snapshot + DuckDB + Parquet-маніфести)."""
    from .storage.backup import create_backup

    settings = get_settings()
    paths = settings.paths
    paths.ensure()
    target = Path(out) if out else paths.root / f"backups/daas-backup-{int(time.time())}.zip"

    store = OperationalStore(paths)
    analytics = AnalyticsStore(paths)
    try:
        result = create_backup(
            store, analytics, paths, target=target, include_cache=include_cache
        )
    except Exception as exc:  # noqa: BLE001 - surface a clear hint for the lock case
        typer.echo(f"❌ Бекап не вдався: {type(exc).__name__}: {exc}", err=True)
        typer.echo(
            "⚠️  Якщо студія запущена (`daas serve`), вона тримає DuckDB. "
            "Зробіть бекап через API: POST /api/maintenance/backup",
            err=True,
        )
        raise typer.Exit(code=1) from exc
    finally:
        analytics.close()
        store.close()

    typer.echo(f"✅ backup: {result} ({result.stat().st_size} B)")


def _writable(path: Path) -> bool:
    try:
        probe = path / ".write-probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return True
    except OSError:
        return False


def _guard_cli(fn) -> None:
    try:
        asyncio.run(fn())
    except RunConflict as exc:
        typer.echo(f"⚠️  {exc}", err=True)
        raise typer.Exit(code=2) from exc
    except Exception as exc:  # noqa: BLE001 - CLI surface
        typer.echo(f"❌ {type(exc).__name__}: {exc}", err=True)
        raise typer.Exit(code=1) from exc


if __name__ == "__main__":
    app()
