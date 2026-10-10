"""Run against an installed AstrBot runtime: python -m <plugin>.checks.astrbot_smoke."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from astrbot.core.message.components import Image, Plain, Reply
from astrbot.core.provider.func_tool_manager import FuncTool, ToolSet

from ..core.image_wait import ImageResolution
from ..core.models import SearchResponse, SourceMatch
from ..main import ImageSearchPlugin


class RuntimeSmokeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.plugin = ImageSearchPlugin(
            None,
            {
                "providers": {
                    "google_lens": {"serpapi_keys": ["test-serp-key"]},
                    "saucenao": {"api_key": "test-sauce-key", "aliases": ["s", "酱"]},
                },
            },
        )

    async def asyncTearDown(self) -> None:
        await self.plugin.terminate()

    def _simple_event(self, text, components=()):
        state = {"stopped": False}
        def stop():
            state["stopped"] = True
        return SimpleNamespace(
            get_messages=lambda: [Plain(text), *components],
            stop_event=Mock(side_effect=stop),
            is_stopped=lambda: state["stopped"],
            send=AsyncMock(), plain_result=lambda text: text,
            unified_msg_origin="platform:group:test",
            get_sender_id=lambda: "sender", plugins_name=[],
        )

    async def test_simple_request_stops_model_and_shares_command_result(self):
        response = SearchResponse(matches=[SourceMatch(
            provider="google_lens", title="Artwork", url="https://example.org/art"
        )])
        self.plugin._service.search = AsyncMock(return_value=response)
        event = self._simple_event("帮我找下出处", [Image(file="https://example.org/image.jpg")])
        await self.plugin.route_simple_search(event, None)
        self.assertTrue(event.is_stopped())
        event.send.assert_awaited_once()
        self.assertIn("Artwork", event.send.await_args.args[0])
        self.assertEqual(self.plugin._service.search.await_args.args[0].url, "https://example.org/image.jpg")

    async def test_simple_request_channels_reuse_invalid_channel_notice(self):
        self.plugin._service.search = AsyncMock(return_value=SearchResponse())
        event = self._simple_event("帮我找下出处 g s 123", [Image(file="https://example.org/image.jpg")])
        await self.plugin.route_simple_search(event, None)
        self.assertEqual(event.send.await_count, 2)
        notice = event.send.await_args_list[0].args[0]
        self.assertIn("123", notice)
        self.assertIn("已使用输入的可行渠道", notice)
        self.assertEqual(self.plugin._service.search.await_args.kwargs["provider_names"],
                         ["google_lens", "saucenao"])

    async def test_simple_request_all_invalid_does_not_search(self):
        self.plugin._service.search = AsyncMock()
        event = self._simple_event("找出处 123")
        await self.plugin.route_simple_search(event, None)
        self.assertTrue(event.is_stopped())
        self.plugin._service.search.assert_not_awaited()
        event.send.assert_awaited_once()

    async def test_simple_request_quoted_image_and_current_image_priority(self):
        self.plugin._service.search = AsyncMock(return_value=SearchResponse())
        self.plugin._get_image_url = AsyncMock(return_value=("https://example.org/quoted.jpg", None))
        event = self._simple_event("帮我找下出处", [Reply(id="1")])
        await self.plugin.route_simple_search(event, None)
        self.assertEqual(self.plugin._service.search.await_args.args[0].url, "https://example.org/quoted.jpg")
        # Use the real resolver for the current + quoted image case.
        del self.plugin._get_image_url
        event = self._simple_event("找原图", [
            Reply(id="1", chain=[Image(file="https://example.org/quoted.jpg")]),
            Image(file="https://example.org/current.jpg"),
        ])
        await self.plugin.route_simple_search(event, None)
        self.assertEqual(self.plugin._service.search.await_args.args[0].url, "https://example.org/current.jpg")

    async def test_contextual_request_without_image_does_not_take_over(self):
        self.plugin._service.search = AsyncMock()
        event = self._simple_event("搜一下")
        await self.plugin.route_simple_search(event, None)
        self.assertFalse(event.is_stopped())
        event.send.assert_not_awaited()
        self.plugin._service.search.assert_not_awaited()

    async def test_contextual_request_resolves_local_image_only_once(self):
        self.plugin._service.search = AsyncMock(return_value=SearchResponse())
        event = self._simple_event("搜一下", [Image(file="base64://test")])
        with patch.object(Image, "register_to_file_service",
                          new=AsyncMock(return_value="https://example.org/published")) as publish:
            await self.plugin.route_simple_search(event, None)
            publish.assert_awaited_once()
        self.assertTrue(event.is_stopped())

    async def test_simple_request_without_image_reuses_wait(self):
        import asyncio
        self.plugin._service.search = AsyncMock(return_value=SearchResponse())
        event = self._simple_event("帮我找下出处")
        requested = asyncio.Event()
        async def send(result):
            if "请在" in result:
                requested.set()
        event.send.side_effect = send
        task = asyncio.create_task(self.plugin.route_simple_search(event, None))
        try:
            await asyncio.wait_for(requested.wait(), timeout=1)
            self.assertTrue(event.is_stopped())
            await self.plugin._image_wait.deliver(
                self.plugin._wait_key(event),
                ImageResolution(url="https://example.org/later.jpg"),
            )
            await asyncio.wait_for(task, timeout=1)
            self.assertEqual(self.plugin._service.search.await_args.args[0].url, "https://example.org/later.jpg")
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_complex_negated_and_quoted_text_is_not_taken_over(self):
        self.plugin._service.search = AsyncMock()
        for text in ("不要搜图", "你会搜图吗", "帮我找出处并分析画风", "/搜图 g"):
            event = self._simple_event(text, [Image(file="https://example.org/image.jpg")])
            await self.plugin.route_simple_search(event, None)
            self.assertFalse(event.is_stopped())
        event = self._simple_event("这张图很好看", [Reply(id="1", chain=[Plain("帮我找下出处")])])
        await self.plugin.route_simple_search(event, None)
        self.assertFalse(event.is_stopped())
        self.plugin._service.search.assert_not_awaited()

    async def test_simple_route_toggle_is_independent_of_llm_tool(self):
        self.plugin._llm_tool_enabled = False
        self.plugin._service.search = AsyncMock(return_value=SearchResponse())
        event = self._simple_event("找出处", [Image(file="https://example.org/image.jpg")])
        await self.plugin.route_simple_search(event, None)
        self.plugin._service.search.assert_awaited_once()
        self.plugin._simple_search_enabled = False
        event = self._simple_event("找出处", [Image(file="https://example.org/image.jpg")])
        await self.plugin.route_simple_search(event, None)
        self.assertFalse(event.is_stopped())
        event.send.assert_not_awaited()

    async def test_simple_route_rich_delivery_does_not_send_duplicate(self):
        self.plugin._service.search = AsyncMock(return_value=SearchResponse(
            matches=[SourceMatch(provider="google_lens", title="Artwork", url="https://example.org/art")]
        ))
        self.plugin._delivery.try_send = AsyncMock(return_value=True)
        event = self._simple_event("找原图", [Image(file="https://example.org/image.jpg")])
        await self.plugin.route_simple_search(event, None)
        self.plugin._delivery.try_send.assert_awaited_once()
        event.send.assert_not_awaited()
        self.assertTrue(event.is_stopped())

    async def test_simple_route_config_can_disable_it(self):
        plugin = ImageSearchPlugin(None, {"command": {"simple_natural_language": False}})
        try:
            plugin._service.search = AsyncMock()
            event = self._simple_event("找出处", [Image(file="https://example.org/image.jpg")])
            await plugin.route_simple_search(event, None)
            self.assertFalse(event.is_stopped())
            plugin._service.search.assert_not_awaited()
        finally:
            await plugin.terminate()

    async def test_stopped_event_is_not_taken_over(self):
        self.plugin._service.search = AsyncMock()
        event = self._simple_event("找出处", [Image(file="https://example.org/image.jpg")])
        event.stop_event()
        await self.plugin.route_simple_search(event, None)
        self.plugin._service.search.assert_not_awaited()
        event.send.assert_not_awaited()

    async def test_core_hook_skips_later_model_hooks_after_takeover(self):
        from astrbot.core.pipeline.context_utils import call_event_hook
        from astrbot.core.star.star_handler import EventType
        self.plugin._service.search = AsyncMock(return_value=SearchResponse())
        event = self._simple_event("帮我找下出处", [Image(file="https://example.org/image.jpg")])
        later = AsyncMock()
        handlers = [
            SimpleNamespace(handler=self.plugin.route_simple_search,
                            handler_module_path="test", handler_name="route"),
            SimpleNamespace(handler=later, handler_module_path="test", handler_name="later"),
        ]
        with patch("astrbot.core.pipeline.context_utils.star_handlers_registry.get_handlers_by_event_type",
                   return_value=handlers), patch(
                   "astrbot.core.pipeline.context_utils.star_map", {"test": SimpleNamespace(name="搜图")}):
            stopped = await call_event_hook(event, EventType.OnLLMRequestEvent, None)
        self.assertTrue(stopped)
        later.assert_not_awaited()

    async def test_low_similarity_configuration_shared_by_natural_and_tool(self):
        from ..core.models import ProviderOutcome
        plugin = ImageSearchPlugin(None, {
            "providers": {
                "saucenao": {"api_key": "test", "send_low_similarity": True,
                            "low_similarity_provider": "google"},
                "google_lens": {"enabled": True, "serpapi_keys": ["test"]},
                "ascii2d": {"enabled": False},
            },
            "output": {"merged_forward_threshold": 0},
            "command": {"source_order": "Google Lens > SauceNAO"},
        })
        try:
            sauce, lens = plugin._service._source_providers
            self.assertTrue(sauce._send_low_similarity)
            sauce.search = AsyncMock(return_value=ProviderOutcome(
                matches=[SourceMatch(provider="saucenao", title="Weak", similarity=20)],
                low_accuracy=True,
            ))
            lens.search = AsyncMock(return_value=ProviderOutcome(
                matches=[SourceMatch(provider="google_lens", title="Web", url="https://example.org/art")]
            ))
            request = self._tool_request()
            await plugin.filter_llm_tools(None, request)
            description = request.func_tool.get_tool("search_image").description
            self.assertIn("low-similarity follow-up: google_lens", description)
            self.assertNotIn("Configured auto/source order", description)
            for kind in ("natural", "tool"):
                event = self._simple_event("帮我找下出处", [Image(file="https://example.org/image")])
                if kind == "natural":
                    await plugin.route_simple_search(event, None)
                else:
                    await plugin.tool_search_image(event)
            self.assertEqual(sauce.search.await_count, 2)
            self.assertEqual(lens.search.await_count, 2)
        finally:
            await plugin.terminate()

    async def test_schema_defaults_labels_and_removed_order(self):
        import json
        from ..main import _bounded_int
        root = Path(__file__).resolve().parents[1]
        schema = json.loads((root / "_conf_schema.json").read_text(encoding="utf-8"))
        sauce = schema["providers"]["items"]["saucenao"]["items"]
        keys = list(sauce)
        self.assertEqual(keys[keys.index("min_similarity") + 1], "send_low_similarity")
        self.assertFalse(sauce["send_low_similarity"]["default"])
        self.assertEqual(keys[-1], "low_similarity_provider")
        self.assertEqual(sauce["low_similarity_provider"]["options"], ["off", "Ascii2d", "google"])
        self.assertEqual(sauce["low_similarity_provider"]["default"], "off")
        self.assertNotIn("source_order", schema["command"]["items"])
        output = schema["output"]["items"]
        self.assertNotIn("merged_forward", output)
        self.assertEqual(output["merged_forward_threshold"]["default"], 2)
        self.assertEqual(output["merged_forward_threshold"]["slider"], {"min": 0, "max": 10, "step": 1})
        self.assertEqual(_bounded_int(0, 2, 0, 10), 0)
        self.assertEqual(_bounded_int(99, 2, 0, 10), 10)
        self.assertEqual(_bounded_int(True, 2, 0, 10), 2)
        self.assertEqual(schema["providers"]["items"]["ascii2d"]["description"], "Ascii2d")

    async def test_search_logs_correlate_start_completion_and_rich_send(self):
        import re
        self.plugin._service.search = AsyncMock(return_value=SearchResponse(
            attempted_providers=["google_lens"],
            matches=[SourceMatch(provider="google_lens", title="private-title", url="https://private.example/art")],
        ))
        self.plugin._delivery.try_send = AsyncMock(return_value=True)
        event = self._simple_event("搜图", [Image(file="https://private.example/image?secret=key")])
        with patch(ImageSearchPlugin.__module__ + ".logger.info") as log:
            _ = [r async for r in self.plugin.search_image_command(event, "g")]
        messages = [str(call.args[0]) for call in log.call_args_list]
        joined = "\n".join(messages)
        for phrase in ("搜索开始", "搜索完成", "结果发送开始", "结果已直接发送",
                       "实际渠道=google_lens", "结果=1"):
            self.assertIn(phrase, joined)
        ids = re.findall(r"id=([a-f0-9]{8})", joined)
        self.assertEqual(len(set(ids)), 1)
        self.assertEqual(len(ids), 4)
        self.assertNotIn("secret", joined)
        self.assertNotIn("private.example", joined)
        self.assertNotIn("private-title", joined)

    async def test_search_log_exception_does_not_expose_error_body(self):
        from ..core.models import ImageAsset, SearchMode
        self.plugin._service.search = AsyncMock(side_effect=RuntimeError(
            "secret-api-key https://private.example/image"
        ))
        with patch(ImageSearchPlugin.__module__ + ".logger.warning") as log:
            with self.assertRaises(RuntimeError):
                await self.plugin._search_with_logging(
                    ImageAsset(url="https://private.example/image"), SearchMode.AUTO,
                )
        message = log.call_args.args[0]
        self.assertIn("搜索中断 (RuntimeError)", message)
        self.assertNotIn("secret", message)
        self.assertNotIn("private.example", message)

    async def test_ascii2d_only_uses_http(self) -> None:
        # Obsolete saved keys must not select a removed implementation.
        plugin = ImageSearchPlugin(None, {
            "providers": {"ascii2d": {
                "transport": "patchright",
                "browser_channel": "chrome",
            }}
        })
        try:
            ascii2d = next(
                item for item in plugin._service._source_providers
                if item.name == "ascii2d"
            )
            self.assertEqual(type(ascii2d).__name__, "Ascii2dProvider")
            self.assertFalse(hasattr(plugin, "_ascii2d_browser"))
            self.assertFalse(hasattr(plugin, "prepare_search_browser"))
        finally:
            await plugin.terminate()

    async def test_schema_and_dependencies_have_no_browser_route(self) -> None:
        import json
        root = Path(__file__).resolve().parents[1]
        schema = json.loads((root / "_conf_schema.json").read_text(encoding="utf-8"))
        ascii2d = schema["providers"]["items"]["ascii2d"]["items"]
        self.assertNotIn("transport", ascii2d)
        self.assertFalse(any(key.startswith("browser_") for key in ascii2d))
        dependencies = (root / "requirements.txt").read_text(encoding="utf-8")
        self.assertNotIn("patchright", dependencies.lower())
        self.assertNotIn("playwright", dependencies.lower())

    async def test_command_and_tool_share_merged_source_presentation(self) -> None:
        response = SearchResponse(matches=[
            SourceMatch(provider="saucenao", title="Artwork", similarity=95,
                        source_url="https://www.pixiv.net/artworks/123",
                        url="https://danbooru.donmai.us/posts/1"),
            SourceMatch(provider="google_lens", title="Web match",
                        url="https://danbooru.donmai.us/posts/1"),
        ])
        self.plugin._get_image_url = AsyncMock(
            return_value=("https://images.example/test.jpg", None)
        )
        self.plugin._service.search = AsyncMock(return_value=response)
        event = SimpleNamespace(plain_result=lambda text: text, send=AsyncMock())
        command_output = [
            item async for item in self.plugin.search_image_command(event, "g s")
        ]
        self.assertEqual(len(command_output), 1)
        self.assertIn("SauceNAO (95.00%) / Google Lens", command_output[0])
        self.assertNotIn("2. [", command_output[0])
        self.assertEqual(command_output[0].count("https://danbooru.donmai.us/posts/1"), 1)
        selected = self.plugin._service.search.await_args.kwargs["provider_names"]
        self.assertEqual(selected, ["google_lens", "saucenao"])
        await self.plugin.tool_search_image(event)
        event.send.assert_awaited_once_with(command_output[0])
        self.assertEqual(len(response.matches), 2)

    async def test_source_enrichment_shared_by_command_and_tool(self):
        from ..core.models import ProviderOutcome
        from ..core.source_enrichment import SourceEnricher
        self.plugin._source_pages.load = AsyncMock(return_value=(
            '<div class="image-container" data-normalized-source="https://www.pixiv.net/artworks/456">'
        ))
        sauce = next(p for p in self.plugin._service._source_providers if p.name == "saucenao")
        sauce.search = AsyncMock(return_value=ProviderOutcome(matches=[
            SourceMatch(provider="saucenao", title="Artwork", similarity=95,
                        url="https://danbooru.donmai.us/posts/123")
        ]))
        self.assertIsInstance(self.plugin._service._source_enricher, SourceEnricher)
        self.plugin._get_image_url = AsyncMock(return_value=("https://example.org/image.jpg", None))
        event = SimpleNamespace(plain_result=lambda text: text, send=AsyncMock())
        command = [r async for r in self.plugin.search_image_command(event, "s")]
        self.assertIn("https://www.pixiv.net/artworks/456", command[0])
        await self.plugin.tool_search_image(event)
        event.send.assert_awaited_once_with(command[0])

    async def test_source_enrichment_can_be_disabled(self):
        plugin = ImageSearchPlugin(None, {"output": {"resolve_original_source": False}})
        try:
            self.assertIsNone(plugin._service._source_enricher)
        finally:
            await plugin.terminate()

    async def test_all_source_providers_are_registered(self) -> None:
        names = {p.name for p in self.plugin._service._source_providers}
        self.assertEqual(names, {"saucenao", "google_lens", "ascii2d"})
        self.assertEqual(
            [p.name for p in self.plugin._service._anime_providers],
            ["trace_moe"],
        )

    async def test_localized_image_uses_original_onebot_url(self) -> None:
        event = SimpleNamespace(
            get_messages=lambda: [Image(file="/tmp/local.jpg", url="/tmp/local.jpg")],
            message_obj=SimpleNamespace(
                raw_message={
                    "message": [
                        {
                            "type": "image",
                            "data": {"url": "https://images.example/original.jpg"},
                        },
                    ]
                }
            ),
        )
        original = Image.register_to_file_service
        publisher = AsyncMock()
        Image.register_to_file_service = publisher
        try:
            url, error = await self.plugin._get_image_url(event)
            self.assertEqual(url, "https://images.example/original.jpg")
            self.assertIsNone(error)
            publisher.assert_not_awaited()
        finally:
            Image.register_to_file_service = original

    async def test_localized_reply_resolves_remote_url_before_publication(self) -> None:
        reply = Reply(
            id="123", chain=[Image(file="/tmp/local.jpg", url="/tmp/local.jpg")]
        )
        event = SimpleNamespace(get_messages=lambda: [reply])
        with (
            patch(
                ImageSearchPlugin.__module__ + ".extract_quoted_message_images",
                new=AsyncMock(
                    return_value=["/tmp/local.jpg", "https://images.example/reply.jpg"]
                ),
            ) as resolver,
            patch.object(
                Image, "register_to_file_service", new=AsyncMock()
            ) as publisher,
        ):
            url, error = await self.plugin._get_image_url(event)
            self.assertEqual(url, "https://images.example/reply.jpg")
            self.assertIsNone(error)
            resolver.assert_awaited_once_with(event, reply)
            publisher.assert_not_awaited()

    async def test_direct_local_image_is_not_replaced_by_quoted_image(self) -> None:
        event = SimpleNamespace(
            get_messages=lambda: [
                Image(file="base64://test"),
                Reply(
                    id="123", chain=[Image(file="https://images.example/quoted.jpg")]
                ),
            ]
        )
        with patch.object(
            Image,
            "register_to_file_service",
            new=AsyncMock(return_value="https://callback.example/direct"),
        ):
            url, error = await self.plugin._get_image_url(event)
            self.assertEqual(url, "https://callback.example/direct")
            self.assertIsNone(error)

    async def test_embedded_base64_uses_astrbot_file_service(self) -> None:
        image = Image(file="base64://test")
        event = SimpleNamespace(get_messages=lambda: [image])
        original = Image.register_to_file_service
        Image.register_to_file_service = AsyncMock(
            return_value="https://callback.example/api/file/test"
        )
        try:
            url, error = await self.plugin._get_image_url(event)
            self.assertEqual(url, "https://callback.example/api/file/test")
            self.assertIsNone(error)
        finally:
            Image.register_to_file_service = original

    async def test_command_formats_provider_result(self) -> None:
        event = SimpleNamespace(
            get_messages=lambda: [Image(file="https://images.example/query.jpg")],
            plain_result=lambda text: text,
        )
        self.plugin._service.search = AsyncMock(
            return_value=SearchResponse(
                matches=[
                    SourceMatch(
                        provider="ascii2d",
                        title="Found artwork",
                        url="https://example.com/artwork",
                    )
                ],
                attempted_providers=["ascii2d"],
            )
        )
        results = [
            result async for result in self.plugin.search_image_command(event, "")
        ]
        self.assertEqual(len(results), 1)
        self.assertIn("Found artwork", results[0])

    async def test_explicit_google_command_passes_selection(self) -> None:
        event = SimpleNamespace(
            get_messages=lambda: [Image(file="https://images.example/query.jpg")],
            plain_result=lambda text: text,
        )
        self.plugin._service.search = AsyncMock(return_value=SearchResponse())
        results = [r async for r in self.plugin.search_image_command(event, "google")]
        self.assertEqual(len(results), 1)
        self.assertEqual(
            self.plugin._service.search.await_args.kwargs,
            {"provider_names": ["google_lens"]},
        )

    async def test_mixed_channels_ignore_invalid_and_search_valid(self) -> None:
        from astrbot.core.star.filter.command import CommandFilter

        command = CommandFilter(
            "搜图",
            handler_md=SimpleNamespace(handler=ImageSearchPlugin.search_image_command),
        )
        params = command.validate_and_convert_params(
            ["google", "SauceNAO", "123"], command.handler_params
        )
        event = SimpleNamespace(
            get_messages=lambda: [Image(file="https://images.example/query.jpg")],
            plain_result=lambda text: text,
        )
        self.plugin._service.search = AsyncMock(return_value=SearchResponse())
        results = [r async for r in self.plugin.search_image_command(event, **params)]
        self.assertEqual(len(results), 2)
        self.assertIn("123", results[0])
        self.assertIn("当前启用渠道", results[0])
        self.assertIn("已使用输入的可行渠道进行搜索：Google Lens、SauceNAO", results[0])
        self.assertEqual(
            self.plugin._service.search.await_args.kwargs,
            {"provider_names": ["google_lens", "saucenao"]},
        )
        self.assertEqual(
            command.validate_and_convert_params([], command.handler_params),
            {"channels": ""},
        )

    async def test_channel_aliases_are_deduplicated(self) -> None:
        event = SimpleNamespace(
            get_messages=lambda: [Image(file="https://images.example/query.jpg")],
            plain_result=lambda text: text,
        )
        self.plugin._service.search = AsyncMock(return_value=SearchResponse())
        results = [
            r
            async for r in self.plugin.search_image_command(
                event, "google，lens、SauceNAO sauce"
            )
        ]
        self.assertEqual(len(results), 1)
        self.assertEqual(
            self.plugin._service.search.await_args.kwargs,
            {"provider_names": ["google_lens", "saucenao"]},
        )

    async def test_custom_aliases_select_only_their_channels(self) -> None:
        event = SimpleNamespace(
            get_messages=lambda: [Image(file="https://images.example/query.jpg")],
            plain_result=lambda text: text,
        )
        self.plugin._service.search = AsyncMock(return_value=SearchResponse())
        results = [r async for r in self.plugin.search_image_command(event, "谷歌 酱")]
        self.assertEqual(len(results), 1)
        self.assertEqual(
            self.plugin._service.search.await_args.kwargs,
            {"provider_names": ["google_lens", "saucenao"]},
        )

    async def test_alias_for_disabled_provider_is_still_unavailable(self) -> None:
        plugin = ImageSearchPlugin(
            None,
            {
                "providers": {
                    "saucenao": {"enabled": False, "aliases": ["酱"]},
                    "ascii2d": {"enabled": False},
                },
            },
        )
        try:
            plugin._service.search = AsyncMock()
            event = SimpleNamespace(plain_result=lambda text: text)
            results = [r async for r in plugin.search_image_command(event, "酱")]
            self.assertIn("搜索渠道不可用", results[0])
            self.assertNotIn("已使用输入的可行渠道进行搜索", results[0])
            plugin._service.search.assert_not_awaited()
        finally:
            await plugin.terminate()

    async def test_unknown_channel_does_not_search(self) -> None:
        event = SimpleNamespace(plain_result=lambda text: text)
        self.plugin._service.search = AsyncMock()
        results = [r async for r in self.plugin.search_image_command(event, "unknown")]
        self.assertIn("搜索渠道不可用", results[0])
        self.assertNotIn("已使用输入的可行渠道进行搜索", results[0])
        self.plugin._service.search.assert_not_awaited()

    async def test_wait_listener_delivers_matching_image(self) -> None:
        event = SimpleNamespace(
            unified_msg_origin="platform:group:1",
            get_sender_id=lambda: "sender",
            get_messages=lambda: [Image(file="https://images.example/query.jpg")],
            stop_event=lambda: None,
        )
        key = self.plugin._wait_key(event)
        pending = await self.plugin._image_wait.create(key)
        await self.plugin.capture_waiting_image(event)
        result = await self.plugin._image_wait.wait(key, pending)
        self.assertIsInstance(result, ImageResolution)
        self.assertEqual(result.url, "https://images.example/query.jpg")

    def _tool_request(self):
        tool = FuncTool(
            name="search_image",
            description="original",
            parameters={"type": "object", "properties": {"mode": {"type": "string"}}},
            handler=self.plugin.tool_search_image,
        )
        other = FuncTool(
            name="other", description="unrelated", parameters={"type": "object"}
        )
        return SimpleNamespace(func_tool=ToolSet([tool, other]))

    async def _configure_services(self, *names: str) -> None:
        await self.plugin.terminate()
        self.plugin = ImageSearchPlugin(
            None,
            {
                "providers": {
                    name: {
                        "enabled": name in names,
                        "api_key": "test-key",
                        "serpapi_keys": ["test-key"],
                    }
                    for name in ("google_lens", "saucenao", "ascii2d", "trace_moe")
                }
            },
        )

    async def test_tool_contract_is_request_local_and_repeatable(self) -> None:
        req = self._tool_request()
        shared = req.func_tool
        original = shared.get_tool("search_image")
        await self.plugin.filter_llm_tools(None, req)
        updated = req.func_tool.get_tool("search_image")
        self.assertIsNot(req.func_tool, shared)
        self.assertIsNot(updated, original)
        self.assertEqual(original.description, "original")
        self.assertNotIn("enum", original.parameters["properties"]["mode"])
        self.assertIs(updated.handler, original.handler)
        self.assertIs(req.func_tool.get_tool("other"), shared.get_tool("other"))
        self.assertEqual(
            updated.parameters["properties"]["mode"]["enum"],
            ["auto", "source", "anime"],
        )
        first_description = updated.description
        await self.plugin.filter_llm_tools(None, req)
        self.assertEqual(
            req.func_tool.get_tool("search_image").description, first_description
        )

    async def test_lens_only_contract(self) -> None:
        await self._configure_services("google_lens")
        req = self._tool_request()
        await self.plugin.filter_llm_tools(None, req)
        tool = req.func_tool.get_tool("search_image")
        self.assertIn("google_lens", tool.description)
        for disabled in ("saucenao", "ascii2d", "trace_moe"):
            self.assertNotIn(disabled, tool.description)
        self.assertEqual(
            tool.parameters["properties"]["mode"]["enum"], ["auto", "source"]
        )
        self.plugin._get_image_url = AsyncMock()
        response = await self.plugin.tool_search_image(None, "anime")
        self.assertIn("当前可用 mode", response)
        self.plugin._get_image_url.assert_not_awaited()

    async def test_anime_only_contract_and_default_invocation(self) -> None:
        await self._configure_services("trace_moe")
        req = self._tool_request()
        await self.plugin.filter_llm_tools(None, req)
        tool = req.func_tool.get_tool("search_image")
        self.assertEqual(tool.parameters["properties"]["mode"]["enum"], ["anime"])
        self.assertEqual(tool.parameters["properties"]["mode"]["default"], "anime")
        self.assertNotIn("google_lens", tool.description)
        self.plugin._get_image_url = AsyncMock(
            return_value=("https://images.example/query.jpg", None)
        )
        self.plugin._service.search = AsyncMock(return_value=SearchResponse())
        event = SimpleNamespace(send=AsyncMock(), plain_result=lambda text: text)
        await self.plugin.tool_search_image(event)
        mode = self.plugin._service.search.await_args.args[1]
        self.assertEqual(mode.value, "anime")

    async def test_no_services_removes_tool_without_mutating_shared_set(self) -> None:
        await self._configure_services()
        req = self._tool_request()
        shared = req.func_tool
        await self.plugin.filter_llm_tools(None, req)
        self.assertIsNone(req.func_tool.get_tool("search_image"))
        self.assertIsNotNone(shared.get_tool("search_image"))
        response = await self.plugin.tool_search_image(None)
        self.assertIn("没有启用的搜索服务", response)

    async def test_disabled_llm_tool_is_removed_and_cannot_execute(self) -> None:
        self.plugin._llm_tool_enabled = False
        req = self._tool_request()
        await self.plugin.filter_llm_tools(None, req)
        self.assertIsNone(req.func_tool.get_tool("search_image"))
        self.plugin._get_image_url = AsyncMock()
        response = await self.plugin.tool_search_image(None)
        self.assertIn("搜图工具未启用", response)
        self.plugin._get_image_url.assert_not_awaited()

    async def test_tool_filter_does_not_reinsert_excluded_tool(self) -> None:
        req = SimpleNamespace(func_tool=ToolSet())
        await self.plugin.filter_llm_tools(None, req)
        self.assertIsNone(req.func_tool.get_tool("search_image"))
        await self.plugin.filter_llm_tools(None, SimpleNamespace(func_tool=None))

    async def test_timeout_config_is_finite_and_schema_has_only_supported_types(self):
        from ..main import _positive_float
        import json
        for value in (float("inf"), float("nan"), "inf", "nan"):
            self.assertEqual(_positive_float(value, 30), 30)
        self.assertEqual(_positive_float(12, 30), 12)
        root = Path(__file__).resolve().parents[1]
        schema = json.loads((root / "_conf_schema.json").read_text(encoding="utf-8"))
        options = schema["providers"]["items"]["google_lens"]["items"]["search_type"]["options"]
        self.assertEqual(set(options), {"all", "visual_matches", "exact_matches"})


if __name__ == "__main__":
    unittest.main()
