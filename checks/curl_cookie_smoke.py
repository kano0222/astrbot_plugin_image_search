"""Local HTTP integration checks for the real curl_cffi Cookie jar."""

from __future__ import annotations

import asyncio
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

from ..runtime.curl_client import CurlCffiWebClient


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


if __name__ == "__main__":
    unittest.main()
