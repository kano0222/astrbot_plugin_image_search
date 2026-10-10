from __future__ import annotations

import unittest

from core.providers.saucenao import SauceNaoProvider


class SauceMetadataTests(unittest.TestCase):
    def parse(self, data):
        provider = SauceNaoProvider(None, api_key="test")
        result = provider._parse_match({
            "header": {"similarity": "95", "index_id": 5},
            "data": data,
        })
        self.assertIsNotNone(result)
        return result[0]

    def test_member_name_and_id_provide_pixiv_author(self):
        match = self.parse({
            "title": "Art", "member_name": "Artist", "member_id": 456,
            "author": "Other", "ext_urls": ["https://www.pixiv.net/artworks/123"],
        })
        self.assertEqual(match.author, "Artist")
        self.assertEqual(match.author_url, "https://pixiv.net/u/456")

    def test_nonpixiv_link_does_not_invent_pixiv_author_home(self):
        match = self.parse({
            "title": "Art", "artist": ["Artist", "Other"], "member_id": 456,
            "ext_urls": ["https://yande.re/post/show/123"],
        })
        self.assertEqual(match.author, "Artist, Other")
        self.assertFalse(match.author_url)

    def test_explicit_author_home_is_retained(self):
        match = self.parse({
            "title": "Art", "author": "Artist", "member_id": 456,
            "author_url": "https://artist.test",
            "ext_urls": ["https://www.pixiv.net/artworks/123"],
        })
        self.assertEqual(match.author, "Artist")
        self.assertEqual(match.author_url, "https://artist.test")

    def test_invalid_member_ids_do_not_create_author_home(self):
        for identifier in (-1, 0, True, "bad"):
            match = self.parse({
                "title": "Art", "member_id": identifier,
                "ext_urls": ["https://www.pixiv.net/artworks/123"],
            })
            self.assertFalse(match.author_url)
