from __future__ import annotations

import unittest

from core.models import SourceMatch
from core.source_results import group_source_matches, source_link_key


class SourceLinkTests(unittest.TestCase):
    def test_modern_pixiv_forms_and_tracking(self):
        expected = "https://www.pixiv.net/artworks/123"
        for url in (
            expected, "http://pixiv.net/i/123",
            "https://www.pixiv.net/en/artworks/123/",
            "https://WWW.PIXIV.NET:443/artworks/123?utm_source=test",
        ):
            with self.subTest(url=url):
                self.assertEqual(source_link_key(url), expected)

    def test_different_pixiv_works_stay_separate(self):
        self.assertNotEqual(source_link_key("https://pixiv.net/i/123"),
                            source_link_key("https://pixiv.net/i/124"))

    def test_only_tracking_parameters_are_removed(self):
        self.assertEqual(
            source_link_key("https://Example.com:443/p?utm_source=x&id=2&fbclid=a"),
            "https://example.com/p?id=2",
        )

    def test_meaningful_query_parameters_stay_distinct(self):
        self.assertNotEqual(source_link_key("https://site.test/p?id=1"),
                            source_link_key("https://site.test/p?id=2"))

    def test_fragments_stay_distinct(self):
        self.assertNotEqual(source_link_key("https://site.test/#/works/1"),
                            source_link_key("https://site.test/#/works/2"))

    def test_path_case_and_trailing_slash_stay_distinct(self):
        for first, second in (("/Work", "/work"), ("/work", "/work/")):
            self.assertNotEqual(source_link_key("https://site.test" + first),
                                source_link_key("https://site.test" + second))

    def test_query_order_and_escaping_are_preserved(self):
        url = "https://site.test/p?b=%2F&a=1&a=2"
        self.assertEqual(source_link_key(url), url)

    def test_unknown_domains_and_protocols_are_not_conflated(self):
        for first, second in (
            ("https://www.site.test/p", "https://site.test/p"),
            ("http://site.test/p", "https://site.test/p"),
            ("https://site.test:444/p", "https://site.test/p"),
        ):
            self.assertNotEqual(source_link_key(first), source_link_key(second))

    def test_invalid_and_credential_urls_are_not_keys(self):
        for url in ("", "/image", "file:///tmp/image", "https://[broken",
                    "https://site.test:bad/p", "https://user:pass@site.test/p"):
            with self.subTest(url=url):
                self.assertEqual(source_link_key(url), "")

    def test_ipv6_hosts_remain_valid(self):
        self.assertEqual(source_link_key("https://[::1]:443/p"), "https://[::1]/p")

    def test_pixiv_unknown_view_parameters_and_fragments_are_preserved(self):
        self.assertNotEqual(source_link_key("https://pixiv.net/i/123?page=1"),
                            source_link_key("https://pixiv.net/i/123?page=2"))
        self.assertNotEqual(source_link_key("https://pixiv.net/i/123#p1"),
                            source_link_key("https://pixiv.net/i/123#p2"))


class SourceGroupingTests(unittest.TestCase):
    def test_groups_preferred_source_with_lens_link(self):
        first = SourceMatch(provider="saucenao", title="Art",
                            source_url="https://www.pixiv.net/artworks/123",
                            url="https://danbooru.donmai.us/posts/1")
        second = SourceMatch(provider="google_lens", title="Art page",
                             url="https://pixiv.net/i/123?utm_source=lens")
        self.assertEqual(group_source_matches([first, second]), [[first, second]])

    def test_different_sources_with_shared_mirror_are_not_merged(self):
        matches = [SourceMatch(provider="saucenao", title="Same title",
                               source_url=f"https://pixiv.net/artworks/{identifier}",
                               url="https://site.test/collection")
                   for identifier in (123, 124)]
        self.assertEqual(len(group_source_matches(matches)), 2)

    def test_title_similarity_and_missing_links_do_not_merge(self):
        matches = [SourceMatch(provider=provider, title="Same title")
                   for provider in ("saucenao", "google_lens")]
        self.assertEqual(len(group_source_matches(matches)), 2)

    def test_same_title_different_links_do_not_merge(self):
        matches = [SourceMatch(provider="google_lens", title="Same",
                               url=f"https://site.test/work/{identifier}")
                   for identifier in (1, 2)]
        self.assertEqual(len(group_source_matches(matches)), 2)

    def test_group_order_and_original_matches_are_preserved(self):
        first = SourceMatch(provider="google_lens", title="First",
                            url="https://site.test/a", similarity=12)
        second = SourceMatch(provider="saucenao", title="Second",
                             url="https://site.test/b", similarity=95)
        third = SourceMatch(provider="saucenao", title="Third",
                            url="https://site.test/a", similarity=99)
        original = [first, second, third]
        groups = group_source_matches(original)
        self.assertEqual(groups, [[first, third], [second]])
        self.assertEqual(original, [first, second, third])
        self.assertIs(groups[0][0], first)

    def test_invalid_source_falls_back_to_valid_result_link(self):
        matches = [SourceMatch(provider="saucenao", title="Art", source_url="/bad",
                               url="https://site.test/a"),
                   SourceMatch(provider="google_lens", title="Art",
                               url="https://site.test/a")]
        self.assertEqual(len(group_source_matches(matches)), 1)

    def test_shared_mirror_merges_in_either_order(self):
        source = SourceMatch(provider="saucenao", title="Art",
                             source_url="https://www.fanbox.cc/@artist/posts/12730887",
                             url="https://yande.re/post/show/1270151")
        mirror = SourceMatch(provider="google_lens", title="Mirror",
                             url="https://yande.re/post/show/1270151")
        other = SourceMatch(provider="google_lens", title="Other revision",
                            url="https://yande.re/post/show/1270150")
        for matches in ([source, mirror, other], [mirror, other, source]):
            with self.subTest(order=matches):
                groups = group_source_matches(matches)
                self.assertEqual(groups, [[source, mirror] if matches[0] is source else [mirror, source], [other]])
                self.assertEqual(len(matches), 3)

    def test_ambiguous_shared_mirror_does_not_attach(self):
        first = SourceMatch(provider="saucenao", title="Art 1",
                            source_url="https://site.test/source/1",
                            url="https://site.test/collection")
        second = SourceMatch(provider="saucenao", title="Art 2",
                             source_url="https://site.test/source/2",
                             url="https://site.test/collection")
        mirror = SourceMatch(provider="google_lens", title="Collection",
                             url="https://site.test/collection")
        for matches in ([first, mirror, second], [mirror, second, first]):
            self.assertEqual(group_source_matches(matches), [[match] for match in matches])

    def test_explicit_source_identity_is_not_overridden_by_alias(self):
        first = SourceMatch(provider="saucenao", title="First",
                            source_url="https://site.test/first",
                            url="https://site.test/second")
        second = SourceMatch(provider="saucenao", title="Second",
                             source_url="https://site.test/second",
                             url="https://site.test/mirror")
        mirror = SourceMatch(provider="google_lens", title="Mirror",
                             url="https://site.test/mirror")
        self.assertEqual(group_source_matches([first, second, mirror]),
                         [[first], [second, mirror]])

    def test_multiple_mirrors_of_one_source_merge_without_score_changes(self):
        first = SourceMatch(provider="saucenao", title="Art", similarity=94.39,
                            source_url="https://site.test/source",
                            url="https://site.test/mirror/1")
        second = SourceMatch(provider="saucenao", title="Art", similarity=90,
                             source_url="https://site.test/source",
                             url="https://site.test/mirror/2")
        mirror = SourceMatch(provider="google_lens", title="Mirror",
                             url="https://site.test/mirror/2?utm_source=test")
        self.assertEqual(group_source_matches([first, second, mirror]),
                         [[first, second, mirror]])
        self.assertEqual((first.similarity, second.similarity), (94.39, 90))
