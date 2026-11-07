"""Image-source search through the SauceNAO API or anonymous website."""

from __future__ import annotations

from collections.abc import Mapping
import asyncio
import math
import re
from urllib.parse import urlsplit

from ..http import JsonHttpClient
from ..models import ImageAsset, ProviderOutcome, SearchMode, SourceMatch
from ..provider import ProviderError
from ..web import WebHttpClient
from .saucenao_html import parse_anonymous_results


class SauceNaoProvider:
    """Return structured source matches and anime follow-up signals."""

    name = "saucenao"
    _ENDPOINT = "https://saucenao.com/search.php"
    _ANIME_DATABASE_IDS = {21, 23, 24}

    def __init__(
        self,
        http: JsonHttpClient,
        *,
        api_key: str = "",
        mode: str = "api",
        web_http: WebHttpClient | None = None,
        max_results: int = 5,
        min_similarity: float = 60,
        timeout_seconds: float = 30,
    ) -> None:
        normalized_key = api_key.strip()
        if mode not in {"api", "anonymous"}:
            raise ValueError("unsupported SauceNAO mode")
        if mode == "api" and not normalized_key:
            raise ValueError("SauceNAO API key is required")
        if mode == "anonymous" and web_http is None:
            raise ValueError("anonymous SauceNAO requires a web HTTP client")
        if max_results <= 0:
            raise ValueError("max_results must be greater than zero")
        if not 0 <= min_similarity <= 100:
            raise ValueError("min_similarity must be between zero and 100")
        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be greater than zero")

        self._http = http
        self._api_key = normalized_key
        self._mode = mode
        self._web_http = web_http
        self._max_results = max_results
        self._min_similarity = min_similarity
        self._timeout_seconds = timeout_seconds

    async def search(
        self,
        image: ImageAsset,
        mode: SearchMode,
    ) -> ProviderOutcome:
        del mode
        image_url = (image.url or "").strip()
        if urlsplit(image_url).scheme.lower() not in {"http", "https"}:
            raise ProviderError("SauceNAO requires a public HTTP image URL")

        if self._mode == "anonymous":
            raw_results, malformed = await self._search_anonymous(image_url)
            outcome = self._select_results(raw_results)
            if malformed:
                outcome.notices.append("SauceNAO 部分网页结果无法解析，已保留可用结果。")
            return outcome

        params = {
            "api_key": self._api_key,
            "output_type": "2",
            "numres": str(self._max_results),
            "db": "999",
            "url": image_url,
        }
        try:
            response = await self._http.get_json(
                self._ENDPOINT,
                params=params,
                timeout_seconds=self._timeout_seconds,
            )
        except (asyncio.TimeoutError, TimeoutError) as exc:
            raise ProviderError("SauceNAO request timed out") from exc
        except ProviderError:
            raise
        except Exception as exc:
            raise ProviderError("SauceNAO request failed") from exc

        if response.status in {401, 403}:
            raise ProviderError("SauceNAO API key was rejected")
        if response.status == 429:
            raise ProviderError("SauceNAO rate limit was exceeded")
        if response.status != 200:
            raise ProviderError(f"SauceNAO returned HTTP {response.status}")
        if not isinstance(response.data, Mapping):
            raise ProviderError("SauceNAO returned an invalid JSON object")

        header = response.data.get("header")
        if not isinstance(header, Mapping):
            raise ProviderError("SauceNAO response header is missing")
        status = _as_int(header.get("status"))
        if status is not None and status < 0:
            if status in {-1, -2}:
                raise ProviderError("SauceNAO search quota was exceeded")
            if status in {-3, -4}:
                raise ProviderError("SauceNAO API key was rejected")
            raise ProviderError("SauceNAO returned a search error")

        raw_results = response.data.get("results")
        if not isinstance(raw_results, list):
            return ProviderOutcome()
        return self._select_results(raw_results)

    async def _search_anonymous(self, image_url: str) -> tuple[list[dict], bool]:
        try:
            response = await self._web_http.post_form(
                self._ENDPOINT,
                data={"url": image_url, "db": "999", "output_type": "0",
                      "numres": str(self._max_results)},
                headers={"Origin": "https://saucenao.com",
                         "Referer": "https://saucenao.com/"},
                cookies={},
                timeout_seconds=self._timeout_seconds,
            )
        except (asyncio.TimeoutError, TimeoutError) as exc:
            raise ProviderError("SauceNAO request timed out") from exc
        except ProviderError:
            raise
        except Exception as exc:
            raise ProviderError("SauceNAO request failed") from exc
        if response.status == 429:
            raise ProviderError("SauceNAO rate limit was exceeded")
        if response.status in {401, 403}:
            raise ProviderError("SauceNAO anonymous access was blocked")
        if response.status != 200:
            raise ProviderError(f"SauceNAO returned HTTP {response.status}")
        location = urlsplit(response.url)
        if location.scheme != "https" or location.hostname not in {"saucenao.com", "www.saucenao.com"}:
            raise ProviderError("SauceNAO returned an unrecognized page")
        return parse_anonymous_results(response.text)

    def _select_results(self, raw_results: list) -> ProviderOutcome:
        candidates = []
        for item in raw_results:
            parsed = self._parse_match(item)
            if parsed is not None and parsed[0].similarity is not None:
                candidates.append(parsed)
        if not candidates:
            return ProviderOutcome()

        candidates.sort(key=lambda item: item[0].similarity, reverse=True)
        selected = candidates[0]
        # CQPS preference applies only within SauceNAO's comparable score scale.
        if selected[1] != 5:
            selected = next(
                (
                    item
                    for item in candidates
                    if item[1] == 5
                    and item[0].similarity >= self._min_similarity
                    and item[0].similarity * 1.03 >= selected[0].similarity
                ),
                selected,
            )
        if selected[1] == 5:
            nearby = [
                item
                for item in candidates
                if item[1] == 5
                and abs(item[0].similarity - selected[0].similarity) < 5
                and item[0].similarity >= self._min_similarity
                and _pixiv_post_id(item[0].url) is not None
            ]
            if nearby:
                selected = min(nearby, key=lambda item: _pixiv_post_id(item[0].url))

        match, index_id = selected
        if match.similarity < self._min_similarity:
            return ProviderOutcome(low_accuracy=True, filtered_below=self._min_similarity)
        return ProviderOutcome(
            matches=[match],
            suggests_anime=index_id in self._ANIME_DATABASE_IDS,
        )

    def _parse_match(self, item: object) -> tuple[SourceMatch, int | None] | None:
        if not isinstance(item, Mapping):
            return None
        header = item.get("header")
        data = item.get("data")
        if not isinstance(header, Mapping) or not isinstance(data, Mapping):
            return None

        similarity = _as_float(header.get("similarity"))
        title = _first_text(
            data,
            "title",
            "eng_name",
            "jp_name",
            "material",
        )
        if urlsplit(title).scheme.lower() in {"http", "https"}:
            title = ""
        raw_source = _first_text(data, "source")
        source_url = _source_page_url(raw_source)
        if not title and raw_source and not urlsplit(raw_source).scheme:
            title = raw_source
        urls = data.get("ext_urls")
        url = ""
        if isinstance(urls, list):
            url = next(
                (
                    value.strip()
                    for value in urls
                    if isinstance(value, str) and value.strip()
                ),
                "",
            )
        if isinstance(urls, list) and _as_int(header.get("index_id")) != 5:
            url = next(
                (
                    value.strip()
                    for value in urls
                    if isinstance(value, str)
                    and urlsplit(value).hostname
                    in {"danbooru.donmai.us", "www.danbooru.donmai.us"}
                ),
                url,
            )
        if not url:
            url = source_url
        if not title and not url:
            return None

        author = _first_text(data, "member_name", "author_name", "author", "artist", "creator")
        author_url = _first_text(data, "author_url", "member_link")
        member_id = _as_int(data.get("member_id"))
        if not author_url and _pixiv_post_id(url) is not None and member_id and member_id > 0:
            author_url = f"https://pixiv.net/u/{member_id}"
        match = SourceMatch(
            provider=self.name,
            title=title or "未提供标题",
            source_url=source_url,
            site_name=str(header.get("index_name") or "").strip(),
            url=url,
            author=author,
            author_url=author_url,
            thumbnail_url=str(header.get("thumbnail") or "").strip(),
            similarity=similarity,
        )
        return match, _as_int(header.get("index_id"))


def _source_page_url(value: str) -> str:
    parsed = urlsplit(value)
    if parsed.scheme.lower() not in {"http", "https"}:
        return ""
    if parsed.hostname in {"i.pximg.net", "pximg.net"}:
        match = re.search(r"/(\d+)(?:_p\d+)?(?:\.[a-zA-Z0-9]+)?$", parsed.path)
        return f"https://www.pixiv.net/artworks/{match.group(1)}" if match else ""
    return value


def _pixiv_post_id(url: str) -> int | None:
    parsed = urlsplit(url)
    if parsed.hostname not in {"pixiv.net", "www.pixiv.net"}:
        return None
    match = re.fullmatch(r"/(?:[a-z]{2}/)?artworks/(\d+)/?", parsed.path)
    if match is None:
        match = re.fullmatch(r"/i/(\d+)/?", parsed.path)
    return int(match.group(1)) if match else None


def _first_text(values: Mapping[object, object], *keys: str) -> str:
    for key in keys:
        value = values.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, list):
            joined = ", ".join(str(item).strip() for item in value if str(item).strip())
            if joined:
                return joined
    return ""


def _as_float(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = float(value)
        return parsed if math.isfinite(parsed) and 0 <= parsed <= 100 else None
    except (TypeError, ValueError):
        return None


def _as_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
