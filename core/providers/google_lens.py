"""Google Lens search through the documented SerpAPI endpoint."""

from __future__ import annotations

import asyncio
import math
from collections.abc import Mapping, Sequence
from urllib.parse import urlsplit

from ..http import JsonHttpClient
from ..models import ImageAsset, ProviderOutcome, SearchMode, SourceMatch
from ..provider import ProviderError


class GoogleLensProvider:
    """Return Google Lens source matches through SerpAPI."""

    name = "google_lens"
    _ENDPOINT = "https://serpapi.com/search"
    _SEARCH_TYPES = {"all", "exact_matches", "visual_matches"}

    def __init__(
        self,
        http: JsonHttpClient,
        *,
        api_keys: Sequence[str],
        max_results: int = 5,
        search_type: str = "all",
        language: str = "zh-cn",
        country: str = "",
        safe_search: str = "off",
        auto_crop: bool = False,
        no_cache: bool = False,
        timeout_seconds: float = 30,
    ) -> None:
        """Initialize Google Lens request settings.

        Args:
            http: JSON HTTP transport supplied by the runtime adapter.
            api_keys: SerpAPI credentials, tried in round-robin order.
            max_results: Maximum valid matches to return.
            search_type: SerpAPI Google Lens result type.
            language: Google result language code.
            country: Optional two-letter country code.
            safe_search: default omits safe; off and active explicitly set it.
            auto_crop: Whether Google should focus on a detected image region.
            no_cache: Whether SerpAPI should bypass its result cache.
            timeout_seconds: Total request timeout.

        Raises:
            ValueError: If required settings are missing or invalid.
        """
        normalized_keys = tuple(
            dict.fromkeys(key.strip() for key in api_keys if key.strip())
        )
        normalized_type = search_type.strip().lower()
        if not isinstance(safe_search, str) or safe_search not in {
            "default",
            "off",
            "active",
        }:
            raise ValueError("safe_search must be default, off, or active")
        if not normalized_keys:
            raise ValueError("at least one SerpAPI key is required")
        if max_results <= 0:
            raise ValueError("max_results must be greater than zero")
        if normalized_type not in self._SEARCH_TYPES:
            raise ValueError(f"unsupported Google Lens search type: {search_type}")
        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be greater than zero")

        self._http = http
        self._api_keys = normalized_keys
        self._next_key_index = 0
        self._key_lock = asyncio.Lock()
        self._max_results = max_results
        self._search_type = normalized_type
        self._language = language.strip().lower() or "zh-cn"
        self._country = country.strip().lower()
        self._safe_search = safe_search
        self._auto_crop = auto_crop
        self._no_cache = no_cache
        self._timeout_seconds = timeout_seconds

    async def search(
        self,
        image: ImageAsset,
        mode: SearchMode,
    ) -> ProviderOutcome:
        """Search one public image URL with Google Lens.

        Args:
            image: Image containing a public HTTP or HTTPS URL.
            mode: Search intent. Google Lens treats auto and source identically.

        Returns:
            Parsed source matches.

        Raises:
            ProviderError: If the image or SerpAPI response is unusable.
        """
        del mode
        image_url = (image.url or "").strip()
        if urlsplit(image_url).scheme.lower() not in {"http", "https"}:
            raise ProviderError("Google Lens requires a public HTTP image URL")

        params = {
            "engine": "google_lens",
            "url": image_url,
            "type": self._search_type,
            "hl": self._language,
            "auto_crop": str(self._auto_crop).lower(),
            "no_cache": str(self._no_cache or image.refresh).lower(),
        }
        if self._country:
            params["country"] = self._country

        start_index = await self._claim_key_index()
        response = None
        if self._safe_search != "default":
            params["safe"] = self._safe_search
        deadline = asyncio.get_running_loop().time() + self._timeout_seconds
        for offset in range(len(self._api_keys)):
            params["api_key"] = self._api_keys[
                (start_index + offset) % len(self._api_keys)
            ]
            try:
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    raise asyncio.TimeoutError()
                response = await asyncio.wait_for(
                    self._http.get_json(
                        self._ENDPOINT, params=dict(params), timeout_seconds=remaining,
                    ),
                    timeout=remaining,
                )
            except (asyncio.TimeoutError, TimeoutError) as exc:
                raise ProviderError("Google Lens request timed out") from exc
            except ProviderError:
                raise
            except Exception as exc:
                raise ProviderError("Google Lens request failed") from exc
            if response.status not in {401, 403, 429}:
                break
        else:
            raise ProviderError("all SerpAPI credentials or quotas were rejected")

        if response is None:
            raise ProviderError("Google Lens request produced no response")
        if response.status != 200:
            raise ProviderError(f"SerpAPI returned HTTP {response.status}")
        if not isinstance(response.data, Mapping):
            raise ProviderError("SerpAPI returned an invalid JSON object")

        error = response.data.get("error")
        if isinstance(error, str) and error:
            if "hasn't returned any results" in error.lower():
                return ProviderOutcome()
            raise ProviderError("SerpAPI returned a search error")

        result_key = (
            self._search_type
            if self._search_type in {"exact_matches", "visual_matches"}
            else "visual_matches"
        )
        raw_matches = response.data.get(result_key)
        if not isinstance(raw_matches, list):
            return ProviderOutcome()

        matches: list[SourceMatch] = []
        for item in raw_matches:
            if not isinstance(item, Mapping):
                continue
            title = str(item.get("title") or "").strip()
            url = str(item.get("link") or "").strip()
            if not title or not url:
                continue
            matches.append(
                SourceMatch(
                    provider=self.name,
                    title=title,
                    site_name=str(item.get("source") or "").strip(),
                    url=url,
                    thumbnail_url=str(item.get("thumbnail") or "").strip(),
                )
            )
            if len(matches) >= self._max_results:
                break

        return ProviderOutcome(matches=matches)

    async def _claim_key_index(self) -> int:
        async with self._key_lock:
            index = self._next_key_index
            self._next_key_index = (index + 1) % len(self._api_keys)
            return index
