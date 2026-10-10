from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from runtime.source_pages import SourcePageClient, _MAX_BYTES

PAGE = "https://danbooru.donmai.us/posts/123"
HTML = b'<div class="image-container" data-normalized-source="https://example.org/art">'


class Response:
    def __init__(self, status=200, data=HTML, headers=None, length=None):
        self.status = status
        self.headers = {"Content-Type": "text/html; charset=utf-8"} if headers is None else headers
        self.content_length = length
        self.data = data
        self.content = self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def iter_chunked(self, size):
        for offset in range(0, len(self.data), size):
            yield self.data[offset:offset + size]


class SourcePageClientTests(unittest.IsolatedAsyncioTestCase):
    def client(self, responses):
        client = SourcePageClient(proxy_url="http://localhost:7890")
        client._resolver = SimpleNamespace(resolve=AsyncMock(), close=AsyncMock())
        client._session = SimpleNamespace(closed=False, get=Mock(side_effect=responses), close=AsyncMock())
        return client

    async def test_public_html_and_no_auto_redirect(self):
        client = self.client([Response()])
        self.assertEqual(await client.load(PAGE), HTML.decode())
        client._resolver.resolve.assert_awaited_once()
        kwargs = client._session.get.call_args.kwargs
        self.assertFalse(kwargs["allow_redirects"])
        self.assertEqual(kwargs["proxy"], "http://localhost:7890")
        await client.close()
        client._session.close.assert_awaited_once()
        client._resolver.close.assert_awaited_once()

    async def test_unsupported_url_is_not_requested(self):
        for url in ("https://evil.test/post", "http://127.0.0.1/post", "https://danbooru.donmai.us:8080/posts/123"):
            client = self.client([])
            self.assertIsNone(await client.load(url))
            client._session.get.assert_not_called()

    async def test_redirect_cannot_leave_post_or_host_or_https(self):
        for location in ("https://127.0.0.1/private", "https://example.org/art",
                         "/posts/456", "/posts/123?token=secret", "http://danbooru.donmai.us/posts/123"):
            client = self.client([Response(status=302, headers={"Location": location})])
            self.assertIsNone(await client.load(PAGE))
            self.assertEqual(client._session.get.call_count, 1)

    async def test_status_content_encoding_type_and_length_fail_closed(self):
        for response in (
            Response(status=403), Response(headers={"Content-Type": "application/json"}),
            Response(headers={"Content-Type": "text/html", "Content-Encoding": "gzip"}),
            Response(length=_MAX_BYTES + 1), Response(data=b"x" * (_MAX_BYTES + 1)),
            Response(status=302, headers={}),
        ):
            client = self.client([response])
            self.assertIsNone(await client.load(PAGE))

    async def test_dns_failure_blocks_request_even_with_proxy(self):
        client = self.client([])
        client._resolver.resolve.side_effect = OSError("not public")
        self.assertIsNone(await client.load(PAGE))
        client._session.get.assert_not_called()

    async def test_session_disables_cookies_and_decompression(self):
        client = SourcePageClient(user_agent="test-agent")
        with patch("runtime.source_pages.aiohttp.ClientSession") as session, patch("runtime.source_pages.aiohttp.TCPConnector"):
            client._get_session()
            kwargs = session.call_args.kwargs
            self.assertEqual(type(kwargs["cookie_jar"]).__name__, "DummyCookieJar")
            self.assertFalse(kwargs["auto_decompress"])
            self.assertEqual(kwargs["headers"]["Accept-Encoding"], "identity")
            await client._resolver.close()
