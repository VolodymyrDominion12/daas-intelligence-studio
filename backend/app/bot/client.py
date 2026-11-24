"""HTTP REST client for Studio API and Telegram Bot API client."""

from __future__ import annotations

from typing import Any

import httpx


class StudioApiError(RuntimeError):
    """Raised when the Studio REST API returns an error or is unreachable."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class StudioApiClient:
    """Thin async REST client talking only to Studio HTTP API with Bearer auth."""

    def __init__(
        self,
        base_url: str,
        *,
        token: str = "",
        timeout_s: float = 10.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        headers: dict[str, str] = {"Accept": "application/json"}
        clean_token = (token or "").strip()
        if clean_token:
            headers["Authorization"] = f"Bearer {clean_token}"
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers=headers,
            timeout=timeout_s,
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _request(
        self, method: str, path: str, *, json_body: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        try:
            response = await self._client.request(method, path, json=json_body)
        except httpx.HTTPError as exc:
            raise StudioApiError(
                "Сервіс студії тимчасово недоступний. Спробуйте ще раз трохи згодом."
            ) from exc

        if response.status_code in {401, 403}:
            raise StudioApiError(
                "Помилка авторизації бота в REST API студії. Зверніться до адміністратора.",
                status_code=response.status_code,
            )
        if response.status_code == 404:
            raise StudioApiError("Задачу або підписку не знайдено у студії.", status_code=404)
        if response.status_code >= 400:
            detail = ""
            try:
                err_data = response.json()
                if isinstance(err_data, dict):
                    detail = str(err_data.get("detail") or "").strip()
            except ValueError:
                detail = ""
            message = detail or f"Помилка сервісу студії (HTTP {response.status_code})."
            raise StudioApiError(message, status_code=response.status_code)

        try:
            data = response.json()
        except ValueError as exc:
            raise StudioApiError("Студія повернула некоректну відповідь JSON.") from exc
        if not isinstance(data, dict):
            raise StudioApiError("Студія повернула неочікуваний формат відповіді.")
        return data

    async def list_tasks(self) -> list[dict[str, Any]]:
        data = await self._request("GET", "/api/tasks")
        items = data.get("items")
        return list(items) if isinstance(items, list) else []

    async def list_subscriptions(self, task_id: str) -> list[dict[str, Any]]:
        data = await self._request("GET", f"/api/tasks/{task_id}/subscriptions")
        items = data.get("items")
        return list(items) if isinstance(items, list) else []

    async def create_subscription(
        self,
        task_id: str,
        *,
        name: str,
        channel: str,
        target: str,
        filters: dict[str, Any],
        enabled: bool = True,
    ) -> dict[str, Any]:
        data = await self._request(
            "POST",
            f"/api/tasks/{task_id}/subscriptions",
            json_body={
                "name": name,
                "channel": channel,
                "target": target,
                "filters": filters,
                "enabled": enabled,
            },
        )
        sub = data.get("subscription")
        return dict(sub) if isinstance(sub, dict) else {}

    async def update_subscription(
        self,
        task_id: str,
        sub_id: str,
        *,
        name: str | None = None,
        filters: dict[str, Any] | None = None,
        enabled: bool | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {}
        if name is not None:
            body["name"] = name
        if filters is not None:
            body["filters"] = filters
        if enabled is not None:
            body["enabled"] = enabled
        data = await self._request(
            "PATCH",
            f"/api/tasks/{task_id}/subscriptions/{sub_id}",
            json_body=body,
        )
        sub = data.get("subscription")
        return dict(sub) if isinstance(sub, dict) else {}

    async def flush_subscription(self, task_id: str, sub_id: str) -> dict[str, Any]:
        return await self._request(
            "POST",
            f"/api/tasks/{task_id}/subscriptions/{sub_id}/flush",
        )


class TelegramBotClient:
    """Minimal async Telegram Bot API client with transport injection for offline tests."""

    def __init__(
        self,
        token: str | None,
        *,
        timeout_s: float = 25.0,
        base_url: str = "https://api.telegram.org",
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.token = (token or "").strip()
        self._has_transport = transport is not None
        self._base_url = base_url.rstrip("/")
        self._client = httpx.AsyncClient(timeout=timeout_s, transport=transport)

    async def aclose(self) -> None:
        await self._client.aclose()

    def _method_url(self, method: str) -> str:
        return f"{self._base_url}/bot{self.token}/{method}"

    async def send_message(
        self,
        chat_id: str,
        text: str,
        *,
        reply_markup: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not self.token and not self._has_transport:
            return {"ok": False, "skipped": "no_token"}
        payload: dict[str, Any] = {"chat_id": str(chat_id), "text": text[:4096]}
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        resp = await self._client.post(self._method_url("sendMessage"), json=payload)
        resp.raise_for_status()
        data = resp.json()
        return data if isinstance(data, dict) else {"ok": True}

    async def answer_callback_query(
        self, callback_query_id: str, text: str = ""
    ) -> dict[str, Any]:
        if not self.token and not self._has_transport:
            return {"ok": False, "skipped": "no_token"}
        payload: dict[str, Any] = {"callback_query_id": callback_query_id}
        if text:
            payload["text"] = text[:200]
        resp = await self._client.post(self._method_url("answerCallbackQuery"), json=payload)
        resp.raise_for_status()
        data = resp.json()
        return data if isinstance(data, dict) else {"ok": True}

    async def get_updates(
        self, *, offset: int | None = None, timeout: int = 25
    ) -> list[dict[str, Any]]:
        if not self.token and not self._has_transport:
            return []
        params: dict[str, Any] = {"timeout": max(0, int(timeout))}
        if offset is not None:
            params["offset"] = int(offset)
        resp = await self._client.get(self._method_url("getUpdates"), params=params)
        resp.raise_for_status()
        data = resp.json()
        if not isinstance(data, dict) or not data.get("ok"):
            return []
        result = data.get("result")
        return list(result) if isinstance(result, list) else []
