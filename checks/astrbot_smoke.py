"""Run against an installed AstrBot runtime: python -m <plugin>.checks.astrbot_smoke."""

from __future__ import annotations

import unittest
import json
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
                "cache": {"enabled": False},
                "providers": {
                    "google_lens": {"serpapi_keys": ["test-serp-key"]},
                    "saucenao": {"api_key": "test-sauce-key", "aliases": ["s", "酱"]},
                },
            },
        )

    async def asyncTearDown(self) -> None:
        await self.plugin.terminate()

    async def test_debug_defaults_off_and_keeps_summary_and_warnings(self):
        self.assertFalse(self.plugin._debug_enabled)
        self.assertFalse(self.plugin._delivery._debug_enabled)
        self.plugin._service.search = AsyncMock(return_value=SearchResponse(
            matches=[SourceMatch("saucenao", "private-title")],
            attempted_providers=["saucenao", "ascii2d"], failed_providers=["ascii2d"],
            failure_details={"ascii2d": "请求超时"},
            diagnostics={"saucenao": ["safe-diagnostic"]},
        ))
        self.plugin._delivery.try_send = AsyncMock(return_value=True)
        event = self._simple_event("搜图", [Image(file="https://private.example/image")])
        for enabled in (False, True):
            self.plugin._debug_enabled = enabled
            with patch(ImageSearchPlugin.__module__ + ".logger.info") as info, patch(
                ImageSearchPlugin.__module__ + ".logger.warning"
            ) as warning:
                _ = [r async for r in self.plugin.search_image_command(event, "")]
            lines = [call.args[0] for call in info.call_args_list]
            self.assertTrue(any("请求完成" in line for line in lines))
            warning.assert_called_once()
            self.assertEqual(any("URL指纹=" in line for line in lines), enabled)
            self.assertEqual(any("safe-diagnostic" in line for line in lines), enabled)
            if not enabled:
                self.assertEqual(len(lines), 1)
            self.assertNotIn("private", "\n".join(lines))

    async def test_debug_config_reaches_result_delivery(self):
        await self.plugin.terminate()
        self.plugin = ImageSearchPlugin(None, {**self.plugin._config, "debug": {"enabled": True}})
        self.assertTrue(self.plugin._debug_enabled)
        self.assertTrue(self.plugin._delivery._debug_enabled)

    async def test_anime_threshold_default_and_explicit_config(self):
        from ..core.http import JsonResponse
        for configured, expected in ((None, ["scene"]), (80, [])):
            await self.plugin.terminate()
            trace = {"enabled": True}
            if configured is not None:
                trace["min_similarity"] = configured
            self.plugin = ImageSearchPlugin(None, {
                "cache": {"enabled": False}, "providers": {
                    "saucenao": {"enabled": False}, "ascii2d": {"enabled": False},
                    "google_lens": {"enabled": False}, "trace_moe": trace,
                },
            })
            self.plugin._get_image_url = AsyncMock(return_value=("https://img.test/scene", None))
            self.plugin._delivery.try_send = AsyncMock(return_value=True)
            self.plugin._http.get_json = AsyncMock(return_value=JsonResponse(200, {
                "result": [{"filename": "scene", "similarity": 0.5}],
            }))
            _ = [r async for r in self.plugin.search_anime_command(self._simple_event("搜番"))]
            response = self.plugin._delivery.try_send.await_args.args[1]
            self.assertEqual([match.title for match in response.matches], expected)
            if configured is not None:
                self.assertEqual(response.filtered_providers, {"trace_moe": 80})

    async def _use_book_config(self, *, enabled=True, only_book=False):
        await self.plugin.terminate()
        self.plugin = ImageSearchPlugin(None, {
            "cache": {"enabled": False},
            "providers": {
                "soutubot": {"enabled": enabled},
                "saucenao": {"enabled": not only_book, "api_key": "test"},
                "google_lens": {"enabled": False}, "ascii2d": {"enabled": False},
                "trace_moe": {"enabled": False},
            },
        })
        self.plugin._get_image_url = AsyncMock(return_value=("https://example.org/image", None))
        self.plugin._delivery.try_send = AsyncMock(return_value=True)
        from ..core.models import ProviderOutcome
        for provider in self.plugin._service._source_providers:
            provider.search = AsyncMock(return_value=ProviderOutcome(matches=[
                SourceMatch(provider=provider.name, title="Candidate", url="https://example.org/" + provider.name)
            ]))

    async def test_book_command_and_refresh_use_explicit_channel(self):
        self.assertNotIn("soutubot", self.plugin._service.source_provider_names)
        await self._use_book_config()
        event = self._simple_event("搜本 -purge")
        _ = [item async for item in self.plugin.search_book_command(event, "-purge")]
        sauce, book = self.plugin._service._source_providers
        sauce.search.assert_not_awaited()
        book.search.assert_awaited_once()
        self.assertTrue(book.search.await_args.args[0].refresh)

    async def test_book_tool_channels_ignore_model_and_quoted_text(self):
        await self._use_book_config()
        sauce, book = self.plugin._service._source_providers
        for text, supplied, expected in (("帮我搜本", "saucenao", "soutubot"),
                                          ("找下出处", "soutubot -purge", "saucenao")):
            sauce.search.reset_mock()
            book.search.reset_mock()
            event = self._simple_event(text, [Reply(id="quoted", text="搜本")])
            response = json.loads(await self.plugin.tool_search_image(event, "source", supplied))
            self.assertEqual(response["attempted_providers"], [expected])
            self.assertEqual(book.search.await_count, int(expected == "soutubot"))
            selected = book if expected == "soutubot" else sauce
            self.assertFalse(selected.search.await_args.args[0].refresh)

    async def test_book_disabled_never_falls_back_or_waits_for_image(self):
        await self._use_book_config(enabled=False)
        self.plugin._resolve_image_request = AsyncMock()
        output = [item async for item in self.plugin.search_book_command(self._simple_event("搜本"))]
        self.assertIn("渠道不可用", output[0])
        self.plugin._resolve_image_request.assert_not_awaited()
        self.plugin._service._source_providers[0].search.assert_not_awaited()

    async def test_only_book_rejects_default_but_allows_explicit_session(self):
        await self._use_book_config(only_book=True)
        event = self._simple_event("搜图")
        output = [item async for item in self.plugin.search_image_command(event, "")]
        self.assertIn("没有可用的默认搜图渠道", output[0])
        output = [item async for item in self.plugin.search_mode_command(event, "")]
        self.assertIn("没有可用的默认搜图渠道", output[0])
        output = [item async for item in self.plugin.search_mode_command(event, "soutubot")]
        self.assertIn("已进入搜图模式", output[0])

    async def test_book_natural_request_routes_without_model(self):
        await self._use_book_config()
        event = self._simple_event("请帮我搜本")
        await self.plugin.route_simple_search(event, None)
        self.assertTrue(event.is_stopped())
        sauce, book = self.plugin._service._source_providers
        sauce.search.assert_not_awaited()
        book.search.assert_awaited_once()

    async def test_book_partial_and_warning_results_are_not_cached(self):
        from ..core.providers.soutubot import SoutubotProvider
        from ..core.http import JsonResponse
        await self._use_book_config(only_book=True)
        self.plugin._cache_enabled = True
        http = AsyncMock()
        self.plugin._service._source_providers = (SoutubotProvider(http),)
        body = {"status": "ok", "partial": False, "api_results": [{"status": "ok"}],
                "results": [
                    {"score": 90-page, "path_segments": [{"links": {"page_url": f"https://example.org/page/{page}"}}]}
                    for page in (1, 2, 3)
                ] + [{"score": 70, "path_segments": [{"source_key": "nhentai", "external_id": "4"}]}]}
        for extra in ({"partial": True}, {"warnings": ["diagnostic"]},
                      {"api_results": [{"status": "ok"}, {"status": "error"}]},
                      {"results": body["results"] + [{"score": "bad", "path_segments": []}]}):
            http.upload_image_json.return_value = JsonResponse(200, {**body, **extra})
            http.upload_image_json.reset_mock()
            for _ in range(2):
                event = self._simple_event("搜本")
                _ = [r async for r in self.plugin.search_book_command(event)]
                response = self.plugin._delivery.try_send.await_args.args[1]
                self.assertEqual(len(response.matches), 3)
                self.assertTrue(response.notices)
            self.assertEqual(http.upload_image_json.await_count, 2)
        http.upload_image_json.return_value = JsonResponse(200, body)
        http.upload_image_json.reset_mock()
        for _ in range(2):
            _ = [r async for r in self.plugin.search_book_command(self._simple_event("搜本"))]
        http.upload_image_json.assert_awaited_once()

    async def test_book_waiting_image_and_continuous_mode_keep_channel(self):
        await self._use_book_config()
        del self.plugin._get_image_url
        output = self.plugin.search_book_command(self._simple_event("搜本 -purge"), "-purge")
        self.assertIn("发送图片", await anext(output))
        later = self._simple_event("", [Image(file="https://img.test/book")])
        await self.plugin.capture_waiting_image(later)
        _ = [r async for r in output]
        sauce, book = self.plugin._service._source_providers
        book.search.assert_awaited_once()
        self.assertTrue(book.search.await_args.args[0].refresh)
        _ = [r async for r in self.plugin.search_mode_command(self._simple_event("搜图模式 soutubot"), "soutubot")]
        await self.plugin.capture_waiting_image(self._simple_event("", [Image(file="https://img.test/next")]))
        self.assertEqual(book.search.await_count, 2)
        sauce.search.assert_not_awaited()

    async def test_book_warning_diagnostics_reach_request_log_without_raw_payload(self):
        from ..core.providers.soutubot import SoutubotProvider
        from ..core.http import JsonResponse
        await self._use_book_config(only_book=True)
        http = AsyncMock()
        http.upload_image_json.return_value = JsonResponse(200, {
            "status": "ok", "partial": False, "api_results": [{"status": "ok"}],
            "warnings": [{"code": "no_search_features", "message": "private-payload"}],
            "results": [{"score": 90, "path_segments": [{"page_url": "https://example.org/page/1"}]}],
        })
        self.plugin._service._source_providers = (SoutubotProvider(http),)
        self.plugin._debug_enabled = True
        with patch(ImageSearchPlugin.__module__ + ".logger.info") as log:
            _ = [r async for r in self.plugin.search_book_command(self._simple_event("搜本"))]
        lines = [call.args[0] for call in log.call_args_list]
        diagnostic = next(line for line in lines if "渠道诊断 soutubot" in line)
        self.assertIn("id=", diagnostic)
        self.assertIn("codes=no_search_features", diagnostic)
        self.assertIn("partial=False libraries=1 failed=0", diagnostic)
        self.assertNotIn("private-payload", "\n".join(lines))

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

    async def test_natural_help_uses_command_output_without_resolving_images(self):
        self.plugin._service.search = AsyncMock()
        self.plugin._resolve_image_request = AsyncMock()
        expected = [r async for r in self.plugin.search_help_command(self._simple_event("搜图帮助"))][0]
        for phrase in ("怎么搜图", "请问如何搜图？", "搜图帮助", "怎么搜番"):
            event = self._simple_event(phrase, [Image(file="https://site.test/image")])
            await self.plugin.route_simple_search(event, None)
            self.assertTrue(event.is_stopped())
            event.send.assert_awaited_once_with(expected)
        self.plugin._service.search.assert_not_awaited()
        self.plugin._resolve_image_request.assert_not_awaited()
        self.assertIsNone(self.plugin._state)

    async def test_natural_help_respects_routing_toggle_and_does_not_match_discussion(self):
        for phrase in ("不要搜图帮助", "他说怎么搜图", "怎么搜图，然后查一下这张"):
            event = self._simple_event(phrase)
            await self.plugin.route_simple_search(event, None)
            self.assertFalse(event.is_stopped())
            event.send.assert_not_awaited()
        self.plugin._simple_search_enabled = False
        event = self._simple_event("怎么搜图")
        await self.plugin.route_simple_search(event, None)
        self.assertFalse(event.is_stopped())
        event.send.assert_not_awaited()

    async def test_search_help_uses_active_services_and_does_not_search(self):
        config = dict(self.plugin._config)
        config["providers"] = {**config["providers"], "ascii2d": {"enabled": False}}
        await self.plugin.terminate()
        self.plugin = ImageSearchPlugin(None, config)
        self.plugin._service.search = AsyncMock()
        self.plugin._resolve_image_request = AsyncMock()
        replies = [r async for r in self.plugin.search_help_command(self._simple_event("搜图帮助"))]
        self.assertEqual(len(replies), 1)
        text = replies[0]
        self.assertIn("SauceNAO", text)
        self.assertIn("酱", text)
        self.assertIn("Google Lens", text)
        self.assertNotIn("Ascii2d", text)
        self.assertNotIn("-purge", text)
        self.assertNotIn("test-serp-key", text)
        self.assertIn("/搜图模式", text)
        self.plugin._search_mode_enabled = False
        self.plugin._cache_enabled = True
        text = [r async for r in self.plugin.search_help_command(self._simple_event("搜图帮助"))][0]
        self.assertIn("搜图模式已关闭。", text)
        self.assertNotIn("/搜图模式", text)
        self.assertIn("-purge", text)
        self.plugin._service.search.assert_not_awaited()
        self.plugin._resolve_image_request.assert_not_awaited()
        self.assertIsNone(self.plugin._state)

    async def test_search_mode_exit_phrases_stop_event_and_close_only_own_mode(self):
        for phrase in ("结束搜图", "退出搜图", "结束", "退出"):
            owner = self._simple_event("搜图模式")
            _ = [r async for r in self.plugin.search_mode_command(owner, "")]
            event = self._simple_event("  " + phrase + "  ")
            await self.plugin.capture_waiting_image(event)
            self.assertTrue(event.is_stopped())
            event.send.assert_awaited_once_with("已退出搜图模式。")
            self.assertIsNone(self.plugin._search_sessions.get(self.plugin._wait_key(owner)))
            # Outside the mode these words remain ordinary conversation.
            outside = self._simple_event(phrase)
            await self.plugin.capture_waiting_image(outside)
            self.assertFalse(outside.is_stopped())
            outside.send.assert_not_awaited()

    async def test_search_mode_exit_does_not_match_other_context_or_sentences(self):
        owner = self._simple_event("搜图模式")
        _ = [r async for r in self.plugin.search_mode_command(owner, "")]
        events = [self._simple_event(text) for text in ("退出登录", "这个故事结束了", "请退出搜图", "退出！")]
        other = self._simple_event("退出")
        other.get_sender_id = lambda: "another-user"
        events.append(other)
        other = self._simple_event("退出")
        other.unified_msg_origin = "other:group:session"
        events.append(other)
        events.append(self._simple_event("退出", [Image(file="https://site.test/image")]))
        for event in events:
            await self.plugin.capture_waiting_image(event)
            self.assertFalse(event.is_stopped())
            event.send.assert_not_awaited()
        self.assertIsNotNone(self.plugin._search_sessions.get(self.plugin._wait_key(owner)))

    async def test_search_mode_exit_works_while_single_image_wait_is_pending(self):
        owner = self._simple_event("搜图模式")
        _ = [r async for r in self.plugin.search_mode_command(owner, "")]
        key = self.plugin._wait_key(owner)
        await self.plugin._image_wait.create(key)
        event = self._simple_event("退出")
        await self.plugin.capture_waiting_image(event)
        self.assertTrue(event.is_stopped())
        self.assertIsNone(self.plugin._search_sessions.get(key))
        self.assertTrue(await self.plugin._image_wait.has_pending(key))

    async def test_search_mode_disabled_and_opening_text_and_logs(self):
        event = self._simple_event("搜图模式")
        self.plugin._debug_enabled = True
        with patch(self.plugin.__module__ + ".logger.info") as log:
            replies = [r async for r in self.plugin.search_mode_command(event, "")]
        self.assertEqual(replies[0], "已进入搜图模式（默认渠道）。\n请发送图片吧！支持批量！60 秒内无动作将自动退出，发送「退出」可提前结束搜图模式。")
        self.assertIn("动作=已进入", log.call_args.args[0])
        self.plugin._search_mode_enabled = False
        replies = [r async for r in self.plugin.search_mode_command(event, "")]
        self.assertEqual(replies, ["搜图模式未启用。"])
        image = self._simple_event("", [Image(file="https://site.test/image")])
        self.plugin._resolve_image_request = AsyncMock()
        await self.plugin.capture_waiting_image(image)
        self.plugin._resolve_image_request.assert_not_awaited()
        self.assertFalse(image.is_stopped())

    async def test_search_mode_deduplicates_batch_and_inflight_but_allows_later_requests(self):
        import asyncio
        owner = self._simple_event("搜图模式")
        _ = [r async for r in self.plugin.search_mode_command(owner, "g")]
        started, release = asyncio.Event(), asyncio.Event()
        async def search(*args, **kwargs):
            started.set()
            await release.wait()
            return SearchResponse(matches=[SourceMatch("google_lens", "Art", url="https://site.test/art")])
        self.plugin._service.search = AsyncMock(side_effect=search)
        self.plugin._resolve_image_request = AsyncMock(return_value=ImageResolution(
            images=(("https://site.test/a", None), ("https://site.test/b", None)), total=2,
            identities=("qq-md5:same", "qq-md5:same")))
        first = self._simple_event("", [Image(file="https://site.test/a"), Image(file="https://site.test/b")])
        task = asyncio.create_task(self.plugin.capture_waiting_image(first))
        try:
            await asyncio.wait_for(started.wait(), timeout=1)
            self.plugin._resolve_image_request.return_value = ImageResolution(
                images=(("https://site.test/changed", None),), total=1, identities=("qq-md5:same",))
            second = self._simple_event("", [Image(file="https://site.test/changed")])
            await self.plugin.capture_waiting_image(second)
            self.assertEqual(second.send.await_args.args[0], "该图已在搜索中。")
            self.assertEqual(self.plugin._service.search.await_count, 1)
        finally:
            release.set()
            await task
        self.assertEqual(first.send.await_args_list[0].args[0], "已跳过 1 张重复图片。")
        self.assertFalse(self.plugin._mode_inflight)
        third = self._simple_event("", [Image(file="https://site.test/changed")])
        await self.plugin.capture_waiting_image(third)
        self.assertEqual(self.plugin._service.search.await_count, 2)

    async def test_search_mode_duplicate_reservations_released_on_send_failure(self):
        owner = self._simple_event("搜图模式")
        _ = [r async for r in self.plugin.search_mode_command(owner, "")]
        self.plugin._resolve_image_request = AsyncMock(return_value=ImageResolution(
            images=(("https://site.test/a", None), ("https://site.test/a", None)), total=2))
        image = self._simple_event("", [Image(file="https://site.test/a")])
        image.send.side_effect = RuntimeError("send failed")
        with self.assertRaises(RuntimeError):
            await self.plugin.capture_waiting_image(image)
        self.assertFalse(self.plugin._mode_inflight)

    async def test_search_mode_repeated_images_channels_and_exit(self):
        opened = self._simple_event("搜图模式 g")
        messages = [r async for r in self.plugin.search_mode_command(opened, "g")]
        self.assertIn("Google Lens", messages[0])
        self.plugin._service.search = AsyncMock(return_value=SearchResponse(matches=[
            SourceMatch("google_lens", "Artwork", url="https://site.test/art")]))
        for _ in range(2):
            image = self._simple_event("", [Image(file="https://site.test/image")])
            await self.plugin.capture_waiting_image(image)
            self.assertTrue(image.is_stopped())
            image.send.assert_awaited_once()
        self.assertEqual(self.plugin._service.search.await_count, 2)
        self.assertEqual(self.plugin._service.search.await_args.kwargs["provider_names"], ["google_lens"])
        messages = [r async for r in self.plugin.search_mode_command(opened, "s")]
        self.assertIn("已切换", messages[0])
        self.assertEqual(self.plugin._search_sessions.get(self.plugin._wait_key(opened)).providers, ("saucenao",))
        messages = [r async for r in self.plugin.exit_search_mode_command(opened)]
        self.assertIn("已退出", messages[0])
        image = self._simple_event("", [Image(file="https://site.test/image")])
        await self.plugin.capture_waiting_image(image)
        self.assertFalse(image.is_stopped())
        self.assertEqual(self.plugin._service.search.await_count, 2)

    async def test_search_mode_isolation_and_text_commands_are_not_consumed(self):
        owner = self._simple_event("搜图模式")
        _ = [r async for r in self.plugin.search_mode_command(owner, "")]
        self.plugin._resolve_image_request = AsyncMock()
        for scope in ("other_user", "other_session", "command", "stopped"):
            image = self._simple_event("搜番" if scope == "command" else "", [Image(file="https://site.test/i")])
            if scope == "other_user":
                image.get_sender_id = lambda: "other"
            if scope == "other_session":
                image.unified_msg_origin = "platform:private:test"
            if scope == "stopped":
                image.stop_event()
            await self.plugin.capture_waiting_image(image)
        self.plugin._resolve_image_request.assert_not_awaited()

    async def test_search_mode_invalid_options_and_pending_wait_preserve_state(self):
        owner = self._simple_event("搜图模式")
        key = self.plugin._wait_key(owner)
        for options in ("nonexistent", "-purge"):
            _ = [r async for r in self.plugin.search_mode_command(owner, options)]
            self.assertIsNone(self.plugin._search_sessions.get(key))
        pending = await self.plugin._image_wait.create(key)
        replies = [r async for r in self.plugin.search_mode_command(owner, "g")]
        self.assertIn("等待单次", replies[0])
        self.assertIsNone(self.plugin._search_sessions.get(key))
        await self.plugin._image_wait.close()
        _ = [r async for r in self.plugin.search_mode_command(owner, "g")]
        session = self.plugin._search_sessions.get(key)
        _ = [r async for r in self.plugin.search_mode_command(owner, "invalid")]
        self.assertIs(self.plugin._search_sessions.get(key), session)

    async def test_search_mode_waiting_command_takes_priority_and_close_clears_mode(self):
        owner = self._simple_event("搜图模式")
        key = self.plugin._wait_key(owner)
        _ = [r async for r in self.plugin.search_mode_command(owner, "g")]
        pending = await self.plugin._image_wait.create(key)
        image = self._simple_event("", [Image(file="https://site.test/image")])
        self.plugin._service.search = AsyncMock()
        await self.plugin.capture_waiting_image(image)
        resolution = await self.plugin._image_wait.wait(key, pending)
        self.assertEqual(resolution.url, "https://site.test/image")
        self.plugin._service.search.assert_not_awaited()
        await self.plugin._search_sessions.close()
        self.assertIsNone(self.plugin._search_sessions.get(key))

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
            "cache": {"enabled": False},
            "providers": {
                "saucenao": {"api_key": "test",
                            "low_similarity_provider": "google"},
                "google_lens": {"enabled": True, "serpapi_keys": ["test"]},
                "ascii2d": {"enabled": False},
            },
            "output": {"merged_forward_threshold": 0},
            "command": {"source_order": "Google Lens > SauceNAO"},
        })
        try:
            sauce, lens = plugin._service._source_providers
            self.assertEqual(sauce._min_similarity, 60)
            sauce.search = AsyncMock(return_value=ProviderOutcome(
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
        self.assertNotIn("send_low_similarity", sauce)
        self.assertNotIn("low_accuracy_threshold", sauce)
        self.assertEqual(sauce["min_similarity"]["default"], 60)
        self.assertEqual(keys[:2], ["enabled", "api_key"])
        self.assertEqual(keys[-1], "max_results")
        self.assertLess(keys.index("min_similarity"), keys.index("low_similarity_provider"))
        self.assertTrue(sauce["api_key"]["secret"])
        self.assertEqual(len(sauce["low_similarity_provider"]["labels"]),
                         len(sauce["low_similarity_provider"]["options"]))
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
        self.assertIn("debug", schema)
        self.assertNotIn("debug_logging", schema)
        self.assertFalse(schema["debug"]["items"]["enabled"]["default"])

    async def test_search_logs_correlate_start_completion_and_rich_send(self):
        import re
        self.plugin._service.search = AsyncMock(return_value=SearchResponse(
            attempted_providers=["google_lens"],
            matches=[SourceMatch(provider="google_lens", title="private-title", url="https://private.example/art")],
        ))
        self.plugin._delivery.try_send = AsyncMock(return_value=True)
        event = self._simple_event("搜图", [Image(file="https://private.example/image?secret=key")])
        self.plugin._debug_enabled = True
        with patch(ImageSearchPlugin.__module__ + ".logger.info") as log:
            _ = [r async for r in self.plugin.search_image_command(event, "g")]
        messages = [str(call.args[0]) for call in log.call_args_list]
        joined = "\n".join(messages)
        for phrase in ("搜索开始", "搜索完成", "结果发送开始", "结果已直接发送",
                       "实际渠道=google_lens", "结果=1"):
            self.assertIn(phrase, joined)
        ids = re.findall(r"id=([a-f0-9]{8})", joined)
        self.assertEqual(len(set(ids)), 1)
        self.assertGreaterEqual(len(ids), 4)
        self.assertIn("缓存=跳过:缓存关闭", joined)
        self.assertIn("URL指纹=", joined)
        self.assertIn("外部搜索图片=1", joined)
        self.assertNotIn("secret", joined)
        self.assertNotIn("private.example", joined)
        self.assertNotIn("private-title", joined)

    async def test_cache_logs_distinguish_hits_url_changes_and_refresh_without_urls(self):
        import re
        self.plugin._cache_enabled = True
        self.plugin._service.search = AsyncMock(return_value=SearchResponse(
            attempted_providers=["saucenao"], matches=[SourceMatch("saucenao", "private-title")]))
        self.plugin._delivery.try_send = AsyncMock(return_value=True)
        snapshots = []
        for url, options in (("https://private.example/image?secret=1", ""),
                             ("https://private.example/image?secret=1", ""),
                             ("https://private.example/image?secret=2", ""),
                             ("https://private.example/image?secret=1", "-purge")):
            event = self._simple_event("搜图", [Image(file=url)])
            self.plugin._debug_enabled = True
            with patch(ImageSearchPlugin.__module__ + ".logger.info") as log:
                _ = [r async for r in self.plugin.search_image_command(event, options)]
            snapshots.append("\n".join(str(call.args[0]) for call in log.call_args_list))
        self.assertIn("缓存=未命中:无有效记录", snapshots[0])
        self.assertIn("缓存=命中 缓存年龄=", snapshots[1])
        self.assertIn("外部搜索=无", snapshots[1])
        self.assertNotIn("搜索开始", snapshots[1])
        self.assertIn("缓存图片=1 外部搜索图片=0", snapshots[1])
        self.assertIn("缓存=未命中:无有效记录", snapshots[2])
        self.assertIn("缓存=跳过:用户重搜", snapshots[3])
        identities = [re.search(r"URL指纹=(\w+)", item).group(1) for item in snapshots]
        self.assertEqual(identities[0], identities[1])
        self.assertNotEqual(identities[0], identities[2])
        conditions = [re.search(r"条件指纹=(\w+)", item).group(1) for item in snapshots]
        self.assertEqual(len(set(conditions)), 1)
        self.assertEqual(self.plugin._service.search.await_count, 3)
        for snapshot in snapshots:
            self.assertNotIn("secret", snapshot)
            self.assertNotIn("private.example", snapshot)
            self.assertNotIn("private-title", snapshot)

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
        completion = await self.plugin.tool_search_image(event)
        event.send.assert_awaited_once_with(command_output[0])
        self.assertEqual(len(response.matches), 2)
        import json
        feedback = json.loads(completion)
        self.assertTrue(feedback["message_sent"])
        self.assertTrue(feedback["success"])
        self.assertEqual(feedback["count"], 1)
        self.assertIn("当前会话的人格语气", feedback["instruction"])
        self.assertNotIn("https://", completion)

    async def test_tool_returns_persona_feedback_after_rich_or_empty_delivery(self):
        import json
        self.plugin._get_image_url = AsyncMock(
            return_value=("https://images.example/test.jpg", None)
        )
        for has_result in (False, True):
            with self.subTest(has_result=has_result):
                self.plugin._service.search = AsyncMock(return_value=SearchResponse(
                    matches=[SourceMatch(provider="google_lens", title="Artwork")]
                    if has_result else [],
                ))
                self.plugin._delivery.try_send = AsyncMock(return_value=has_result)
                event = SimpleNamespace(plain_result=lambda text: text, send=AsyncMock())
                feedback = json.loads(await self.plugin.tool_search_image(event))
                self.assertEqual(feedback["success"], has_result)
                self.assertEqual(feedback["count"], int(has_result))
                self.assertTrue(feedback["message_sent"])
                self.assertIn("只回复一句", feedback["instruction"])
                if has_result:
                    event.send.assert_not_awaited()
                else:
                    event.send.assert_awaited_once()
                    self.assertIn("没有可展示的结果", feedback["instruction"])

    async def test_tool_explicit_channels_share_aliases_and_never_add_services(self):
        from ..core.models import ProviderOutcome
        self.plugin._get_image_url = AsyncMock(
            return_value=("https://images.example/test.jpg", None)
        )
        providers = {p.name: p for p in self.plugin._service._source_providers}
        for provider in providers.values():
            provider.search = AsyncMock(return_value=ProviderOutcome())
        for channels, selected in (
            ("Google G 谷歌 lens", ["google_lens"]),
            ("s", ["saucenao"]),
            ("google,酱 123", ["saucenao", "google_lens"]),
        ):
            with self.subTest(channels=channels):
                for provider in providers.values():
                    provider.search.reset_mock()
                event = self._simple_event("搜图 " + channels)
                await self.plugin.tool_search_image(event, "auto", channels)
                for name, provider in providers.items():
                    self.assertEqual(provider.search.await_count, int(name in selected))
                if "123" in channels:
                    self.assertEqual(event.send.await_count, 2)
                    self.assertIn("已忽略：123", event.send.await_args_list[0].args[0])
                    self.assertIn("可行渠道", event.send.await_args_list[0].args[0])
        self.plugin._get_image_url.assert_awaited()

    async def test_tool_invalid_channels_do_not_resolve_image_or_search(self):
        import json
        await self._configure_services("google_lens", "trace_moe")
        self.plugin._get_image_url = AsyncMock()
        self.plugin._service.search = AsyncMock()
        for mode, channels in (("source", "s"), ("source", "123"),
                               ("anime", "google"), ("source", ["google"])):
            with self.subTest(mode=mode, channels=channels):
                event = self._simple_event("搜图 " + channels if isinstance(channels, str) else "找出处")
                feedback = json.loads(await self.plugin.tool_search_image(event, mode, channels))
                self.assertFalse(feedback["success"])
                self.assertIn("error", feedback)
        self.plugin._get_image_url.assert_not_awaited()
        self.plugin._service.search.assert_not_awaited()

    async def test_tool_binds_current_request_when_model_omits_or_misselects_channels(self):
        import json
        from ..core.models import ProviderOutcome
        self.plugin._get_image_url = AsyncMock(
            return_value=("https://images.example/test.jpg", None)
        )
        providers = {p.name: p for p in self.plugin._service._source_providers}
        for name, provider in providers.items():
            provider.search = AsyncMock(return_value=ProviderOutcome(matches=[
                SourceMatch(provider=name, title=name, url="https://example.org/" + name),
            ]))
        for text, model_channels, selected in (
            ("搜一下谷歌", "", ["google_lens"]),
            ("搜一下谷歌", "s", ["google_lens"]),
            ("搜一下谷歌和SauceNAO", "g", ["saucenao", "google_lens"]),
        ):
            with self.subTest(text=text, model_channels=model_channels):
                for provider in providers.values():
                    provider.search.reset_mock()
                event = self._simple_event(text)
                feedback = json.loads(await self.plugin.tool_search_image(event, "source", model_channels))
                self.assertEqual(feedback["attempted_providers"], selected)
                self.assertEqual(feedback["result_providers"], selected)
                for name, provider in providers.items():
                    self.assertEqual(provider.search.await_count, int(name in selected))

    async def test_model_channels_cannot_expand_default_search_or_inject_refresh(self):
        from ..core.models import ProviderOutcome
        self.plugin._get_image_url = AsyncMock(return_value=("https://images.example/test.jpg", None))
        providers = {p.name: p for p in self.plugin._service._source_providers}
        for name, provider in providers.items():
            provider.search = AsyncMock(return_value=ProviderOutcome(matches=[
                SourceMatch(provider=name, title=name, url="https://example.org/" + name)]))
        for text in ("找下出处", "find a source", "search a source", "search a picture", "帮我找一下出处并分析画风"):
            for supplied in ("saucenao,google_lens", "g -purge", "made-up"):
                event = self._simple_event(text, [Reply(id="quoted", text="搜图 g")])
                req = self._tool_request()
                await self.plugin.filter_llm_tools(event, req)
                self.assertEqual(req.func_tool.get_tool("search_image").parameters["properties"]["channels"]["enum"], [""])
                for provider in providers.values():
                    provider.search.reset_mock()
                result = json.loads(await self.plugin.tool_search_image(event, "source", supplied))
                self.assertEqual(result["attempted_providers"], ["saucenao"])
                providers["google_lens"].search.assert_not_awaited()
                self.assertFalse(providers["saucenao"].search.await_args.args[0].refresh)

    async def test_ambiguous_channel_request_stops_before_image_resolution(self):
        self.plugin._resolve_image_request = AsyncMock()
        self.plugin._service.search = AsyncMock()
        for text in ("不要用Google，找出处", "搜一下谷歌并分析画风", '他说“搜一下谷歌”',
                     "不要用g，帮我找出处", "用酱找出处", "搜一下谷歌 -purge"):
            event = self._simple_event(text)
            result = json.loads(await self.plugin.tool_search_image(event, "source", "g"))
            self.assertFalse(result["success"], text)
            self.assertIn("明确命令", result["error"])
            event.send.assert_not_awaited()
        self.plugin._resolve_image_request.assert_not_awaited()
        self.plugin._service.search.assert_not_awaited()

    async def test_tool_bound_disabled_channel_cannot_fall_back(self):
        import json
        await self._configure_services("google_lens")
        self.plugin._get_image_url = AsyncMock()
        self.plugin._service.search = AsyncMock()
        feedback = json.loads(await self.plugin.tool_search_image(self._simple_event("搜一下SauceNAO")))
        self.assertFalse(feedback["success"])
        self.plugin._get_image_url.assert_not_awaited()
        self.plugin._service.search.assert_not_awaited()

    async def test_tool_request_schema_binds_exact_channels_without_using_quoted_text(self):
        event = self._simple_event("搜一下谷歌和SauceNAO")
        request = self._tool_request()
        await self.plugin.filter_llm_tools(event, request)
        schema = next(t["function"]["parameters"] for t in request.func_tool.openai_schema()
                      if t["function"]["name"] == "search_image")
        self.assertIn("channels", schema["required"])
        self.assertEqual(schema["properties"]["channels"]["enum"], ["谷歌 SauceNAO"])
        event = self._simple_event("找下出处", [Reply(text="搜一下谷歌", id="quoted")])
        request = self._tool_request()
        await self.plugin.filter_llm_tools(event, request)
        self.assertEqual(request.func_tool.get_tool("search_image").parameters["properties"]["channels"]["enum"], [""])

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

    async def test_llm_tool_diagnostics_distinguish_hook_outcomes_without_sensitive_data(self):
        image_event = self._simple_event("sou一下google secret-text", [Image(file="https://private.test/secret")])
        cases = [
            (SimpleNamespace(func_tool=None), "未收到工具集"),
            (SimpleNamespace(func_tool=ToolSet()), "上游未提供搜图工具"),
            (self._tool_request(), "插件检查通过"),
        ]
        for req, status in cases:
            self.plugin._debug_enabled = True
            with patch(f"{ImageSearchPlugin.__module__}.logger.info") as log:
                await self.plugin.filter_llm_tools(image_event, req)
                log.assert_called_once()
                line = log.call_args.args[0]
                self.assertIn(status, line)
                self.assertIn("阶段=插件请求钩子", line)
                self.assertIn("search_image=" + ("有" if status == "插件检查通过" else "无"), line)
                for secret in ("secret-text", "private.test", "sender", "platform:group:test"):
                    self.assertNotIn(secret, line)
        for disabled, text, status in ((True, "搜图", "插件工具已关闭"),
                                      (False, "", "纯图片已拦截")):
            self.plugin._llm_tool_enabled = not disabled
            with patch(f"{ImageSearchPlugin.__module__}.logger.info") as log:
                await self.plugin.filter_llm_tools(self._simple_event(text, [Image(file="https://site.test/i")]), self._tool_request())
                self.assertIn(status, log.call_args.args[0])
        with patch(f"{ImageSearchPlugin.__module__}.logger.info") as log:
            await self.plugin.filter_llm_tools(self._simple_event("你好"), self._tool_request())
            log.assert_not_called()
        with patch(f"{ImageSearchPlugin.__module__}.logger.info") as log:
            await self.plugin.filter_llm_tools(self._simple_event("搜一下谷歌", [Reply(id="quoted-secret")]), self._tool_request())
            self.assertIn("引用=是", log.call_args.args[0])
            self.assertNotIn("quoted-secret", log.call_args.args[0])

    async def test_bare_image_cannot_invoke_llm_search_or_mutate_shared_tools(self):
        self.plugin._resolve_image_request = AsyncMock()
        self.plugin._service.search = AsyncMock()
        for text in ("", "  "):
            event = self._simple_event(text, [Image(file="https://site.test/image")])
            req = self._tool_request()
            shared = req.func_tool
            await self.plugin.route_simple_search(event, req)
            await self.plugin.filter_llm_tools(event, req)
            self.assertIsNone(req.func_tool.get_tool("search_image"))
            self.assertIsNotNone(shared.get_tool("search_image"))
            self.assertIsNotNone(req.func_tool.get_tool("other"))
            result = json.loads(await self.plugin.tool_search_image(event))
            self.assertFalse(result["success"])
            event.send.assert_not_awaited()
        self.plugin._resolve_image_request.assert_not_awaited()
        self.plugin._service.search.assert_not_awaited()
        req = self._tool_request()
        await self.plugin.filter_llm_tools(
            self._simple_event("帮我查一下出处", [Image(file="https://site.test/image")]), req)
        self.assertIsNotNone(req.func_tool.get_tool("search_image"))

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
        self.assertNotIn("channels", original.parameters["properties"])
        self.assertIn("channels", updated.parameters["properties"])
        self.assertIn("谷歌=google_lens", updated.parameters["properties"]["channels"]["description"])
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
        self.assertNotIn("channels", tool.parameters["properties"])
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


    async def test_cache_hit_is_free_and_refresh_updates_result(self):
        import json
        self.plugin._cache_enabled = True
        self.plugin._daily_limit = 1
        event = self._simple_event("搜图", [Image(file="https://img.test/a")])
        value = SearchResponse(matches=[SourceMatch("saucenao", "first")], attempted_providers=["saucenao"])
        self.plugin._service.search = AsyncMock(return_value=value)
        await self.plugin.tool_search_image(event)
        event.send.reset_mock()
        feedback = json.loads(await self.plugin.tool_search_image(event))
        self.assertEqual(self.plugin._service.search.await_count, 1)
        self.assertEqual(feedback["cached_images"], 1)
        self.assertEqual(feedback["attempted_providers"], [])
        self.assertIn("[缓存]", event.send.await_args_list[0].args[0])
        self.assertEqual(event.send.await_count, 1)
        user = self.plugin._quota_user(event)
        self.assertEqual(self.plugin._search_state().count(user, self.plugin._day()), 1)
        # Refresh obeys quota even when an old cache entry exists.
        event.send.reset_mock()
        await self.plugin.tool_search_image(event, refresh=True)
        self.assertEqual(self.plugin._service.search.await_count, 1)
        self.assertIn("上限", event.send.await_args.args[0])
        self.plugin._daily_limit = 2
        self.plugin._service.search.return_value = SearchResponse(matches=[SourceMatch("saucenao", "new")])
        await self.plugin.tool_search_image(event, refresh=True)
        self.assertEqual(self.plugin._service.search.await_count, 2)
        self.assertTrue(self.plugin._service.search.await_args.args[0].refresh)
        event.send.reset_mock()
        await self.plugin.tool_search_image(event)
        self.assertIn("new", event.send.await_args.args[0])

    async def test_empty_and_failed_search_refund_but_filtered_candidate_counts(self):
        self.plugin._daily_limit = 1
        event = self._simple_event("搜图", [Image(file="https://img.test/a")])
        self.plugin._service.search = AsyncMock(return_value=SearchResponse(
            attempted_providers=["saucenao"], failed_providers=["saucenao"]))
        await self.plugin.tool_search_image(event)
        user = self.plugin._quota_user(event)
        self.assertEqual(self.plugin._search_state().count(user, self.plugin._day()), 0)
        self.plugin._service.search.return_value = SearchResponse(attempted_providers=["saucenao"])
        await self.plugin.tool_search_image(event)
        self.assertEqual(self.plugin._search_state().count(user, self.plugin._day()), 0)
        self.plugin._service.search.return_value = SearchResponse(
            attempted_providers=["saucenao"], filtered_providers={"saucenao": 60})
        await self.plugin.tool_search_image(event)
        self.assertEqual(self.plugin._search_state().count(user, self.plugin._day()), 1)
        await self.plugin.tool_search_image(event)
        self.assertEqual(self.plugin._service.search.await_count, 3)

    async def test_admin_is_exempt_and_group_private_share_quota(self):
        self.plugin._daily_limit = 1
        self.plugin._service.search = AsyncMock(return_value=SearchResponse(
            matches=[SourceMatch("saucenao", "work")]))
        first = self._simple_event("搜图", [Image(file="https://img.test/a")])
        first.get_platform_id = lambda: "qq"
        second = self._simple_event("搜图", [Image(file="https://img.test/b")])
        second.get_platform_id = lambda: "qq"
        second.unified_msg_origin = "qq:FriendMessage:sender"
        await self.plugin.tool_search_image(first)
        await self.plugin.tool_search_image(second)
        self.assertEqual(self.plugin._service.search.await_count, 1)
        second.is_admin = lambda: True
        await self.plugin.tool_search_image(second)
        await self.plugin.tool_search_image(second)
        self.assertEqual(self.plugin._service.search.await_count, 3)

    async def test_batch_limit_order_partial_failure_and_per_image_quota(self):
        self.plugin._batch_limit = 2
        event = self._simple_event("搜图", [Image(file=f"https://img.test/{i}") for i in range(3)])
        self.plugin._service.search = AsyncMock(side_effect=[
            SearchResponse(matches=[SourceMatch("google_lens", "work")]),
            SearchResponse(attempted_providers=["google_lens"], failed_providers=["google_lens"]),
        ])
        output = [message async for message in self.plugin.search_image_command(event, "g -purge")]
        self.assertIn("后 1 张未处理", output[0])
        self.assertFalse(any("第 1/2" in message or "第 2/2" in message for message in output))
        self.assertIn("第 2 张图片", output[-1])
        self.assertIn("work", output[-2])
        self.assertEqual(len(output), 3)
        calls = self.plugin._service.search.await_args_list
        self.assertEqual([call.args[0].url for call in calls], ["https://img.test/0", "https://img.test/1"])
        self.assertTrue(all(call.args[0].refresh for call in calls))
        self.assertTrue(all(call.kwargs["provider_names"] == ["google_lens"] for call in calls))
        self.assertEqual(self.plugin._search_state().count(self.plugin._quota_user(event), self.plugin._day()), 1)

    async def test_command_and_tool_deliver_batch_once_after_all_searches(self):
        self.plugin._service.search = AsyncMock(return_value=SearchResponse(
            matches=[SourceMatch("google_lens", "work", url="https://site.test/same")]))
        self.plugin._delivery.try_send_batch = AsyncMock(return_value=True)
        self.plugin._delivery.try_send = AsyncMock(return_value=True)
        for use_tool in (False, True):
            event = self._simple_event("搜图", [Image(file=f"https://img.test/{i}") for i in range(2)])
            self.plugin._delivery.try_send_batch.reset_mock()
            self.plugin._service.search.reset_mock()
            if use_tool:
                await self.plugin.tool_search_image(event)
            else:
                self.assertEqual([r async for r in self.plugin.search_image_command(event, "")], [])
            self.plugin._delivery.try_send_batch.assert_awaited_once()
            self.assertEqual(self.plugin._service.search.await_count, 2)
            entries = self.plugin._delivery.try_send_batch.await_args.args[1]
            self.assertEqual([item.image_index for item in entries], [1, 2])
        self.plugin._delivery.try_send.assert_not_awaited()

    async def test_batch_cache_is_marked_in_results_without_separate_notices(self):
        self.plugin._cache_enabled = True
        self.plugin._service.search = AsyncMock(return_value=SearchResponse(
            matches=[SourceMatch("saucenao", "work")], attempted_providers=["saucenao"]))
        self.plugin._delivery.try_send_batch = AsyncMock(return_value=True)
        event = self._simple_event("搜图", [Image(file=f"https://img.test/{i}") for i in range(2)])
        _ = [r async for r in self.plugin.search_image_command(event, "")]
        output = [r async for r in self.plugin.search_image_command(event, "")]
        self.assertEqual(output, [])
        entries = self.plugin._delivery.try_send_batch.await_args.args[1]
        self.assertTrue(all(item.cached and not item.message for item in entries))
        self.assertEqual(self.plugin._service.search.await_count, 2)

    async def test_changed_urls_hit_stable_identity_across_batch_command_and_quoted_tool(self):
        import json
        self.plugin._cache_enabled = True
        self.plugin._service.search = AsyncMock(return_value=SearchResponse(
            matches=[SourceMatch("saucenao", "work")], attempted_providers=["saucenao"]))
        self.plugin._delivery.try_send_batch = AsyncMock(return_value=True)
        def payload(prefix):
            return {"message": [{"type": "image", "data": {
                "file_unique": f"stable-{i}", "url": f"https://img.test/{prefix}/{i}"}}
                for i in range(2)]}
        first = self._simple_event("搜图", [Image(file=f"https://img.test/first/{i}") for i in range(2)])
        first.message_obj = SimpleNamespace(raw_message=payload("first"))
        _ = [r async for r in self.plugin.search_image_command(first, "")]
        quoted = self._simple_event("搜图", [Reply(id="123", chain=[Image(file=f"https://img.test/second/{i}") for i in range(2)])])
        quoted.bot = SimpleNamespace(api=SimpleNamespace(call_action=AsyncMock(return_value=payload("second"))))
        feedback = json.loads(await self.plugin.tool_search_image(quoted, mode="auto"))
        self.assertEqual(feedback["cached_images"], 2)
        self.assertEqual(feedback["attempted_providers"], [])
        self.assertEqual(self.plugin._service.search.await_count, 2)
        self.assertTrue(all(item.cached for item in self.plugin._delivery.try_send_batch.await_args.args[1]))
        self.assertEqual(quoted.send.await_count, 0)
        _ = [r async for r in self.plugin.search_image_command(first, "-purge")]
        self.assertEqual(self.plugin._service.search.await_count, 4)
        different = self._simple_event("搜图", [Image(file=f"https://img.test/first/{i}") for i in range(2)])
        changed_payload = payload("first")
        for segment in changed_payload["message"]:
            segment["data"]["file_unique"] += "-different"
        different.message_obj = SimpleNamespace(raw_message=changed_payload)
        _ = [r async for r in self.plugin.search_image_command(different, "")]
        self.assertEqual(self.plugin._service.search.await_count, 6)
        self.assertTrue(all(not item.cached for item in self.plugin._delivery.try_send_batch.await_args.args[1]))

    async def test_waiting_image_preserves_stable_identity_for_later_resend(self):
        self.plugin._cache_enabled = True
        self.plugin._service.search = AsyncMock(return_value=SearchResponse(
            matches=[SourceMatch("saucenao", "work")], attempted_providers=["saucenao"]))
        initial = self._simple_event("搜图")
        output = self.plugin.search_image_command(initial, "")
        self.assertIn("发送图片", await anext(output))
        later = self._simple_event("", [Image(file="https://img.test/first")])
        later.message_obj = SimpleNamespace(raw_message={"message": [{"type": "image", "data": {
            "file": "a" * 32 + ".image", "url": "https://img.test/first"}}]})
        await self.plugin.capture_waiting_image(later)
        _ = [r async for r in output]
        resent = self._simple_event("搜图", [Image(file="https://img.test/second")])
        resent.message_obj = SimpleNamespace(raw_message={"message": [{"type": "image", "data": {
            "md5": "a" * 32, "url": "https://img.test/second"}}]})
        messages = [r async for r in self.plugin.search_image_command(resent, "")]
        self.assertEqual(self.plugin._service.search.await_count, 1)
        self.assertTrue(any("[缓存]" in message for message in messages))

    async def test_purge_overwrites_success_and_keeps_old_record_on_failure(self):
        self.plugin._cache_enabled = True
        event = self._simple_event("搜图", [Image(file="https://img.test/same")])
        self.plugin._delivery.try_send = AsyncMock(return_value=True)
        self.plugin._service.search = AsyncMock(side_effect=[
            SearchResponse(matches=[SourceMatch("saucenao", "old")], attempted_providers=["saucenao"]),
            SearchResponse(matches=[SourceMatch("saucenao", "new")], attempted_providers=["saucenao"]),
            SearchResponse(attempted_providers=["saucenao"], failed_providers=["saucenao"]),
        ])
        _ = [r async for r in self.plugin.search_image_command(event, "")]
        _ = [r async for r in self.plugin.search_image_command(event, "-purge")]
        _ = [r async for r in self.plugin.search_image_command(event, "")]
        self.assertEqual(self.plugin._service.search.await_count, 2)
        self.assertEqual(self.plugin._delivery.try_send.await_args.args[1].matches[0].title, "new")
        failed = [r async for r in self.plugin.search_image_command(event, "-purge")]
        self.assertFalse(any("[缓存]" in message for message in failed))
        self.assertEqual(self.plugin._delivery.try_send.await_args.args[1].matches, [])
        _ = [r async for r in self.plugin.search_image_command(event, "")]
        self.assertEqual(self.plugin._service.search.await_count, 3)
        self.assertEqual(self.plugin._delivery.try_send.await_args.args[1].matches[0].title, "new")

    async def test_refresh_order_and_anime_options(self):
        self.plugin._service.search = AsyncMock(return_value=SearchResponse())
        event = self._simple_event("搜图", [Image(file="https://img.test/a")])
        for text in ("-purge g s", "g -purge s", "g s -purge"):
            _ = [r async for r in self.plugin.search_image_command(event, text)]
            call = self.plugin._service.search.await_args
            self.assertTrue(call.args[0].refresh)
            self.assertEqual(call.kwargs["provider_names"], ["google_lens", "saucenao"])
        _ = [r async for r in self.plugin.search_anime_command(event, "-purge")]
        self.assertEqual(self.plugin._service.search.await_args.args[1].value, "anime")
        self.assertTrue(self.plugin._service.search.await_args.args[0].refresh)

    async def test_wait_handoff_keeps_batch_and_refresh(self):
        import asyncio
        from ..core.models import SearchMode
        event = self._simple_event("搜图 -purge")
        self.plugin._service.search = AsyncMock(return_value=SearchResponse())
        output = self.plugin._run_search(event, SearchMode.AUTO, refresh=True)
        self.assertIn("发送图片", await anext(output))
        later = self._simple_event("", [Image(file="https://img.test/a"), Image(file="https://img.test/b")])
        await self.plugin.capture_waiting_image(later)
        self.assertTrue(later.is_stopped())
        _ = [message async for message in output]
        self.assertEqual(self.plugin._service.search.await_count, 2)
        self.assertTrue(all(call.args[0].refresh for call in self.plugin._service.search.await_args_list))

    async def test_feedback_once_after_actual_search_budget_and_not_for_cache(self):
        import asyncio
        self.plugin._feedback_delay = 0.01
        self.plugin._cache_enabled = True
        async def search(*args, **kwargs):
            await asyncio.sleep(0.02)
            return SearchResponse(matches=[SourceMatch("saucenao", "work")])
        self.plugin._service.search = AsyncMock(side_effect=search)
        event = self._simple_event("搜图", [Image(file="https://img.test/a"), Image(file="https://img.test/b")])
        await self.plugin.tool_search_image(event)
        self.assertEqual(sum("正在搜索" in call.args[0] for call in event.send.await_args_list), 1)
        event.send.reset_mock()
        await self.plugin.tool_search_image(event)
        self.assertFalse(any("正在搜索" in call.args[0] for call in event.send.await_args_list))

    async def test_cancellation_refunds_and_cleans_feedback(self):
        import asyncio
        started = asyncio.Event()
        async def search(*args, **kwargs):
            started.set()
            await asyncio.Event().wait()
        self.plugin._service.search = AsyncMock(side_effect=search)
        event = self._simple_event("搜图", [Image(file="https://img.test/a")])
        task = asyncio.create_task(self.plugin.tool_search_image(event))
        await started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(self.plugin._search_state().count(self.plugin._quota_user(event), self.plugin._day()), 0)
        event.send.assert_not_awaited()

    async def test_day_uses_bot_timezone(self):
        from datetime import datetime, timezone
        self.plugin.context = SimpleNamespace(get_config=lambda: {"timezone": "America/Los_Angeles"})
        instant = datetime(2026, 10, 11, 0, 30, tzinfo=timezone.utc)
        with patch(ImageSearchPlugin.__module__ + ".datetime") as clock:
            clock.now.side_effect = lambda zone: instant.astimezone(zone)
            self.assertEqual(self.plugin._day(), "2026-10-10")
            self.plugin.context.get_config = lambda: {"timezone": "Asia/Shanghai"}
            self.assertEqual(self.plugin._day(), "2026-10-11")

    async def test_tool_refresh_schema_and_current_request(self):
        import json
        event = self._simple_event("搜图 g -purge", [Image(file="https://img.test/a")])
        request = self._tool_request()
        await self.plugin.filter_llm_tools(event, request)
        tool = request.func_tool.get_tool("search_image")
        self.assertEqual(tool.parameters["properties"]["refresh"]["enum"], [True])
        self.assertEqual(tool.parameters["properties"]["channels"]["enum"], ["g"])
        self.plugin._service.search = AsyncMock(return_value=SearchResponse())
        await self.plugin.tool_search_image(event)
        self.assertTrue(self.plugin._service.search.await_args.args[0].refresh)
        self.assertEqual(self.plugin._service.search.await_args.kwargs["provider_names"], ["google_lens"])
        invalid = json.loads(await self.plugin.tool_search_image(event, refresh="yes"))
        self.assertIn("布尔", invalid["error"])

    async def test_concurrent_requests_cannot_exceed_daily_quota(self):
        import asyncio
        self.plugin._daily_limit = 1
        started, release = asyncio.Event(), asyncio.Event()
        async def search(*args, **kwargs):
            started.set()
            await release.wait()
            return SearchResponse(matches=[SourceMatch("saucenao", "work")])
        self.plugin._service.search = AsyncMock(side_effect=search)
        event = self._simple_event("搜图", [Image(file="https://img.test/a")])
        first = asyncio.create_task(self.plugin.tool_search_image(event))
        await started.wait()
        await self.plugin.tool_search_image(event)
        release.set()
        await first
        self.assertEqual(self.plugin._service.search.await_count, 1)
        self.assertTrue(any("上限" in call.args[0] for call in event.send.await_args_list))

    async def test_unexpected_single_image_failure_does_not_stop_batch(self):
        event = self._simple_event("搜图", [Image(file="https://img.test/a"), Image(file="https://img.test/b")])
        self.plugin._service.search = AsyncMock(side_effect=[RuntimeError("secret upstream URL"),
            SearchResponse(matches=[SourceMatch("saucenao", "second")])])
        output = [message async for message in self.plugin.search_image_command(event, "")]
        self.assertTrue(any("这张图片搜索失败" in message for message in output))
        self.assertTrue(any("second" in message for message in output))
        self.assertFalse(any("secret" in message for message in output))
        self.assertEqual(self.plugin._search_state().count(self.plugin._quota_user(event), self.plugin._day()), 1)

    async def test_delivery_wait_does_not_trigger_search_feedback(self):
        import asyncio
        self.plugin._feedback_delay = 0.01
        self.plugin._service.search = AsyncMock(return_value=SearchResponse(matches=[SourceMatch("saucenao", "work")]))
        async def deliver(*args, **kwargs):
            await asyncio.sleep(0.02)
            return True
        self.plugin._delivery.try_send_batch = AsyncMock(side_effect=deliver)
        event = self._simple_event("搜图", [Image(file="https://img.test/a"), Image(file="https://img.test/b")])
        await self.plugin.tool_search_image(event)
        self.assertFalse(any("正在搜索" in call.args[0] for call in event.send.await_args_list))

    async def test_storage_is_confined_and_survives_plugin_reload(self):
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            self.plugin.context = SimpleNamespace(get_config=lambda: {"timezone": "Asia/Shanghai"})
            with patch(ImageSearchPlugin.__module__ + ".StarTools.get_data_dir", return_value=Path(directory)) as data_dir:
                state = self.plugin._search_state()
                self.assertTrue(state.reserve("user", "2026-10-10", 1))
                state.close()
                self.plugin._state = None
                self.assertFalse(self.plugin._search_state().reserve("user", "2026-10-10", 1))
                self.assertTrue(all(call.args == ("astrbot_plugin_image_search",) for call in data_dir.call_args_list))
                self.assertEqual([file.name for file in Path(directory).iterdir()], ["search_state.sqlite3"])
                self.plugin._state.close()
                self.plugin._state = None

    async def test_quoted_batch_is_supported_and_direct_images_take_priority(self):
        quoted = Reply(id="quoted", chain=[Image(file="https://img.test/q1"), Image(file="https://img.test/q2")])
        event = self._simple_event("搜图", [quoted])
        with patch(ImageSearchPlugin.__module__ + ".extract_quoted_message_images", AsyncMock(return_value=["https://img.test/q1", "https://img.test/q2"])) as extract:
            images, total = await self.plugin._resolve_images(event)
            self.assertEqual([url for url, _ in images], ["https://img.test/q1", "https://img.test/q2"])
            self.assertEqual(total, 2)
            extract.assert_not_awaited()
            extract.reset_mock()
            event = self._simple_event("搜图", [Image(file="https://img.test/a"), Image(file="https://img.test/b"), quoted])
            images, total = await self.plugin._resolve_images(event)
            self.assertEqual([url for url, _ in images], ["https://img.test/a", "https://img.test/b"])
            extract.assert_not_awaited()


    async def test_localized_single_reply_alternatives_are_one_image(self):
        reply = Reply(id="123", chain=[Image(file="/tmp/local.jpg", url="/tmp/local.jpg")])
        event = self._simple_event("搜图", [reply])
        self.plugin._service.search = AsyncMock(return_value=SearchResponse(
            matches=[SourceMatch("saucenao", "work")]))
        with patch(ImageSearchPlugin.__module__ + ".extract_quoted_message_images",
                   AsyncMock(return_value=["/tmp/local.jpg", "https://img.test/original"])) as extract, patch.object(
                   Image, "register_to_file_service", AsyncMock()) as publish:
            images, total = await self.plugin._resolve_images(event)
            self.assertEqual(images, [("https://img.test/original", None)])
            self.assertEqual(total, 1)
            output = [message async for message in self.plugin.search_image_command(event, "")]
            self.assertFalse(any("第 1/2" in message or "文件服务" in message for message in output))
            self.assertEqual(self.plugin._service.search.await_count, 1)
            publish.assert_not_awaited()

    async def test_structured_reply_counts_segments_not_alternative_references(self):
        self.plugin._cache_enabled = True
        reply = Reply(id="123", chain=[Image(file="/tmp/local.jpg")])
        event = self._simple_event("搜图", [reply])
        call_action = AsyncMock(return_value={"data": {"message": [
            {"type": "image", "data": {"file": "/tmp/local.jpg", "url": "https://img.test/original"}},
        ]}})
        event.bot = SimpleNamespace(api=SimpleNamespace(call_action=call_action))
        self.plugin._service.search = AsyncMock(return_value=SearchResponse(
            matches=[SourceMatch("saucenao", "work")]))
        with patch.object(Image, "register_to_file_service", AsyncMock()) as publish:
            first = [message async for message in self.plugin.search_image_command(event, "")]
            second = [message async for message in self.plugin.search_image_command(event, "")]
            self.assertFalse(any("第 " in message for message in first))
            self.assertTrue(any("[缓存]" in message for message in second))
            self.assertEqual(self.plugin._service.search.await_count, 1)
            self.assertEqual(self.plugin._search_state().count(self.plugin._quota_user(event), self.plugin._day()), 1)
            publish.assert_not_awaited()
            call_action.assert_awaited_with("get_msg", message_id=123)

    async def test_localized_multi_reply_preserves_original_order(self):
        reply = Reply(id="123", chain=[Image(file="/tmp/a.jpg"), Image(file="/tmp/b.jpg")])
        event = self._simple_event("搜图", [reply])
        event.bot = SimpleNamespace(api=SimpleNamespace(call_action=AsyncMock(return_value={"message": [
            {"type": "image", "data": {"file": "/tmp/a.jpg", "url": "https://img.test/a"}},
            {"type": "image", "data": {"file": "/tmp/b.jpg", "url": "https://img.test/b"}},
        ]})))
        with patch.object(Image, "register_to_file_service", AsyncMock()) as publish:
            images, total = await self.plugin._resolve_images(event)
            self.assertEqual(total, 2)
            self.assertEqual(images, [("https://img.test/a", None), ("https://img.test/b", None)])
            publish.assert_not_awaited()

if __name__ == "__main__":
    unittest.main()
