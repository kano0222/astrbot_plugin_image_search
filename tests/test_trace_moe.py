from __future__ import annotations

import unittest
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from core.http import JsonResponse
from core.models import ImageAsset, SearchMode
from core.provider import ProviderError
from core.providers.trace_moe import TraceMoeProvider


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


class TraceMoeProviderTests(unittest.IsolatedAsyncioTestCase):
    async def test_timeout_has_explicit_safe_diagnostic(self):
        from unittest.mock import AsyncMock
        from core.provider import safe_failure_reason
        provider = TraceMoeProvider(AsyncMock(get_json=AsyncMock(side_effect=TimeoutError())))
        with self.assertRaises(ProviderError) as error:
            await provider.search(ImageAsset(url="https://img.test/a"), SearchMode.ANIME)
        self.assertEqual(safe_failure_reason(error.exception), "请求超时")

    async def test_filtered_candidate_preserves_quota_signal(self):
        http = FakeHttpClient(JsonResponse(200, {"result": [{"filename": "scene", "similarity": 0.49}]}))
        outcome = await TraceMoeProvider(http).search(ImageAsset(url="https://img.test/a"), SearchMode.ANIME)
        self.assertEqual(outcome.matches, [])
        self.assertEqual(outcome.filtered_below, 50)

    async def test_default_threshold_includes_exactly_fifty_percent(self):
        http = FakeHttpClient(JsonResponse(200, {"result": [
            {"filename": "included", "similarity": 0.5},
            {"filename": "filtered", "similarity": 0.49},
        ]}))
        outcome = await TraceMoeProvider(http).search(ImageAsset(url="https://img.test/a"), SearchMode.ANIME)
        self.assertEqual([match.title for match in outcome.matches], ["included"])

    async def test_parses_anilist_scene_result(self) -> None:
        http = FakeHttpClient(
            JsonResponse(
                200,
                {
                    "result": [
                        {
                            "anilist": {
                                "title": {
                                    "native": "葬送のフリーレン",
                                    "romaji": "Sousou no Frieren",
                                },
                                "isAdult": False,
                            },
                            "episode": 6,
                            "from": 612.4,
                            "to": 614.1,
                            "similarity": 0.9342,
                            "image": "https://media.trace.moe/image.jpg",
                        }
                    ]
                },
            )
        )
        provider = TraceMoeProvider(http, token="token", min_similarity=0.9)

        outcome = await provider.search(
            ImageAsset(url="https://example.com/frame.jpg"),
            SearchMode.ANIME,
        )

        self.assertEqual(len(outcome.matches), 1)
        match = outcome.matches[0]
        self.assertEqual(match.title, "葬送のフリーレン")
        self.assertEqual(match.episode, "6")
        self.assertEqual(match.timestamp_start, 612.4)
        self.assertAlmostEqual(match.similarity or 0, 93.42)
        self.assertFalse(match.adult)
        endpoint, params, _ = http.calls[0]
        self.assertEqual(endpoint, "https://api.trace.moe/search")
        self.assertEqual(params["key"], "token")
        self.assertEqual(params["anilistInfo"], "true")
        self.assertEqual(params["cutBorders"], "true")

    async def test_filters_low_similarity_and_uses_filename_fallback(self) -> None:
        http = FakeHttpClient(
            JsonResponse(
                200,
                {
                    "result": [
                        {
                            "filename": "low.mkv",
                            "similarity": 0.7,
                        },
                        {
                            "filename": "fallback.mkv",
                            "similarity": 0.9,
                        },
                    ]
                },
            )
        )
        provider = TraceMoeProvider(http, min_similarity=0.8)

        outcome = await provider.search(
            ImageAsset(url="https://example.com/frame.jpg"),
            SearchMode.ANIME,
        )

        self.assertEqual([match.title for match in outcome.matches], ["fallback.mkv"])

    async def test_rejects_local_only_image_before_request(self) -> None:
        http = FakeHttpClient(JsonResponse(200, {}))
        provider = TraceMoeProvider(http)

        with self.assertRaisesRegex(ProviderError, "public HTTP image URL"):
            await provider.search(ImageAsset(path=Path("frame.jpg")), SearchMode.ANIME)

        self.assertEqual(http.calls, [])

    async def test_reports_rate_limit_without_response_body(self) -> None:
        provider = TraceMoeProvider(
            FakeHttpClient(JsonResponse(429, {"error": "internal detail"}))
        )

        with self.assertRaisesRegex(ProviderError, "rate limit"):
            await provider.search(
                ImageAsset(url="https://example.com/frame.jpg"),
                SearchMode.ANIME,
            )


class TraceMoeConfigurationTests(unittest.TestCase):
    def test_rejects_invalid_similarity(self) -> None:
        with self.assertRaisesRegex(ValueError, "min_similarity"):
            TraceMoeProvider(
                FakeHttpClient(JsonResponse(200, {})),
                min_similarity=1.1,
            )


if __name__ == "__main__":
    unittest.main()
