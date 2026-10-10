from __future__ import annotations

import unittest
import asyncio
from unittest.mock import AsyncMock
from collections.abc import Mapping
from dataclasses import dataclass, field

from core.http import JsonResponse
from core.models import ImageAsset, SearchMode
from core.provider import ProviderError
from core.providers.saucenao import SauceNaoProvider


@dataclass
class FakeHttpClient:
    response: JsonResponse
    calls: list[tuple[str, Mapping[str, str], float]] = field(default_factory=list)

    async def get_json(
        self,
        url: str,
        *,
        params: Mapping[str, str],
        timeout_seconds: float,
    ) -> JsonResponse:
        self.calls.append((url, dict(params), timeout_seconds))
        return self.response


class SauceNaoProviderTests(unittest.IsolatedAsyncioTestCase):
    async def test_parses_source_result_and_signals_anime_database(self) -> None:
        http = FakeHttpClient(
            JsonResponse(
                200,
                {
                    "header": {"status": 0},
                    "results": [
                        {
                            "header": {
                                "similarity": "91.25",
                                "index_id": 21,
                                "index_name": "Anime",
                                "thumbnail": "https://example.com/thumb.jpg",
                            },
                            "data": {
                                "title": "Episode title",
                                "source": "Series name",
                                "ext_urls": ["https://anidb.net/anime/1"],
                            },
                        }
                    ],
                },
            )
        )
        provider = SauceNaoProvider(http, api_key="secret")

        outcome = await provider.search(
            ImageAsset(url="https://example.com/image.jpg"),
            SearchMode.AUTO,
        )

        self.assertTrue(outcome.suggests_anime)
        self.assertEqual(len(outcome.matches), 1)
        match = outcome.matches[0]
        self.assertEqual(match.title, "Episode title")
        self.assertEqual(match.site_name, "Anime")
        self.assertEqual(match.url, "https://anidb.net/anime/1")
        endpoint, params, _ = http.calls[0]
        self.assertEqual(endpoint, "https://saucenao.com/search.php")
        self.assertEqual(params["output_type"], "2")
        self.assertEqual(params["db"], "999")

    async def test_low_similarity_is_filtered_without_notice_or_signal(self) -> None:
        http = FakeHttpClient(
            JsonResponse(
                200,
                {
                    "header": {"status": 0},
                    "results": [
                        {
                            "header": {
                                "similarity": "20",
                                "index_id": 21,
                            },
                            "data": {"title": "Weak result"},
                        }
                    ],
                },
            )
        )
        provider = SauceNaoProvider(
            http,
            api_key="secret",
            min_similarity=40,
        )

        outcome = await provider.search(
            ImageAsset(url="https://example.com/image.jpg"),
            SearchMode.AUTO,
        )

        self.assertEqual(outcome.matches, [])
        self.assertFalse(outcome.suggests_anime)
        self.assertEqual(outcome.notices, [])
        self.assertTrue(outcome.low_accuracy)

    async def test_reports_quota_without_exposing_response_message(self) -> None:
        provider = SauceNaoProvider(
            FakeHttpClient(
                JsonResponse(
                    200,
                    {
                        "header": {
                            "status": -1,
                            "message": "sensitive account detail",
                        }
                    },
                )
            ),
            api_key="secret",
        )

        with self.assertRaisesRegex(ProviderError, "quota"):
            await provider.search(
                ImageAsset(url="https://example.com/image.jpg"),
                SearchMode.SOURCE,
            )

    async def test_rejects_invalid_json_shape(self) -> None:
        provider = SauceNaoProvider(
            FakeHttpClient(JsonResponse(200, [])),
            api_key="secret",
        )

        with self.assertRaisesRegex(ProviderError, "invalid JSON"):
            await provider.search(
                ImageAsset(url="https://example.com/image.jpg"),
                SearchMode.SOURCE,
            )

    async def test_timeout_has_a_safe_distinct_error(self):
        http = AsyncMock()
        http.get_json.side_effect = asyncio.TimeoutError("secret")
        provider = SauceNaoProvider(http, api_key="test")
        with self.assertRaisesRegex(ProviderError, "^SauceNAO request timed out$"):
            await provider.search(ImageAsset(url="https://example.com/a"), SearchMode.SOURCE)


class SauceNaoConfigurationTests(unittest.TestCase):
    def test_requires_api_key(self) -> None:
        with self.assertRaisesRegex(ValueError, "API key"):
            SauceNaoProvider(
                FakeHttpClient(JsonResponse(200, {})),
                api_key=" ",
            )


    def test_nonfinite_timeout_is_rejected(self):
        for timeout in (float("inf"), float("nan")):
            with self.assertRaises(ValueError):
                SauceNaoProvider(None, api_key="test", timeout_seconds=timeout)


if __name__ == "__main__":
    unittest.main()
