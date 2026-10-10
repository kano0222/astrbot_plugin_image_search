"""Bounded, public-network-only thumbnail loading; no persistent image files."""
from __future__ import annotations

import asyncio
import ipaddress
import socket
from urllib.parse import urljoin, urlsplit

import aiohttp

_MAX_BYTES = 3 * 1024 * 1024


def public_address(value: str) -> bool:
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
        address = address.ipv4_mapped
    return address.is_global and not address.is_multicast


def preview_url_allowed(url: str) -> bool:
    try:
        parsed = urlsplit(url)
        if (
            parsed.scheme not in {"http", "https"} or not parsed.hostname
            or parsed.username is not None or parsed.password is not None
            or parsed.port not in {None, 80, 443}
            or "%" in parsed.hostname
        ):
            return False
        host = parsed.hostname.rstrip(".").lower()
        if host == "localhost" or host.endswith((".localhost", ".local")):
            return False
        try:
            ipaddress.ip_address(host)
        except ValueError:
            return True  # Hostnames are checked by the resolver before every request.
        return public_address(host)
    except (AttributeError, ValueError):
        return False


def raster_image(data: bytes) -> bool:
    return (
        data.startswith(b"\x89PNG\r\n\x1a\n")
        or data.startswith(b"\xff\xd8\xff")
        or data.startswith((b"GIF87a", b"GIF89a"))
        or (data.startswith(b"RIFF") and data[8:12] == b"WEBP")
    )


class PublicResolver(aiohttp.abc.AbstractResolver):
    def __init__(self) -> None:
        self._resolver = aiohttp.resolver.DefaultResolver()

    async def resolve(self, host, port=0, family=socket.AF_INET):
        addresses = await self._resolver.resolve(host, port, family)
        if not addresses or any(not public_address(item["host"]) for item in addresses):
            raise OSError("thumbnail host is not public")
        return addresses

    async def close(self) -> None:
        await self._resolver.close()


class PreviewImageLoader:
    def __init__(self, *, proxy_url: str = "", user_agent: str = "") -> None:
        self._proxy = proxy_url or None
        self._headers = {"User-Agent": user_agent} if user_agent else {}
        self._session: aiohttp.ClientSession | None = None
        self._resolver: PublicResolver | None = None

    async def load(self, url: str) -> bytes | None:
        try:
            return await asyncio.wait_for(self._load(url), timeout=5)
        except (aiohttp.ClientError, OSError, ValueError, asyncio.TimeoutError):
            return None

    def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._resolver = PublicResolver()
            self._session = aiohttp.ClientSession(
                connector=aiohttp.TCPConnector(resolver=self._resolver),
                headers=self._headers,
                auto_decompress=False,
            )
        return self._session

    async def _load(self, url: str) -> bytes | None:
        session = self._get_session()
        for _ in range(4):
            if not preview_url_allowed(url):
                return None
            parsed = urlsplit(url)
            # Validate even with a configured proxy, which resolves targets itself.
            assert self._resolver is not None
            await self._resolver.resolve(
                parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80),
                socket.AF_UNSPEC,
            )
            async with session.get(
                url, proxy=self._proxy, allow_redirects=False,
                timeout=aiohttp.ClientTimeout(total=5),
            ) as response:
                if response.status in {301, 302, 303, 307, 308}:
                    location = response.headers.get("Location")
                    if not location:
                        return None
                    url = urljoin(url, location)
                    continue
                if response.status != 200:
                    return None
                if response.content_length and response.content_length > _MAX_BYTES:
                    return None
                data = bytearray()
                async for chunk in response.content.iter_chunked(65536):
                    data.extend(chunk)
                    if len(data) > _MAX_BYTES:
                        return None
                result = bytes(data)
                return result if raster_image(result) else None
        return None

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
        if self._resolver is not None:
            await self._resolver.close()
