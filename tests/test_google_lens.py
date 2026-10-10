from __future__ import annotations

import unittest
import asyncio
from unittest.mock import AsyncMock
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from core.http import JsonResponse
from core.models import ImageAsset, SearchMode
from core.provider import ProviderError
from core.providers.google_lens import GoogleLensProvider


@dataclass
class FakeHttpClient:
    response: JsonResponse
    calls: list[tuple[str, Mapping[str, str], float]] = field(default_factory=list)
    error: Exception | None = None

    async def get_json(
        self,
        url: str,
        *,
        params: Mapping[str, str],
        timeout_seconds: float,
    ) -> JsonResponse:
        self.calls.append((url, dict(params), timeout_seconds))
        if self.error is not None:
            raise self.error
        return self.response


@dataclass
class QueueHttpClient:
    responses: list[JsonResponse]
    calls: list[tuple[str, Mapping[str, str], float]] = field(default_factory=list)

    async def get_json(
        self,
        url: str,
        *,
        params: Mapping[str, str],
        timeout_seconds: float,
    ) -> JsonResponse:
        self.calls.append((url, dict(params), timeout_seconds))
        if not self.responses:
            raise AssertionError("unexpected HTTP request")
        return self.responses.pop(0)


class GoogleLensProviderTests(unittest.IsolatedAsyncioTestCase):
    def make_provider(
        self,
        response: JsonResponse | None = None,
        **kwargs: object,
    ) -> tuple[GoogleLensProvider, FakeHttpClient]:
        http = FakeHttpClient(response or JsonResponse(200, {}))
        provider = GoogleLensProvider(http, api_keys=["secret-key"], **kwargs)
        return provider, http

    async def test_disabled_safe_search_is_sent_explicitly(self) -> None:
        provider, http = self.make_provider(safe_search="off")

        await provider.search(
            ImageAsset(url="https://example.com/image.jpg"),
            SearchMode.SOURCE,
        )

        _, params, _ = http.calls[0]
        self.assertEqual(params["safe"], "off")

    async def test_enabled_safe_search_is_sent_explicitly(self) -> None:
        provider, http = self.make_provider(safe_search="active")

        await provider.search(
            ImageAsset(url="https://example.com/image.jpg"),
            SearchMode.SOURCE,
        )

        _, params, _ = http.calls[0]
        self.assertEqual(params["safe"], "active")

    async def test_service_default_omits_safe_parameter(self) -> None:
        provider, http = self.make_provider(safe_search="default")
        await provider.search(
            ImageAsset(url="https://example.com/image.jpg"), SearchMode.SOURCE
        )
        self.assertNotIn("safe", http.calls[0][1])

    async def test_invalid_safe_search_is_rejected(self) -> None:
        for value in ("invalid", False, None):
            with self.assertRaisesRegex(ValueError, "safe_search"):
                self.make_provider(safe_search=value)

    async def test_request_options_are_forwarded(self) -> None:
        provider, http = self.make_provider(
            search_type="exact_matches",
            language="JA",
            country="JP",
            auto_crop=True,
            no_cache=True,
            timeout_seconds=12,
        )

        await provider.search(
            ImageAsset(url="https://example.com/image.jpg"),
            SearchMode.AUTO,
        )

        endpoint, params, timeout = http.calls[0]
        self.assertEqual(endpoint, "https://serpapi.com/search")
        self.assertEqual(params["type"], "exact_matches")
        self.assertEqual(params["hl"], "ja")
        self.assertEqual(params["country"], "jp")
        self.assertEqual(params["auto_crop"], "true")
        self.assertEqual(params["no_cache"], "true")
        self.assertLessEqual(timeout, 12)
        self.assertGreater(timeout, 11)

    async def test_parses_valid_results_and_skips_incomplete_items(self) -> None:
        provider, _ = self.make_provider(
            JsonResponse(
                200,
                {
                    "visual_matches": [
                        {"title": "missing link"},
                        {
                            "title": "result",
                            "link": "https://example.com/source",
                            "source": "Example",
                            "thumbnail": "https://example.com/thumb.jpg",
                        },
                        {
                            "title": "second",
                            "link": "https://example.com/second",
                        },
                    ]
                },
            ),
            max_results=1,
        )

        outcome = await provider.search(
            ImageAsset(url="https://example.com/image.jpg"),
            SearchMode.SOURCE,
        )

        self.assertEqual(len(outcome.matches), 1)
        match = outcome.matches[0]
        self.assertEqual(match.title, "result")
        self.assertEqual(match.site_name, "Example")
        self.assertEqual(match.url, "https://example.com/source")

    async def test_exact_match_response_uses_exact_match_key(self) -> None:
        provider, _ = self.make_provider(
            JsonResponse(
                200,
                {
                    "exact_matches": [
                        {
                            "title": "original",
                            "link": "https://example.com/original",
                        }
                    ]
                },
            ),
            search_type="exact_matches",
        )

        outcome = await provider.search(
            ImageAsset(url="https://example.com/image.jpg"),
            SearchMode.SOURCE,
        )

        self.assertEqual([match.title for match in outcome.matches], ["original"])

    async def test_local_only_image_is_rejected_before_request(self) -> None:
        provider, http = self.make_provider()

        with self.assertRaisesRegex(ProviderError, "public HTTP image URL"):
            await provider.search(
                ImageAsset(path=Path("image.jpg")),
                SearchMode.SOURCE,
            )

        self.assertEqual(http.calls, [])

    async def test_quota_status_is_reported_without_response_body(self) -> None:
        provider, _ = self.make_provider(JsonResponse(429, {"error": "secret"}))

        with self.assertRaisesRegex(ProviderError, "credentials or quotas"):
            await provider.search(
                ImageAsset(url="https://example.com/image.jpg"),
                SearchMode.SOURCE,
            )

    async def test_no_results_error_is_an_empty_success(self) -> None:
        provider, _ = self.make_provider(
            JsonResponse(200, {"error": "Google hasn't returned any results."})
        )

        outcome = await provider.search(
            ImageAsset(url="https://example.com/image.jpg"),
            SearchMode.SOURCE,
        )

        self.assertEqual(outcome.matches, [])

    async def test_retries_next_key_after_quota_rejection(self) -> None:
        http = QueueHttpClient(
            [
                JsonResponse(429, {"error": "quota"}),
                JsonResponse(
                    200,
                    {
                        "visual_matches": [
                            {
                                "title": "result",
                                "link": "https://example.com/source",
                            }
                        ]
                    },
                ),
            ]
        )
        provider = GoogleLensProvider(http, api_keys=["first", "second"])

        outcome = await provider.search(
            ImageAsset(url="https://example.com/image.jpg"),
            SearchMode.SOURCE,
        )

        self.assertEqual([match.title for match in outcome.matches], ["result"])
        self.assertEqual(
            [call[1]["api_key"] for call in http.calls],
            ["first", "second"],
        )

    async def test_requests_start_with_keys_in_round_robin_order(self) -> None:
        http = QueueHttpClient([JsonResponse(200, {}), JsonResponse(200, {})])
        provider = GoogleLensProvider(http, api_keys=["first", "second"])
        image = ImageAsset(url="https://example.com/image.jpg")

        await provider.search(image, SearchMode.SOURCE)
        await provider.search(image, SearchMode.SOURCE)

        self.assertEqual(
            [call[1]["api_key"] for call in http.calls],
            ["first", "second"],
        )


    async def test_timeout_does_not_retry_another_key(self):
        http = AsyncMock()
        http.get_json.side_effect = asyncio.TimeoutError("secret-token")
        provider = GoogleLensProvider(http, api_keys=["first", "second"])
        with self.assertRaisesRegex(ProviderError, "^Google Lens request timed out$"):
            await provider.search(ImageAsset(url="https://example.com/a"), SearchMode.SOURCE)
        self.assertEqual(http.get_json.await_count, 1)

    async def test_key_failover_shares_one_timeout_budget(self):
        http = AsyncMock()
        calls = []

        async def delayed(url, *, params, timeout_seconds):
            calls.append((dict(params), timeout_seconds))
            if len(calls) == 1:
                await asyncio.sleep(0.02)
                return JsonResponse(429, {})
            await asyncio.Future()

        http.get_json.side_effect = delayed
        provider = GoogleLensProvider(http, api_keys=["first", "second"],
                                      timeout_seconds=0.1)
        with self.assertRaisesRegex(ProviderError, "timed out"):
            await provider.search(ImageAsset(url="https://example.com/a"), SearchMode.SOURCE)
        self.assertEqual([params["api_key"] for params, _ in calls], ["first", "second"])
        self.assertLess(calls[1][1], calls[0][1])
        self.assertLess(calls[0][1], 0.101)

    async def test_cancellation_propagates_without_trying_next_key(self):
        http = AsyncMock()
        http.get_json.side_effect = asyncio.CancelledError()
        provider = GoogleLensProvider(http, api_keys=["first", "second"])
        with self.assertRaises(asyncio.CancelledError):
            await provider.search(ImageAsset(url="https://example.com/a"), SearchMode.SOURCE)
        self.assertEqual(http.get_json.await_count, 1)

    async def test_http_500_does_not_consume_another_key(self):
        http = QueueHttpClient([JsonResponse(500, {"error": "secret"})])
        provider = GoogleLensProvider(http, api_keys=["first", "second"])
        with self.assertRaisesRegex(ProviderError, "^SerpAPI returned HTTP 500$"):
            await provider.search(ImageAsset(url="https://example.com/a"), SearchMode.SOURCE)
        self.assertEqual(len(http.calls), 1)


class GoogleLensConfigurationTests(unittest.TestCase):
    def test_rejects_missing_api_keys(self) -> None:
        http = FakeHttpClient(JsonResponse(200, {}))
        with self.assertRaisesRegex(ValueError, "SerpAPI key"):
            GoogleLensProvider(http, api_keys=["  "])

    def test_discontinued_products_type_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "unsupported Google Lens search type"):
            GoogleLensProvider(FakeHttpClient(JsonResponse(200, {})),
                               api_keys=["test"], search_type="products")

    def test_nonfinite_timeout_is_rejected(self):
        for timeout in (float("inf"), float("nan")):
            with self.assertRaises(ValueError):
                GoogleLensProvider(FakeHttpClient(JsonResponse(200, {})),
                                   api_keys=["test"], timeout_seconds=timeout)


if __name__ == "__main__":
    unittest.main()
