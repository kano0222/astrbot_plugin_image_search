"""Exercise rich delivery and real OneBot serialization without sending QQ messages."""
from __future__ import annotations

import asyncio
import base64
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from astrbot.api.event import MessageChain
from astrbot.core.message.components import Image, Nodes, Plain
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import AiocqhttpMessageEvent

from ..core.models import SearchResponse, SourceMatch
from ..main import ImageSearchPlugin
from ..result_delivery import ResultDelivery
from ..presentation import BatchResult

# A valid, one-pixel PNG, not a URL or a user image.
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aG0cAAAAASUVORK5CYII="
)


def event(platform="aiocqhttp", send=None):
    return SimpleNamespace(
        get_platform_name=lambda: platform, get_self_id=lambda: "12345",
        chain_result=lambda chain: MessageChain(chain),
        plain_result=lambda text: MessageChain([Plain(text)]),
        send=send or AsyncMock(),
    )


def response(google=True):
    return SearchResponse(matches=[
        SourceMatch(provider="saucenao", title="Art", similarity=94,
                    source_url="https://site.test/source", url="https://site.test/mirror",
                    thumbnail_url="https://images.test/a.png"),
        SourceMatch(provider="google_lens" if google else "ascii2d", title="Mirror", url="https://site.test/mirror",
                    thumbnail_url="https://images.test/b.png"),
        SourceMatch(provider="google_lens" if google else "ascii2d", title="Other", url="https://site.test/other",
                    thumbnail_url="https://images.test/other.png"),
    ])


class DeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_google_force_forward_overrides_threshold_on_qq_only(self):
        for threshold in (0, 2, 10):
            self.delivery._merged_forward_threshold = threshold
            for cached in (False, True):
                for platform in ("aiocqhttp", "telegram"):
                    target = event(platform=platform)
                    self.assertTrue(await self.delivery.try_send(target, response(), cached=cached))
                    self.assertEqual(isinstance(target.send.await_args.args[0].chain[0], Nodes),
                                     platform == "aiocqhttp")
        self.delivery._merged_forward_threshold = 0
        self.delivery._show_thumbnails = False
        target = event()
        self.assertTrue(await self.delivery.try_send_batch(target, [BatchResult(1, response()),
            BatchResult(2, response(), cached=True)]))
        target.send.assert_awaited_once()
        self.assertEqual(len(target.send.await_args.args[0].chain[0].nodes), 2)

    async def test_duplicate_google_matches_do_not_force_forward(self):
        self.delivery._merged_forward_threshold = 0
        one = SourceMatch("google_lens", "Art", url="https://site.test/one",
                          thumbnail_url="https://images.test/one")
        target = event()
        self.assertTrue(await self.delivery.try_send(target, SearchResponse(matches=[one, one])))
        self.assertFalse(isinstance(target.send.await_args.args[0].chain[0], Nodes))

    async def test_google_matches_share_one_message_per_input(self):
        self.delivery._merged_forward_threshold = 2
        value = SearchResponse(matches=[SourceMatch("google_lens", f"Art {i}",
            url=f"https://site.test/{i}", thumbnail_url=f"https://images.test/{i}") for i in range(6)])
        target = event()
        self.assertTrue(await self.delivery.try_send(target, value))
        nodes = target.send.await_args.args[0].chain[0].nodes
        self.assertEqual(len(nodes), 1)
        self.assertEqual(sum(isinstance(item, Image) for item in nodes[0].content), 6)
        target = event()
        self.assertTrue(await self.delivery.try_send_batch(target, [BatchResult(1, value, cached=True), BatchResult(2, value)]))
        target.send.assert_awaited_once()
        nodes = target.send.await_args.args[0].chain[0].nodes
        self.assertEqual(len(nodes), 2)
        for index, node in enumerate(nodes):
            text = "".join(item.text for item in node.content if isinstance(item, Plain))
            self.assertEqual(text.count("Google Lens"), 1)
            self.assertEqual(text.count("[缓存]"), 1 if index == 0 else 0)
            self.assertEqual(text.count(f"第 {index + 1} 张图片"), 1)
            for i in range(6):
                self.assertIn(f"{i + 1}. Art {i}", text)
                self.assertIn(f"https://site.test/{i}", text)
        self.assertEqual(sum(isinstance(item, Image) for node in nodes for item in node.content), 12)

    async def test_cached_batch_marks_only_cached_cards_in_both_delivery_modes(self):
        one = SearchResponse(matches=[SourceMatch("google_lens", "Art", url="https://site.test/1",
                                                 thumbnail_url="https://images.test/a")])
        for threshold in (0, 2):
            self.delivery._merged_forward_threshold = threshold
            target = event()
            await self.delivery.try_send_batch(target, [BatchResult(1, one, cached=True), BatchResult(2, one)])
            if threshold == 2:
                nodes = target.send.await_args.args[0].chain[0].nodes
                texts = [node.content[0].text for node in nodes]
                target.send.assert_awaited_once()
            else:
                texts = [call.args[0].chain[0].text for call in target.send.await_args_list]
                self.assertEqual(target.send.await_count, 2)
            self.assertTrue(texts[0].startswith("Google Lens[缓存]\nArt"))
            self.assertNotIn("[缓存]", texts[1])
            self.assertNotIn("重搜", "".join(texts))

    async def test_delivery_log_separates_results_cards_and_thumbnails(self):
        single = SearchResponse(matches=[SourceMatch("google_lens", "Art",
            thumbnail_url="https://images.test/shared")], notices=["诊断提示"])
        results = [BatchResult(1, single), BatchResult(2, single)]
        self.delivery._merged_forward_threshold = 3
        with patch(ResultDelivery.__module__ + ".logger.info") as log:
            self.assertTrue(await self.delivery.try_send_batch(event(), results, log_context="id=example"))
        message = log.call_args.args[0]
        self.assertIn("id=example", message)
        self.assertIn("类型=普通图文", message)
        self.assertIn("结果=2 普通消息=2 门槛=3 卡片=4 缩略图=2 消息=2", message)

    async def test_batch_threshold_counts_all_inputs_without_cross_image_merge(self):
        single = SearchResponse(matches=[SourceMatch(
            "google_lens", "Art", url="https://site.test/same",
            thumbnail_url="https://images.test/shared")])
        for threshold, count, forward in ((0, 3, False), (1, 1, True),
                                           (2, 1, True), (2, 2, True), (2, 3, True)):
            self.delivery._merged_forward_threshold = threshold
            target = event()
            results = [BatchResult(i + 1, single) for i in range(count)]
            results.append(BatchResult(count + 1, message="获取图片失败"))
            self.assertTrue(await self.delivery.try_send_batch(target, results))
            self.assertEqual(target.send.await_count, 1 if forward else count + 1)
            chain = target.send.await_args.args[0].chain
            self.assertEqual(isinstance(chain[0], Nodes), forward)
            if forward:
                self.assertEqual(len(chain[0].nodes), count + 1)

    async def test_batch_without_results_does_not_forward_errors(self):
        target = event()
        self.assertTrue(await self.delivery.try_send_batch(target, [BatchResult(1, message="失败")]))
        self.assertEqual(len(target.send.await_args.args[0].chain[0].nodes), 1)

    async def test_plain_batch_retries_only_failed_image_without_repeating_previous(self):
        self.delivery._merged_forward_threshold = 3
        first = SearchResponse(matches=[SourceMatch("google_lens", "First", thumbnail_url="https://images.test/a")])
        second = SearchResponse(matches=[SourceMatch("google_lens", "Second", thumbnail_url="https://images.test/b")])
        target = event(send=AsyncMock(side_effect=[None, RuntimeError("send failed"), None]))
        self.assertTrue(await self.delivery.try_send_batch(target, [BatchResult(1, first), BatchResult(2, second)]))
        self.assertEqual(target.send.await_count, 3)
        retry = target.send.await_args.args[0]
        self.assertIn("Second", retry.chain[0].text)
        self.assertNotIn("First", retry.chain[0].text)

    async def test_plain_card_separators_are_attached_to_visible_text(self):
        self.delivery._merged_forward_threshold = 0
        target = event(platform="telegram")
        await self.delivery.try_send(target, response())
        text_parts = [item.text for item in target.send.await_args.args[0].chain if isinstance(item, Plain)]
        self.assertTrue(all(text.strip() for text in text_parts))
        self.assertTrue(any(text.startswith("\n\nGoogle Lens\n1. Other") for text in text_parts))

    async def test_batch_has_independent_thumbnail_limits_and_preserves_heading(self):
        self.delivery._max_images = 1
        target = event()
        await self.delivery.try_send_batch(target, [BatchResult(1, response()), BatchResult(2, response())])
        nodes = target.send.await_args.args[0].chain[0].nodes
        self.assertEqual(len(nodes), 2)
        self.assertEqual(sum(isinstance(item, Image) for node in nodes for item in node.content), 2)
        self.assertTrue(nodes[0].content[0].text.startswith("第 1 张图片\n1. "))
        self.assertTrue(nodes[1].content[0].text.startswith("第 2 张图片\n1. "))

    async def asyncSetUp(self):
        self.delivery = ResultDelivery(merged_forward_threshold=1)
        self.delivery._images = SimpleNamespace(load=AsyncMock(return_value=PNG),
                                                close=AsyncMock())

    async def asyncTearDown(self):
        await self.delivery.close()

    async def test_threshold_zero_one_two_and_ten(self):
        for threshold, count, forward in (
            (0, 4, False), (1, 1, True), (2, 1, False), (2, 2, True), (2, 3, True),
            (10, 9, False), (10, 10, True), (10, 11, True),
        ):
            self.delivery._merged_forward_threshold = threshold
            value = SearchResponse(matches=[
                SourceMatch(provider="saucenao", title=str(i), url=f"https://example.org/{i}",
                            thumbnail_url="https://images.test/shared")
                for i in range(count)
            ])
            target = event()
            self.assertTrue(await self.delivery.try_send_batch(target, [BatchResult(i + 1, value) for i in range(count)]))
            actual = isinstance(target.send.await_args.args[0].chain[0], Nodes)
            self.assertEqual(actual, forward, (threshold, count))

    async def test_threshold_counts_groups_not_raw_matches_or_notices(self):
        self.delivery._merged_forward_threshold = 3
        value = response(google=False)  # Three raw matches, two grouped results.
        value.notices = ["Notice"]
        value.failed_providers = ["ascii2d"]
        value.attempted_providers = ["saucenao", "google_lens", "ascii2d"]
        value.failure_details = {"ascii2d": "403"}
        target = event()
        self.assertTrue(await self.delivery.try_send(target, value))
        self.assertFalse(any(isinstance(item, Nodes) for item in target.send.await_args.args[0].chain))
        text = "\n".join(item.text for item in target.send.await_args.args[0].chain if isinstance(item, Plain))
        self.assertIn("Notice", text)
        self.assertIn("Ascii2d", text)
        self.assertIn("403", text)

    async def test_all_thresholds_have_inclusive_boundary(self):
        self.delivery._show_thumbnails = False
        for threshold in range(2, 11):
            self.delivery._merged_forward_threshold = threshold
            for count in (threshold - 1, threshold, threshold + 1):
                value = SearchResponse(matches=[
                    SourceMatch(provider="saucenao", title=str(i), url=f"https://site.test/{i}")
                    for i in range(count)
                ])
                target = event()
                sent = await self.delivery.try_send_batch(target, [BatchResult(i + 1, value) for i in range(count)])
                self.assertEqual(sent, count >= threshold)
                if sent:
                    self.assertIsInstance(target.send.await_args.args[0].chain[0], Nodes)

    async def test_threshold_default_and_invalid_values(self):
        value = ResultDelivery()
        try:
            self.assertEqual(value._merged_forward_threshold, 2)
        finally:
            await value.close()
        for invalid in (-1, 11, True, 1.5):
            with self.assertRaises(ValueError):
                ResultDelivery(merged_forward_threshold=invalid)

    async def test_forward_node_grouping_and_onebot_serialization(self):
        target = event()
        self.assertTrue(await self.delivery.try_send(target, response()))
        message = target.send.await_args.args[0]
        self.assertIsInstance(message.chain[0], Nodes)
        self.assertEqual(len(message.chain[0].nodes), 1)
        payload = await message.chain[0].to_dict()
        first = payload["messages"][0]["data"]
        self.assertEqual(first["user_id"], "12345")
        self.assertEqual(first["nickname"], "搜图")
        self.assertIn("SauceNAO (94.00%) / Google Lens", first["content"][0]["data"]["text"])
        self.assertTrue(first["content"][1]["data"]["file"].startswith("base64://"))
        self.assertIn("https://site.test/source", first["content"][2]["data"]["text"])
        self.assertNotIn("图片来源", first["content"][0]["data"]["text"])

    async def test_google_node_places_thumbnail_before_links(self):
        value = SearchResponse(matches=[
            SourceMatch(provider="google_lens", title="Art", site_name="Example",
                        url="https://site.test/a", thumbnail_url="https://images.test/a"),
        ])
        for platform in ("aiocqhttp", "telegram"):
            target = event(platform=platform)
            self.assertTrue(await self.delivery.try_send(target, value))
            chain = target.send.await_args.args[0].chain
            content = chain[0].nodes[0].content if platform == "aiocqhttp" else chain
            self.assertEqual(content[0].text, "Google Lens\nArt")
            self.assertIsInstance(content[1], Image)
            self.assertEqual(content[2].text, "\nhttps://site.test/a")

    async def test_google_image_failure_keeps_full_text(self):
        self.delivery._images.load.return_value = None
        target = event()
        value = SearchResponse(matches=[
            SourceMatch(provider="google_lens", title="Art", url="https://site.test/a",
                        thumbnail_url="https://images.test/a"),
        ])
        self.assertTrue(await self.delivery.try_send(target, value))
        content = target.send.await_args.args[0].chain[0].nodes[0].content
        self.assertEqual(len(content), 1)
        self.assertEqual(content[0].text, "Google Lens\nArt\nhttps://site.test/a")

    async def test_real_adapter_routes_group_and_private_forward(self):
        for is_group, action in ((True, "send_group_forward_msg"),
                                 (False, "send_private_forward_msg")):
            target = event()
            await self.delivery.try_send(target, response())
            bot = SimpleNamespace(call_action=AsyncMock())
            await AiocqhttpMessageEvent.send_message(
                bot, target.send.await_args.args[0], is_group=is_group, session_id="999",
            )
            self.assertEqual(bot.call_action.await_args.args[0], action)
            key = "group_id" if is_group else "user_id"
            self.assertEqual(bot.call_action.await_args.kwargs[key], "999")
            self.assertEqual(len(bot.call_action.await_args.kwargs["messages"]), 1)

    async def test_missing_images_keep_text_forward(self):
        self.delivery._images.load.return_value = None
        target = event()
        self.assertTrue(await self.delivery.try_send(target, response()))
        nodes = target.send.await_args.args[0].chain[0].nodes
        self.assertTrue(all(isinstance(item, Plain) for node in nodes for item in node.content))
        self.assertIn("https://site.test/source", nodes[0].content[0].text)

    async def test_image_send_failure_retries_text_forward(self):
        target = event(send=AsyncMock(side_effect=[RuntimeError("image"), None]))
        self.assertTrue(await self.delivery.try_send(target, response()))
        self.assertEqual(target.send.await_count, 2)
        nodes = target.send.await_args.args[0].chain[0].nodes
        self.assertTrue(all(isinstance(item, Plain) for node in nodes for item in node.content))

    async def test_forward_failure_requests_plain_fallback(self):
        target = event(send=AsyncMock(side_effect=RuntimeError("unsupported")))
        self.assertFalse(await self.delivery.try_send(target, response()))
        self.assertEqual(target.send.await_count, 2)

    async def test_nonqq_uses_one_ordinary_image_text_chain(self):
        target = event(platform="telegram")
        self.assertTrue(await self.delivery.try_send(target, response()))
        chain = target.send.await_args.args[0].chain
        self.assertFalse(any(isinstance(item, Nodes) for item in chain))
        self.assertEqual(sum(isinstance(item, Image) for item in chain), 2)
        self.assertIn("SauceNAO (94.00%) / Google Lens",
                      "\n".join(item.text for item in chain if isinstance(item, Plain)))

    async def test_disabled_forward_still_sends_ordinary_images(self):
        self.delivery._merged_forward_threshold = 0
        target = event()
        self.assertTrue(await self.delivery.try_send(target, response(google=False)))
        self.assertFalse(any(isinstance(item, Nodes) for item in target.send.await_args.args[0].chain))

    async def test_disabled_images_do_not_download(self):
        self.delivery._show_thumbnails = False
        target = event()
        self.assertTrue(await self.delivery.try_send(target, response()))
        self.delivery._images.load.assert_not_awaited()
        self.assertFalse(any(isinstance(item, Image)
                             for node in target.send.await_args.args[0].chain[0].nodes
                             for item in node.content))

    async def test_all_disabled_or_empty_do_not_send(self):
        self.delivery._merged_forward_threshold = 0
        self.delivery._show_thumbnails = False
        target = event()
        self.assertFalse(await self.delivery.try_send(target, response(google=False)))
        self.assertFalse(await self.delivery.try_send(target, SearchResponse()))
        target.send.assert_not_awaited()
        self.delivery._images.load.assert_not_awaited()

    async def test_image_limit_does_not_remove_text_results(self):
        self.delivery._max_images = 1
        target = event()
        await self.delivery.try_send(target, response())
        nodes = target.send.await_args.args[0].chain[0].nodes
        self.assertEqual(len(nodes), 1)
        self.assertEqual(sum(isinstance(item, Image) for node in nodes for item in node.content), 1)
        self.delivery._images.load.assert_awaited_once()

    async def test_merged_thumbnail_failure_tries_next_candidate(self):
        self.delivery._images.load.side_effect = [None, PNG, PNG]
        target = event()
        await self.delivery.try_send(target, response())
        urls = [call.args[0] for call in self.delivery._images.load.await_args_list]
        self.assertIn("https://images.test/b.png", urls)
        self.assertIsInstance(target.send.await_args.args[0].chain[0].nodes[0].content[1], Image)

    async def test_download_cache_is_request_scoped(self):
        value = SearchResponse(matches=[
            SourceMatch(provider="google_lens", title=str(i), url=f"https://site.test/{i}",
                        thumbnail_url="https://images.test/shared.png") for i in (1, 2)
        ])
        target = event()
        await self.delivery.try_send(target, value)
        self.delivery._images.load.assert_awaited_once()
        await self.delivery.try_send(target, value)
        self.assertEqual(self.delivery._images.load.await_count, 2)

    async def test_deadline_cancels_downloads_and_preserves_text(self):
        cancelled = asyncio.Event()

        async def slow(url):
            try:
                await asyncio.Future()
            finally:
                cancelled.set()

        self.delivery._images.load.side_effect = slow
        original_wait = asyncio.wait

        async def short_wait(tasks, timeout):
            return await original_wait(tasks, timeout=0.01)

        target = event()
        with patch(__package__.rsplit(".", 1)[0] + ".result_delivery.asyncio.wait",
                   side_effect=short_wait):
            self.assertTrue(await self.delivery.try_send(target, response()))
        self.assertTrue(cancelled.is_set())
        nodes = target.send.await_args.args[0].chain[0].nodes
        self.assertTrue(all(isinstance(item, Plain) for node in nodes for item in node.content))

    async def test_notices_and_errors_are_retained_in_forward(self):
        value = response()
        value.attempted_providers = ["saucenao", "google_lens", "ascii2d"]
        value.failed_providers = ["ascii2d"]
        value.failure_details = {"ascii2d": "HTTP 403"}
        value.notices = ["123 不可用", "已使用输入的可行渠道进行搜索"]
        target = event()
        await self.delivery.try_send(target, value)
        last = target.send.await_args.args[0].chain[0].nodes[-1]
        text = "".join(item.text for item in last.content if isinstance(item, Plain))
        self.assertIn("123 不可用", text)
        self.assertIn("HTTP 403", text)


class PluginDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.plugin = ImageSearchPlugin(None, {
            "output": {"merged_forward_threshold": 1},
            "providers": {"google_lens": {"serpapi_keys": ["test"]},
                          "saucenao": {"api_key": "test"}},
        })
        self.plugin._get_image_url = AsyncMock(return_value=("https://images.test/query", None))
        self.plugin._service.search = AsyncMock(return_value=response())
        self.plugin._delivery._images = SimpleNamespace(
            load=AsyncMock(return_value=PNG), close=AsyncMock(),
        )

    async def asyncTearDown(self):
        await self.plugin.terminate()

    async def test_command_and_tool_share_rich_delivery_with_tool_feedback(self):
        target = event()
        outputs = [item async for item in self.plugin.search_image_command(target, "g s")]
        self.assertEqual(outputs, [])
        self.assertEqual(self.plugin._service.search.await_args.kwargs["provider_names"],
                         ["google_lens", "saucenao"])
        command = await target.send.await_args.args[0].chain[0].to_dict()
        result = await self.plugin.tool_search_image(target)
        feedback = json.loads(result)
        self.assertTrue(feedback["message_sent"])
        self.assertEqual(feedback["count"], 2)
        self.assertIn("当前会话的人格语气", feedback["instruction"])
        tool = await target.send.await_args.args[0].chain[0].to_dict()
        self.assertEqual(command, tool)
        self.assertEqual(target.send.await_count, 2)

    async def test_command_forward_failure_yields_plain_result(self):
        target = event(send=AsyncMock(side_effect=RuntimeError("blocked")))
        outputs = [item async for item in self.plugin.search_image_command(target, "g s")]
        self.assertEqual(len(outputs), 1)
        self.assertIn("SauceNAO (94.00%) / Google Lens", outputs[0].chain[0].text)

    async def test_tool_forward_failure_sends_plain_result_once(self):
        target = event(send=AsyncMock(side_effect=[RuntimeError(), RuntimeError(), None]))
        feedback = json.loads(await self.plugin.tool_search_image(target))
        self.assertTrue(feedback["message_sent"])
        self.assertEqual(feedback["count"], 2)
        self.assertEqual(target.send.await_count, 3)
        last = target.send.await_args.args[0].chain
        self.assertEqual(len(last), 1)
        self.assertIsInstance(last[0], Plain)
        self.assertIn("SauceNAO (94.00%) / Google Lens", last[0].text)


if __name__ == "__main__":
    unittest.main()
