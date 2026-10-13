"""curl_cffi browser-impersonating adapter for HTML providers."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from pathlib import Path
from urllib.parse import urljoin, urlsplit

from curl_cffi import CurlMime
from curl_cffi.curl import CURL_WRITEFUNC_ERROR
from curl_cffi.requests import AsyncSession

from ..core.http import JsonResponse
from ..core.models import ImageAsset
from ..core.provider import ProviderError
from ..core.web import TextResponse

_MAX_UPLOAD_BYTES = 5 * 1024 * 1024


def _read_image(path: Path) -> bytes:
    with path.open("rb") as image:
        return image.read(_MAX_UPLOAD_BYTES + 1)


def _upload_image_url_allowed(url: str) -> bool:
    try:
        parsed = urlsplit(url)
        return bool(parsed.scheme in {"http", "https"} and parsed.hostname
                    and parsed.username is None and parsed.password is None)
    except ValueError:
        return False


def _image_format(content: bytes) -> tuple[str, str]:
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png", "image.png"
    if content.startswith(b"\xff\xd8\xff"):
        return "image/jpeg", "image.jpg"
    if content.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif", "image.gif"
    if content.startswith(b"RIFF") and content[8:12] == b"WEBP":
        return "image/webp", "image.webp"
    if content.startswith(b"BM"):
        return "image/bmp", "image.bmp"
    raise ProviderError("Soutubot image format is unsupported")


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

    async def upload_image_json(
        self,
        url: str,
        *,
        image: ImageAsset,
        data: Mapping[str, str],
        timeout_seconds: float,
    ) -> JsonResponse:
        """Download/read at most 5 MiB, then upload within one total deadline."""
        return await asyncio.wait_for(
            self._upload_image_json(url, image, data, timeout_seconds),
            timeout=timeout_seconds,
        )

    async def _upload_image_json(
        self, url: str, image: ImageAsset,
        data: Mapping[str, str], timeout_seconds: float,
    ) -> JsonResponse:
        if url != "https://soutubot.moe/api/search":
            raise ValueError("Unsupported image upload endpoint")
        if image.path is not None:
            try:
                content = await asyncio.to_thread(_read_image, image.path)
            except OSError as exc:
                raise ProviderError("Soutubot image could not be read") from exc
        else:
            image_url = image.url or ""
            content_buffer = bytearray()
            oversized = False

            def receive(chunk: bytes) -> int:
                nonlocal oversized
                if len(content_buffer) + len(chunk) > _MAX_UPLOAD_BYTES:
                    oversized = True
                    return CURL_WRITEFUNC_ERROR
                content_buffer.extend(chunk)
                return len(chunk)

            for redirect in range(4):
                if not _upload_image_url_allowed(image_url):
                    raise ProviderError("Soutubot image URL is invalid")
                session = await self._get_session(url=image_url, cookies={})
                content_buffer.clear()
                try:
                    downloaded = await session.get(
                        image_url, timeout=timeout_seconds, allow_redirects=False,
                        content_callback=receive,
                    )
                except Exception as exc:
                    if oversized:
                        raise ProviderError("Soutubot image exceeds 5 MiB") from exc
                    raise
                if oversized:
                    raise ProviderError("Soutubot image exceeds 5 MiB")
                if downloaded.status_code not in {301, 302, 303, 307, 308}:
                    break
                location = downloaded.headers.get("Location", "")
                if not location or redirect == 3:
                    raise ProviderError("Soutubot image download failed")
                image_url = urljoin(image_url, location)
            if downloaded.status_code != 200:
                raise ProviderError("Soutubot image download failed")
            content = bytes(content_buffer)
        if len(content) > _MAX_UPLOAD_BYTES:
            raise ProviderError("Soutubot image exceeds 5 MiB")
        content_type, filename = _image_format(content)
        session = await self._get_session(url=url, cookies={})
        multipart = CurlMime()
        try:
            multipart.addpart(
                name="file", content_type=content_type,
                filename=filename, data=content,
            )
            response = await session.post(
                url, data=dict(data), multipart=multipart,
                timeout=timeout_seconds, allow_redirects=False,
            )
            if response.status_code != 200:
                return JsonResponse(status=response.status_code, data=None)
            try:
                result = response.json()
            except ValueError as exc:
                raise ProviderError("Soutubot returned invalid data") from exc
            return JsonResponse(status=response.status_code, data=result)
        finally:
            multipart.close()

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
