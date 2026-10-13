from __future__ import annotations

import unittest
from dataclasses import dataclass
from pathlib import Path

from core.models import AnimeMatch, ImageAsset, ProviderOutcome, SearchMode, SourceMatch
from core.service import SearchService
from core.provider import ProviderError


@dataclass
class FakeProvider:
    name: str
    outcome: ProviderOutcome | None = None
    error: Exception | None = None

    def __post_init__(self) -> None:
        self.calls: list[SearchMode] = []

    async def search(
        self,
        image: ImageAsset,
        mode: SearchMode,
    ) -> ProviderOutcome:
        self.calls.append(mode)
        if self.error is not None:
            raise self.error
        return self.outcome or ProviderOutcome()


class SearchServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_source_mode_only_runs_source_providers(self) -> None:
        source = FakeProvider(
            "saucenao",
            ProviderOutcome(matches=[SourceMatch(provider="saucenao", title="match")]),
        )
        anime = FakeProvider("anime")
        service = SearchService(source_providers=[source], anime_providers=[anime])

        response = await service.search(
            ImageAsset(url="https://example.com/image.jpg"),
            SearchMode.SOURCE,
        )

        self.assertEqual([match.title for match in response.matches], ["match"])
        self.assertEqual(source.calls, [SearchMode.SOURCE])
        self.assertEqual(anime.calls, [])

    async def test_anime_mode_only_runs_anime_providers(self) -> None:
        source = FakeProvider("saucenao")
        anime = FakeProvider(
            "anime",
            ProviderOutcome(matches=[AnimeMatch(provider="anime", title="show")]),
        )
        service = SearchService(source_providers=[source], anime_providers=[anime])

        response = await service.search(
            ImageAsset(path=Path("image.jpg")),
            SearchMode.ANIME,
        )

        self.assertEqual([match.title for match in response.matches], ["show"])
        self.assertEqual(source.calls, [])
        self.assertEqual(anime.calls, [SearchMode.ANIME])

    async def test_auto_mode_skips_anime_followup_without_signal(self) -> None:
        source = FakeProvider(
            "saucenao",
            ProviderOutcome(matches=[SourceMatch(provider="saucenao", title="match")]),
        )
        followup = FakeProvider("followup")
        service = SearchService(
            source_providers=[source],
            anime_followup_providers=[followup],
        )

        await service.search(
            ImageAsset(url="https://example.com/image.jpg"),
            SearchMode.AUTO,
        )

        self.assertEqual(source.calls, [SearchMode.AUTO])
        self.assertEqual(followup.calls, [])

    async def test_auto_mode_runs_anime_followup_after_signal(self) -> None:
        source = FakeProvider(
            "saucenao",
            ProviderOutcome(
                matches=[SourceMatch(provider="saucenao", title="anime database")],
                suggests_anime=True,
            ),
        )
        followup = FakeProvider(
            "followup",
            ProviderOutcome(matches=[AnimeMatch(provider="followup", title="show")]),
        )
        service = SearchService(
            source_providers=[source],
            anime_followup_providers=[followup],
        )

        response = await service.search(
            ImageAsset(url="https://example.com/image.jpg"),
            SearchMode.AUTO,
        )

        self.assertEqual(
            [match.title for match in response.matches],
            ["anime database", "show"],
        )
        self.assertEqual(followup.calls, [SearchMode.ANIME])

    async def test_safe_http_failure_survives_lens_fallback(self) -> None:
        ascii_provider = FakeProvider(
            "ascii2d", error=ProviderError("Ascii2d search returned HTTP 403")
        )
        lens = FakeProvider(
            "google_lens",
            ProviderOutcome(
                matches=[SourceMatch(provider="google_lens", title="match")]
            ),
        )
        response = await SearchService(source_providers=[ascii_provider, lens]).search(
            ImageAsset(url="https://example.com/image.jpg"), SearchMode.AUTO
        )
        self.assertEqual(
            response.failure_details["ascii2d"], "提交搜索返回 HTTP 403，访问受限"
        )
        self.assertEqual(response.matches[0].title, "match")

    async def test_arbitrary_exception_message_is_not_exposed(self) -> None:
        failed = FakeProvider(
            "ascii2d",
            error=ProviderError("secret-api-key https://image.example/?secret=token"),
        )
        response = await SearchService(source_providers=[failed]).search(
            ImageAsset(url="https://example.com/image.jpg"), SearchMode.AUTO
        )
        self.assertNotIn("secret", str(response.failure_details))
        self.assertNotIn("image.example", str(response.failure_details))

    async def test_provider_failure_does_not_discard_success(self) -> None:
        failed = FakeProvider("failed", error=RuntimeError("unavailable"))
        successful = FakeProvider(
            "successful",
            ProviderOutcome(
                matches=[SourceMatch(provider="successful", title="match")]
            ),
        )
        service = SearchService(source_providers=[failed, successful])

        response = await service.search(
            ImageAsset(url="https://example.com/image.jpg"),
            SearchMode.SOURCE, provider_names=["failed", "successful"],
        )

        self.assertEqual([match.title for match in response.matches], ["match"])
        self.assertEqual(response.attempted_providers, ["failed", "successful"])
        self.assertEqual(response.failed_providers, ["failed"])


class ImageAssetTests(unittest.TestCase):
    def test_requires_a_representation(self) -> None:
        with self.assertRaisesRegex(ValueError, "image URL or local path"):
            ImageAsset()


if __name__ == "__main__":
    unittest.main()
