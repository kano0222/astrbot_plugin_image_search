"""Ascii2d color and feature search with structured HTML parsing."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from html.parser import HTMLParser
from urllib.parse import unquote, urljoin, urlsplit

from ..models import ImageAsset, ProviderOutcome, SearchMode, SourceMatch
from ..provider import ProviderError
from ..web import TextResponse, WebHttpClient

_BASE_URL = "https://ascii2d.net"
_SEARCH_URL = f"{_BASE_URL}/search/uri"


class Ascii2dProvider:
    """Search Ascii2d color and BOVW result pages."""

    name = "ascii2d"

    def __init__(
        self,
        http: WebHttpClient,
        *,
        session_id: str = "",
        cf_clearance: str = "",
        timeout_seconds: float = 30,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be greater than zero")
        self._http = http
        self._cookies = {}
        if session_id.strip():
            self._cookies["_session_id"] = session_id.strip()
        if cf_clearance.strip():
            self._cookies["cf_clearance"] = cf_clearance.strip()
        self._timeout_seconds = timeout_seconds

    async def search(
        self,
        image: ImageAsset,
        mode: SearchMode,
    ) -> ProviderOutcome:
        del mode
        image_url = (image.url or "").strip()
        if urlsplit(image_url).scheme.lower() not in {"http", "https"}:
            raise ProviderError("Ascii2d requires a public HTTP image URL")

        home = await self._get(_BASE_URL)
        if home.status != 200:
            raise ProviderError(f"Ascii2d home page returned HTTP {home.status}")
        token = _parse_authenticity_token(home.text)
        if not token:
            raise ProviderError("Ascii2d authenticity token is missing")

        submitted = await self._post(
            _SEARCH_URL,
            data={
                "utf8": "✓",
                "authenticity_token": token,
                "uri": image_url,
                "search": "",
            },
        )
        if submitted.status == 403:
            body = submitted.text.lower()
            if "challenge-platform" in body and (
                "cloudflare" in body or "cf-chl" in body
            ):
                raise ProviderError("Ascii2d search blocked by Cloudflare challenge")
        if submitted.status != 200:
            raise ProviderError(f"Ascii2d search returned HTTP {submitted.status}")
        result_location = urlsplit(submitted.url)
        if (
            result_location.scheme != "https"
            or result_location.netloc != "ascii2d.net"
            or not result_location.path.startswith(("/search/color/", "/search/bovw/"))
        ):
            raise ProviderError("Ascii2d did not return a result page")

        color_url = _replace_mode(submitted.url, "color")
        bovw_url = _replace_mode(submitted.url, "bovw")
        results = await asyncio.gather(
            self._fetch_results(bovw_url, "BOVW"),
            self._fetch_results(color_url, "color"),
            return_exceptions=True,
        )

        matches: list[SourceMatch] = []
        failed_modes: list[str] = []
        for label, result in zip(("BOVW", "color"), results, strict=True):
            if isinstance(result, asyncio.CancelledError):
                raise result
            if isinstance(result, BaseException):
                failed_modes.append(label)
                continue
            matches.extend(result)

        if len(failed_modes) == 2:
            if all(isinstance(result, ProviderError) and str(result) == "Ascii2d request timed out"
                   for result in results):
                raise ProviderError("Ascii2d request timed out")
            raise ProviderError("Ascii2d result pages both failed")
        notices = []
        if failed_modes:
            notices.append("Ascii2d 部分搜索模式失败：" + "、".join(
                label + ("（请求超时）" if isinstance(result, ProviderError)
                         and str(result) == "Ascii2d request timed out" else "")
                for label, result in zip(("BOVW", "color"), results, strict=True)
                if isinstance(result, BaseException)
            ))
        return ProviderOutcome(matches=matches, notices=notices)

    async def _fetch_results(
        self,
        url: str,
        label: str,
    ) -> list[SourceMatch]:
        response = await self._get(url)
        if response.status != 200:
            raise ProviderError(f"Ascii2d {label} returned HTTP {response.status}")
        parser = _ResultParser(label)
        parser.feed(response.text)
        parser.close()
        if parser.box_count == 0:
            raise ProviderError(f"Ascii2d {label} returned an unrecognized page")
        return parser.matches[:1]

    async def _get(self, url: str) -> TextResponse:
        try:
            return await self._http.get_text(
                url,
                headers={"Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"},
                cookies=self._cookies,
                timeout_seconds=self._timeout_seconds,
            )
        except (asyncio.TimeoutError, TimeoutError) as exc:
            raise ProviderError("Ascii2d request timed out") from exc
        except ProviderError:
            raise
        except Exception as exc:
            raise ProviderError("Ascii2d request failed") from exc

    async def _post(self, url: str, *, data: Mapping[str, str]) -> TextResponse:
        try:
            return await self._http.post_form(
                url,
                data=data,
                headers={
                    "Origin": _BASE_URL,
                    "Referer": f"{_BASE_URL}/",
                },
                cookies=self._cookies,
                timeout_seconds=self._timeout_seconds,
            )
        except (asyncio.TimeoutError, TimeoutError) as exc:
            raise ProviderError("Ascii2d request timed out") from exc
        except ProviderError:
            raise
        except Exception as exc:
            raise ProviderError("Ascii2d request failed") from exc


class _TokenParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.token = ""

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        if tag != "input" or self.token:
            return
        values = dict(attrs)
        if values.get("name") == "authenticity_token":
            self.token = (values.get("value") or "").strip()


def _parse_authenticity_token(html: str) -> str:
    parser = _TokenParser()
    parser.feed(html)
    parser.close()
    return parser.token


class _ResultParser(HTMLParser):
    def __init__(self, mode: str) -> None:
        super().__init__()
        self._mode = mode
        self._box_depth = 0
        self._in_h6 = 0
        self._in_small = 0
        self._capture_title = False
        self._title_parts: list[str] = []
        self._title_url = ""
        self._external_url = ""
        self._thumbnail_url = ""
        self._seen_boxes = 0
        self.matches: list[SourceMatch] = []

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        values = dict(attrs)
        classes = set((values.get("class") or "").split())
        if tag == "div":
            if self._box_depth:
                self._box_depth += 1
            elif {"row", "item-box"}.issubset(classes):
                self._start_box()
            return
        if not self._box_depth:
            return
        if tag == "h6":
            self._in_h6 += 1
        elif tag == "small":
            self._in_small += 1
        elif tag == "img" and not self._thumbnail_url:
            self._thumbnail_url = values.get("src") or ""
        elif tag == "a":
            href = (values.get("href") or "").strip()
            if self._in_h6 and not self._title_url:
                self._title_url = href
                self._capture_title = True
            elif self._in_small and not self._external_url:
                self._external_url = href

    def handle_endtag(self, tag: str) -> None:
        if not self._box_depth:
            return
        if tag == "a":
            self._capture_title = False
        elif tag == "h6" and self._in_h6:
            self._in_h6 -= 1
        elif tag == "small" and self._in_small:
            self._in_small -= 1
        elif tag == "div":
            self._box_depth -= 1
            if self._box_depth == 0:
                self._finish_box()

    def handle_data(self, data: str) -> None:
        if self._capture_title and data.strip():
            self._title_parts.append(data.strip())

    @property
    def box_count(self) -> int:
        return self._seen_boxes

    def _start_box(self) -> None:
        self._box_depth = 1
        self._in_h6 = 0
        self._in_small = 0
        self._capture_title = False
        self._title_parts = []
        self._title_url = ""
        self._external_url = ""
        self._thumbnail_url = ""

    def _finish_box(self) -> None:
        self._seen_boxes += 1
        if self._seen_boxes == 1:
            return

        title = " ".join(self._title_parts).strip()
        result_url = unquote(self._title_url)
        if urlsplit(result_url).scheme.lower() not in {"http", "https"}:
            result_url = self._external_url
        if not title or urlsplit(result_url).scheme.lower() not in {"http", "https"}:
            return
        thumbnail = urljoin(_BASE_URL, self._thumbnail_url)
        self.matches.append(
            SourceMatch(
                provider="ascii2d",
                title=title,
                site_name=f"Ascii2d {self._mode}",
                url=result_url,
                thumbnail_url=thumbnail,
            )
        )


def _replace_mode(url: str, mode: str) -> str:
    if "/search/color/" in url:
        return url.replace("/search/color/", f"/search/{mode}/")
    if "/search/bovw/" in url:
        return url.replace("/search/bovw/", f"/search/{mode}/")
    return url
