"""Provider contract for image search implementations."""

from __future__ import annotations

from typing import Protocol
import re

from .models import ImageAsset, ProviderOutcome, SearchMode


class ProviderError(RuntimeError):
    """Expected provider failure safe to classify without exposing secrets."""


def safe_failure_reason(error: BaseException) -> str:
    """Classify failures without echoing arbitrary error messages or URLs."""
    if isinstance(error, ProviderError):
        text = str(error)
        http = re.fullmatch(
            r"Ascii2d (home page|search|BOVW|color) returned HTTP (\d{3})", text
        )
        if http:
            stage = {
                "home page": "主页",
                "search": "提交搜索",
                "BOVW": "特征结果页",
                "color": "色合结果页",
            }[http.group(1)]
            suffix = "，访问受限" if http.group(2) == "403" else ""
            return f"{stage}返回 HTTP {http.group(2)}{suffix}"
        service_http = re.fullmatch(r"(SauceNAO|SerpAPI|Soutubot) returned HTTP (\d{3})", text)
        if service_http:
            status = service_http.group(2)
            suffix = "，服务端异常" if status.startswith("5") else ""
            if service_http.group(1) == "Soutubot":
                suffix = {"403": "，访问受限", "429": "，请求频率或额度受限"}.get(status, suffix)
            return f"搜索接口返回 HTTP {status}{suffix}"
        known = {
            "Soutubot request timed out": "请求超时",
            "Soutubot request failed": "网络请求失败",
            "Soutubot returned invalid data": "搜索接口返回无效数据",
            "Soutubot returned a search error": "搜索接口返回错误",
            "Soutubot all libraries failed": "全部图库搜索失败",
            "Soutubot image format is unsupported": "图片格式不支持，请使用 JPEG、PNG、GIF、WebP 或 BMP",
            "Soutubot image could not be read": "无法读取图片",
            "Soutubot image URL is invalid": "图片下载地址无效",
            "Soutubot image exceeds 5 MiB": "图片超过 5 MiB，请缩小后重试",
            "Soutubot image download failed": "图片下载失败",
            "Google Lens request timed out": "请求超时",
            "SauceNAO request timed out": "请求超时",
            "Ascii2d request timed out": "请求超时",
            "trace.moe request timed out": "请求超时",
            "Google Lens request failed": "网络请求失败",
            "SauceNAO request failed": "网络请求失败",
            "all SerpAPI credentials or quotas were rejected": "全部 SerpAPI Key 认证失败或额度受限",
            "SauceNAO API key was rejected": "API Key 认证失败",
            "SauceNAO search quota was exceeded": "搜索额度已用尽",
            "SauceNAO rate limit was exceeded": "请求频率受限",
            "SerpAPI returned an invalid JSON object": "搜索接口返回无效数据",
            "SauceNAO returned an invalid JSON object": "搜索接口返回无效数据",
            "SauceNAO response header is missing": "搜索接口返回数据缺少必要字段",
            "SerpAPI returned a search error": "搜索接口返回错误",
            "SauceNAO returned a search error": "搜索接口返回错误",
            "Ascii2d search blocked by Cloudflare challenge": (
                "提交搜索返回 HTTP 403，Cloudflare 验证拦截"
            ),
            "Ascii2d authenticity token is missing": "主页缺少表单令牌",
            "Ascii2d did not return a result page": "未返回预期搜索结果页",
            "Ascii2d result pages both failed": "色合与特征结果页均失败",
            "Ascii2d request failed": "网络请求失败",
        }
        return known.get(text, "搜索服务请求失败")
    return "请求超时" if isinstance(error, TimeoutError) else "搜索服务异常"


class SearchProvider(Protocol):
    """A provider capable of searching one image for one intent."""

    @property
    def name(self) -> str:
        """Return the stable provider name."""
        ...

    async def search(
        self,
        image: ImageAsset,
        mode: SearchMode,
    ) -> ProviderOutcome:
        """Search an image.

        Args:
            image: Image representations available to the provider.
            mode: Search intent selected by the application.

        Returns:
            Structured provider results and notices.
        """
        ...
