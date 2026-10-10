"""aiohttp adapter for provider JSON requests."""

from __future__ import annotations

from collections.abc import Mapping

import aiohttp

from ..core.http import JsonResponse


class AioHttpJsonClient:
    """Shared aiohttp session implementing the core JSON transport."""

    def __init__(self, *, proxy_url: str = "", user_agent: str = "") -> None:
        self._proxy_url = proxy_url.strip()
        self._headers = {"User-Agent": user_agent.strip()} if user_agent.strip() else {}
        self._session: aiohttp.ClientSession | None = None

    async def get_json(
        self,
        url: str,
        *,
        params: Mapping[str, str],
        timeout_seconds: float,
    ) -> JsonResponse:
        session = self._get_session()
        timeout = aiohttp.ClientTimeout(total=timeout_seconds)
        async with session.get(
            url,
            params=params,
            proxy=self._proxy_url or None,
            timeout=timeout,
        ) as response:
            try:
                data = await response.json(content_type=None)
            except (aiohttp.ContentTypeError, ValueError):
                data = None
            return JsonResponse(status=response.status, data=data)

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()

    def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(headers=self._headers)
        return self._session
