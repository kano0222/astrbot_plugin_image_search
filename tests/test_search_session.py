import asyncio
import unittest
from unittest.mock import AsyncMock

from core.search_session import SearchSessions


class SearchSessionsTests(unittest.IsolatedAsyncioTestCase):
    async def test_scoping_renewal_and_exit(self):
        sessions = SearchSessions(60)
        callback = AsyncMock()
        key = ("group:a", "user")
        try:
            sessions.start(key, ("google_lens",), callback)
            old = sessions.get(key)
            self.assertIsNone(sessions.get(("group:b", "user")))
            self.assertIsNone(sessions.get(("group:a", "other")))
            sessions.start(key, None, callback)
            self.assertIsNot(sessions.get(key), old)
            self.assertIsNone(sessions.get(key).providers)
            self.assertTrue(sessions.stop(key))
            self.assertFalse(sessions.stop(key))
            self.assertIsNone(sessions.get(key))
            await asyncio.sleep(0)
            callback.assert_not_awaited()
        finally:
            await sessions.close()

    async def test_expiry_and_close_cancel_timers(self):
        sessions = SearchSessions(0.01)
        expired = asyncio.Event()
        async def callback():
            expired.set()
        sessions.start(("group", "user"), None, callback)
        await asyncio.wait_for(expired.wait(), timeout=1)
        self.assertIsNone(sessions.get(("group", "user")))
        callback = AsyncMock()
        sessions.start(("group", "user"), None, callback)
        await sessions.close()
        self.assertIsNone(sessions.get(("group", "user")))
        callback.assert_not_awaited()
