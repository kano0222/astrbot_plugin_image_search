from __future__ import annotations

import unittest

from core.models import AnimeMatch, SearchResponse, SourceMatch
from presentation import format_search_response


class PresentationTests(unittest.TestCase):
    def test_empty_results_distinguish_filtered_candidates_and_no_match(self):
        text = format_search_response(SearchResponse(
            attempted_providers=["saucenao", "google_lens"],
            filtered_providers={"saucenao": 60},
        ))
        self.assertEqual(text, "SauceNAO 候选相似度低于 60%，未展示。\n\nGoogle Lens 未找到匹配结果。")
        self.assertNotIn("更清晰", text)
        self.assertNotIn("仅供参考", text)

    def test_successful_followup_does_not_add_filtered_candidate_notice(self):
        text = format_search_response(SearchResponse(
            matches=[SourceMatch(provider="google_lens", title="Artwork", url="https://site.test/art")],
            attempted_providers=["saucenao", "google_lens"],
            filtered_providers={"saucenao": 60},
        ))
        self.assertIn("Artwork", text)
        self.assertNotIn("SauceNAO", text)
        self.assertNotIn("未展示", text)

    def test_filtered_candidate_and_service_failure_are_separate(self):
        text = format_search_response(SearchResponse(
            attempted_providers=["saucenao", "google_lens"],
            filtered_providers={"saucenao": 75},
            failed_providers=["google_lens"],
            failure_details={"google_lens": "搜索请求超时"},
        ))
        self.assertIn("低于 75%，未展示", text)
        self.assertIn("Google Lens：搜索请求超时", text)
        self.assertNotIn("Google Lens 未找到匹配结果", text)

    def test_empty_candidates_are_not_reported_as_low_similarity(self):
        self.assertEqual(format_search_response(SearchResponse(
            attempted_providers=["saucenao"],
        )), "SauceNAO 未找到匹配结果。")

    def test_groups_source_and_anime_results(self) -> None:
        response = SearchResponse(
            matches=[
                SourceMatch(
                    provider="google_lens",
                    title="Source",
                    site_name="Example",
                    url="https://example.com/source",
                ),
                AnimeMatch(
                    provider="trace_moe",
                    title="Anime",
                    episode="2",
                    timestamp_start=65,
                    timestamp_end=67,
                    similarity=92.5,
                    preview_url="https://example.com/preview.jpg",
                ),
            ],
            attempted_providers=["google_lens", "trace_moe"],
        )

        text = format_search_response(response)

        self.assertNotIn("图片来源", text)
        self.assertIn("1. Google Lens\nSource", text)
        self.assertIn("1. [trace.moe] Anime", text)
        self.assertIn("动画场景", text)
        self.assertIn("01:05–01:07", text)
        self.assertIn("https://example.com/source", text)

    def test_shows_source_page_and_safe_failure_detail(self) -> None:
        response = SearchResponse(
            matches=[
                SourceMatch(
                    provider="saucenao",
                    title="未提供标题",
                    source_url="https://www.pixiv.net/artworks/144087390",
                    url="https://danbooru.donmai.us/posts/1",
                )
            ],
            attempted_providers=["saucenao", "ascii2d"],
            failed_providers=["ascii2d"],
            failure_details={"ascii2d": "提交搜索返回 HTTP 403，访问受限"},
        )
        text = format_search_response(response)
        self.assertIn("https://www.pixiv.net/artworks/144087390", text)
        self.assertIn("https://danbooru.donmai.us/posts/1", text)
        self.assertIn("HTTP 403", text)

    def test_mixed_source_results_keep_each_channel(self) -> None:
        response = SearchResponse(
            matches=[
                SourceMatch(provider="saucenao", title="Illustration"),
                SourceMatch(provider="ascii2d", title="Color match"),
                SourceMatch(provider="google_lens", title="Web match"),
                SourceMatch(provider="custom", title="Extra"),
            ]
        )
        text = format_search_response(response)
        self.assertIn("1. SauceNAO\n「Illustration」", text)
        self.assertIn("2. [Ascii2d] Color match", text)
        self.assertIn("3. Google Lens\nWeb match", text)
        self.assertIn("4. [custom] Extra", text)

    def test_hides_adult_preview_url(self) -> None:
        response = SearchResponse(
            matches=[
                AnimeMatch(
                    provider="trace_moe",
                    title="Anime",
                    adult=True,
                    preview_url="https://example.com/adult.jpg",
                )
            ],
            attempted_providers=["trace_moe"],
        )

        text = format_search_response(response)

        self.assertIn("成人内容", text)
        self.assertNotIn("adult.jpg", text)

    def test_reports_partial_provider_failure(self) -> None:
        response = SearchResponse(
            matches=[SourceMatch(provider="google_lens", title="Source")],
            attempted_providers=["google_lens", "trace_moe"],
            failed_providers=["trace_moe"],
        )

        text = format_search_response(response)

        self.assertIn("部分搜索服务失败：trace.moe", text)


    def test_same_source_merges_channels_and_keeps_cqps_link_order(self):
        response = SearchResponse(matches=[
            SourceMatch(provider="saucenao", title="未提供标题", similarity=95.08,
                        source_url="https://www.pixiv.net/artworks/123",
                        url="https://danbooru.donmai.us/posts/1"),
            SourceMatch(provider="google_lens", title="Artwork",
                        author="Artist", url="https://pixiv.net/i/123"),
        ])
        text = format_search_response(response)
        self.assertIn("1. SauceNAO (95.08%) / Google Lens\n「Artwork」/「Artist」", text)
        self.assertIn("Artist", text)
        self.assertIn("SauceNAO (95.08%)", text)
        self.assertNotIn("2. [", text)
        self.assertNotIn("https://pixiv.net/i/123", text)
        self.assertLess(text.index("https://danbooru"), text.index("https://www.pixiv"))
        self.assertEqual(len(response.matches), 2)

    def test_merged_scores_are_not_compared_or_averaged(self):
        response = SearchResponse(matches=[
            SourceMatch(provider="saucenao", title="Artwork", similarity=80,
                        url="https://site.test/work"),
            SourceMatch(provider="google_lens", title="Web page", similarity=99,
                        url="https://site.test/work"),
        ])
        text = format_search_response(response)
        self.assertIn("SauceNAO (80.00%) / Google Lens (99.00%)\n「Artwork」", text)
        self.assertIn("SauceNAO (80.00%)", text)
        self.assertIn("Google Lens (99.00%)", text)
        self.assertNotIn("89.50%", text)

    def test_duplicates_within_one_channel_only_show_one_label_and_link(self):
        match = SourceMatch(provider="google_lens", title="Page",
                            url="https://site.test/work")
        text = format_search_response(SearchResponse(matches=[match, match]))
        self.assertEqual(text.count("Google Lens"), 1)
        self.assertEqual(text.count("https://site.test/work"), 1)

    def test_distinct_works_are_numbered_separately_after_merge(self):
        response = SearchResponse(matches=[
            SourceMatch(provider="saucenao", title="Same",
                        url="https://pixiv.net/artworks/123"),
            SourceMatch(provider="google_lens", title="Same",
                        url="https://pixiv.net/i/123"),
            SourceMatch(provider="google_lens", title="Same",
                        url="https://pixiv.net/artworks/124"),
        ])
        text = format_search_response(response)
        self.assertIn("1. SauceNAO / Google Lens\n「Same」", text)
        self.assertIn("2. Google Lens\nSame", text)

    def test_merge_preserves_anime_sections_notices_and_failures(self):
        response = SearchResponse(
            matches=[
                SourceMatch(provider="saucenao", title="Page", url="https://site.test/p"),
                SourceMatch(provider="google_lens", title="Page", url="https://site.test/p"),
                AnimeMatch(provider="trace_moe", title="Anime", episode="2"),
            ],
            attempted_providers=["saucenao", "google_lens", "ascii2d"],
            failed_providers=["ascii2d"],
            notices=["Low confidence notice"],
            failure_details={"ascii2d": "HTTP 403"},
        )
        text = format_search_response(response)
        self.assertIn("SauceNAO / Google Lens", text)
        self.assertIn("动画场景", text)
        self.assertIn("Low confidence notice", text)
        self.assertIn("Ascii2d：HTTP 403", text)

    def test_source_and_lens_mirror_merge_and_distinct_revision_stays(self):
        response = SearchResponse(matches=[
            SourceMatch(provider="saucenao", title="未提供标题", similarity=94.39,
                        author="Artist", source_url="https://www.fanbox.cc/@artist/posts/12730887",
                        url="https://yande.re/post/show/1270151"),
            SourceMatch(provider="google_lens", title="Other page",
                        url="https://danbooru.donmai.us/posts/4909331"),
            SourceMatch(provider="google_lens", title="Artwork",
                        url="https://yande.re/post/show/1270151"),
            SourceMatch(provider="google_lens", title="Revision",
                        url="https://yande.re/post/show/1270150"),
        ])
        text = format_search_response(response)
        self.assertIn("1. SauceNAO (94.39%) / Google Lens\n「Artwork」/「Artist」", text)
        self.assertIn("SauceNAO (94.39%)", text)
        self.assertEqual(text.count("https://yande.re/post/show/1270151"), 1)
        self.assertIn("https://www.fanbox.cc/@artist/posts/12730887", text)
        self.assertIn("2. Google Lens\nOther page", text)
        self.assertIn("3. Google Lens\nRevision", text)
        self.assertEqual(len(response.matches), 4)

    def test_saucenao_cqps_caption_and_link_order(self):
        match = SourceMatch(
            provider="saucenao", title="Artwork", author="Artist", similarity=95.08,
            site_name="Index #5: Pixiv - diagnostic.jpg",
            url="https://www.pixiv.net/artworks/123",
            author_url="https://pixiv.net/u/456",
            source_url="https://artist.test/post/789",
        )
        text = format_search_response(SearchResponse(matches=[match]))
        self.assertEqual(text, (
            "1. SauceNAO (95.08%)\n「Artwork」/「Artist」\n"
            "https://www.pixiv.net/artworks/123\n"
            "Author: https://pixiv.net/u/456\nhttps://artist.test/post/789"
        ))
        self.assertNotIn("Index #5", text)
        self.assertNotIn("图片来源", text)

    def test_saucenao_missing_title_does_not_print_placeholder(self):
        match = SourceMatch(provider="saucenao", title="未提供标题", author="Artist",
                            similarity=94.39, url="https://yande.re/post/show/1")
        text = format_search_response(SearchResponse(matches=[match]))
        self.assertIn("SauceNAO (94.39%)\n「Artist」", text)
        self.assertNotIn("未提供标题", text)
        self.assertNotIn("「」", text)

    def test_saucenao_missing_title_author_and_score_are_omitted(self):
        match = SourceMatch(provider="saucenao", title="未提供标题",
                            url="https://site.test/art", source_url="https://site.test/art")
        text = format_search_response(SearchResponse(matches=[match]))
        self.assertEqual(text, "1. SauceNAO\nhttps://site.test/art")

    def test_saucenao_format_survives_lens_first_order(self):
        matches = [
            SourceMatch(provider="google_lens", title="Web title",
                        url="https://site.test/art", similarity=99),
            SourceMatch(provider="saucenao", title="Original title", author="Artist",
                        url="https://site.test/art", similarity=80),
        ]
        text = format_search_response(SearchResponse(matches=matches))
        self.assertIn("Google Lens (99.00%) / SauceNAO (80.00%)", text)
        self.assertIn("「Original title」/「Artist」", text)
        self.assertNotIn("Web title", text)

    def test_google_compact_layout_omits_redundant_site_and_absent_fields(self):
        match = SourceMatch(provider="google_lens", title='Artist on X: "Autumn"',
                            site_name="X", url="https://x.com/artist/status/1")
        text = format_search_response(SearchResponse(matches=[match]))
        self.assertEqual(text, '1. Google Lens\nArtist on X: "Autumn"\n'
                               'https://x.com/artist/status/1')
        self.assertNotIn("Author:", text)
        self.assertNotIn("%", text)
        self.assertNotIn("[Google Lens]", text)

    def test_google_keeps_only_explicit_author_and_score(self):
        match = SourceMatch(provider="google_lens", title="Art", author="Artist",
                            similarity=90, url="https://site.test/a")
        text = format_search_response(SearchResponse(matches=[match]))
        self.assertIn("Google Lens (90.00%)", text)
        self.assertIn("「Art」/「Artist」", text)

    def test_google_missing_title_is_not_a_placeholder(self):
        match = SourceMatch(provider="google_lens", title="未提供标题",
                            url="https://site.test/a")
        self.assertEqual(format_search_response(SearchResponse(matches=[match])),
                         "1. Google Lens\nhttps://site.test/a")


if __name__ == "__main__":
    unittest.main()
