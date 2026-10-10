"""curl_cffi browser-impersonating adapter for HTML providers."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from urllib.parse import urlsplit

from curl_cffi.requests import AsyncSession

from ..core.web import TextResponse


class CurlCffiWebClient:
    """Shared curl_cffi session with a current Chrome fingerprint."""

    def __init__(self, *, proxy_url: str = "") -> None:
        self._proxy_url = proxy_url.strip()
        self._session: AsyncSession | None = None
        self._lock = asyncio.Lock()
        self._cookie_seeds: dict[tuple[str, str], str] = {}

    async def get_text(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        cookies: Mapping[str, str],
        timeout_seconds: float,
    ) -> TextResponse:
        session = await self._get_session(url=url, cookies=cookies)
        response = await session.get(
            url,
            headers=dict(headers),
            timeout=timeout_seconds,
            allow_redirects=True,
        )
        return TextResponse(
            status=response.status_code,
            url=str(response.url),
            text=response.text,
        )

    async def post_form(
        self,
        url: str,
        *,
        data: Mapping[str, str],
        headers: Mapping[str, str],
        cookies: Mapping[str, str],
        timeout_seconds: float,
    ) -> TextResponse:
        session = await self._get_session(url=url, cookies=cookies)
        response = await session.post(
            url,
            data=dict(data),
            headers=dict(headers),
            timeout=timeout_seconds,
            allow_redirects=True,
        )
        return TextResponse(
            status=response.status_code,
            url=str(response.url),
            text=response.text,
        )

    async def close(self) -> None:
        async with self._lock:
            if self._session is not None:
                await self._session.close()
                self._session = None
            self._cookie_seeds.clear()

    async def _get_session(
        self, *, url: str, cookies: Mapping[str, str]
    ) -> AsyncSession:
        domain = urlsplit(url).hostname
        if domain is None:
            raise ValueError("Cookie session requires a URL with a hostname")
        async with self._lock:
            if self._session is None:
                proxies = None
                if self._proxy_url:
                    proxies = {
                        "http": self._proxy_url,
                        "https": self._proxy_url,
                    }
                self._session = AsyncSession(
                    impersonate="chrome",
                    proxies=proxies,
                )
            # Seed configured values once; retain subsequent Set-Cookie updates.
            # Reseed only when a caller actually changes the configured value.
            for name, value in cookies.items():
                key = (domain, name)
                if self._cookie_seeds.get(key) != value:
                    self._session.cookies.set(name, value, domain=domain, path="/")
                    self._cookie_seeds[key] = value
            return self._session
