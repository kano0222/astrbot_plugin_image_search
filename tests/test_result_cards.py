from __future__ import annotations

import unittest

from core.models import AnimeMatch, SearchResponse, SourceMatch
from presentation import build_result_cards, format_search_response


class ResultCardTests(unittest.TestCase):
    def test_grouped_card_keeps_all_channels_and_thumbnail_candidates(self):
        response = SearchResponse(matches=[
            SourceMatch(provider="saucenao", title="Artwork", similarity=94,
                        source_url="https://site.test/source", url="https://site.test/mirror",
                        thumbnail_url="https://images.test/a.jpg"),
            SourceMatch(provider="google_lens", title="Mirror",
                        url="https://site.test/mirror",
                        thumbnail_url="https://images.test/b.jpg"),
        ])
        cards = build_result_cards(response)
        self.assertEqual(len(cards), 1)
        self.assertIn("SauceNAO (94.00%) / Google Lens", cards[0].text)
        self.assertIn("SauceNAO (94.00%)", cards[0].text)
        self.assertEqual(cards[0].preview_urls,
                         ("https://images.test/a.jpg", "https://images.test/b.jpg"))
        self.assertEqual(len(response.matches), 2)

    def test_notes_and_partial_failures_have_a_separate_text_node(self):
        response = SearchResponse(
            matches=[SourceMatch(provider="google_lens", title="Art")],
            attempted_providers=["google_lens", "ascii2d"],
            failed_providers=["ascii2d"], failure_details={"ascii2d": "HTTP 403"},
            notices=["123 不可用", "已使用输入的可行渠道进行搜索"],
        )
        cards = build_result_cards(response)
        self.assertEqual(len(cards), 2)
        self.assertIn("123 不可用", cards[1].text)
        self.assertIn("已使用输入的可行渠道", cards[1].text)
        self.assertIn("部分搜索服务失败：Ascii2d", cards[1].text)
        self.assertIn("HTTP 403", cards[1].text)
        self.assertFalse(cards[1].preview_urls)

    def test_duplicate_thumbnail_candidates_are_removed(self):
        matches = [SourceMatch(provider=provider, title="Art",
                               url="https://site.test/art",
                               thumbnail_url="https://images.test/a.jpg")
                   for provider in ("saucenao", "google_lens")]
        self.assertEqual(build_result_cards(SearchResponse(matches=matches))[0].preview_urls,
                         ("https://images.test/a.jpg",))

    def test_adult_anime_preview_is_not_exposed(self):
        response = SearchResponse(matches=[
            AnimeMatch(provider="trace_moe", title="Adult", adult=True,
                       preview_url="https://images.test/adult.jpg"),
            AnimeMatch(provider="trace_moe", title="Normal", episode="2",
                       preview_url="https://images.test/normal.jpg"),
        ])
        cards = build_result_cards(response)
        self.assertFalse(cards[0].preview_urls)
        self.assertNotIn("adult.jpg", cards[0].text)
        self.assertEqual(cards[1].preview_urls, ("https://images.test/normal.jpg",))
        self.assertIn("第 2 集", cards[1].text)

    def test_empty_response_uses_existing_plain_status(self):
        response = SearchResponse(attempted_providers=["google_lens"],
                                  failed_providers=["google_lens"])
        cards = build_result_cards(response)
        self.assertEqual(cards[0].text, format_search_response(response))
        self.assertFalse(cards[0].preview_urls)

    def test_source_card_has_no_heading_and_image_precedes_links(self):
        cards = build_result_cards(SearchResponse(matches=[
            SourceMatch(provider="saucenao", title="Art", author="Artist", similarity=90,
                        url="https://site.test/a", thumbnail_url="https://images.test/a"),
        ]))
        self.assertEqual(cards[0].image_after_lines, 2)
        self.assertNotIn("图片来源", cards[0].text)
        self.assertTrue(cards[0].text.startswith("1. SauceNAO (90.00%)"))

    def test_google_only_card_has_no_source_heading(self):
        cards = build_result_cards(SearchResponse(matches=[
            SourceMatch(provider="google_lens", title="Art", url="https://site.test/a"),
        ]))
        self.assertNotIn("图片来源", cards[0].text)
        self.assertEqual(cards[0].image_after_lines, 2)

    def test_google_thumbnail_is_inserted_between_title_and_links(self):
        match = SourceMatch(provider="google_lens", title="Art", site_name="Example",
                            url="https://site.test/a", thumbnail_url="https://images.test/a")
        card = build_result_cards(SearchResponse(matches=[match]))[0]
        self.assertEqual(card.text, "1. Google Lens\nArt\nhttps://site.test/a")
        self.assertEqual(card.image_after_lines, 2)
        self.assertEqual(card.preview_urls, ("https://images.test/a",))
