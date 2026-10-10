from __future__ import annotations

import unittest

from core.http import JsonResponse
from core.models import ImageAsset, ProviderOutcome, SearchMode, SourceMatch
from core.providers.saucenao import SauceNaoProvider
from core.service import SearchService
from test_service import FakeProvider
from test_saucenao import FakeHttpClient

IMAGE = ImageAsset(url="https://example.com/image.jpg")


def result(score, database=9, url="https://example.com/work"):
    return {
        "header": {"similarity": str(score), "index_id": database},
        "data": {"title": str(score), "ext_urls": [url]},
    }


class CqpsSelectionTests(unittest.IsolatedAsyncioTestCase):
    async def search(self, items):
        provider = SauceNaoProvider(
            FakeHttpClient(
                JsonResponse(200, {"header": {"status": 0}, "results": items})
            ),
            api_key="test",
        )
        return await provider.search(IMAGE, SearchMode.AUTO)

    async def test_close_pixiv_candidate_is_preferred(self):
        outcome = await self.search(
            [
                result(90),
                result(88, 5, "https://www.pixiv.net/artworks/100"),
            ]
        )
        self.assertEqual(outcome.matches[0].similarity, 88)
        self.assertEqual(len(outcome.matches), 1)

    async def test_pixiv_preference_cannot_override_large_score_gap(self):
        outcome = await self.search([result(90), result(80, 5)])
        self.assertEqual(outcome.matches[0].similarity, 90)

    async def test_nearby_pixiv_candidates_prefer_earlier_post(self):
        outcome = await self.search(
            [
                result(94, 5, "https://www.pixiv.net/artworks/200"),
                result(90, 5, "https://www.pixiv.net/artworks/100"),
                result(80, 5, "https://www.pixiv.net/artworks/10"),
            ]
        )
        self.assertEqual(outcome.matches[0].url, "https://www.pixiv.net/artworks/100")

    async def test_weak_anime_candidate_does_not_trigger_anime(self):
        outcome = await self.search([result(55, 21)])
        self.assertTrue(outcome.low_accuracy)
        self.assertFalse(outcome.suggests_anime)
        self.assertEqual(outcome.matches, [])
        self.assertEqual(outcome.notices, [])

    async def test_custom_threshold_controls_filter_and_anime_signal(self):
        provider = SauceNaoProvider(
            FakeHttpClient(
                JsonResponse(
                    200, {"header": {"status": 0}, "results": [result(75, 21)]}
                )
            ),
            api_key="test",
            min_similarity=80,
        )
        outcome = await provider.search(IMAGE, SearchMode.AUTO)
        self.assertTrue(outcome.low_accuracy)
        self.assertFalse(outcome.suggests_anime)
        self.assertEqual(outcome.matches, [])
        self.assertEqual(outcome.notices, [])

    async def test_threshold_boundary_is_reliable(self):
        outcome = await self.search([result(60, 21)])
        self.assertFalse(outcome.low_accuracy)
        self.assertTrue(outcome.suggests_anime)

    async def test_nonfinite_and_out_of_range_scores_are_ignored(self):
        outcome = await self.search(
            [result("nan"), result("inf"), result(101), result(90)]
        )
        self.assertEqual(outcome.matches[0].similarity, 90)

    async def test_pximg_source_becomes_work_page_not_title(self):
        item = result(95)
        item["data"] = {
            "source": "https://i.pximg.net/img-original/img/2026/04/28/10/58/15/144087390",
            "ext_urls": ["https://danbooru.donmai.us/post/show/11257941"],
        }
        outcome = await self.search([item])
        match = outcome.matches[0]
        self.assertEqual(match.title, "未提供标题")
        self.assertEqual(match.source_url, "https://www.pixiv.net/artworks/144087390")
        self.assertEqual(match.url, "https://danbooru.donmai.us/post/show/11257941")

    async def test_pximg_filename_becomes_work_page(self):
        item = result(95)
        item["data"]["source"] = (
            "https://i.pximg.net/img-original/img/2026/01/01/00/00/00/123_p2.png"
        )
        outcome = await self.search([item])
        self.assertEqual(
            outcome.matches[0].source_url, "https://www.pixiv.net/artworks/123"
        )

    async def test_danbooru_link_is_preferred_for_non_pixiv(self):
        item = result(90)
        item["data"]["ext_urls"].append("https://danbooru.donmai.us/posts/1")
        outcome = await self.search([item])
        self.assertEqual(outcome.matches[0].url, "https://danbooru.donmai.us/posts/1")


class CqpsPlanTests(unittest.IsolatedAsyncioTestCase):
    async def run_plan(self, sauce=None, ascii_outcome=None, sauce_error=None):
        sauce_provider = FakeProvider("saucenao", sauce, sauce_error)
        ascii_provider = FakeProvider("ascii2d", ascii_outcome)
        lens = FakeProvider(
            "google_lens",
            ProviderOutcome(
                matches=[SourceMatch(provider="google_lens", title="lens")],
            ),
        )
        service = SearchService(source_providers=[lens, ascii_provider, sauce_provider],
                                low_similarity_provider="ascii2d")
        response = await service.search(IMAGE, SearchMode.AUTO)
        return response

    async def test_reliable_sauce_skips_other_engines(self):
        response = await self.run_plan(
            ProviderOutcome(
                matches=[SourceMatch(provider="saucenao", title="sauce")],
            )
        )
        self.assertEqual(response.attempted_providers, ["saucenao"])

    async def test_weak_sauce_supplements_ascii_without_lens(self):
        response = await self.run_plan(
            ProviderOutcome(
                low_accuracy=True,
            ),
            ProviderOutcome(matches=[SourceMatch(provider="ascii2d", title="ascii")]),
        )
        self.assertEqual(response.attempted_providers, ["saucenao", "ascii2d"])
        self.assertEqual([m.title for m in response.matches], ["ascii"])

    async def test_failure_and_empty_results_fall_back_to_lens(self):
        response = await self.run_plan(sauce_error=RuntimeError("quota"))
        self.assertEqual(
            response.attempted_providers, ["saucenao", "ascii2d", "google_lens"]
        )
        self.assertEqual(response.failed_providers, ["saucenao"])
        self.assertEqual(response.matches[0].title, "lens")

    async def test_explicit_lens_never_runs_ascii_or_anime(self):
        sauce = FakeProvider("saucenao")
        ascii_provider = FakeProvider("ascii2d")
        lens = FakeProvider("google_lens", ProviderOutcome(suggests_anime=True))
        anime = FakeProvider("trace_moe")
        service = SearchService(
            source_providers=[sauce, ascii_provider, lens],
            anime_followup_providers=[anime],
        )
        response = await service.search(
            IMAGE, SearchMode.AUTO, provider_names=["google_lens"]
        )
        self.assertEqual(response.attempted_providers, ["google_lens"])
        self.assertEqual(ascii_provider.calls, [])
        self.assertEqual(sauce.calls, [])
        self.assertEqual(anime.calls, [])

    async def test_unavailable_explicit_provider_does_not_fall_back(self):
        lens = FakeProvider("google_lens")
        service = SearchService(source_providers=[lens])
        with self.assertRaises(ValueError):
            await service.search(IMAGE, SearchMode.AUTO, provider_names=["ascii2d"])
        self.assertEqual(lens.calls, [])

    async def test_only_lens_remains_usable(self):
        lens = FakeProvider(
            "google_lens",
            ProviderOutcome(
                matches=[SourceMatch(provider="google_lens", title="lens")],
            ),
        )
        response = await SearchService(source_providers=[lens]).search(
            IMAGE, SearchMode.SOURCE
        )
        self.assertEqual(response.attempted_providers, ["google_lens"])

    async def test_explicit_anime_does_not_run_source_engines(self):
        sauce = FakeProvider("saucenao")
        anime = FakeProvider("trace_moe")
        await SearchService(source_providers=[sauce], anime_providers=[anime]).search(
            IMAGE, SearchMode.ANIME
        )
        self.assertEqual(sauce.calls, [])
        self.assertEqual(anime.calls, [SearchMode.ANIME])
