from __future__ import annotations

import importlib
import importlib.util
import asyncio
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
import warnings
from unittest.mock import AsyncMock, Mock, patch


_HAS_CURL = importlib.util.find_spec("curl_cffi") is not None
PNG = b"\x89PNG\r\n\x1a\nexample"
ENDPOINT = "https://soutubot.moe/api/search"


@unittest.skipUnless(_HAS_CURL, "curl_cffi is not installed")
class SoutubotTransportTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        root = Path(__file__).resolve().parents[1]
        sys.path.insert(0, str(root.parent))
        try:
            cls.module = importlib.import_module(root.name + ".runtime.curl_client")
        finally:
            sys.path.pop(0)

    def client(self, responses=()):
        module = self.module
        iterator = iter(responses)

        async def download(url, **kwargs):
            status, body, headers = next(iterator)
            callback = kwargs["content_callback"]
            for offset in range(0, len(body), 65536):
                part = body[offset:offset + 65536]
                if callback(part) != len(part):
                    raise OSError("callback aborted")
            return SimpleNamespace(status_code=status, headers=headers)

        session = SimpleNamespace(
            get=AsyncMock(side_effect=download),
            post=AsyncMock(return_value=SimpleNamespace(
                status_code=200, json=lambda: {"status": "ok"},
            )),
        )
        client = module.CurlCffiWebClient()
        client._get_session = AsyncMock(return_value=session)
        return client, session

    async def upload(self, client, *, url="https://images.test/input", path=None):
        image = self.module.ImageAsset(url=url, path=path)
        return await client.upload_image_json(
            ENDPOINT, image=image, data={"top_k": "3"}, timeout_seconds=2,
        )

    async def test_download_redirect_and_multipart_cleanup(self):
        client, session = self.client([
            (302, b"redirect", {"Location": "/final.png"}), (200, PNG, {}),
        ])
        mime = Mock()
        with patch.object(self.module, "CurlMime", return_value=mime):
            result = await self.upload(client)
        self.assertEqual(result.status, 200)
        self.assertEqual(session.get.call_args_list[1].args[0], "https://images.test/final.png")
        mime.addpart.assert_called_once_with(
            name="file", content_type="image/png", filename="image.png", data=PNG,
        )
        mime.close.assert_called_once()
        self.assertFalse(session.post.call_args.kwargs["allow_redirects"])

    async def test_oversized_download_aborts_before_upload(self):
        client, session = self.client([(200, PNG + b"x" * self.module._MAX_UPLOAD_BYTES, {})])
        with self.assertRaisesRegex(self.module.ProviderError, "exceeds 5 MiB"):
            await self.upload(client)
        session.post.assert_not_called()

    async def test_local_file_is_preferred_and_bounded(self):
        client, session = self.client()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "private-name.png"
            path.write_bytes(PNG)
            with patch.object(self.module, "CurlMime") as mime:
                await self.upload(client, path=path)
                self.assertEqual(mime.return_value.addpart.call_args.kwargs["filename"], "image.png")
            session.get.assert_not_called()
            path.write_bytes(b"x" * (self.module._MAX_UPLOAD_BYTES + 1))
            with self.assertRaisesRegex(self.module.ProviderError, "exceeds 5 MiB"):
                await self.upload(client, path=path)

    async def test_html_response_is_not_uploaded(self):
        client, session = self.client([(200, b"<html>error</html>", {})])
        with self.assertRaisesRegex(self.module.ProviderError, "format is unsupported"):
            await self.upload(client)
        session.post.assert_not_called()

    async def test_redirect_limit_and_unsafe_target(self):
        for responses in (
            [(302, b"", {"Location": "/again"})] * 4,
            [(302, b"", {"Location": "file:///private"})],
            [(302, b"", {"Location": "https://user:secret@images.test/file"})],
        ):
            client, session = self.client(responses)
            with self.assertRaises(self.module.ProviderError):
                await self.upload(client)
            session.post.assert_not_called()

    async def test_upload_failure_still_closes_multipart(self):
        client, session = self.client([(200, PNG, {})])
        session.post.side_effect = TimeoutError()
        mime = Mock()
        with patch.object(self.module, "CurlMime", return_value=mime):
            with self.assertRaises(TimeoutError):
                await self.upload(client)
        mime.close.assert_called_once()

    async def test_http_error_does_not_decode_body(self):
        client, session = self.client([(200, PNG, {})])
        session.post.return_value = SimpleNamespace(status_code=403, json=Mock())
        with patch.object(self.module, "CurlMime"):
            result = await self.upload(client)
        self.assertEqual(result.status, 403)
        self.assertIsNone(result.data)
        session.post.return_value.json.assert_not_called()

    async def test_invalid_json_is_expected_error(self):
        client, session = self.client([(200, PNG, {})])
        session.post.return_value.json = Mock(side_effect=ValueError("private"))
        with patch.object(self.module, "CurlMime"):
            with self.assertRaisesRegex(self.module.ProviderError, "returned invalid data"):
                await self.upload(client)

    async def test_real_curl_aborts_oversized_download(self):
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                self.send_response(200)
                self.end_headers()
                try:
                    self.wfile.write(PNG + b"x" * 65536)
                except (BrokenPipeError, ConnectionResetError):
                    pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        client = self.module.CurlCffiWebClient()
        try:
            with patch.object(self.module, "_MAX_UPLOAD_BYTES", 1024), warnings.catch_warnings(record=True) as seen:
                warnings.simplefilter("always")
                with self.assertRaisesRegex(self.module.ProviderError, "exceeds 5 MiB") as caught:
                    await self.upload(client, url=f"http://127.0.0.1:{server.server_port}/image")
            # Real libcurl must fail the write, not accept a short callback return.
            self.assertIsNotNone(caught.exception.__cause__)
            self.assertFalse(any("Wrote bytes" in str(w.message) for w in seen))
        finally:
            await client.close()
            await asyncio.to_thread(server.shutdown)
            server.server_close()
            thread.join()

    async def test_deadline_and_cancellation_close_multipart(self):
        for cancel in (False, True):
            client, session = self.client([(200, PNG, {})])
            started = asyncio.Event()

            async def stalled(*args, **kwargs):
                started.set()
                await asyncio.Event().wait()

            session.post.side_effect = stalled
            mime = Mock()
            with patch.object(self.module, "CurlMime", return_value=mime):
                task = asyncio.create_task(client.upload_image_json(
                    ENDPOINT, image=self.module.ImageAsset(url="https://images.test/input"),
                    data={}, timeout_seconds=1 if cancel else 0.05,
                ))
                await started.wait()
                if cancel:
                    task.cancel()
                with self.assertRaises(asyncio.CancelledError if cancel else TimeoutError):
                    await task
            mime.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
