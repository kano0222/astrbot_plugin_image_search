from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from core.models import AnimeMatch, SearchResponse, SourceMatch
from core.search_state import SearchState, cache_key


class SearchStateTests(unittest.TestCase):
    def test_quota_survives_reopen_and_resets_on_new_day(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite3"
            state = SearchState(path)
            self.assertTrue(state.reserve("user", "2026-10-10", 1))
            state.close()
            state = SearchState(path)
            self.assertFalse(state.reserve("user", "2026-10-10", 1))
            self.assertTrue(state.reserve("user", "2026-10-11", 1))
            state.close()

    def test_refund_user_isolation_and_unlimited(self):
        state = SearchState()
        try:
            self.assertTrue(state.reserve("a", "2026-10-10", 1))
            self.assertFalse(state.reserve("a", "2026-10-10", 1))
            self.assertTrue(state.reserve("b", "2026-10-10", 1))
            state.refund("a", "2026-10-10")
            self.assertTrue(state.reserve("a", "2026-10-10", 1))
            for _ in range(35):
                self.assertTrue(state.reserve("a", "2026-10-10", 0))
        finally:
            state.close()

    def test_quota_is_atomic_across_connections(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite3"
            first, second = SearchState(path), SearchState(path)
            try:
                self.assertTrue(first.reserve("a", "2026-10-10", 1))
                self.assertFalse(second.reserve("a", "2026-10-10", 1))
            finally:
                first.close()
                second.close()

    def test_cache_round_trip_expiry_and_copies(self):
        state = SearchState()
        response = SearchResponse(matches=[SourceMatch("saucenao", "Artwork"),
                                            AnimeMatch("trace_moe", "Anime", adult=True)],
                                  attempted_providers=["saucenao", "trace_moe"])
        try:
            with patch("core.search_state.time.time", return_value=100):
                state.put("key", response)
                fetched, created = state.get("key", 10)
                self.assertEqual(response, fetched)
                self.assertEqual(created, 100)
                fetched.matches.clear()
                self.assertEqual(len(state.get("key", 10)[0].matches), 2)
            with patch("core.search_state.time.time", return_value=110):
                self.assertIsNone(state.get("key", 10))
        finally:
            state.close()

    def test_cache_separates_intents_channels_config_and_signed_urls(self):
        baseline = cache_key("https://img.test/a?token=1", "auto", ("s", "g"), {"min": 60})
        self.assertEqual(baseline, cache_key("https://img.test/a?token=1", "auto", ("g", "s"), {"min": 60}))
        for url, mode, names, config in (
            ("https://img.test/a?token=2", "auto", ("s", "g"), {"min": 60}),
            ("https://img.test/a?token=1", "source", ("s", "g"), {"min": 60}),
            ("https://img.test/a?token=1", "auto", ("s",), {"min": 60}),
            ("https://img.test/a?token=1", "auto", ("s", "g"), {"min": 80}),
        ):
            self.assertNotEqual(baseline, cache_key(url, mode, names, config))
