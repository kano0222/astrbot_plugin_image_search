from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from runtime.preview_images import (
    PreviewImageLoader, PublicResolver, _MAX_BYTES,
    preview_url_allowed, public_address, raster_image,
)

PNG = b"\x89PNG\r\n\x1a\n" + b"test"


class Response:
    def __init__(self, status=200, data=PNG, headers=None, length=None):
        self.status = status
        self.headers = headers or {}
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


class PreviewPolicyTests(unittest.TestCase):
    def test_nonpublic_and_credential_urls_are_blocked(self):
        for url in (
            "file:///tmp/a", "data:image/png,a", "http://localhost/a",
            "http://host.local/a", "http://127.0.0.1/a", "http://10.0.0.1/a",
            "http://169.254.169.254/a", "http://[::1]/a",
            "http://[::ffff:127.0.0.1]/a", "http://224.0.0.1/a",
            "https://user:pass@example.com/a", "http://example.com:8080/a",
            "https://example.com:bad/a", "https://[bad/a",
        ):
            with self.subTest(url=url):
                self.assertFalse(preview_url_allowed(url))

    def test_public_images_are_allowed(self):
        self.assertTrue(preview_url_allowed("https://images.example.com/a.jpg"))
        self.assertTrue(preview_url_allowed("https://8.8.8.8/a"))
        self.assertFalse(public_address("100.64.0.1"))
        self.assertFalse(public_address("::ffff:192.168.1.1"))

    def test_only_known_raster_signatures_are_accepted(self):
        for data in (PNG, b"\xff\xd8\xffa", b"GIF89a-test", b"RIFF0000WEBPtest"):
            self.assertTrue(raster_image(data))
        for data in (b"<html>403</html>", b"<svg></svg>", b"", b"RIFF0000WAVE"):
            self.assertFalse(raster_image(data))


class PreviewLoaderTests(unittest.IsolatedAsyncioTestCase):
    def loader(self, responses):
        loader = PreviewImageLoader()
        resolver = AsyncMock()
        session = SimpleNamespace(get=Mock(side_effect=responses))
        loader._resolver = resolver
        loader._get_session = Mock(return_value=session)
        return loader, session, resolver

    async def test_downloads_valid_bytes_with_redirects_disabled(self):
        loader, session, resolver = self.loader([Response()])
        self.assertEqual(await loader.load("https://images.test/a"), PNG)
        self.assertFalse(session.get.call_args.kwargs["allow_redirects"])
        resolver.resolve.assert_awaited_once()

    async def test_403_and_html_are_skipped(self):
        for response in (Response(status=403), Response(data=b"<html>challenge</html>")):
            loader, _, _ = self.loader([response])
            self.assertIsNone(await loader.load("https://images.test/a"))

    async def test_redirect_to_private_address_is_not_fetched(self):
        loader, session, _ = self.loader([
            Response(status=302, headers={"Location": "http://127.0.0.1/private"}),
        ])
        self.assertIsNone(await loader.load("https://images.test/a"))
        self.assertEqual(session.get.call_count, 1)

    async def test_public_relative_redirect_is_revalidated(self):
        loader, session, resolver = self.loader([
            Response(status=302, headers={"Location": "/b"}), Response(),
        ])
        self.assertEqual(await loader.load("https://images.test/a"), PNG)
        self.assertEqual(session.get.call_args.args[0], "https://images.test/b")
        self.assertEqual(resolver.resolve.await_count, 2)

    async def test_redirect_loop_is_bounded(self):
        loader, session, _ = self.loader([
            Response(status=302, headers={"Location": "/a"}) for _ in range(4)
        ])
        self.assertIsNone(await loader.load("https://images.test/a"))
        self.assertEqual(session.get.call_count, 4)

    async def test_declared_and_streaming_oversize_are_rejected(self):
        for response in (
            Response(length=_MAX_BYTES + 1),
            Response(data=PNG + b"x" * _MAX_BYTES),
        ):
            loader, _, _ = self.loader([response])
            self.assertIsNone(await loader.load("https://images.test/a"))

    async def test_private_dns_answer_blocks_request_even_with_proxy(self):
        loader, session, resolver = self.loader([])
        loader._proxy = "http://proxy.test:8080"
        resolver.resolve.side_effect = OSError("not public")
        self.assertIsNone(await loader.load("https://images.test/a"))
        session.get.assert_not_called()

    async def test_request_timeout_is_a_skipped_image(self):
        loader, session, _ = self.loader([])
        session.get.side_effect = TimeoutError()
        self.assertIsNone(await loader.load("https://images.test/a"))

    async def test_mixed_public_private_dns_answers_are_rejected(self):
        resolver = PublicResolver()
        original = resolver._resolver
        resolver._resolver = AsyncMock()
        resolver._resolver.resolve.return_value = [
            {"host": "8.8.8.8"}, {"host": "127.0.0.1"},
        ]
        try:
            with self.assertRaises(OSError):
                await resolver.resolve("images.test")
        finally:
            await original.close()

    async def test_session_and_resolver_are_closed(self):
        loader = PreviewImageLoader()
        loader._session = SimpleNamespace(close=AsyncMock())
        loader._resolver = SimpleNamespace(close=AsyncMock())
        await loader.close()
        loader._session.close.assert_awaited_once()
        loader._resolver.close.assert_awaited_once()
