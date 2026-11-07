from __future__ import annotations

import asyncio
import unittest
from unittest.mock import AsyncMock

from core.models import ImageAsset, SearchMode
from core.provider import ProviderError, safe_failure_reason
from core.providers.saucenao import SauceNaoProvider
from core.providers.saucenao_html import MAX_HTML_BYTES, parse_anonymous_results
from core.web import TextResponse


def card(score="95.2", title="Art &amp; title", url="https://www.pixiv.net/artworks/123",
         columns='', misc=None, thumbnail='/thumb.jpg'):
    return f'''<div id="res0" class="result hidden">
      <div class="resultimage"><img src="{thumbnail}"></div>
      <div class="resulttable"><div class="resultsimilarityinfo">{score}%</div>
      <div class="resultcontent"><div class="resulttitle"><strong>{title}</strong></div>
      <div class="resultcontentcolumn">{columns}</div></div>
      <div class="resultmiscinfo">{misc if misc is not None else f'<a href="{url}">source</a>'}</div>
      </div></div>'''


class SauceAnonymousTests(unittest.IsolatedAsyncioTestCase):
    def provider(self, html, status=200, **kwargs):
        self.http = AsyncMock()
        self.web = AsyncMock()
        self.web.post_form.return_value = TextResponse(
            status, "https://saucenao.com/search.php", html,
        )
        return SauceNaoProvider(self.http, web_http=self.web, **kwargs)

    async def search(self, provider):
        return await provider.search(ImageAsset(url="https://image.test/a?token=private"), SearchMode.AUTO)

    async def test_anonymous_form_has_no_key_and_preserves_metadata(self):
        provider = self.provider(card(columns='''<strong>Member: </strong>
          <a href="https://www.pixiv.net/users/456">Artist</a><br>
          <strong>Source: </strong><a href="https://original.test/art">Original</a>'''),
          api_key="  ", timeout_seconds=12)
        outcome = await self.search(provider)
        match = outcome.matches[0]
        self.assertEqual(match.title, "Art & title")
        self.assertEqual(match.similarity, 95.2)
        self.assertEqual(match.url, "https://www.pixiv.net/artworks/123")
        self.assertEqual(match.author, "Artist")
        self.assertEqual(match.author_url, "https://www.pixiv.net/users/456")
        self.assertEqual(match.source_url, "https://original.test/art")
        self.assertEqual(match.thumbnail_url, "https://saucenao.com/thumb.jpg")
        self.assertFalse(outcome.suggests_anime)
        call = self.web.post_form.await_args
        self.assertEqual(call.args, ("https://saucenao.com/search.php",))
        self.assertEqual(call.kwargs["data"], {
            "url": "https://image.test/a?token=private", "db": "999",
            "output_type": "0", "numres": "5",
        })
        self.assertEqual(call.kwargs["timeout_seconds"], 12)
        self.assertEqual(call.kwargs["cookies"], {})
        self.assertFalse(call.kwargs["allow_redirects"])
        self.assertEqual(call.kwargs["max_response_bytes"], MAX_HTML_BYTES)
        self.assertNotIn("api_key", call.kwargs["data"])
        self.http.get_json.assert_not_awaited()

    async def test_score_belongs_to_each_card_and_threshold_is_inclusive(self):
        provider = self.provider(card("20", "weak", "https://example.test/weak") +
                                 card("60", "strong", "https://example.test/strong"))
        outcome = await self.search(provider)
        self.assertEqual(outcome.matches[0].title, "strong")
        self.assertEqual(outcome.matches[0].similarity, 60)

    async def test_preserves_pixiv_nearby_preference(self):
        provider = self.provider(card("96", "Mirror", "https://danbooru.donmai.us/posts/1") +
                                 card("95", "New", "https://www.pixiv.net/artworks/999") +
                                 card("92", "Old", "https://www.pixiv.net/artworks/123"))
        outcome = await self.search(provider)
        self.assertEqual(outcome.matches[0].title, "Old")

    async def test_anime_followup_only_for_reliable_result(self):
        for score, expected in (("91", True), ("20", False)):
            provider = self.provider(card(score, "Anime", "https://anidb.net/anime/1"))
            outcome = await self.search(provider)
            self.assertEqual(outcome.suggests_anime, expected)
            self.assertEqual(outcome.low_accuracy, not expected)
            self.assertEqual(outcome.filtered_below, None if expected else 60)
            if not expected:
                self.assertEqual(outcome.matches, [])

    async def test_block_fields_keep_author_and_source_separate(self):
        columns = ('<div><strong>Author:</strong><a href="https://artist.test">Artist</a></div>'
                   '<p><strong>Source:</strong><a href="https://original.test/art">Original</a></p>')
        match = (await self.search(self.provider(card(columns=columns)))).matches[0]
        self.assertEqual(match.author, "Artist")
        self.assertEqual(match.author_url, "https://artist.test")
        self.assertEqual(match.source_url, "https://original.test/art")

    async def test_legacy_pixiv_preserves_preference_and_earlier_work_selection(self):
        html = card('96', 'Mirror', 'https://danbooru.donmai.us/posts/1')
        html += card('95', 'New', 'https://www.pixiv.net/member_illust.php?mode=medium&amp;illust_id=999')
        html += card('92', 'Old', 'https://www.pixiv.net/member_illust.php?mode=medium&amp;illust_id=123')
        match = (await self.search(self.provider(html))).matches[0]
        self.assertEqual(match.title, 'Old')
        self.assertIn('illust_id=123', match.url)
        self.assertEqual(match.site_name, 'pixiv Images')

    async def test_database_identity_restores_followup_and_index_label(self):
        for database, label, url in (
            (23, 'Movies', 'https://www.imdb.com/title/tt1234567/'),
            (24, 'Shows', 'https://www.themoviedb.org/tv/123'),
        ):
            with self.subTest(database=database):
                outcome = await self.search(self.provider(card(
                    url=url, thumbnail=f'https://saucenao.com/res/{database}/thumb.jpg')))
                self.assertTrue(outcome.suggests_anime)
                self.assertEqual(outcome.matches[0].site_name, label)
                # A third-party path or just an IMDb host is not database evidence.
                outcome = await self.search(self.provider(card(
                    url=url, thumbnail=f'https://untrusted.test/res/{database}/thumb.jpg')))
                self.assertFalse(outcome.suggests_anime)
                self.assertEqual(outcome.matches[0].site_name, '')

    async def test_unidentified_database_does_not_invent_index_label(self):
        outcome = await self.search(self.provider(card(
            url='https://example.test/work', thumbnail='/res/999/thumb.jpg')))
        self.assertEqual(outcome.matches[0].site_name, '')
        self.assertFalse(outcome.suggests_anime)

    async def test_explicit_database_link_with_non_numeric_thumbnail(self):
        html = card(url='https://www.imdb.com/title/tt1234567/',
                    thumbnail='https://img3.saucenao.com/booru/cache/thumb.jpg',
                    misc='<a href="/search.php?db=24">Shows</a>'
                         '<a href="https://www.imdb.com/title/tt1234567/">IMDb</a>')
        outcome = await self.search(self.provider(html))
        self.assertTrue(outcome.suggests_anime)
        self.assertEqual(outcome.matches[0].site_name, 'Shows')
        self.assertEqual(outcome.matches[0].url, 'https://www.imdb.com/title/tt1234567/')

    async def test_redirect_response_is_failure_without_retry(self):
        for status in (301, 302, 303, 307, 308):
            with self.subTest(status=status):
                with self.assertRaisesRegex(ProviderError, f'HTTP {status}'):
                    await self.search(self.provider('', status=status))
                self.assertEqual(self.web.post_form.await_count, 1)

    async def test_oversized_mock_response_is_rejected_before_parsing(self):
        for html in ('x' * (MAX_HTML_BYTES + 1), '图' * (MAX_HTML_BYTES // 3 + 1)):
            with self.assertRaisesRegex(ProviderError, 'size limit'):
                await self.search(self.provider(html))

    def test_parser_limits_nodes_and_nesting(self):
        for html in ('<div>' * 128, '<img>' * 20001):
            with self.assertRaisesRegex(ProviderError, 'parsing limits'):
                parse_anonymous_results(html)

    async def test_unrelated_links_and_author_link_are_not_sources(self):
        provider = self.provider('<a href="https://noise.test">navigation</a>' + card(
            columns='<strong>Author:</strong><a href="https://artist.test">Name</a>',
            misc='<a href="/similar.php">similar</a><a href="javascript:alert(1)">bad</a>'
                 '<a href="https://user:secret@unsafe.test/p">bad</a>'
                 '<a href="//danbooru.donmai.us/posts/1">Danbooru</a>',
        ))
        match = (await self.search(provider)).matches[0]
        self.assertEqual(match.url, "https://danbooru.donmai.us/posts/1")
        self.assertEqual(match.author_url, "https://artist.test")

    async def test_unrecognized_page_does_not_become_empty_success(self):
        for html in ("", "<h1>Server busy</h1>", card(score="nan"),
                     card(score="101"), '<div class="result">broken</div>'):
            with self.subTest(html=html):
                with self.assertRaisesRegex(ProviderError, "unrecognized page"):
                    await self.search(self.provider(html))

    async def test_explicit_empty_results_are_success(self):
        outcome = await self.search(self.provider("<p>No results found.</p>"))
        self.assertEqual(outcome.matches, [])
        self.assertFalse(outcome.low_accuracy)

    async def test_partial_parse_retains_match_and_reports_notice(self):
        outcome = await self.search(self.provider(card() + card(score="unknown")))
        self.assertEqual(len(outcome.matches), 1)
        self.assertTrue(outcome.notices)

    async def test_result_text_is_not_mistaken_for_service_errors(self):
        outcome = await self.search(self.provider(card(title="Captcha: Too many requests")))
        self.assertEqual(outcome.matches[0].title, "Captcha: Too many requests")

    async def test_limit_and_challenge_have_safe_distinct_errors_without_retry(self):
        for status, html, error in (
            (429, "secret", "rate limit"),
            (403, "secret", "anonymous access"),
            (200, "Daily search limit reached", "rate limit"),
            (200, "You have exceeded your daily search limit", "rate limit"),
            (200, '<form id="captcha">Verify you are human</form>', "anonymous access"),
            (503, "private upstream payload", "HTTP 503"),
        ):
            with self.subTest(status=status, html=html):
                provider = self.provider(html, status)
                with self.assertRaisesRegex(ProviderError, error) as raised:
                    await self.search(provider)
                self.assertNotIn("secret", str(raised.exception))
                self.assertNotIn("private", safe_failure_reason(raised.exception))
                self.assertEqual(self.web.post_form.await_count, 1)
                self.http.get_json.assert_not_awaited()

    async def test_timeout_network_failure_and_cancellation(self):
        for error, expected in ((TimeoutError("private"), "timed out"),
                                (RuntimeError("private"), "request failed")):
            provider = self.provider("")
            self.web.post_form.side_effect = error
            with self.assertRaisesRegex(ProviderError, expected):
                await self.search(provider)
        provider = self.provider("")
        self.web.post_form.side_effect = asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await self.search(provider)

    async def test_redirect_outside_service_is_rejected(self):
        provider = self.provider(card())
        self.web.post_form.return_value = TextResponse(200, "https://other.test/", card())
        with self.assertRaisesRegex(ProviderError, "unrecognized page"):
            await self.search(provider)

    def test_anonymous_requires_web_client(self):
        with self.assertRaisesRegex(ValueError, "web HTTP client"):
            SauceNaoProvider(None)
