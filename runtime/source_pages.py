"""Small public HTML requests for source enrichment; no browser or cookies."""
from __future__ import annotations

import asyncio
import socket
from urllib.parse import urljoin, urlsplit

import aiohttp

from .preview_images import PublicResolver, preview_url_allowed

_MAX_BYTES = 1024 * 1024
_HOSTS = {"danbooru.donmai.us", "gelbooru.com", "konachan.com", "yande.re"}


class SourcePageClient:
    def __init__(self, *, proxy_url: str = "", user_agent: str = "") -> None:
        self._proxy = proxy_url or None
        self._headers = {"Accept-Encoding": "identity", "Accept": "text/html"}
        if user_agent:
            self._headers["User-Agent"] = user_agent
        self._session: aiohttp.ClientSession | None = None
        self._resolver: PublicResolver | None = None

    async def load(self, url: str) -> str | None:
        try:
            return await asyncio.wait_for(self._load(url), timeout=3)
        except (aiohttp.ClientError, OSError, ValueError, asyncio.TimeoutError):
            return None

    def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._resolver = PublicResolver()
            self._session = aiohttp.ClientSession(
                connector=aiohttp.TCPConnector(resolver=self._resolver),
                headers=self._headers, auto_decompress=False,
                cookie_jar=aiohttp.DummyCookieJar(),
            )
        return self._session

    async def _load(self, url: str) -> str | None:
        original = urlsplit(url)
        if not preview_url_allowed(url) or original.hostname not in _HOSTS:
            return None
        session = self._get_session()
        for _ in range(3):
            parsed = urlsplit(url)
            # Only transport-level redirects of the same post are accepted.
            if (not preview_url_allowed(url) or parsed.hostname != original.hostname
                    or parsed.path != original.path or parsed.query != original.query
                    or parsed.scheme != "https"):
                return None
            assert self._resolver is not None
            await self._resolver.resolve(parsed.hostname, parsed.port or 443, socket.AF_UNSPEC)
            async with session.get(
                url, proxy=self._proxy, allow_redirects=False,
                timeout=aiohttp.ClientTimeout(total=3),
            ) as response:
                if response.status in {301, 302, 303, 307, 308}:
                    location = response.headers.get("Location")
                    if not location:
                        return None
                    url = urljoin(url, location)
                    continue
                if response.status != 200:
                    return None
                content_type = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
                if content_type not in {"text/html", "application/xhtml+xml"}:
                    return None
                if response.headers.get("Content-Encoding", "identity").lower() != "identity":
                    return None
                if response.content_length and response.content_length > _MAX_BYTES:
                    return None
                data = bytearray()
                async for chunk in response.content.iter_chunked(65536):
                    data.extend(chunk)
                    if len(data) > _MAX_BYTES:
                        return None
                return data.decode("utf-8", errors="replace")
        return None

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
        if self._resolver is not None:
            await self._resolver.close()
