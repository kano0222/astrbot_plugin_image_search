"""Local HTTP integration checks for the real curl_cffi Cookie jar."""

from __future__ import annotations

import asyncio
import gzip
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

from ..runtime.curl_client import CurlCffiWebClient
from ..core.provider import ProviderError


class CookieHandler(BaseHTTPRequestHandler):
    def log_message(self, *args) -> None:
        pass

    def do_GET(self) -> None:
        self.send_response(200)
        self.send_header("Set-Cookie", "_session_id=server-updated; Path=/")
        self.end_headers()
        self.wfile.write(self.headers.get("Cookie", "").encode())

    def do_POST(self) -> None:
        self.send_response(200)
        self.end_headers()
        self.wfile.write(self.headers.get("Cookie", "").encode())


class CurlCookieTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.server = HTTPServer(("127.0.0.1", 0), CookieHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}/"
        self.client = CurlCffiWebClient()
        self.options = {
            "headers": {},
            "cookies": {
                "_session_id": "configured-old",
                "cf_clearance": "test-clearance",
            },
            "timeout_seconds": 3,
        }

    async def asyncTearDown(self) -> None:
        await self.client.close()
        await asyncio.to_thread(self.server.shutdown)
        self.server.server_close()
        self.thread.join(timeout=2)

    async def test_updated_cookie_survives_next_post_and_get(self) -> None:
        home = await self.client.get_text(self.url, **self.options)
        self.assertIn("_session_id=configured-old", home.text)
        posted = await self.client.post_form(self.url, data={}, **self.options)
        self.assertIn("_session_id=server-updated", posted.text)
        self.assertIn("cf_clearance=test-clearance", posted.text)
        self.assertNotIn("configured-old", posted.text)
        repeated = await self.client.get_text(self.url, **self.options)
        self.assertIn("_session_id=server-updated", repeated.text)

    async def test_server_cookie_is_preserved_without_configured_cookie(self) -> None:
        self.options["cookies"] = {}
        await self.client.get_text(self.url, **self.options)
        posted = await self.client.post_form(self.url, data={}, **self.options)
        self.assertIn("_session_id=server-updated", posted.text)

    async def test_changed_configuration_reseeds_cookie(self) -> None:
        await self.client.get_text(self.url, **self.options)
        self.options["cookies"]["_session_id"] = "new-setting"
        posted = await self.client.post_form(self.url, data={}, **self.options)
        self.assertIn("_session_id=new-setting", posted.text)

    async def test_close_resets_seed_tracking(self) -> None:
        await self.client.get_text(self.url, **self.options)
        await self.client.close()
        posted = await self.client.post_form(self.url, data={}, **self.options)
        self.assertIn("_session_id=configured-old", posted.text)


class FormHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        self.server.received.append((self.path, b""))
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        self.server.received.append((self.path, body))
        if self.path.startswith("/redirect/"):
            self.send_response(int(self.path.rsplit("/", 1)[1]))
            self.send_header("Location", self.server.target_url)
            self.end_headers()
            return
        if self.path == "/slow":
            self.server.release.wait(1)
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=" +
                         ("invalid-codec" if self.path == "/badcharset" else "utf-8"))
        content = self.server.content
        if self.path == "/gzip":
            self.send_header("Content-Encoding", "gzip")
            content = gzip.compress(content)
        if self.path == "/chunked":
            self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        try:
            if self.path == "/chunked":
                for offset in range(0, len(content), 64):
                    chunk = content[offset:offset + 64]
                    self.wfile.write(f"{len(chunk):x}\r\n".encode() + chunk + b"\r\n")
                self.wfile.write(b"0\r\n\r\n")
            else:
                self.wfile.write(content)
        except (BrokenPipeError, ConnectionResetError):
            pass


class CurlBoundedFormTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.servers = []
        self.threads = []
        for _ in range(2):
            server = HTTPServer(("127.0.0.1", 0), FormHandler)
            server.received = []
            server.content = b"ok"
            server.release = threading.Event()
            self.servers.append(server)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            self.threads.append(thread)
        self.origin, self.target = self.servers
        self.url = f"http://127.0.0.1:{self.origin.server_port}"
        self.origin.target_url = f"http://127.0.0.1:{self.target.server_port}/capture"
        self.client = CurlCffiWebClient()
        self.options = {"data": {"url": "https://image.test/a?token=private"},
                        "headers": {}, "cookies": {}, "timeout_seconds": 3}

    async def asyncTearDown(self):
        await self.client.close()
        for server, thread in zip(self.servers, self.threads):
            server.release.set()
            await asyncio.to_thread(server.shutdown)
            server.server_close()
            thread.join(timeout=2)

    async def test_no_redirect_policy_never_contacts_target(self):
        for status in (301, 302, 303, 307, 308):
            with self.subTest(status=status):
                result = await self.client.post_form(
                    f"{self.url}/redirect/{status}", **self.options,
                    allow_redirects=False, max_response_bytes=256)
                self.assertEqual(result.status, status)
        self.assertEqual(len(self.origin.received), 5)
        self.assertEqual(self.target.received, [])

    async def test_default_redirect_policy_preserves_existing_post_behavior(self):
        result = await self.client.post_form(f"{self.url}/redirect/307", **self.options)
        self.assertEqual(result.status, 200)
        self.assertEqual(result.text, "ok")
        self.assertEqual(self.target.received[0][1], self.origin.received[0][1])

    async def test_exact_limit_and_utf8_decoding(self):
        self.origin.content = "图".encode() + b"x" * 253
        result = await self.client.post_form(
            f"{self.url}/body", **self.options, max_response_bytes=256)
        self.assertEqual(result.text, "图" + "x" * 253)

    async def test_bounded_response_keeps_unknown_charset_fallback(self):
        result = await self.client.post_form(
            f"{self.url}/badcharset", **self.options, max_response_bytes=256)
        self.assertEqual(result.text, "ok")

    async def test_oversized_plain_chunked_and_decompressed_bodies_abort(self):
        for path in ("/body", "/chunked", "/gzip"):
            with self.subTest(path=path):
                self.origin.content = b"x" * 257
                with self.assertRaisesRegex(ProviderError, "^HTML response exceeds size limit$"):
                    await self.client.post_form(
                        self.url + path, **self.options, max_response_bytes=256)
                # Aborting a callback must not poison the shared session.
                self.origin.content = b"ok"
                result = await self.client.post_form(
                    f"{self.url}/body", **self.options, max_response_bytes=256)
                self.assertEqual(result.text, "ok")

    async def test_cancelled_bounded_request_releases_session(self):
        task = asyncio.create_task(self.client.post_form(
            f"{self.url}/slow", **self.options, max_response_bytes=256))
        while not self.origin.received:
            await asyncio.sleep(0.01)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.origin.release.set()
        result = await self.client.post_form(
            f"{self.url}/body", **self.options, max_response_bytes=256)
        self.assertEqual(result.text, "ok")


if __name__ == "__main__":
    unittest.main()
