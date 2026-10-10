"""Unified image source and anime screenshot search for AstrBot."""

from __future__ import annotations

import json
import hashlib
import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
import math
import re
import time
from uuid import uuid4
from copy import copy, deepcopy
from typing import Any

from astrbot.api import llm_tool, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.core.star.filter.command import GreedyStr
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star, StarTools
from astrbot.core import AstrBotConfig
from astrbot.core.utils.quoted_message_parser import extract_quoted_message_images
from astrbot.core.message.components import Image, Plain, Reply

from .core.image_wait import (
    ImageResolution,
    ImageWaitCoordinator,
    WaitOutcome,
)
from .core import ImageAsset, SearchMode, SearchService
from .core.providers import (
    Ascii2dProvider,
    GoogleLensProvider,
    SauceNaoProvider,
    TraceMoeProvider,
)
from .image_input import find_public_image_url, find_raw_image_url, first_public_url, image_candidates_from_message, image_cache_identity, image_identities_from_message
from .command_aliases import build_channel_aliases
from .simple_requests import parse_simple_request, parse_tool_channels, split_search_options
from .core.search_state import SearchState, cache_key
from .presentation import BatchResult, build_batch_cards, count_search_results, format_search_response, provider_label
from .result_delivery import ResultDelivery
from .tool_contract import available_modes, completion_instruction, tool_description
from .runtime.http_client import AioHttpJsonClient
from .runtime.curl_client import CurlCffiWebClient
from .runtime.source_pages import SourcePageClient
from .core.source_enrichment import SourceEnricher

_DEFAULT_USER_AGENT = "AstrBot Image Search/0.1"
_FILE_SERVICE_ERROR = (
    "图片不是公网 URL，且 AstrBot 文件服务不可用。"
    "请配置 callback_api_base，或发送可公开访问的图片。"
)


class ImageSearchPlugin(Star):
    """Search general image sources and anime screenshots."""

    def __init__(self, context: Context, config: AstrBotConfig) -> None:
        super().__init__(context)
        self._config = _as_dict(config)
        llm_config = _section(self._config, "llm")
        self._llm_tool_enabled = _as_bool(llm_config.get("enabled"), True)
        command_config = _section(self._config, "command")
        self._channel_aliases, invalid_aliases = build_channel_aliases(
            _section(self._config, "providers")
        )
        if invalid_aliases:
            logger.warning(
                f"[ImageSearch] 已忽略 {invalid_aliases} 项无效或冲突的渠道别名配置"
            )
        self._simple_search_enabled = _as_bool(
            command_config.get("simple_natural_language"), True
        )
        wait_timeout = _bounded_float(
            command_config.get("image_wait_timeout_seconds"), 60, 10, 120
        )
        self._image_wait = ImageWaitCoordinator(wait_timeout)
        self._batch_limit = _positive_int(command_config.get("max_batch_images"), 5)
        self._daily_limit = _bounded_int(command_config.get("daily_search_limit"), 30, 0, 1000000)
        cache_config = _section(self._config, "cache")
        self._cache_enabled = _as_bool(cache_config.get("enabled"), True)
        self._cache_ttl = _positive_int(cache_config.get("expire_seconds"), 172800)
        self._state = None
        self._feedback_delay = 3

        network = _section(self._config, "network")
        self._timeout_seconds = _positive_float(
            network.get("timeout_seconds"),
            30,
        )
        self._http = AioHttpJsonClient(
            proxy_url=str(network.get("proxy_url") or ""),
            user_agent=str(network.get("user_agent") or _DEFAULT_USER_AGENT),
        )

        self._web_http = CurlCffiWebClient(
            proxy_url=str(network.get("proxy_url") or ""),
        )
        output = _section(self._config, "output")
        self._delivery = ResultDelivery(
            merged_forward_threshold=_bounded_int(
                output.get("merged_forward_threshold"), 2, 0, 10,
            ),
            show_thumbnails=_as_bool(output.get("show_thumbnails"), True),
            max_preview_images=min(20, _positive_int(output.get("max_preview_images"), 6)),
            proxy_url=str(network.get("proxy_url") or ""),
            user_agent=str(network.get("user_agent") or _DEFAULT_USER_AGENT),
        )
        self._source_pages = SourcePageClient(
            proxy_url=str(network.get("proxy_url") or ""),
            user_agent=str(network.get("user_agent") or _DEFAULT_USER_AGENT),
        )
        source_enricher = (
            SourceEnricher(self._source_pages)
            if _as_bool(output.get("resolve_original_source"), True) else None
        )
        source_providers = []
        anime_providers = []
        providers = _section(self._config, "providers")

        sauce = _section(providers, "saucenao")
        sauce_key = str(sauce.get("api_key") or "").strip()
        if _as_bool(sauce.get("enabled"), True) and sauce_key:
            source_providers.append(
                SauceNaoProvider(
                    self._http,
                    api_key=sauce_key,
                    max_results=_positive_int(sauce.get("max_results"), 5),
                    min_similarity=_percentage_value(sauce.get("min_similarity"), 60),
                    timeout_seconds=self._timeout_seconds,
                )
            )
        elif _as_bool(sauce.get("enabled"), True):
            logger.warning("[ImageSearch] 未配置 SauceNAO API Key，该服务未启用")

        google = _section(providers, "google_lens")
        serpapi_keys = google.get("serpapi_keys")
        if not isinstance(serpapi_keys, list):
            serpapi_keys = []
        normalized_keys = [
            key.strip() for key in serpapi_keys if isinstance(key, str) and key.strip()
        ]
        if _as_bool(google.get("enabled"), True) and normalized_keys:
            source_providers.append(
                GoogleLensProvider(
                    self._http,
                    api_keys=normalized_keys,
                    max_results=_positive_int(google.get("max_results"), 5),
                    search_type=str(google.get("search_type") or "all"),
                    language=str(google.get("language") or "zh-cn"),
                    country=str(google.get("country") or ""),
                    safe_search=str(google.get("safe_search", "off")),
                    auto_crop=_as_bool(google.get("auto_crop"), False),
                    no_cache=_as_bool(google.get("no_cache"), False),
                    timeout_seconds=self._timeout_seconds,
                )
            )
        elif _as_bool(google.get("enabled"), True):
            logger.warning("[ImageSearch] 未配置 SerpAPI Key，Google Lens 未启用")

        ascii2d = _section(providers, "ascii2d")
        if _as_bool(ascii2d.get("enabled"), True):
            source_providers.append(
                Ascii2dProvider(
                    self._web_http,
                    session_id=str(ascii2d.get("session_id") or ""),
                    cf_clearance=str(ascii2d.get("cf_clearance") or ""),
                    timeout_seconds=self._timeout_seconds,
                )
            )

        trace = _section(providers, "trace_moe")
        if _as_bool(trace.get("enabled"), True):
            anime_providers.append(
                TraceMoeProvider(
                    self._http,
                    token=str(trace.get("token") or ""),
                    max_results=_positive_int(trace.get("max_results"), 3),
                    min_similarity=_percentage(trace.get("min_similarity"), 85),
                    cut_borders=_as_bool(trace.get("cut_borders"), True),
                    timeout_seconds=self._timeout_seconds,
                )
            )

        followup = str(sauce.get("low_similarity_provider") or "off").strip().lower()
        followup = {"google": "google_lens"}.get(followup, followup)
        if followup not in {"off", "ascii2d", "google_lens"}:
            logger.warning("[ImageSearch] 无效的低相似度补搜配置，已关闭补搜")
            followup = "off"
        self._service = SearchService(
            source_providers=source_providers,
            anime_providers=anime_providers,
            anime_followup_providers=anime_providers,
            source_enricher=source_enricher,
            low_similarity_provider=followup,
        )
        self._has_providers = bool(source_providers or anime_providers)
        logger.info(
            "[ImageSearch] 已加载 "
            f"{len(source_providers)} 个来源搜索服务和 "
            f"{len(anime_providers)} 个动画搜索服务；默认来源候选："
            + " > ".join(self._service.source_plan_names)
        )

    @filter.platform_adapter_type(filter.PlatformAdapterType.ALL, priority=1)
    async def capture_waiting_image(self, event: AstrMessageEvent) -> None:
        """Consume a later image only when this sender has an active wait."""
        key = self._wait_key(event)
        if not await self._image_wait.has_pending(key):
            return

        resolution = await self._resolve_image_request(event)
        images = list(resolution.images)
        if not images:
            return
        delivered = await self._image_wait.deliver(
            key,
            resolution,
        )
        if delivered:
            event.stop_event()

    @filter.command("搜图")
    async def search_image_command(self, event: AstrMessageEvent, channels: GreedyStr):
        """搜索图片；指定渠道时只搜索所选渠道，不自动补搜。"""
        async for result in self._search_source_channels(event, channels):
            yield result

    async def _search_source_channels(
        self, event: AstrMessageEvent, channels: str,
        *, image_resolution: ImageResolution | tuple[str | None, str | None] | None = None,
        refresh: bool = False,
    ):
        channels, option = split_search_options(channels)
        refresh = refresh or option
        provider_names, notice = self._resolve_source_channels(channels)
        if notice:
            yield event.plain_result(notice)
        if provider_names == []:
            return
        async for result in self._run_search(
            event, SearchMode.AUTO, provider_names=provider_names,
            image_resolution=image_resolution,
            refresh=refresh,
        ):
            yield result

    def _resolve_source_channels(self, channels: str) -> tuple[list[str] | None, str | None]:
        """Share exact channel selection and diagnostics across commands and tools."""
        aliases = self._channel_aliases
        provider_names = None
        notice = None
        if channels.strip():
            provider_names = list(
                dict.fromkeys(
                    aliases.get(name, name)
                    for name in re.split(r"[\s,，、]+", channels.strip().lower())
                )
            )
            unavailable = [
                name
                for name in provider_names
                if name not in self._service.source_provider_names
            ]
            if unavailable:
                provider_names = [
                    name for name in provider_names if name not in unavailable
                ]
                notice = (
                    "搜索渠道不可用，已忽略："
                    + "、".join(provider_label(name) for name in unavailable)
                    + "\n当前启用渠道："
                    + "、".join(provider_label(name) for name in self._service.source_provider_names)
                    + (
                        "\n已使用输入的可行渠道进行搜索：" + "、".join(provider_label(name) for name in provider_names)
                        if provider_names
                        else ""
                    )
                )
        return provider_names, notice

    @filter.command("搜番")
    async def search_anime_command(self, event: AstrMessageEvent, options: GreedyStr = ""):
        """搜索附带或回复动画截图的番剧与时间点。"""
        remaining, refresh = split_search_options(options)
        if remaining:
            yield event.plain_result("搜番仅支持 -purge 参数。")
            return
        async for result in self._run_search(event, SearchMode.ANIME, refresh=refresh):
            yield result

    @filter.on_llm_request(priority=1)
    async def route_simple_search(
        self, event: AstrMessageEvent, req: ProviderRequest,
    ) -> None:
        """Take explicit simple requests over before calling the language model."""
        del req
        if not self._simple_search_enabled or event.is_stopped():
            return
        # Do not match quoted text, forwarded nodes, or image captions/metadata.
        text = "".join(
            component.text for component in event.get_messages()
            if isinstance(component, Plain)
        )
        request = parse_simple_request(text, self._channel_aliases)
        if request is None:
            return
        image_resolution = None
        if request.requires_image:
            image_resolution = await self._resolve_image_request(event)
            if not image_resolution.images:
                return
        # Stopping here makes the core hook skip the model and all later hooks;
        # results are sent directly rather than relying on the stopped pipeline.
        event.stop_event()
        async for result in self._search_source_channels(
            event, request.channels, image_resolution=image_resolution,
            refresh=request.refresh,
        ):
            await event.send(result)

    @filter.on_llm_request(priority=-1)
    async def filter_llm_tools(
        self,
        event: AstrMessageEvent,
        req: ProviderRequest,
    ) -> None:
        if req.func_tool is None:
            return
        tool = req.func_tool.get_tool("search_image")
        if tool is None:
            return
        # Tool instances may be shared by requests; only modify this request.
        req.func_tool = copy(req.func_tool)
        req.func_tool.tools = list(req.func_tool.tools)
        if not self._llm_tool_enabled or not self._has_providers:
            req.func_tool.remove_tool("search_image")
            return
        source_names = self._service.source_provider_names
        anime_names = self._service.anime_provider_names
        modes = available_modes(source_names, anime_names)
        tool = copy(tool)
        tool.description = tool_description(source_names, anime_names)
        tool.description += f"\nProcess at most {self._batch_limit} images per call."
        if source_names:
            tool.description += (
                "\nSauceNAO low-similarity follow-up: " + self._service.low_similarity_provider
                + ". Explicit channel commands never add other channels."
            )
        tool.parameters = deepcopy(tool.parameters)
        tool.parameters["properties"]["mode"].update(
            {
                "enum": list(modes),
                "default": modes[0],
                "description": "Search intent. Allowed: " + ", ".join(modes),
            }
        )
        if source_names:
            aliases = [
                f"{name}={target}" for name, target in self._channel_aliases.items()
                if target in source_names
            ]
            tool.parameters["properties"]["channels"] = {
                "type": "string", "default": "",
                "description": (
                    "Use the source channels explicitly requested by the user; "
                    "separate multiple names with spaces or commas. Enabled: "
                    + ", ".join(source_names) + ". Aliases: " + ", ".join(aliases)
                    + ". Pass an empty string only when the user did not select channels. "
                    "Explicit channels never add fallback or anime search. "
                    "Only valid for auto/source, not anime."
                ),
            }
            requested = self._requested_source_channels(event)
            if requested:
                tool.parameters["properties"]["channels"]["enum"] = [requested]
                tool.parameters["properties"]["channels"]["default"] = requested
            required = list(tool.parameters.get("required", []))
            if "channels" not in required:
                required.append("channels")
            tool.parameters["required"] = required
        else:
            tool.parameters["properties"].pop("channels", None)
            if "required" in tool.parameters:
                tool.parameters["required"] = [
                    name for name in tool.parameters["required"] if name != "channels"
                ]
        tool.parameters["properties"]["refresh"] = {
            "type": "boolean", "default": False,
            "description": "True only when the user explicitly requests a fresh search without cache (-purge). Applies to every attached image.",
        }
        request = parse_simple_request(self._current_text(event), self._channel_aliases)
        if request is not None and request.refresh:
            tool.parameters["properties"]["refresh"].update({"enum": [True], "default": True})
        req.func_tool.add_tool(tool)

    @staticmethod
    def _current_text(event) -> str:
        get_messages = getattr(event, "get_messages", None)
        if not callable(get_messages):
            return ""
        return "".join(component.text for component in get_messages()
                       if isinstance(component, Plain))

    def _requested_source_channels(self, event) -> str | None:
        return parse_tool_channels(self._current_text(event), self._channel_aliases)

    @llm_tool("search_image")
    async def tool_search_image(
        self,
        event: AstrMessageEvent,
        mode: str = "",
        channels: str = "",
        refresh: bool = False,
    ) -> str:
        """Search an attached or replied image after an explicit user request

        Call this tool only when the user explicitly asks to find an image source,
        perform reverse image search, or identify an anime screenshot. Do not call
        it merely because an image is present or being discussed.

        Args:
            mode(string): Search intent from the request-specific allowed modes.
                Omit to use auto when source services are enabled, otherwise anime.
            channels(string): Source channels explicitly requested by the user,
                separated by spaces or commas. Use an empty string for the default search plan.
                Only valid for auto/source. Explicit channels never add others.
            refresh(boolean): Skip cached results only when explicitly requested.
                All attached images are searched in one call, within the batch limit.
        """
        modes = available_modes(
            self._service.source_provider_names, self._service.anime_provider_names
        )
        if not self._llm_tool_enabled or not modes:
            return json.dumps(
                {"success": False, "error": "搜图工具未启用或没有启用的搜索服务"},
                ensure_ascii=False,
            )
        selected_mode = str(mode).strip().lower() or modes[0]
        if selected_mode not in modes:
            return json.dumps(
                {"success": False, "error": "当前可用 mode：" + "、".join(modes)},
                ensure_ascii=False,
            )
        search_mode = SearchMode(selected_mode)
        if not isinstance(channels, str):
            return json.dumps({"success": False, "error": "channels 必须是渠道名称字符串"},
                              ensure_ascii=False)
        if not isinstance(refresh, bool):
            return json.dumps({"success": False, "error": "refresh 必须是布尔值"}, ensure_ascii=False)
        channels, option = split_search_options(channels)
        request = parse_simple_request(self._current_text(event), self._channel_aliases)
        refresh = refresh or option or bool(request and request.refresh)
        requested = self._requested_source_channels(event)
        if requested:
            channels = requested
        if channels.strip() and search_mode is SearchMode.ANIME:
            return json.dumps({"success": False, "error": "搜番模式不能指定来源搜索渠道"},
                              ensure_ascii=False)
        provider_names, notice = self._resolve_source_channels(channels)
        if provider_names == []:
            return json.dumps({"success": False, "error": notice,
                               "instruction": "转告渠道不可用的说明，不要改用其他渠道搜索。"},
                              ensure_ascii=False)
        if notice:
            await event.send(event.plain_result(notice))
        resolution = await self._resolve_image_request(event)
        if not resolution.images:
            return json.dumps({"success": False, "error": "请在当前消息中附带图片或引用一条带图消息"}, ensure_ascii=False)
        records = []
        async for message in self._run_search(
            event, search_mode, provider_names=provider_names,
            image_resolution=resolution,
            refresh=refresh, records=records,
        ):
            await event.send(message)
        result_count = sum(count_search_results(response) for response, _ in records)
        return json.dumps(
            {
                "success": result_count > 0,
                "count": result_count,
                "message_sent": True,
                "processed_images": len(records),
                "total_images": resolution.total,
                "cached_images": sum(cached for _, cached in records),
                "attempted_providers": list(dict.fromkeys(
                    name for response, cached in records if not cached
                    for name in response.attempted_providers)),
                "result_providers": list(dict.fromkeys(
                    match.provider for response, _ in records for match in response.matches)),
                "instruction": completion_instruction(result_count),
            }, ensure_ascii=False,
        )

    def _search_state(self) -> SearchState:
        if self._state is None:
            path = (StarTools.get_data_dir("astrbot_plugin_image_search") / "search_state.sqlite3"
                    if self.context is not None else None)
            self._state = SearchState(path)
        return self._state

    def _timezone(self):
        name = (self.context.get_config().get("timezone", "")
                if self.context is not None else "")
        if name:
            try:
                return ZoneInfo(name)
            except (ZoneInfoNotFoundError, ValueError, TypeError):
                logger.warning("[ImageSearch] Bot 时区无效，使用系统时区")
        return None

    def _day(self) -> str:
        return datetime.now(self._timezone()).date().isoformat()

    def _quota_user(self, event) -> str | None:
        sender = getattr(event, "get_sender_id", lambda: "")()
        if not sender:
            return None
        platform = getattr(event, "get_platform_id", lambda: "")()
        return json.dumps([str(platform), str(sender)], ensure_ascii=False)

    async def _delayed_feedback(self, event, delay: float, sent: list[bool]) -> None:
        await asyncio.sleep(delay)
        sent[0] = True
        try:
            await event.send(event.plain_result("正在搜索，请稍候。"))
        except Exception as exc:
            logger.warning(f"[ImageSearch] 搜索反馈发送失败 ({type(exc).__name__})")

    async def _run_search(
        self, event: AstrMessageEvent, mode: SearchMode,
        *, provider_names: list[str] | None = None,
        image_resolution: ImageResolution | tuple[str | None, str | None] | None = None,
        refresh: bool = False, records: list | None = None,
    ):
        identities = []
        if isinstance(image_resolution, ImageResolution):
            identities = list(image_resolution.identities)
            images = list(image_resolution.images) or [(image_resolution.url, image_resolution.error)]
            total = image_resolution.total or len(images)
        elif image_resolution is not None:
            images = [] if image_resolution == (None, None) else [image_resolution]
            total = len(images)
        else:
            resolution = await self._resolve_image_request(event)
            images, total = list(resolution.images), resolution.total
            identities = list(resolution.identities)
        if not images:
            key = self._wait_key(event)
            pending = await self._image_wait.create(key)
            if pending is None:
                yield event.plain_result("当前已在等待图片，请直接发送图片。")
                return
            timeout = int(self._image_wait.timeout_seconds)
            yield event.plain_result(f"请在 {timeout} 秒内发送图片。")
            result = await self._image_wait.wait(key, pending)
            if result is WaitOutcome.TIMED_OUT:
                yield event.plain_result("图片等待已超时。")
                return
            if result is WaitOutcome.CANCELLED:
                return
            identities = list(result.identities)
            images = list(result.images) or [(result.url, result.error)]
            total = result.total or len(images)
        if total > self._batch_limit:
            yield event.plain_result(f"每次最多搜索 {self._batch_limit} 张图片，后 {total-self._batch_limit} 张未处理。")
        images = images[:self._batch_limit]
        user = self._quota_user(event)
        admin = bool(getattr(event, "is_admin", lambda: False)())
        state = self._search_state()
        names = (tuple(provider_names) if provider_names is not None else
                 self._service.anime_provider_names if mode is SearchMode.ANIME else
                 self._service.source_plan_names)
        feedback = None
        feedback_sent = [False]
        search_elapsed = 0.0
        batch_results = []
        batch_id = uuid4().hex[:8]
        batch_started = time.monotonic()
        cached_count = 0
        external_count = 0
        logger.info(
            f"[ImageSearch] id={batch_id} 请求开始 mode={mode.value} "
            f"输入图片={total} 处理上限={self._batch_limit} 待处理={len(images)}"
        )
        try:
            for index, (url, error) in enumerate(images, 1):
                request_id = f"{batch_id}/{index}"
                if not url:
                    logger.info(f"[ImageSearch] id={request_id} 图片跳过 原因=图片地址不可用")
                    batch_results.append(BatchResult(index, message=error or "获取图片失败，请重新发送。"))
                    continue
                if not names:
                    logger.info(f"[ImageSearch] id={request_id} 搜索停止 原因=没有可用渠道")
                    batch_results.append(BatchResult(index, message="没有可用的搜索服务，请先检查插件配置。"))
                    break
                cache_config = {
                    "providers": self._config.get("providers", {}),
                    "enrichment": _section(self._config, "output").get("resolve_original_source", True),
                    "network": _section(self._config, "network"),
                }
                identity = identities[index-1] if index <= len(identities) else None
                key = cache_key(identity or url, mode.value, names, cache_config)
                condition = cache_key("", mode.value, names, cache_config)[:12]
                image_identity = hashlib.sha256(url.encode()).hexdigest()[:12]
                stable_fingerprint = hashlib.sha256(identity.encode()).hexdigest()[:12] if identity else "无"
                cached = state.get(key, self._cache_ttl) if self._cache_enabled and not refresh else None
                cache_status = ("跳过:缓存关闭" if not self._cache_enabled else
                                "跳过:用户重搜" if refresh else
                                "命中" if cached else "未命中:无有效记录")
                cache_age = f" 缓存年龄={max(0, time.time()-cached[1]):.0f}s" if cached else ""
                logger.info(
                    f"[ImageSearch] id={request_id} 图片处理 URL指纹={image_identity} "
                    f"缓存身份={identity.split(':', 1)[0] if identity else 'URL'} 标识指纹={stable_fingerprint} "
                    f"条件指纹={condition} 缓存键={key[:12]} 缓存={cache_status}{cache_age}"
                )
                if cached:
                    cached_count += 1
                    response, _ = cached
                    logger.info(
                        f"[ImageSearch] id={request_id} 缓存读取完成 "
                        f"结果={count_search_results(response)} 外部搜索=无"
                    )
                else:
                    day = self._day()
                    reserved = user is not None and not admin
                    if reserved and not state.reserve(user, day, self._daily_limit):
                        logger.info(f"[ImageSearch] id={request_id} 搜索停止 原因=每日限额已满")
                        batch_results.append(BatchResult(index, message=f"今日搜索次数已达上限（{self._daily_limit} 次），剩余图片未处理。"))
                        break
                    started = time.monotonic()
                    external_count += 1
                    if not feedback_sent[0]:
                        feedback = asyncio.create_task(self._delayed_feedback(
                            event, max(0, self._feedback_delay-search_elapsed), feedback_sent))
                    try:
                        request_id, response = await self._search_with_logging(
                            ImageAsset(url=url, refresh=refresh), mode, provider_names=provider_names,
                            request_id=request_id,
                        )
                    except asyncio.CancelledError:
                        if reserved:
                            state.refund(user, day)
                        raise
                    except Exception as exc:
                        if reserved:
                            state.refund(user, day)
                        response = None
                        logger.warning(f"[ImageSearch] id={request_id} 单图搜索失败 ({type(exc).__name__})")
                    finally:
                        search_elapsed += time.monotonic()-started
                        if feedback is not None:
                            if not feedback.done():
                                feedback.cancel()
                            await asyncio.gather(feedback, return_exceptions=True)
                            feedback = None
                    if response is None:
                        batch_results.append(BatchResult(index, message="这张图片搜索失败，请稍后重试。"))
                        continue
                    if reserved and not (response.matches or response.filtered_providers):
                        state.refund(user, day)
                    if self._cache_enabled and response.matches and not response.failed_providers and not response.notices:
                        state.put(key, response)
                        storage = "已保存"
                    else:
                        storage = ("未保存:缓存关闭" if not self._cache_enabled else
                                   "未保存:无结果" if not response.matches else
                                   "未保存:渠道失败" if response.failed_providers else "未保存:含诊断提示")
                    logger.info(
                        f"[ImageSearch] id={request_id} 搜索收尾 缓存={storage} "
                        f"计次={'免计' if not reserved else '计入' if response.matches or response.filtered_providers else '退回'}"
                    )
                self._log_search_failures(response)
                if records is not None:
                    records.append((response, bool(cached)))
                batch_results.append(BatchResult(index, response, cached=bool(cached)))
            if len(images) == 1 and batch_results:
                item = batch_results[0]
                if item.response is None:
                    yield event.plain_result(item.message)
                elif not await self._send_rich_result(event, item.response, request_id, cached=item.cached):
                    yield event.plain_result(format_search_response(item.response, cached=item.cached))
            elif batch_results:
                logger.info(f"[ImageSearch] id={batch_id} 批量结果发送开始")
                if not await self._delivery.try_send_batch(event, batch_results, log_context=f"id={batch_id}"):
                    logger.info(f"[ImageSearch] id={batch_id} 结果交由普通文字发送")
                    for item in batch_results:
                        yield event.plain_result("\n\n".join(card.text for card in build_batch_cards([item])))
                else:
                    logger.info(f"[ImageSearch] id={batch_id} 结果已直接发送")
            logger.info(
                f"[ImageSearch] id={batch_id} 请求完成 搜索完成图片={sum(item.response is not None for item in batch_results)} "
                f"缓存图片={cached_count} 外部搜索图片={external_count} "
                f"结果={sum(count_search_results(item.response) for item in batch_results if item.response is not None)} "
                f"无结果图片={sum(item.response is not None and not item.response.matches for item in batch_results)} "
                f"跳过或失败图片={sum(item.response is None for item in batch_results)} "
                f"耗时={time.monotonic()-batch_started:.2f}s"
            )
        finally:
            if feedback is not None:
                if not feedback.done():
                    feedback.cancel()
                await asyncio.gather(feedback, return_exceptions=True)

    async def _resolve_image_request(self, event) -> ImageResolution:
        identities = []
        images, total = await self._resolve_images(event, identities=identities)
        identities = [
            (identities[index] if index < len(identities) and identities[index]
             else image_cache_identity({"url": url}))
            for index, (url, _) in enumerate(images)
        ]
        url, error = images[0] if images else (None, None)
        return ImageResolution(url=url, error=error, images=tuple(images), total=total, identities=tuple(identities))

    async def _resolve_images(self, event, *, identities: list | None = None) -> tuple[list[tuple[str | None, str | None]], int]:
        get_messages = getattr(event, "get_messages", None)
        if not callable(get_messages):
            url, error = await self._get_image_url(event)
            return ([(url, error)], 1) if url or error else ([], 0)
        components = get_messages()
        direct = [c for c in components if isinstance(c, Image)]
        if direct:
            # Preserve the existing first-image/raw OneBot priority.
            first = await self._get_image_url(event)
            resolved = [first]
            raw = getattr(getattr(event, "message_obj", None), "raw_message", None)
            segments = raw.get("message") if isinstance(raw, dict) else getattr(raw, "message", None)
            raw_images = [segment.get("data") if isinstance(segment.get("data"), dict) else {} for segment in segments
                          if isinstance(segment, dict) and segment.get("type") == "image"] if isinstance(segments, (list, tuple)) else []
            if identities is not None and len(raw_images) == len(direct):
                identities.extend(image_cache_identity(data) for data in raw_images[:self._batch_limit])
            for index, image in enumerate(direct[1:self._batch_limit], 1):
                raw_url = first_public_url([raw_images[index].get("url"), raw_images[index].get("file")]) if index < len(raw_images) else None
                url = raw_url or find_public_image_url([image], image_type=Image, reply_type=Reply)
                url = url or await self._publish_image(image)
                resolved.append((url, None if url else _FILE_SERVICE_ERROR))
            return resolved, len(direct)
        reply = next((c for c in components if isinstance(c, Reply)), None)
        if reply is not None:
            quoted = [c for c in (reply.chain or []) if isinstance(c, Image)]
            public_urls = [find_public_image_url([image], image_type=Image, reply_type=Reply)
                           for image in quoted]
            if public_urls and all(public_urls):
                if identities is not None and self._cache_enabled:
                    candidates = await self._quoted_image_candidates(event, reply, identities=identities)
                    if not candidates or len(candidates) != len(quoted):
                        identities.clear()
                return [(url, None) for url in public_urls[:self._batch_limit]], len(public_urls)
            candidates = await self._quoted_image_candidates(event, reply, identities=identities)
            if candidates:
                images = []
                for refs in candidates[:self._batch_limit]:
                    url = first_public_url(refs)
                    if not url and refs:
                        # Resolve one image at a time; never count alternate refs as images.
                        single = Reply(id="", chain=[Image(file=refs[0])])
                        try:
                            resolved = await extract_quoted_message_images(event, single)
                        except Exception:
                            resolved = []
                        url = first_public_url(resolved)
                        if not url:
                            url = await self._publish_image(Image(file=resolved[0] if resolved else refs[0]))
                    images.append((url, None if url else _FILE_SERVICE_ERROR))
                return images, len(candidates)
            try:
                values = await extract_quoted_message_images(event, reply)
            except Exception as exc:
                logger.warning(f"[ImageSearch] 引用图片解析失败: {type(exc).__name__}")
                values = []
            if len(quoted) == 1:
                # A single embedded image plus its fetched URL is still one image.
                url = first_public_url(values)
                url = url or public_urls[0] or await self._publish_image(quoted[0])
                return [(url, None if url else _FILE_SERVICE_ERROR)], 1
            if values and not quoted:
                images = []
                for value in values[:self._batch_limit]:
                    url = first_public_url([value]) or await self._publish_image(Image(file=value))
                    images.append((url, None if url else _FILE_SERVICE_ERROR))
                return images, len(values)
            if quoted:
                images = []
                for image in quoted[:self._batch_limit]:
                    url = find_public_image_url([image], image_type=Image, reply_type=Reply) or await self._publish_image(image)
                    images.append((url, None if url else _FILE_SERVICE_ERROR))
                return images, len(quoted)
        url, error = await self._get_image_url(event)
        return ([(url, error)], 1) if url or error else ([], 0)

    async def _quoted_image_candidates(self, event, reply, *, identities: list | None = None):
        api = getattr(getattr(event, "bot", None), "api", None)
        call_action = getattr(api, "call_action", None)
        reply_id = str(getattr(reply, "id", "") or "").strip()
        if not callable(call_action) or not reply_id:
            return None
        message_id = int(reply_id) if reply_id.lstrip("-").isdigit() else reply_id
        try:
            payload = await asyncio.wait_for(
                call_action("get_msg", message_id=message_id), timeout=self._timeout_seconds,
            )
            if identities is not None:
                identities.extend(image_identities_from_message(payload))
            return image_candidates_from_message(payload)
        except Exception as exc:
            logger.warning(f"[ImageSearch] 引用消息结构解析失败: {type(exc).__name__}")
            return None

    async def _search_with_logging(
        self, image: ImageAsset, mode: SearchMode,
        *, provider_names: list[str] | None = None, request_id: str | None = None,
    ):
        request_id = request_id or uuid4().hex[:8]
        started = time.monotonic()
        plan = (
            tuple(provider_names) if provider_names is not None else
            self._service.anime_provider_names if mode is SearchMode.ANIME else
            self._service.source_plan_names
        )
        plan_text = ("、" if provider_names is not None else " > ").join(plan)
        logger.info(
            f"[ImageSearch] id={request_id} 搜索开始 mode={mode.value} "
            f"选择={'指定' if provider_names is not None else '默认'} "
            f"计划={plan_text or '无'}"
        )
        try:
            response = await self._service.search(image, mode, provider_names=provider_names)
        except BaseException as exc:
            logger.warning(
                f"[ImageSearch] id={request_id} 搜索中断 ({type(exc).__name__}) "
                f"耗时={time.monotonic() - started:.2f}s"
            )
            raise
        logger.info(
            f"[ImageSearch] id={request_id} 搜索完成 "
            f"实际渠道={','.join(response.attempted_providers) or '无'} "
            f"结果={count_search_results(response)} 原始条目={len(response.matches)} "
            f"低相似度渠道={','.join(response.filtered_providers) or '无'} "
            f"失败渠道={','.join(response.failed_providers) or '无'} "
            f"耗时={time.monotonic() - started:.2f}s"
        )
        return request_id, response

    async def _send_rich_result(self, event, response, request_id: str, *, cached: bool = False) -> bool:
        logger.info(f"[ImageSearch] id={request_id} 结果发送开始")
        sent = await self._delivery.try_send(event, response, log_context=f"id={request_id}", cached=cached)
        if sent:
            logger.info(f"[ImageSearch] id={request_id} 结果已直接发送")
        else:
            logger.info(f"[ImageSearch] id={request_id} 结果交由普通文字发送")
        return sent

    @staticmethod
    def _log_search_failures(response) -> None:
        for name, reason in response.failure_details.items():
            logger.warning(f"[ImageSearch] {name}: {reason}")

    @staticmethod
    async def _get_image_url(
        event: AstrMessageEvent,
    ) -> tuple[str | None, str | None]:
        components = event.get_messages()
        raw_url = find_raw_image_url(event)
        if raw_url is not None:
            return raw_url, None
        direct_components = [c for c in components if isinstance(c, Image)]
        direct_url = find_public_image_url(
            direct_components[:1] if direct_components else components,
            image_type=Image,
            reply_type=Reply,
        )
        if direct_url is not None:
            return direct_url, None

        direct_image = next((c for c in components if isinstance(c, Image)), None)
        if direct_image is not None:
            published_url = await ImageSearchPlugin._publish_image(direct_image)
            return (
                (published_url, None) if published_url else (None, _FILE_SERVICE_ERROR)
            )

        reply = next((c for c in components if isinstance(c, Reply)), None)
        if reply is None:
            return None, None
        try:
            resolved = await extract_quoted_message_images(event, reply)
        except Exception as exc:
            logger.warning(f"[ImageSearch] 引用图片解析失败: {type(exc).__name__}")
            resolved = []
        resolved_url = first_public_url(resolved)
        if resolved_url is not None:
            return resolved_url, None
        embedded_image = next(
            (c for c in (reply.chain or []) if isinstance(c, Image)), None
        )
        fallback = Image(file=resolved[0]) if resolved else embedded_image
        if fallback is not None:
            published_url = await ImageSearchPlugin._publish_image(fallback)
            return (
                (published_url, None) if published_url else (None, _FILE_SERVICE_ERROR)
            )
        return None, None

    @staticmethod
    async def _publish_image(image: Image) -> str | None:
        try:
            published_url = await image.register_to_file_service()
        except Exception as exc:
            logger.warning(
                f"[ImageSearch] AstrBot 文件服务发布图片失败: {type(exc).__name__}"
            )
            return None
        return first_public_url([published_url])

    @staticmethod
    def _wait_key(event: AstrMessageEvent) -> tuple[str, str]:
        return (
            str(event.unified_msg_origin),
            str(event.get_sender_id() or ""),
        )

    async def terminate(self) -> None:
        if self._state is not None:
            self._state.close()
        await self._image_wait.close()
        await self._delivery.close()
        await self._source_pages.close()
        try:
            await self._http.close()
        finally:
            await self._web_http.close()


def _as_dict(config: AstrBotConfig | dict[str, Any]) -> dict[str, Any]:
    try:
        return dict(config.items())
    except (AttributeError, TypeError):
        return dict(config)


def _section(config: dict[str, Any], key: str) -> dict[str, Any]:
    value = config.get(key)
    return value if isinstance(value, dict) else {}


def _as_bool(value: object, default: bool) -> bool:
    return value if isinstance(value, bool) else default


def _positive_int(value: object, default: int) -> int:
    if isinstance(value, bool):
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _positive_float(value: object, default: float) -> float:
    if isinstance(value, bool):
        return default
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if math.isfinite(parsed) and parsed > 0 else default


def _percentage(value: object, default: int) -> float:
    if isinstance(value, bool):
        return default / 100
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default / 100
    return min(100, max(0, parsed)) / 100


def _percentage_value(value: object, default: int) -> float:
    if isinstance(value, bool):
        return float(default)
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return float(default)
    return min(100, max(0, parsed))


def _bounded_float(
    value: object,
    default: float,
    minimum: float,
    maximum: float,
) -> float:
    parsed = _positive_float(value, default)
    return min(maximum, max(minimum, parsed))


def _bounded_int(value: object, default: int, minimum: int, maximum: int) -> int:
    if isinstance(value, bool):
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return min(maximum, max(minimum, parsed))
