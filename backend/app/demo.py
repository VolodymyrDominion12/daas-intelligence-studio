"""Offline demo: generates a small clinic directory site and runs the whole pipeline."""

from __future__ import annotations

import asyncio
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .config import Settings
from .storage.exports import export_dataset

DEMO_PROMPT = (
    "Збери стоматологічні клініки Києва: назва, місто, адреса, телефон, сайт, рейтинг, "
    "відгуки, email, онлайн-запис і технології сайту"
)

CLINICS = [
    {
        "slug": "alpha",
        "name": "Альфа Стоматологія",
        "city": "Київ",
        "address": "вул. Хрещатик, 1",
        "phone": "+380 44 111 22 33",
        "rating": "4.8",
        "reviews": "128",
        "email": "info@alpha-clinic.example",
        "booking": "Онлайн-запис на консультацію доступний на сайті.",
    },
    {
        "slug": "beta",
        "name": "Бета Дентал",
        "city": "Київ",
        "address": "вул. Антоновича, 12",
        "phone": "+380 44 222 33 44",
        "rating": "4.1",
        "reviews": "64",
        "email": "hello@beta-clinic.example",
        "booking": "Записатися можна за телефоном або через форму.",
    },
    {
        "slug": "gamma",
        "name": "Гама Смайл",
        "city": "Львів",
        "address": "вул. Личаківська, 5",
        "phone": "+380 32 333 44 55",
        "rating": "3.9",
        "reviews": "31",
        "email": "contact@gamma-clinic.example",
        "booking": "Забронювати візит: +380 32 333 44 55.",
    },
    {
        "slug": "delta",
        "name": "Дельта Орто",
        "city": "Київ",
        "address": "вул. Саксаганського, 44",
        "phone": "+380 44 444 55 66",
        "rating": "4.6",
        "reviews": "203",
        "email": "care@delta-clinic.example",
        "booking": "Онлайн-запис через Booksy.",
    },
]

CATALOG_TEMPLATE = """<!doctype html>
<html lang="uk">
<head><meta charset="utf-8"><title>Стоматологічні клініки — каталог</title>
<meta name="description" content="Демо-каталог клінік для DaaS Intelligence Studio"></head>
<body>
<header><nav><a href="/catalog.html">Каталог</a></nav></header>
<main>
  <h1>Стоматологічні клініки</h1>
  <p>Демонстраційний каталог, згенерований локально.</p>
  <section class="results">
{cards}
  </section>
</main>
<footer><p>Demo site · DaaS Intelligence Studio</p></footer>
</body>
</html>
"""

CARD_TEMPLATE = """    <article class="clinic-card">
      <h3 class="clinic-card__name"><a href="{site}">{name}</a></h3>
      <div class="clinic-card__city">{city}</div>
      <div class="clinic-card__address">{address}</div>
      <div class="clinic-card__phone">{phone}</div>
      <div class="clinic-card__rating">{rating}</div>
      <div class="clinic-card__reviews">{reviews} відгуків</div>
      <a class="clinic-card__site" href="{site}">Сайт</a>
    </article>"""

SITE_TEMPLATE = """<!doctype html>
<html lang="uk">
<head>
  <meta charset="utf-8">
  <title>{name} — сучасна стоматологія</title>
  <meta name="generator" content="WordPress 6.5">
  <script src="https://js.stripe.com/v3/"></script>
  <script>window.intercomSettings = {{app_id: "demo"}};</script>
  <script src="https://widget.intercom.io/widget/demo"></script>
</head>
<body>
<header><nav><a href="/catalog.html">Каталог</a> <a href="/contacts.html">Контакти</a></nav></header>
<main>
  <h1>{name}</h1>
  <p>{booking}</p>
  <p>Телефон: <a href="tel:{phone_digits}">{phone}</a></p>
  <p>Пошта: <a href="mailto:{email}">{email}</a></p>
  <p>Ми в соцмережах:
    <a href="https://www.linkedin.com/company/{slug}-dental">LinkedIn</a>,
    <a href="https://www.instagram.com/{slug}dental">Instagram</a>
  </p>
  <p>Адреса: {address}, {city}.</p>
</main>
<footer><p>© {name}</p></footer>
</body>
</html>
"""

ROBOTS = """User-agent: *
Disallow: /private/
Allow: /
"""


class _QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, format: str, *args) -> None:  # noqa: A002 - stdlib signature
        return


def build_site(directory: Path, base_url: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    cards = "\n".join(
        CARD_TEMPLATE.format(
            site=f"{base_url}/site-{clinic['slug']}.html", **clinic
        )
        for clinic in CLINICS
    )
    (directory / "catalog.html").write_text(
        CATALOG_TEMPLATE.format(cards=cards), encoding="utf-8"
    )
    (directory / "index.html").write_text(
        CATALOG_TEMPLATE.format(cards=cards), encoding="utf-8"
    )
    (directory / "robots.txt").write_text(ROBOTS, encoding="utf-8")
    for clinic in CLINICS:
        digits = "".join(ch for ch in clinic["phone"] if ch.isdigit() or ch == "+")
        page = SITE_TEMPLATE.format(phone_digits=digits, **clinic)
        (directory / f"site-{clinic['slug']}.html").write_text(page, encoding="utf-8")
    return directory


def start_server(directory: Path) -> tuple[ThreadingHTTPServer, str]:
    handler = partial(_QuietHandler, directory=str(directory))
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = int(server.server_address[1])
    return server, f"http://127.0.0.1:{port}"


def demo_settings(base: Settings) -> Settings:
    return Settings(
        _env_file=None,
        storage_dir=str(base.paths.root),
        offline=True,
        disable_auth=False,
        respect_robots=True,
        allow_private_network=True,
        llm_provider="disabled",
        llm_local_base_url=None,
        sample_size=4,
        max_pages_per_run=20,
        per_domain_rps=20.0,
        request_timeout_s=10.0,
        log_level="WARNING",
    )


def run_demo(
    base: Settings,
    *,
    quiet: bool = False,
    serve: bool = False,
    host: str | None = None,
    port: int | None = None,
    on_ready=None,
) -> dict:
    """Seed the demo dataset and optionally keep the studio running with a live source.

    The demo site runs in a background thread, so ``serve=True`` lets the user click
    "Зібрати" again and see real crawling against the local source.
    """
    from .runtime import Studio

    site_dir = base.paths.root / "demo_site"
    server, base_url = start_server(site_dir)
    try:
        build_site(site_dir, base_url)
        settings = demo_settings(base)
        studio = Studio(settings)
        try:
            result = asyncio.run(_pipeline(studio, base_url, quiet=quiet))
        finally:
            asyncio.run(studio.aclose())

        if on_ready is not None:
            on_ready(result)
        if serve:
            import uvicorn

            from .main import create_app

            uvicorn.run(
                create_app(settings),
                host=host or settings.host,
                port=port or settings.port,
                log_level="warning",
            )
        return result
    finally:
        server.shutdown()
        server.server_close()


async def _pipeline(studio, base_url: str, *, quiet: bool) -> dict:
    for existing in studio.store.list_tasks(limit=200):
        if existing.title.startswith("Демо:"):
            studio.analytics.delete_dataset(existing.dataset_id or existing.id)
            studio.store.delete_task(existing.id)
    seed = f"{base_url}/catalog.html"
    task = await studio.create_task(DEMO_PROMPT, [seed], title="Демо: клініки Києва")
    log = (lambda message: None) if quiet else print
    log(f"• задача {task.id}: {task.title}")

    calibration = await studio.calibrate(task.id)
    await studio.wait(calibration.id, timeout=120)
    log("• калібрування завершено")

    collect = await studio.collect(task.id)
    await studio.wait(collect.id, timeout=300)
    log("• збір завершено")

    enrich = await studio.enrich(task.id)
    await studio.wait(enrich.id, timeout=300)
    log("• збагачення завершено")

    snapshot = await studio.snapshot(task.id)
    await studio.wait(snapshot.id, timeout=120)
    log("• снапшот записано")

    dataset = studio.store.dataset(task.id)
    assert dataset is not None
    dataset.freshness = studio.analytics.freshness(dataset.id)
    dataset.quality = studio.analytics.quality(dataset.id, task.spec.columns)
    manifest = export_dataset(
        studio.analytics,
        dataset,
        fmt="xlsx",
        exports_dir=studio.paths.exports,
        mask_pii=True,
    )
    rows = studio.analytics.all_current(dataset.id)
    return {
        "task_id": task.id,
        "rows": len(rows),
        "version": dataset.version,
        "export": manifest["file"],
        "export_path": manifest["path"],
        "storage": str(studio.paths.root),
        "token": studio.token,
    }
