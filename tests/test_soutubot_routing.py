import unittest
from unittest.mock import AsyncMock

from command_aliases import build_channel_aliases
from core.models import ImageAsset, ProviderOutcome, SearchMode, SourceMatch
from core.service import SearchService
from presentation import format_search_help
from simple_requests import parse_simple_request, parse_tool_channels, has_channel_reference
from tool_contract import tool_description


class SoutubotRoutingTests(unittest.IsolatedAsyncioTestCase):
    def provider(self, name, matched=True):
        provider = AsyncMock()
        provider.name = name
        provider.search.return_value = ProviderOutcome(matches=[
            SourceMatch(provider=name, title=name)
        ] if matched else [])
        return provider

    async def test_default_never_adds_soutubot(self):
        for mode in (SearchMode.AUTO, SearchMode.SOURCE):
            for matched in (True, False):
                for failed in (True, False):
                    sauce = self.provider("saucenao", matched)
                    if failed:
                        sauce.search.side_effect = RuntimeError("failure")
                    book = self.provider("soutubot")
                    service = SearchService(source_providers=[sauce, book])
                    result = await service.search(ImageAsset(url="https://example.org/image"), mode)
                    self.assertEqual(result.attempted_providers, ["saucenao"])
                    book.search.assert_not_awaited()

    async def test_only_soutubot_requires_explicit_selection(self):
        book = self.provider("soutubot")
        service = SearchService(source_providers=[book])
        self.assertEqual(service.source_plan_names, ())
        result = await service.search(ImageAsset(url="https://example.org/image"), SearchMode.AUTO)
        self.assertEqual(result.attempted_providers, [])
        book.search.assert_not_awaited()
        result = await service.search(ImageAsset(url="https://example.org/image"), SearchMode.AUTO,
                                      provider_names=["soutubot"])
        self.assertEqual(result.attempted_providers, ["soutubot"])

    async def test_explicit_combination_isolates_failure_and_never_adds_anime(self):
        sauce, book, anime = [self.provider(n) for n in ("saucenao", "soutubot", "trace_moe")]
        sauce.search.return_value.suggests_anime = True
        book.search.side_effect = RuntimeError("failure")
        result = await SearchService(source_providers=[sauce, book],
            anime_followup_providers=[anime]).search(ImageAsset(url="https://example.org/image"),
                SearchMode.AUTO, provider_names=["soutubot", "saucenao"])
        self.assertEqual(result.failed_providers, ["soutubot"])
        self.assertEqual([m.provider for m in result.matches], ["saucenao"])
        anime.search.assert_not_awaited()

    def test_book_requests_bind_current_text_and_refresh(self):
        aliases, _ = build_channel_aliases({})
        for phrase in ("搜本", "帮我搜本", "请帮我搜本 -purge"):
            request = parse_simple_request(phrase, aliases)
            self.assertEqual(request.channels, "soutubot")
            self.assertEqual(request.refresh, phrase.endswith("-purge"))
            self.assertEqual(parse_tool_channels(phrase, aliases), "soutubot")
        self.assertEqual(parse_tool_channels("搜图 soutubot google", aliases), "soutubot google")
        for phrase in ("不要搜本", "搜本并介绍作者", "不用soutubot，找出处"):
            self.assertIsNone(parse_tool_channels(phrase, aliases))
            self.assertTrue(has_channel_reference(phrase, aliases))

    def test_only_book_help_explains_explicit_selection(self):
        aliases, _ = build_channel_aliases({})
        text = format_search_help(("soutubot",), (), aliases, batch_limit=5,
            cache_enabled=True, search_mode_enabled=True, mode_timeout=60)
        self.assertIn("/搜本", text)
        self.assertIn("没有默认搜图渠道", text)
        self.assertIn("No default source channels", tool_description(("soutubot",), ()))
