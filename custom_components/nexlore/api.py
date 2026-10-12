"""Small asynchronous client for the NEXLORE HTTP API."""

from __future__ import annotations

import asyncio
from typing import Any
from urllib.parse import urlsplit

import aiohttp


class NexloreApiError(Exception):
    """A non-success response from NEXLORE."""

    def __init__(self, status: int | None, code: str) -> None:
        """Initialize without retaining response bodies or credentials."""
        super().__init__(code)
        self.status = status
        self.code = code


class NexloreAuthError(NexloreApiError):
    """The configured token is no longer accepted."""


class NexloreTaskChangedError(NexloreApiError):
    """The task locator is stale."""


class NexloreConnectionError(Exception):
    """NEXLORE could not be reached before the request completed."""


class NexloreInvalidResponse(Exception):
    """NEXLORE returned a malformed success response."""


class NexloreApiClient:
    """Call NEXLORE with a Home Assistant-managed HTTP session."""

    def __init__(self, session: aiohttp.ClientSession, base_url: str, token: str) -> None:
        """Initialize the client."""
        self._session = session
        self._api_url = f"{base_url.rstrip('/')}/api/v1"
        self._token = token

    @staticmethod
    def normalize_base_url(base_url: str) -> str:
        """Validate and normalize the user supplied origin."""
        normalized = base_url.strip().rstrip("/")
        parsed = urlsplit(normalized)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.netloc
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("Enter an HTTP or HTTPS base URL without credentials or query parameters")
        return normalized

    async def _request(
        self,
        method: str,
        route: str,
        *,
        params: dict[str, str | int] | None = None,
        body: dict[str, Any] | None = None,
    ) -> Any:
        """Make one request. Requests are never automatically retried."""
        try:
            async with self._session.request(
                method,
                f"{self._api_url}{route}",
                headers={"Authorization": f"Bearer {self._token}"},
                params=params,
                json=body,
                timeout=aiohttp.ClientTimeout(total=20),
            ) as response:
                try:
                    payload = await response.json()
                except (aiohttp.ContentTypeError, ValueError):
                    if 200 <= response.status < 300:
                        raise NexloreInvalidResponse from None
                    payload = {}

                if not 200 <= response.status < 300:
                    detail = payload.get("detail", {}) if isinstance(payload, dict) else {}
                    code = detail.get("code") if isinstance(detail, dict) else None
                    if response.status == 401:
                        raise NexloreAuthError(response.status, str(code or "unauthorized"))
                    if response.status == 409 and code == "task_changed":
                        raise NexloreTaskChangedError(response.status, "task_changed")
                    raise NexloreApiError(response.status, str(code or "http_error"))
                return payload
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            raise NexloreConnectionError from err

    async def async_get_profile_and_spaces(self) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        """Validate the token and return spaces visible to it."""
        profile = await self._request("GET", "/me")
        spaces = await self._request("GET", "/spaces")
        if not isinstance(profile, dict) or not isinstance(spaces, list):
            raise NexloreInvalidResponse
        if any(not isinstance(space, dict) for space in spaces):
            raise NexloreInvalidResponse
        return profile, spaces

    async def async_get_tasks(self, space: str) -> list[dict[str, Any]]:
        """Fetch every task, including completed and cancelled tasks."""
        items: list[dict[str, Any]] = []
        offset = 0

        while True:
            payload = await self._request(
                "GET",
                "/tasks",
                params={
                    "space": space,
                    "status": "all",
                    "limit": 500,
                    "offset": offset,
                },
            )
            if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
                raise NexloreInvalidResponse
            page = payload["items"]
            if any(not isinstance(item, dict) for item in page):
                raise NexloreInvalidResponse
            items.extend(page)

            total = payload.get("total")
            if not isinstance(total, int) or isinstance(total, bool):
                if len(page) < 500:
                    break
                offset += len(page)
                continue
            if not page:
                if len(items) < total:
                    raise NexloreInvalidResponse
                break
            if len(items) >= total:
                break
            offset += len(page)

        return items

    async def async_create_inbox_task(self, space: str, summary: str) -> dict[str, Any]:
        """Capture one task in the configured space's inbox."""
        payload = await self._request(
            "POST",
            "/inbox",
            body={"space": space, "text": f"[ ] {summary}"},
        )
        if not isinstance(payload, dict):
            raise NexloreInvalidResponse
        return payload

    async def async_set_task_status(
        self,
        *,
        path: str,
        line: int,
        raw: str,
        file_hash: str,
        done: bool,
        today: str,
    ) -> dict[str, Any]:
        """Complete or reopen a task using its latest locator."""
        payload = await self._request(
            "POST",
            "/tasks/complete",
            body={
                "path": path,
                "line": line,
                "raw": raw,
                "hash": file_hash,
                "done": done,
                "today": today,
            },
        )
        if not isinstance(payload, dict):
            raise NexloreInvalidResponse
        return payload
