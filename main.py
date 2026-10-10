"""Unified image source and anime screenshot search for AstrBot."""

from __future__ import annotations

import json
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
from .image_input import find_public_image_url, find_raw_image_url, first_public_url
from .command_aliases import build_channel_aliases
from .simple_requests import parse_simple_request, parse_tool_channels
from .presentation import count_search_results, format_search_response, provider_label
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

        image_url, image_error = await self._get_image_url(event)
        if image_url is None and image_error is None:
            return
        delivered = await self._image_wait.deliver(
            key,
            ImageResolution(url=image_url, error=image_error),
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
        *, image_resolution: tuple[str | None, str | None] | None = None,
    ):
        provider_names, notice = self._resolve_source_channels(channels)
        if notice:
            yield event.plain_result(notice)
        if provider_names == []:
            return
        async for result in self._run_search(
            event, SearchMode.AUTO, provider_names=provider_names,
            image_resolution=image_resolution,
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
    async def search_anime_command(self, event: AstrMessageEvent):
        """搜索附带或回复动画截图的番剧与时间点。"""
        async for result in self._run_search(event, SearchMode.ANIME):
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
            image_resolution = await self._get_image_url(event)
            if image_resolution == (None, None):
                return
        # Stopping here makes the core hook skip the model and all later hooks;
        # results are sent directly rather than relying on the stopped pipeline.
        event.stop_event()
        async for result in self._search_source_channels(
            event, request.channels, image_resolution=image_resolution,
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
        req.func_tool.add_tool(tool)

    def _requested_source_channels(self, event) -> str | None:
        get_messages = getattr(event, "get_messages", None)
        if not callable(get_messages):
            return None
        text = "".join(component.text for component in get_messages()
                       if isinstance(component, Plain))
        return parse_tool_channels(text, self._channel_aliases)

    @llm_tool("search_image")
    async def tool_search_image(
        self,
        event: AstrMessageEvent,
        mode: str = "",
        channels: str = "",
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
        image_url, image_error = await self._get_image_url(event)
        if image_url is None:
            return json.dumps(
                {
                    "success": False,
                    "error": image_error or "当前消息或回复消息中没有可搜索的图片",
                },
                ensure_ascii=False,
            )

        request_id, response = await self._search_with_logging(
            ImageAsset(url=image_url), search_mode, provider_names=provider_names,
        )
        self._log_search_failures(response)
        if not await self._send_rich_result(event, response, request_id):
            await event.send(event.plain_result(format_search_response(response)))
            logger.info(f"[ImageSearch] id={request_id} 普通文字结果已发送")
        result_count = count_search_results(response)
        return json.dumps(
            {
                "success": result_count > 0,
                "count": result_count,
                "message_sent": True,
                "attempted_providers": response.attempted_providers,
                "result_providers": list(dict.fromkeys(match.provider for match in response.matches)),
                "instruction": completion_instruction(result_count),
            },
            ensure_ascii=False,
        )

    async def _run_search(
        self,
        event: AstrMessageEvent,
        mode: SearchMode,
        *,
        provider_names: list[str] | None = None,
        image_resolution: tuple[str | None, str | None] | None = None,
    ):
        image_url, image_error = (
            image_resolution if image_resolution is not None
            else await self._get_image_url(event)
        )
        if image_url is None and image_error is not None:
            yield event.plain_result(image_error)
            return

        if image_url is None:
            key = self._wait_key(event)
            pending = await self._image_wait.create(key)
            if pending is None:
                yield event.plain_result("当前已在等待图片，请直接发送图片。")
                return

            timeout = int(self._image_wait.timeout_seconds)
            yield event.plain_result(f"请在 {timeout} 秒内发送图片。")
            wait_result = await self._image_wait.wait(key, pending)
            if wait_result is WaitOutcome.TIMED_OUT:
                yield event.plain_result("图片等待已超时。")
                return
            if wait_result is WaitOutcome.CANCELLED:
                return
            if wait_result.error is not None:
                yield event.plain_result(wait_result.error)
                return
            image_url = wait_result.url
            if image_url is None:
                yield event.plain_result("获取图片失败，请重新发送。")
                return

        request_id, response = await self._search_with_logging(
            ImageAsset(url=image_url), mode, provider_names=provider_names,
        )
        self._log_search_failures(response)
        if not await self._send_rich_result(event, response, request_id):
            logger.info(f"[ImageSearch] id={request_id} 结果交由普通文字发送")
            yield event.plain_result(format_search_response(response))

    async def _search_with_logging(
        self, image: ImageAsset, mode: SearchMode,
        *, provider_names: list[str] | None = None,
    ):
        request_id = uuid4().hex[:8]
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
            f"结果={len(response.matches)} "
            f"失败渠道={','.join(response.failed_providers) or '无'} "
            f"耗时={time.monotonic() - started:.2f}s"
        )
        return request_id, response

    async def _send_rich_result(self, event, response, request_id: str) -> bool:
        logger.info(f"[ImageSearch] id={request_id} 结果发送开始")
        sent = await self._delivery.try_send(event, response)
        if sent:
            logger.info(f"[ImageSearch] id={request_id} 结果已直接发送")
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
