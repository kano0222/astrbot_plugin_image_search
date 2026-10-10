from __future__ import annotations

import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from core.models import AnimeMatch, ImageAsset, ProviderOutcome, SearchMode, SearchResponse, SourceMatch
from core.service import SearchService
from core.source_enrichment import SourceEnricher, parse_source, post_page_url, source_page_url
from core.source_results import group_source_matches

PAGE = "https://danbooru.donmai.us/posts/123"
SOURCE = "https://www.pixiv.net/artworks/456"
HTML = f'<div class="image-container" data-normalized-source="{SOURCE}"></div>'


class SourceParsingTests(unittest.TestCase):
    def test_supported_post_pages(self):
        cases = {
            PAGE: PAGE,
            "http://danbooru.donmai.us/post/show/123?utm_source=test": PAGE,
            "https://yande.re/post/show/123/": "https://yande.re/post/show/123",
            "https://konachan.com/post/show/123": "https://konachan.com/post/show/123",
            "https://gelbooru.com/index.php?id=123&s=view&page=post&tags=test":
                "https://gelbooru.com/index.php?page=post&s=view&id=123",
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(post_page_url(raw), expected)

    def test_rejects_non_posts_and_untrusted_hosts(self):
        for raw in (
            "https://danbooru.donmai.us/posts?tags=test",
            "https://danbooru.donmai.us/users/123",
            "https://yande.re/post?tags=test",
            "https://gelbooru.com/index.php?page=post&s=list&id=123",
            "https://gelbooru.com/index.php?page=post&s=view&id=123&id=456",
            "https://danbooru.donmai.us.evil.test/posts/123",
            "https://user:pass@danbooru.donmai.us/posts/123",
            "https://danbooru.donmai.us:8080/posts/123",
            "https://127.0.0.1/posts/123", "file:///posts/123",
            "https://danbooru.donmai.us/posts/0",
        ):
            with self.subTest(raw=raw):
                self.assertEqual(post_page_url(raw), "")

    def test_four_site_fields(self):
        cases = (
            (PAGE, HTML),
            ("https://gelbooru.com/index.php?page=post&s=view&id=123", HTML),
            ("https://yande.re/post/show/123", f'<input value="{SOURCE}" id="post_source">'),
            ("https://konachan.com/post/show/123", f'<input id="post_source" value="{SOURCE}"/>'),
        )
        for page, html in cases:
            with self.subTest(page=page):
                self.assertEqual(parse_source(page, html), SOURCE)

    def test_no_arbitrary_link_or_wrong_site_field(self):
        self.assertEqual(parse_source(PAGE, f'<a href="{SOURCE}">Source</a>'), "")
        self.assertEqual(parse_source(PAGE, f'<input id="post_source" value="{SOURCE}">'), "")
        self.assertEqual(parse_source("https://yande.re/post/show/123", HTML), "")

    def test_entities_and_multiclass(self):
        html = '<div class="large image-container" data-normalized-source="https://example.org/art?id=1&amp;view=2">'
        self.assertEqual(parse_source(PAGE, html), "https://example.org/art?id=1&view=2")

    def test_missing_invalid_self_or_conflicting_source(self):
        for value in ("", "Artist name", "javascript:alert(1)", PAGE):
            html = f'<div class="image-container" data-normalized-source="{value}">'
            self.assertEqual(parse_source(PAGE, html), "")
        self.assertEqual(parse_source(PAGE, HTML + HTML.replace("456", "789")), "")
        self.assertEqual(parse_source(PAGE, HTML + HTML), SOURCE)

    def test_pximg_source_is_artwork_page(self):
        raw = "https://i.pximg.net/img-original/img/2026/01/01/01/01/01/456_p0.jpg"
        self.assertEqual(source_page_url(raw), SOURCE)
        self.assertEqual(source_page_url("https://i.pximg.net/not-an-artwork.jpg"), "")
        self.assertEqual(source_page_url("https://u:p@example.org/art"), "")
        self.assertEqual(source_page_url("https://example.org/art\nbad"), "")


class SourceEnrichmentTests(unittest.IsolatedAsyncioTestCase):
    def match(self, url=PAGE, source="", provider="google_lens"):
        return SourceMatch(provider=provider, title="Artwork", url=url, source_url=source,
                           similarity=95, author="Author", thumbnail_url="https://example.org/thumb")

    async def test_shared_page_fetched_once_and_order_metadata_preserved(self):
        loader = AsyncMock()
        loader.load.return_value = HTML
        original = [self.match(), self.match(provider="saucenao")]
        response = SearchResponse(matches=list(original), attempted_providers=["google_lens", "saucenao"])
        await SourceEnricher(loader).enrich(response)
        loader.load.assert_awaited_once_with(PAGE)
        self.assertEqual([m.source_url for m in response.matches], [SOURCE, SOURCE])
        self.assertEqual([m.provider for m in response.matches], ["google_lens", "saucenao"])
        self.assertEqual(response.matches[0].similarity, 95)
        self.assertEqual(response.matches[0].author, "Author")
        self.assertEqual(response.matches[0].thumbnail_url, original[0].thumbnail_url)
        self.assertEqual(original[0].source_url, "")
        self.assertEqual(response.failed_providers, [])

    async def test_existing_source_and_unsupported_pages_do_not_fetch(self):
        loader = AsyncMock()
        response = SearchResponse(matches=[
            self.match(source=SOURCE), self.match(url="https://x.com/user/status/1"),
            AnimeMatch(provider="trace_moe", title="Anime"),
        ])
        await SourceEnricher(loader).enrich(response)
        loader.load.assert_not_awaited()
        self.assertEqual(response.matches[0].source_url, SOURCE)

    async def test_failed_page_preserves_original_and_other_success(self):
        loader = AsyncMock()
        async def load(url):
            if url.endswith("/123"):
                raise OSError("sensitive upstream text")
            return HTML
        loader.load.side_effect = load
        original = self.match()
        response = SearchResponse(matches=[original, self.match(url=PAGE.replace("123", "124"))])
        await SourceEnricher(loader).enrich(response)
        self.assertIs(response.matches[0], original)
        self.assertEqual(response.matches[1].source_url, SOURCE)
        self.assertFalse(response.failure_details)
        self.assertFalse(response.notices)

    async def test_missing_html_preserves_original(self):
        loader = AsyncMock()
        loader.load.return_value = None
        original = self.match()
        response = SearchResponse(matches=[original])
        await SourceEnricher(loader).enrich(response)
        self.assertIs(response.matches[0], original)

    async def test_six_distinct_pages_limit(self):
        loader = AsyncMock()
        loader.load.return_value = HTML
        response = SearchResponse(matches=[self.match(url=PAGE.replace("123", str(i))) for i in range(1, 9)])
        await SourceEnricher(loader).enrich(response)
        self.assertEqual(loader.load.await_count, 6)
        self.assertEqual([m.source_url for m in response.matches[6:]], ["", ""])

    async def test_enriched_origins_merge_different_mirrors(self):
        loader = AsyncMock()
        loader.load.return_value = HTML
        response = SearchResponse(matches=[self.match(), self.match(url="https://yande.re/post/show/999")])
        loader.load.side_effect = lambda url: HTML if url == PAGE else f'<input id="post_source" value="{SOURCE}">'
        await SourceEnricher(loader).enrich(response)
        self.assertEqual(len(group_source_matches(response.matches)), 1)

    async def test_cancellation_cleans_pending_requests(self):
        loader = AsyncMock()
        entered = asyncio.Event()
        cancelled = asyncio.Event()
        async def load(url):
            entered.set()
            try:
                await asyncio.Future()
            finally:
                cancelled.set()
        loader.load.side_effect = load
        task = asyncio.create_task(SourceEnricher(loader).enrich(SearchResponse(matches=[self.match()])))
        await entered.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(cancelled.is_set())

    async def test_page_timeout_retains_search_result(self):
        loader = AsyncMock()
        async def load(url):
            await asyncio.Future()
        loader.load.side_effect = load
        original = self.match()
        response = SearchResponse(matches=[original])
        with patch("core.source_enrichment._PAGE_TIMEOUT_SECONDS", 0.01):
            await SourceEnricher(loader).enrich(response)
        self.assertIs(response.matches[0], original)
        self.assertFalse(response.failed_providers)

    async def test_overall_timeout_keeps_completed_results_and_cancels_rest(self):
        loader = AsyncMock()
        cancelled = asyncio.Event()
        async def load(url):
            if url == PAGE:
                return HTML
            try:
                await asyncio.Future()
            finally:
                cancelled.set()
        loader.load.side_effect = load
        original = self.match(url=PAGE.replace("123", "124"))
        response = SearchResponse(matches=[self.match(), original])
        with patch("core.source_enrichment._TOTAL_TIMEOUT_SECONDS", 0.02):
            await SourceEnricher(loader).enrich(response)
        self.assertEqual(response.matches[0].source_url, SOURCE)
        self.assertIs(response.matches[1], original)
        self.assertTrue(cancelled.is_set())

    async def test_service_enriches_explicit_source_and_auto_after_plan(self):
        for mode, selected in ((SearchMode.SOURCE, None), (SearchMode.AUTO, None),
                               (SearchMode.AUTO, ["saucenao"])):
            with self.subTest(mode=mode, selected=selected):
                sauce = AsyncMock()
                sauce.name = "saucenao"
                sauce.search.return_value = ProviderOutcome(matches=[self.match(provider="saucenao")])
                lens = AsyncMock()
                lens.name = "google_lens"
                loader = AsyncMock()
                loader.load.return_value = HTML
                service = SearchService(source_providers=[sauce, lens], source_enricher=SourceEnricher(loader))
                result = await service.search(ImageAsset(url="https://example.org/image"), mode,
                                              provider_names=selected)
                self.assertEqual(result.matches[0].source_url, SOURCE)
                lens.search.assert_not_awaited()
                self.assertEqual(result.attempted_providers, ["saucenao"])

    async def test_anime_only_does_not_fetch_source_pages(self):
        provider = AsyncMock()
        provider.name = "trace_moe"
        provider.search.return_value = ProviderOutcome(matches=[AnimeMatch(provider="trace_moe", title="Anime")])
        loader = AsyncMock()
        service = SearchService(anime_providers=[provider], source_enricher=SourceEnricher(loader))
        await service.search(ImageAsset(url="https://example.org/image"), SearchMode.ANIME)
        loader.load.assert_not_awaited()
