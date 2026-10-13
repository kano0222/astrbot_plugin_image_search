"""Explicit Soutubot search, preserving candidates from every returned library."""

from __future__ import annotations

import asyncio
import math
import re
from urllib.parse import urlsplit

from ..http import ImageUploadHttpClient
from ..models import ImageAsset, ProviderOutcome, SearchMode, SourceMatch
from ..provider import ProviderError

_SEARCH_URL = "https://soutubot.moe/api/search"


class SoutubotProvider:
    name = "soutubot"

    def __init__(
        self,
        http: ImageUploadHttpClient,
        *,
        timeout_seconds: float = 30,
        max_results: int = 3,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be greater than zero")
        if not 1 <= max_results <= 10:
            raise ValueError("max_results must be between 1 and 10")
        self._http = http
        self._timeout_seconds = timeout_seconds
        self._max_results = max_results

    async def search(self, image: ImageAsset, mode: SearchMode) -> ProviderOutcome:
        del mode
        try:
            response = await self._http.upload_image_json(
                _SEARCH_URL,
                image=image,
                data={
                    "factor": "1.2",
                    "metadata_mode": "display",
                    "top_k": str(self._max_results),
                },
                timeout_seconds=self._timeout_seconds,
            )
        except (asyncio.TimeoutError, TimeoutError) as exc:
            raise ProviderError("Soutubot request timed out") from exc
        except ProviderError:
            raise
        except Exception as exc:
            raise ProviderError("Soutubot request failed") from exc
        if response.status != 200:
            raise ProviderError(f"Soutubot returned HTTP {response.status}")
        payload = response.data
        if not isinstance(payload, dict):
            raise ProviderError("Soutubot returned invalid data")
        if payload.get("status") not in ("ok", "partial"):
            raise ProviderError("Soutubot returned a search error")
        results = payload.get("results")
        libraries = payload.get("api_results")
        if (
            not isinstance(results, list) or not isinstance(libraries, list)
            or not libraries or not isinstance(payload.get("partial"), bool)
            or any(not isinstance(item, dict) or not isinstance(item.get("status"), str)
                   for item in libraries)
        ):
            raise ProviderError("Soutubot returned invalid data")
        failed = sum(item["status"] != "ok" for item in libraries)
        if failed == len(libraries):
            raise ProviderError("Soutubot all libraries failed")
        matches = []
        malformed = False
        parse_diagnostics = []
        for hit_index, hit in enumerate(results, start=1):
            reasons: list[str] = []
            parsed = _parse_hit(hit, reasons=reasons)
            segments = hit.get("path_segments") if isinstance(hit, dict) else None
            if (not parsed or not isinstance(segments, list)
                    or len(parsed) != len(segments)):
                malformed = True
                if len(parse_diagnostics) < 10:
                    score = hit.get("score") if isinstance(hit, dict) else None
                    parse_diagnostics.append(
                        f"hit={hit_index} type={type(hit).__name__} score_type={type(score).__name__} "
                        f"segments_type={type(segments).__name__} "
                        f"segments={len(segments) if isinstance(segments, list) else 0} valid={len(parsed)} "
                        f"reasons={';'.join(reasons[:5]) or 'empty_segments'}"
                    )
            matches.extend(parsed)
        if results and not matches:
            raise ProviderError("Soutubot returned invalid data")
        matches.sort(key=lambda match: match.similarity or 0, reverse=True)
        notices = []
        if payload["partial"] or failed or payload["status"] == "partial":
            notices.append("Soutubot 部分图库搜索失败，结果可能不完整。")
        if payload.get("warnings"):
            notices.append("Soutubot 返回服务诊断提示，结果可能不完整。")
        if malformed:
            notices.append("Soutubot 部分结果格式异常，已跳过。")
        diagnostics = []
        if notices:
            warnings = payload.get("warnings")
            items = warnings if isinstance(warnings, list) else [warnings] if warnings else []
            codes = []
            for item in items[:10]:
                code = item.get("code") if isinstance(item, dict) else None
                codes.append(code if isinstance(code, str) and re.fullmatch(r"[a-z][a-z0-9_]{0,63}", code) else "unknown")
            diagnostics.append(
                f"status={payload['status']} partial={payload['partial']} "
                f"libraries={len(libraries)} failed={failed} "
                f"warnings={len(items)} codes={','.join(codes) or 'none'} malformed={malformed}"
            )
            diagnostics.extend(parse_diagnostics)
        return ProviderOutcome(matches=matches[:self._max_results], notices=notices,
                               diagnostics=diagnostics)


def _text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _url(value: object) -> str:
    return _text(value) if _url_state(value) == "valid" else ""


def _url_state(value: object) -> str:
    if value is None:
        return "missing"
    if not isinstance(value, str):
        return "not_string"
    value = value.strip()
    if not value:
        return "empty"
    try:
        parsed = urlsplit(value)
        if parsed.scheme not in {"https", "http"}:
            return "unsupported_scheme"
        if not parsed.hostname:
            return "missing_host"
        if parsed.username is not None or parsed.password is not None:
            return "credentials"
    except ValueError:
        return "invalid_url"
    return "valid"


def _parse_hit(hit: object, *, reasons: list[str]) -> list[SourceMatch]:
    if not isinstance(hit, dict):
        reasons.append("hit_not_object")
        return []
    score = hit.get("score")
    if isinstance(score, bool) or not isinstance(score, (int, float)):
        reasons.append("score_not_number")
        return []
    if not math.isfinite(score):
        reasons.append("score_not_finite")
        return []
    if not 0 <= score <= 100:
        reasons.append("score_out_of_range")
        return []
    segments = hit.get("path_segments")
    if not isinstance(segments, list):
        reasons.append("segments_not_list")
        return []
    matches = []
    for segment_index, segment in enumerate(segments, start=1):
        if not isinstance(segment, dict):
            if len(reasons) < 5:
                reasons.append(f"segment={segment_index}:not_object")
            continue
        metadata = segment.get("metadata")
        metadata = metadata if isinstance(metadata, dict) else {}
        source = metadata.get("source")
        source = source if isinstance(source, dict) else {}
        links = segment.get("links")
        links = links if isinstance(links, dict) else {}
        # Page identity must also remain the preferred source identity: otherwise
        # the shared result grouper merges distinct pages of the same gallery.
        url = (_url(segment.get("page_url")) or _url(links.get("page_url"))
               or _url(segment.get("chapter_url")) or _url(links.get("chapter_url"))
               or _url(segment.get("source_url")) or _url(links.get("source_url"))
               or _url(source.get("url")))
        title = metadata.get("title")
        if isinstance(title, dict):
            title = title.get("primary")
        thumbnail = _url(segment.get("thumbnail_url")) or _url(links.get("thumbnail_url"))
        has_details = bool(
            _text(title) or _text(segment.get("source_key")) or _text(source.get("name"))
            or segment.get("external_id") is not None or thumbnail
        )
        link_states = [_url_state(value) for value in (
            segment.get("page_url"), links.get("page_url"),
            segment.get("chapter_url"), links.get("chapter_url"),
            segment.get("source_url"), links.get("source_url"), source.get("url"),
        )]
        # The service also returns sources without public links. Preserve their
        # available metadata; malformed/unsafe links are a different condition.
        if not url and (not has_details or any(state not in {"missing", "empty"} for state in link_states)):
            if len(reasons) < 5:
                reasons.append(
                    f"segment={segment_index}:no_usable_url,"
                    f"page_url={_url_state(segment.get('page_url'))},"
                    f"source_url={_url_state(segment.get('source_url'))},"
                    f"metadata_source_url={_url_state(source.get('url'))},"
                    f"links_type={type(segment.get('links')).__name__},"
                    f"links_page_url={_url_state(links.get('page_url'))},"
                    f"links_source_url={_url_state(links.get('source_url'))},"
                    f"chapter_url={_url_state(segment.get('chapter_url'))},"
                    f"links_chapter_url={_url_state(links.get('chapter_url'))}"
                )
            continue
        site = (_text(source.get("name")) or _text(segment.get("source_key"))
                or "Soutubot")
        title = _text(title) or site + " " + str(segment.get("external_id") or "")
        page = segment.get("page_no")
        if isinstance(page, int) and not isinstance(page, bool) and page > 0:
            title += f" · 第 {page} 页"
        creators = metadata.get("creators")
        tags = metadata.get("tags")
        if not creators and isinstance(tags, dict):
            creators = tags.get("artists")
        author = "、".join(
            name for creator in (creators if isinstance(creators, list) else [])
            if (name := (_text(creator.get("name")) or _text(creator.get("text")))
                if isinstance(creator, dict) else _text(creator))
        )
        matches.append(SourceMatch(
            provider="soutubot", title=title.strip(), site_name=site,
            url=url, source_url=url, author=author,
            thumbnail_url=thumbnail,
            similarity=float(score),
        ))
    return matches
