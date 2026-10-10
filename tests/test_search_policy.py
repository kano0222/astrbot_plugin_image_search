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

    async def test_display_below_minimum_is_opt_in_and_remains_low_accuracy(self):
        for send in (False, True):
            http = AsyncMock()
            http.get_json.return_value = JsonResponse(200, {
                "header": {"status": 0}, "results": [{
                    "header": {"similarity": "20", "index_id": 21},
                    "data": {"title": "Candidate", "ext_urls": ["https://example.org/art"]},
                }],
            })
            result = await SauceNaoProvider(http, api_key="test",
                send_low_similarity=send).search(IMAGE, SearchMode.AUTO)
            self.assertEqual(len(result.matches), int(send))
            self.assertTrue(result.low_accuracy)
            self.assertFalse(result.suggests_anime)
            if send:
                self.assertIn("仅供参考", result.notices[0])

    async def test_invalid_followup_rejected(self):
        with self.assertRaises(ValueError):
            SearchService(low_similarity_provider="typo")
