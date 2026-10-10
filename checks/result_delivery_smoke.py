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


def response():
    return SearchResponse(matches=[
        SourceMatch(provider="saucenao", title="Art", similarity=94,
                    source_url="https://site.test/source", url="https://site.test/mirror",
                    thumbnail_url="https://images.test/a.png"),
        SourceMatch(provider="google_lens", title="Mirror", url="https://site.test/mirror",
                    thumbnail_url="https://images.test/b.png"),
        SourceMatch(provider="google_lens", title="Other", url="https://site.test/other",
                    thumbnail_url="https://images.test/other.png"),
    ])


class DeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.delivery = ResultDelivery(merged_forward_threshold=1)
        self.delivery._images = SimpleNamespace(load=AsyncMock(return_value=PNG),
                                                close=AsyncMock())

    async def asyncTearDown(self):
        await self.delivery.close()

    async def test_threshold_zero_one_two_and_ten(self):
        for threshold, count, forward in (
            (0, 4, False), (1, 1, True), (2, 2, False), (2, 3, True),
            (10, 10, False), (10, 11, True),
        ):
            self.delivery._merged_forward_threshold = threshold
            value = SearchResponse(matches=[
                SourceMatch(provider="google_lens", title=str(i), url=f"https://example.org/{i}",
                            thumbnail_url="https://images.test/shared")
                for i in range(count)
            ])
            target = event()
            self.assertTrue(await self.delivery.try_send(target, value))
            actual = isinstance(target.send.await_args.args[0].chain[0], Nodes)
            self.assertEqual(actual, forward, (threshold, count))

    async def test_threshold_counts_groups_not_raw_matches_or_notices(self):
        self.delivery._merged_forward_threshold = 2
        value = response()  # Three raw matches, two grouped results.
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

    async def test_all_thresholds_have_strict_exceeds_boundary(self):
        self.delivery._show_thumbnails = False
        for threshold in range(2, 11):
            self.delivery._merged_forward_threshold = threshold
            for count in (threshold, threshold + 1):
                value = SearchResponse(matches=[
                    SourceMatch(provider="google_lens", title=str(i), url=f"https://site.test/{i}")
                    for i in range(count)
                ])
                target = event()
                sent = await self.delivery.try_send(target, value)
                self.assertEqual(sent, count > threshold)
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
        self.assertEqual(len(message.chain[0].nodes), 2)
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
            self.assertEqual(content[0].text, "1. Google Lens\nArt")
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
        self.assertEqual(content[0].text, "1. Google Lens\nArt\nhttps://site.test/a")

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
            self.assertEqual(len(bot.call_action.await_args.kwargs["messages"]), 2)

    async def test_missing_images_keep_text_forward(self):
        self.delivery._images.load.return_value = None
        target = event()
        self.assertTrue(await self.delivery.try_send(target, response()))
        nodes = target.send.await_args.args[0].chain[0].nodes
        self.assertTrue(all(len(node.content) == 1 for node in nodes))
        self.assertIn("https://site.test/source", nodes[0].content[0].text)

    async def test_image_send_failure_retries_text_forward(self):
        target = event(send=AsyncMock(side_effect=[RuntimeError("image"), None]))
        self.assertTrue(await self.delivery.try_send(target, response()))
        self.assertEqual(target.send.await_count, 2)
        nodes = target.send.await_args.args[0].chain[0].nodes
        self.assertTrue(all(len(node.content) == 1 for node in nodes))

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
        self.assertTrue(await self.delivery.try_send(target, response()))
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
        self.assertFalse(await self.delivery.try_send(target, response()))
        self.assertFalse(await self.delivery.try_send(target, SearchResponse()))
        target.send.assert_not_awaited()
        self.delivery._images.load.assert_not_awaited()

    async def test_image_limit_does_not_remove_text_results(self):
        self.delivery._max_images = 1
        target = event()
        await self.delivery.try_send(target, response())
        nodes = target.send.await_args.args[0].chain[0].nodes
        self.assertEqual(len(nodes), 2)
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
        self.assertTrue(all(len(node.content) == 1 for node in nodes))

    async def test_notices_and_errors_are_retained_in_forward(self):
        value = response()
        value.attempted_providers = ["saucenao", "google_lens", "ascii2d"]
        value.failed_providers = ["ascii2d"]
        value.failure_details = {"ascii2d": "HTTP 403"}
        value.notices = ["123 不可用", "已使用输入的可行渠道进行搜索"]
        target = event()
        await self.delivery.try_send(target, value)
        last = target.send.await_args.args[0].chain[0].nodes[-1]
        self.assertEqual(len(last.content), 1)
        self.assertIn("123 不可用", last.content[0].text)
        self.assertIn("HTTP 403", last.content[0].text)


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
