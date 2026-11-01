"""FastAPI application factory: local API + built SPA, guarded by a bearer token."""

from __future__ import annotations

import html
import json
import logging
import secrets
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import __version__
from .api import data as data_routes
from .api import tasks as task_routes
from .config import PROJECT_ROOT, Settings, get_settings
from .runtime import RunConflict, Studio
from .utils import UnsafeUrlError

logger = logging.getLogger(__name__)

PUBLIC_API_PATHS = {"/api/health", "/api/docs", "/api/openapi.json", "/api/redoc"}
FRONTEND_DIST = PROJECT_ROOT / "frontend" / "dist"

PLACEHOLDER_PAGE = """<!doctype html>
<html lang="uk"><head><meta charset="utf-8"><title>DaaS Intelligence Studio</title>
<style>
 body{{font-family:ui-sans-serif,system-ui,-apple-system,sans-serif;background:#0b0f14;color:#e8eef7;
      display:flex;min-height:100vh;align-items:center;justify-content:center;margin:0}}
 .card{{max-width:760px;padding:40px;border:1px solid #223041;border-radius:18px;background:#111823}}
 h1{{font-size:26px;margin:0 0 8px}} p{{color:#9fb0c3;line-height:1.6}}
 code{{background:#0b1119;border:1px solid #223041;padding:2px 6px;border-radius:6px;color:#8ee6c1}}
 a{{color:#7cc4ff}} li{{margin:8px 0;color:#c3d1e0}}
</style></head><body><div class="card">
<h1>DaaS Intelligence Studio</h1>
<p>API працює, але веб-інтерфейс ще не зібраний.</p>
<ol>
<li>Зберіть фронтенд: <code>cd frontend && npm install && npm run build</code></li>
<li>Або запустіть dev-сервер: <code>cd frontend && npm run dev</code> (http://localhost:5173)</li>
<li>API-документація: <a href="/api/docs">/api/docs</a></li>
</ol>
<p>Токен доступу лежить у <code>storage/runtime.json</code>.</p>
</div></body></html>"""


def create_app(settings: Settings | None = None, *, studio_factory=None) -> FastAPI:
    settings = settings or get_settings()
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        studio = studio_factory(settings) if studio_factory else Studio(settings)
        interrupted = studio.interrupt_stale_runs()
        if interrupted:
            logger.warning("marked %s interrupted runs as failed", interrupted)
        app.state.studio = studio
        await studio.router.probe(timeout=1.5)
        logger.info(
            "studio ready | storage=%s | llm=%s | fetch=%s | auth=%s",
            studio.paths.root,
            "ready" if studio.router.available else "off",
            studio.fetcher.name,
            "on" if not settings.disable_auth else "OFF",
        )
        try:
            yield
        finally:
            await studio.aclose()

    app = FastAPI(
        title="DaaS Intelligence Studio",
        version=__version__,
        description="Local-first AI data studio: prompt → calibrated scraper → enriched dataset.",
        lifespan=lifespan,
        docs_url="/api/docs",
        redoc_url=None,
        openapi_url="/api/openapi.json",
    )
    app.state.settings = settings
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_credentials=False,
        allow_methods=["GET", "POST", "PATCH", "PUT", "DELETE"],
        allow_headers=["Authorization", "Content-Type", "X-Daas-Token"],
    )
    _install_guard(app, settings)
    app.include_router(task_routes.router)
    app.include_router(data_routes.router)

    @app.get("/api/health", tags=["system"])
    def health(request: Request) -> dict[str, Any]:
        studio: Studio = request.app.state.studio
        return {
            "status": "ok" if studio.analytics.ping() else "degraded",
            "version": __version__,
            "llm_ready": studio.router.available,
            "offline": settings.offline,
        }

    @app.exception_handler(RunConflict)
    async def _conflict(_request: Request, exc: RunConflict) -> JSONResponse:
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    @app.exception_handler(KeyError)
    async def _missing(_request: Request, exc: KeyError) -> JSONResponse:
        return JSONResponse(status_code=404, content={"detail": str(exc).strip("'\"")})

    @app.exception_handler(UnsafeUrlError)
    async def _unsafe(_request: Request, exc: Exception) -> JSONResponse:
        return JSONResponse(status_code=400, content={"detail": str(exc)})

    _mount_frontend(app)
    return app


def _install_guard(app: FastAPI, settings: Settings) -> None:
    allowed_hosts = _allowed_hosts(settings)

    @app.middleware("http")
    async def guard(request: Request, call_next):
        host = (request.headers.get("host") or "").split(":")[0].strip().lower().strip("[]")
        if host and host not in allowed_hosts:
            return JSONResponse(
                status_code=403,
                content={"detail": f"host not allowed: {host} (DNS-rebinding guard)"},
            )

        path = request.url.path
        if path.startswith("/api/") and path not in PUBLIC_API_PATHS and not settings.disable_auth:
            studio: Studio | None = getattr(request.app.state, "studio", None)
            expected = studio.token if studio is not None else settings.api_token
            provided = _extract_token(request)
            if not expected or not provided or not secrets.compare_digest(provided, expected):
                return JSONResponse(
                    status_code=401,
                    content={"detail": "invalid or missing API token"},
                    headers={"WWW-Authenticate": "Bearer"},
                )

        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        response.headers.setdefault("X-Frame-Options", "DENY")
        if not path.startswith("/api/docs"):
            response.headers.setdefault(
                "Content-Security-Policy",
                "default-src 'self'; img-src 'self' data: blob:; style-src 'self' 'unsafe-inline'; "
                "font-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'; "
                "frame-ancestors 'none'",
            )
        return response


def _extract_token(request: Request) -> str:
    header = request.headers.get("authorization", "")
    if header.lower().startswith("bearer "):
        return header[7:].strip()
    direct = request.headers.get("x-daas-token")
    if direct:
        return direct.strip()
    return request.query_params.get("token", "").strip()


def _allowed_hosts(settings: Settings) -> set[str]:
    hosts = {"127.0.0.1", "localhost", "::1", "testserver", settings.host.lower()}
    for origin in settings.cors_origin_list:
        try:
            from urllib.parse import urlsplit

            host = urlsplit(origin).hostname
            if host:
                hosts.add(host.lower())
        except ValueError:
            continue
    return hosts


def _mount_frontend(app: FastAPI) -> None:
    if (FRONTEND_DIST / "assets").exists():
        app.mount("/assets", StaticFiles(directory=FRONTEND_DIST / "assets"), name="assets")

    @app.get("/{full_path:path}", include_in_schema=False)
    def spa(full_path: str, request: Request) -> HTMLResponse:
        if full_path.startswith("api/"):
            raise HTTPException(status_code=404, detail="Not found")
        index = FRONTEND_DIST / "index.html"
        if not index.exists():
            return HTMLResponse(PLACEHOLDER_PAGE, status_code=200)
        studio: Studio | None = getattr(request.app.state, "studio", None)
        token = studio.token if studio else ""
        boot = {
            "token": token,
            "version": __version__,
            "apiBase": "",
        }
        content = index.read_text(encoding="utf-8")
        content = content.replace("__DAAS_TOKEN__", html.escape(token))
        content = content.replace("__DAAS_BOOT__", json.dumps(boot))
        return HTMLResponse(content)


app = create_app()
