import unittest
from unittest.mock import AsyncMock

from core.http import JsonResponse
from core.models import ImageAsset, ProviderOutcome, SearchMode, SourceMatch
from core.providers.saucenao import SauceNaoProvider
from core.service import SearchService

IMAGE = ImageAsset(url="https://example.org/image")


class SearchPolicyTests(unittest.IsolatedAsyncioTestCase):
    def provider(self, name, weak=False, matched=True):
        p = AsyncMock()
        p.name = name
        p.search.return_value = ProviderOutcome(
            matches=[SourceMatch(provider=name, title="Match")] if matched else [],
            low_accuracy=weak,
        )
        return p

    async def test_low_similarity_off_stops_without_other_services(self):
        sauce, lens, ascii2d = [self.provider(name, weak=name == "saucenao")
                                for name in ("saucenao", "google_lens", "ascii2d")]
        service = SearchService(source_providers=[sauce, lens, ascii2d])
        result = await service.search(IMAGE, SearchMode.AUTO)
        self.assertEqual(result.attempted_providers, ["saucenao"])
        lens.search.assert_not_awaited()
        ascii2d.search.assert_not_awaited()

    async def test_selected_followup_only_even_when_empty_or_failed(self):
        for selected in ("google_lens", "ascii2d"):
            for failure in (False, True):
                sauce = self.provider("saucenao", weak=True, matched=False)
                lens, ascii2d = self.provider("google_lens", matched=False), self.provider("ascii2d")
                target = lens if selected == "google_lens" else ascii2d
                if failure:
                    target.search.side_effect = RuntimeError("secret")
                result = await SearchService(source_providers=[sauce, lens, ascii2d],
                    low_similarity_provider=selected).search(IMAGE, SearchMode.SOURCE)
                self.assertEqual(result.attempted_providers, ["saucenao", selected])

    async def test_disabled_followup_reports_notice_without_other_fallback(self):
        sauce = self.provider("saucenao", weak=True)
        lens = self.provider("google_lens")
        result = await SearchService(source_providers=[sauce, lens],
            low_similarity_provider="ascii2d").search(IMAGE, SearchMode.AUTO)
        self.assertEqual(result.attempted_providers, ["saucenao"])
        self.assertIn("未启用", result.notices[0])

    async def test_no_other_service_still_reports_disabled_followup(self):
        sauce = self.provider("saucenao", weak=True)
        result = await SearchService(source_providers=[sauce],
            low_similarity_provider="ascii2d").search(IMAGE, SearchMode.AUTO)
        self.assertIn("未启用", result.notices[0])

    async def test_reliable_sauce_does_not_follow_up(self):
        sauce, lens = self.provider("saucenao"), self.provider("google_lens")
        await SearchService(source_providers=[sauce, lens],
            low_similarity_provider="google_lens").search(IMAGE, SearchMode.AUTO)
        lens.search.assert_not_awaited()

    async def test_explicit_sauce_keeps_followup_disabled(self):
        sauce, lens = self.provider("saucenao", weak=True), self.provider("google_lens")
        result = await SearchService(source_providers=[sauce, lens],
            low_similarity_provider="google_lens").search(IMAGE, SearchMode.AUTO,
                                                         provider_names=["saucenao"])
        self.assertEqual(result.attempted_providers, ["saucenao"])
        lens.search.assert_not_awaited()

    async def test_failure_and_empty_sauce_keep_existing_backup_behavior(self):
        for fail in (False, True):
            sauce = self.provider("saucenao", matched=False)
            if fail:
                sauce.search.side_effect = RuntimeError("failed")
            lens = self.provider("google_lens")
            result = await SearchService(source_providers=[sauce, lens]).search(IMAGE, SearchMode.AUTO)
            self.assertEqual(result.attempted_providers, ["saucenao", "google_lens"])

    async def test_unified_threshold_filters_candidates_without_notice_or_anime(self):
        for threshold in (60, 80):
            for score in (20, threshold - 0.01, threshold, threshold + 0.01):
                with self.subTest(threshold=threshold, score=score):
                    http = self.sauce_http(score)
                    outcome = await SauceNaoProvider(
                        http, api_key="test", min_similarity=threshold,
                    ).search(IMAGE, SearchMode.AUTO)
                    accepted = score >= threshold
                    self.assertEqual(len(outcome.matches), int(accepted))
                    self.assertEqual(outcome.low_accuracy, not accepted)
                    self.assertEqual(outcome.suggests_anime, accepted)
                    self.assertEqual(outcome.notices, [])
                    self.assertEqual(outcome.filtered_below, None if accepted else threshold)

    def sauce_http(self, score):
        http = AsyncMock()
        http.get_json.return_value = JsonResponse(200, {
            "header": {"status": 0}, "results": [{
                "header": {"similarity": str(score), "index_id": 21},
                "data": {"title": "Candidate", "ext_urls": ["https://example.org/art"]},
            }],
        })
        return http

    async def test_filtered_candidate_followup_and_explicit_isolation(self):
        for mode in (SearchMode.AUTO, SearchMode.SOURCE):
            for explicit in (False, True):
                with self.subTest(mode=mode, explicit=explicit):
                    sauce = SauceNaoProvider(self.sauce_http(59.99), api_key="test")
                    lens = self.provider("google_lens")
                    anime = self.provider("trace_moe")
                    result = await SearchService(
                        source_providers=[sauce, lens],
                        anime_followup_providers=[anime],
                        low_similarity_provider="google_lens",
                    ).search(IMAGE, mode,
                             provider_names=["saucenao"] if explicit else None)
                    self.assertEqual(result.attempted_providers,
                                     ["saucenao"] if explicit else ["saucenao", "google_lens"])
                    self.assertEqual([m.provider for m in result.matches],
                                     [] if explicit else ["google_lens"])
                    self.assertEqual(result.notices, [])
                    self.assertEqual(result.filtered_providers, {"saucenao": 60})
                    anime.search.assert_not_awaited()

    async def test_invalid_followup_rejected(self):
        with self.assertRaises(ValueError):
            SearchService(low_similarity_provider="typo")
