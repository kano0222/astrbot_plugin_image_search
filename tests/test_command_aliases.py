from __future__ import annotations

import unittest

from command_aliases import build_channel_aliases


class ChannelAliasTests(unittest.TestCase):
    def test_refresh_keyword_is_reserved(self):
        aliases, invalid = build_channel_aliases({"saucenao": {"aliases": ["-purge"]}})
        self.assertNotIn("-purge", aliases)
        self.assertEqual(invalid, 1)

    def test_custom_aliases_and_builtin_targets(self):
        aliases, invalid = build_channel_aliases(
            {
                "google_lens": {"aliases": ["谷歌"]},
                "saucenao": {"aliases": ["酱"]},
                "ascii2d": {"aliases": ["A2D"]},
            }
        )
        self.assertEqual(invalid, 0)
        self.assertEqual(aliases["谷歌"], "google_lens")
        self.assertEqual(aliases["酱"], "saucenao")
        self.assertEqual(aliases["a2d"], "ascii2d")
        self.assertEqual(aliases["lens"], "google_lens")

    def test_invalid_entries_are_ignored(self):
        aliases, invalid = build_channel_aliases(
            {
                "google_lens": {
                    "aliases": ["", "=google", "x=unknown", "two words", "x=y=z", 123]
                }
            }
        )
        self.assertEqual(invalid, 6)
        self.assertNotIn("x", aliases)

    def test_builtin_and_canonical_names_cannot_be_overridden(self):
        aliases, invalid = build_channel_aliases(
            {"ascii2d": {"aliases": ["google", "saucenao"]}}
        )
        self.assertEqual(invalid, 2)
        self.assertEqual(aliases["google"], "google_lens")
        self.assertNotIn("saucenao", aliases)

    def test_conflicting_duplicate_keeps_first_mapping(self):
        aliases, invalid = build_channel_aliases(
            {
                "google_lens": {"aliases": ["foo", "foo"]},
                "ascii2d": {"aliases": ["FOO"]},
            }
        )
        self.assertEqual(invalid, 1)
        self.assertEqual(aliases["foo"], "google_lens")

    def test_empty_default_and_malformed_container(self):
        aliases, invalid = build_channel_aliases({})
        self.assertEqual(invalid, 0)
        self.assertEqual(aliases["g"], "google_lens")
        self.assertEqual(aliases["谷歌"], "google_lens")
        self.assertEqual(aliases["s"], "saucenao")
        self.assertEqual(aliases["a"], "ascii2d")
        self.assertEqual(build_channel_aliases(None)[1], 0)
        self.assertEqual(build_channel_aliases({"google_lens": {"aliases": "g"}})[1], 1)

    def test_explicit_empty_list_removes_presets(self):
        aliases, invalid = build_channel_aliases({"google_lens": {"aliases": []}})
        self.assertEqual(invalid, 0)
        self.assertNotIn("g", aliases)
        self.assertNotIn("谷歌", aliases)
        self.assertEqual(aliases["google"], "google_lens")
