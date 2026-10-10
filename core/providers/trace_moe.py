"""Anime screenshot search through the trace.moe API."""

from __future__ import annotations

import asyncio
import math

from collections.abc import Mapping
from urllib.parse import urlsplit

from ..http import JsonHttpClient
from ..models import AnimeMatch, ImageAsset, ProviderOutcome, SearchMode
from ..provider import ProviderError


class TraceMoeProvider:
    """Return anime scene matches from trace.moe."""

    name = "trace_moe"
    _ENDPOINT = "https://api.trace.moe/search"

    def __init__(
        self,
        http: JsonHttpClient,
        *,
        token: str = "",
        max_results: int = 3,
        min_similarity: float = 0.85,
        cut_borders: bool = True,
        timeout_seconds: float = 30,
    ) -> None:
        if max_results <= 0:
            raise ValueError("max_results must be greater than zero")
        if not 0 <= min_similarity <= 1:
            raise ValueError("min_similarity must be between zero and one")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be greater than zero")

        self._http = http
        self._token = token.strip()
        self._max_results = max_results
        self._min_similarity = min_similarity
        self._cut_borders = cut_borders
        self._timeout_seconds = timeout_seconds

    async def search(
        self,
        image: ImageAsset,
        mode: SearchMode,
    ) -> ProviderOutcome:
        del mode
        image_url = (image.url or "").strip()
        if urlsplit(image_url).scheme.lower() not in {"http", "https"}:
            raise ProviderError("trace.moe requires a public HTTP image URL")

        params = {
            "url": image_url,
            "anilistInfo": "true",
            "cutBorders": str(self._cut_borders).lower(),
        }
        if self._token:
            params["key"] = self._token

        try:
            response = await self._http.get_json(
                self._ENDPOINT,
                params=params,
                timeout_seconds=self._timeout_seconds,
            )
        except (asyncio.TimeoutError, TimeoutError) as exc:
            raise ProviderError("trace.moe request timed out") from exc
        except ProviderError:
            raise
        except Exception as exc:
            raise ProviderError("trace.moe request failed") from exc

        if response.status == 429:
            raise ProviderError("trace.moe rate limit was exceeded")
        if response.status in {401, 403}:
            raise ProviderError("trace.moe token was rejected")
        if response.status != 200:
            raise ProviderError(f"trace.moe returned HTTP {response.status}")
        if not isinstance(response.data, Mapping):
            raise ProviderError("trace.moe returned an invalid JSON object")

        error = response.data.get("error")
        if isinstance(error, str) and error:
            raise ProviderError("trace.moe returned a search error")

        raw_matches = response.data.get("result")
        if not isinstance(raw_matches, list):
            return ProviderOutcome()

        matches: list[AnimeMatch] = []
        for item in raw_matches:
            match = self._parse_match(item)
            if match is None:
                continue
            matches.append(match)
            if len(matches) >= self._max_results:
                break
        filtered = any(
            isinstance(item, Mapping)
            and (score := self._as_float(item.get("similarity"))) is not None
            and 0 <= score < self._min_similarity
            for item in raw_matches
        )
        return ProviderOutcome(matches=matches,
                               filtered_below=self._min_similarity * 100 if filtered and not matches else None)

    def _parse_match(self, item: object) -> AnimeMatch | None:
        if not isinstance(item, Mapping):
            return None

        similarity = self._as_float(item.get("similarity"))
        if similarity is None or similarity < self._min_similarity:
            return None

        anilist = item.get("anilist")
        adult: bool | None = None
        title = ""
        if isinstance(anilist, Mapping):
            adult_value = anilist.get("isAdult")
            adult = adult_value if isinstance(adult_value, bool) else None
            titles = anilist.get("title")
            if isinstance(titles, Mapping):
                for key in ("native", "romaji", "english"):
                    value = titles.get(key)
                    if isinstance(value, str) and value.strip():
                        title = value.strip()
                        break

        if not title:
            title = str(item.get("filename") or "").strip()
        if not title:
            return None

        episode_value = item.get("episode")
        episode = "" if episode_value is None else str(episode_value).strip()
        return AnimeMatch(
            provider=self.name,
            title=title,
            episode=episode,
            timestamp_start=self._as_float(item.get("from")),
            timestamp_end=self._as_float(item.get("to")),
            similarity=similarity * 100,
            preview_url=str(item.get("image") or "").strip(),
            adult=adult,
        )

    @staticmethod
    def _as_float(value: object) -> float | None:
        if isinstance(value, bool):
            return None
        try:
            number = float(value)
            return number if math.isfinite(number) else None
        except (TypeError, ValueError):
            return None
