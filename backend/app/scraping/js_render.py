"""Deterministic empty-DOM / JS-shell detection and Playwright sidecar HTTP client (P1-07)."""

from __future__ import annotations

import importlib.util
from dataclasses import dataclass
from typing import Any

import httpx

from ..config import Settings
from .page import extract_json_ld, html_to_text, parse_html
from .resilience import detect_ban_marker, redact_proxy_url

_SPA_ROOT_SELECTORS = (
    "#root",
    "#app",
    "#__next",
    "#__nuxt",
    "app-root",
    "[data-reactroot]",
    "[ng-version]",
)

_NOSCRIPT_JS_HINTS = (
    "enable javascript",
    "javascript is required",
    "requires javascript",
    "turn on javascript",
    "without javascript",
    "doesn't work properly without javascript",
    "you need to enable javascript",
    "увімкніть javascript",
    "потрібен javascript",
)

_BUNDLE_MARKERS = (
    "/_next/static/",
    "/_nuxt/",
    "__next_data__",
    "__nuxt__",
    "webpack",
    "vite",
)


def detect_js_shell(html: str, *, min_text_chars: int = 120) -> str | None:
    """Detect whether HTML looks like an empty JS-rendered SPA shell rather than real content."""
    if not html or not html.strip():
        return None
    if extract_json_ld(html):
        return None
    if detect_ban_marker(html, 200) is not None:
        return None

    visible_text = html_to_text(html).strip()
    if len(visible_text) >= min_text_chars:
        return None

    tree = parse_html(html)
    for selector in _SPA_ROOT_SELECTORS:
        node = tree.css_first(selector)
        if node is not None:
            node_text = (node.text(strip=True) or "").strip()
            if len(node_text) < min_text_chars:
                return f"порожній SPA-контейнер ({selector}) без видимого контенту"

    for ns in tree.css("noscript"):
        ns_text = (ns.text(strip=True) or "").lower()
        if any(hint in ns_text for hint in _NOSCRIPT_JS_HINTS):
            return "сторінка вимагає JavaScript (<noscript>)"

    has_external_script = bool(tree.css("script[src]") or tree.css('script[type="module"]'))
    lowered = html.lower()
    has_bundle_hint = any(marker in lowered for marker in _BUNDLE_MARKERS)
    if has_external_script or has_bundle_hint:
        return "мінімальний текст із JS-бандлами"

    return None


@dataclass(slots=True)
class JsRenderStats:
    escalations: int = 0
    rendered_ok: int = 0
    fallbacks: int = 0
    errors: int = 0

    def snapshot(
        self,
        *,
        mode: str,
        active_engine: str,
        sidecar_url: str | None,
        local_available: bool | None = None,
    ) -> dict[str, Any]:
        return {
            "mode": mode,
            "active_engine": active_engine,
            "sidecar_configured": bool(sidecar_url and sidecar_url.strip()),
            "sidecar_url": redact_proxy_url(sidecar_url.strip())
            if sidecar_url and sidecar_url.strip()
            else None,
            "local_playwright": local_playwright_available()
            if local_available is None
            else local_available,
            "escalations": self.escalations,
            "rendered_ok": self.rendered_ok,
            "fallbacks": self.fallbacks,
            "errors": self.errors,
        }


@dataclass(slots=True)
class SidecarResponse:
    url: str
    final_url: str
    status_code: int
    html: str
    headers: dict[str, str]
    elapsed_ms: int = 0


class SidecarRenderError(RuntimeError):
    """Raised when the Playwright sidecar fails or returns an invalid payload."""


class PlaywrightSidecarClient:
    """Async HTTP client talking to an isolated Playwright rendering container."""

    def __init__(
        self,
        base_url: str,
        *,
        wait_until: str = "networkidle",
        wait_ms: int = 700,
        timeout_s: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.strip().rstrip("/")
        self.wait_until = wait_until
        self.wait_ms = wait_ms
        self.timeout_s = timeout_s
        kwargs: dict[str, Any] = {"timeout": httpx.Timeout(timeout_s, connect=5.0)}
        if transport is not None:
            kwargs["transport"] = transport
        self._client = httpx.AsyncClient(**kwargs)

    @property
    def redacted_url(self) -> str:
        return redact_proxy_url(self.base_url)

    async def render(
        self,
        url: str,
        *,
        user_agent: str,
        timeout_s: float | None = None,
        proxy_url: str | None = None,
    ) -> SidecarResponse:
        effective_timeout = timeout_s if timeout_s is not None else self.timeout_s
        payload: dict[str, Any] = {
            "url": url,
            "user_agent": user_agent,
            "timeout_ms": int(max(1.0, effective_timeout) * 1000),
            "wait_until": self.wait_until,
            "wait_ms": self.wait_ms,
        }
        if proxy_url:
            payload["proxy_url"] = proxy_url
        try:
            response = await self._client.post(f"{self.base_url}/render", json=payload)
        except httpx.HTTPError as exc:
            raise SidecarRenderError(f"{type(exc).__name__}: {exc}") from exc

        if response.status_code >= 400:
            detail = f"HTTP {response.status_code}"
            try:
                err_body = response.json()
                if isinstance(err_body, dict) and err_body.get("detail"):
                    detail = f"HTTP {response.status_code}: {err_body['detail']}"
            except ValueError:
                pass
            raise SidecarRenderError(detail)

        try:
            data = response.json()
        except ValueError as exc:
            raise SidecarRenderError("некоректний JSON від Playwright сайдкара") from exc

        if not isinstance(data, dict) or not isinstance(data.get("html"), str):
            raise SidecarRenderError("неповна відповідь Playwright сайдкара (відсутній html)")

        raw_headers = data.get("headers")
        headers = (
            {str(k).lower(): str(v) for k, v in raw_headers.items()}
            if isinstance(raw_headers, dict)
            else {}
        )
        return SidecarResponse(
            url=str(data.get("url") or url),
            final_url=str(data.get("final_url") or url),
            status_code=int(data.get("status_code") or 200),
            html=str(data["html"])[: 6 * 1024 * 1024],
            headers=headers,
            elapsed_ms=int(data.get("elapsed_ms") or 0),
        )

    async def health(self, *, timeout_s: float = 2.0) -> dict[str, Any] | None:
        try:
            response = await self._client.get(f"{self.base_url}/health", timeout=timeout_s)
            if response.status_code >= 400:
                return None
            data = response.json()
            return data if isinstance(data, dict) else {"status": "ok"}
        except (httpx.HTTPError, ValueError):
            return None

    async def aclose(self) -> None:
        await self._client.aclose()


def build_sidecar_client(
    settings: Settings,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> PlaywrightSidecarClient | None:
    raw = (settings.playwright_sidecar_url or "").strip()
    if not raw:
        return None
    timeout_s = max(settings.request_timeout_s + (settings.playwright_wait_ms / 1000.0) + 5.0, 20.0)
    return PlaywrightSidecarClient(
        raw,
        wait_until=settings.playwright_wait_until,
        wait_ms=settings.playwright_wait_ms,
        timeout_s=timeout_s,
        transport=transport,
    )


def local_playwright_available() -> bool:
    try:
        return (
            importlib.util.find_spec("playwright") is not None
            and importlib.util.find_spec("playwright.async_api") is not None
        )
    except ModuleNotFoundError:
        return False
