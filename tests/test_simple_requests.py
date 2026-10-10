import unittest

from command_aliases import build_channel_aliases
from simple_requests import parse_simple_request, parse_tool_channels


class SimpleRequestTests(unittest.TestCase):
    def setUp(self):
        self.aliases, _ = build_channel_aliases({"saucenao": {"aliases": ["s", "酱"]}})

    def parse(self, text):
        return parse_simple_request(text, self.aliases)

    def test_explicit_whole_message_requests(self):
        for text in ("搜图", "帮我搜图", "请帮我找下出处", "找来源", "找原图",
                     "麻烦你找一下这张图的来源。", "帮我找下出处！",
                     "查一下图片的出处", "反向搜图", "搜索图片"):
            with self.subTest(text=text):
                request = self.parse(text)
                self.assertIsNotNone(request)
                self.assertFalse(request.requires_image)
                self.assertEqual(request.channels, "")

    def test_contextual_requests_require_image(self):
        for text in ("搜一下", "帮我搜一下", "请搜下"):
            with self.subTest(text=text):
                self.assertTrue(self.parse(text).requires_image)

    def test_channel_aliases_case_and_invalid_numeric_preserved(self):
        for text, expected in (
            ("帮我找下出处 g s 123", "g s 123"),
            ("找原图 谷歌，酱", "谷歌，酱"),
            ("搜图 SauceNAO", "SauceNAO"),
            ("搜一下 google_lens", "google_lens"),
        ):
            with self.subTest(text=text):
                self.assertEqual(self.parse(text).channels, expected)

    def test_discussion_negations_complex_tasks_and_quotes_do_not_route(self):
        for text in (
            "不要搜图", "不用找出处", "你会搜图吗", "搜图怎么用",
            "我觉得搜图可能有问题", "能不能解释一下搜图原理",
            "帮我找出处然后分析画风", "帮我找下出处并总结一下",
            "搜图 and explain the style", "搜图，然后说说作者",
            "“帮我找下出处”", '"搜图"', "搜图\n顺便解释一下",
            "这个出处是什么意思", "图片很好看", "帮我找下出处 xyz",
            "/搜图", "/搜图 g s", "/new", "",
        ):
            with self.subTest(text=text):
                self.assertIsNone(self.parse(text))

    def test_custom_alias_only_is_accepted(self):
        self.assertIsNotNone(self.parse("找出处 酱"))
        self.assertIsNone(self.parse("找出处 分析"))
        self.assertIsNotNone(self.parse("找出处 谷歌，，酱"))

    def test_size_bound(self):
        self.assertIsNone(self.parse("搜图 " + "1 " * 200))

    def test_tool_exact_attached_channels_and_conjunction(self):
        for text, expected in (
            ("搜一下谷歌", "谷歌"),
            ("搜一下谷歌和SauceNAO", "谷歌 SauceNAO"),
            ("搜索一下google", "google"),
            ("请帮我搜下酱和g", "酱 g"),
            ("搜一下 谷歌 和 SauceNAO", "谷歌 SauceNAO"),
            ("找出处 g s 123", "g s 123"),
        ):
            with self.subTest(text=text):
                self.assertEqual(parse_tool_channels(text, self.aliases), expected)

    def test_tool_does_not_infer_channels_from_mentions_or_mixed_intents(self):
        for text in ("不要搜一下谷歌", "搜一下谷歌并分析画风", "谷歌不是来源",
                     '“搜一下谷歌”', "搜一下谷歌谢谢喵", "搜一下gogle", "找出处",
                     "搜一下谷歌\n顺便分析", "搜图 " + "1 " * 200):
            with self.subTest(text=text):
                self.assertIsNone(parse_tool_channels(text, self.aliases))

    def test_tool_binding_does_not_expand_direct_route(self):
        self.assertIsNone(self.parse("搜一下谷歌"))
        self.assertIsNone(self.parse("搜一下谷歌和SauceNAO"))
        self.assertEqual(self.parse("搜一下 g s").channels, "g s")
