"""Isolated Playwright Chromium rendering sidecar service (P1-07)."""

from __future__ import annotations

import asyncio
import contextlib
import time
from typing import Literal
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException
from playwright.async_api import async_playwright
from pydantic import BaseModel, Field

MAX_HTML_BYTES = 6 * 1024 * 1024


class RenderRequest(BaseModel):
    url: str
    user_agent: str = "Mozilla/5.0 (compatible; DaaSStudioSidecar/1.0)"
    timeout_ms: int = Field(default=25_000, ge=1000, le=120_000)
    wait_until: Literal["networkidle", "domcontentloaded", "load"] = "networkidle"
    wait_ms: int = Field(default=700, ge=0, le=15_000)
    proxy_url: str | None = None


class SidecarState:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._playwright = None
        self._browser = None

    async def get_browser(self):
        async with self._lock:
            if self._browser is None:
                self._playwright = await async_playwright().start()
                self._browser = await self._playwright.chromium.launch(headless=True)
            return self._browser

    async def close(self) -> None:
        async with self._lock:
            if self._browser is not None:
                await self._browser.close()
                self._browser = None
            if self._playwright is not None:
                await self._playwright.stop()
                self._playwright = None


state = SidecarState()


@contextlib.asynccontextmanager
async def lifespan(_app: FastAPI):
    yield
    await state.close()


app = FastAPI(title="DaaS Playwright Sidecar", version="0.1.0", lifespan=lifespan)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "engine": "playwright", "browser": "chromium"}


@app.post("/render")
async def render(req: RenderRequest) -> dict:
    parsed = urlsplit(req.url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise HTTPException(status_code=400, detail="Потрібен валідний http/https URL")

    started = time.monotonic()
    browser = await state.get_browser()
    context_kwargs: dict = {"user_agent": req.user_agent}
    if req.proxy_url:
        context_kwargs["proxy"] = {"server": req.proxy_url}

    context = await browser.new_context(**context_kwargs)
    page = await context.new_page()
    try:
        response = await page.goto(req.url, timeout=req.timeout_ms, wait_until=req.wait_until)
        if req.wait_ms > 0:
            await page.wait_for_timeout(req.wait_ms)
        html = (await page.content())[:MAX_HTML_BYTES]
        return {
            "url": req.url,
            "final_url": page.url or req.url,
            "status_code": response.status if response else 200,
            "html": html,
            "headers": dict(response.headers) if response else {},
            "elapsed_ms": int((time.monotonic() - started) * 1000),
        }
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"{type(exc).__name__}: {exc}") from exc
    finally:
        await page.close()
        await context.close()
