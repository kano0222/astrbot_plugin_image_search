"""Search-plan orchestration independent from AstrBot handlers."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass, field

from .models import ImageAsset, ProviderOutcome, SearchMode, SearchResponse
from .provider import SearchProvider, safe_failure_reason
from .source_enrichment import SourceEnricher


@dataclass(slots=True)
class _StageResult:
    """Internal result retaining provider outcomes for plan decisions."""

    response: SearchResponse = field(default_factory=SearchResponse)
    outcomes: list[ProviderOutcome] = field(default_factory=list)


class SearchService:
    """Execute source, anime, and conditional automatic search plans."""

    def __init__(
        self,
        *,
        source_providers: Sequence[SearchProvider] = (),
        anime_providers: Sequence[SearchProvider] = (),
        anime_followup_providers: Sequence[SearchProvider] = (),
        source_enricher: SourceEnricher | None = None,
        low_similarity_provider: str = "off",
    ) -> None:
        """Initialize search plans.

        Args:
            source_providers: Providers used for source-only and automatic search.
            anime_providers: Providers used for explicit anime search.
            anime_followup_providers: Providers added to automatic search after a
                structured anime signal.
        """
        if low_similarity_provider not in {"off", "ascii2d", "google_lens"}:
            raise ValueError("invalid low-similarity provider")
        self._low_similarity_provider = low_similarity_provider
        self._source_enricher = source_enricher
        self._source_providers = tuple(source_providers)
        self._anime_providers = tuple(anime_providers)
        self._anime_followup_providers = tuple(anime_followup_providers)

    @property
    def low_similarity_provider(self) -> str:
        return self._low_similarity_provider

    @property
    def source_provider_names(self) -> tuple[str, ...]:
        return tuple(provider.name for provider in self._source_providers)

    @property
    def source_plan_names(self) -> tuple[str, ...]:
        """Default candidates; conditions determine which services actually run."""
        return tuple(name for name in ("saucenao", "ascii2d", "google_lens")
                     if name in self.source_provider_names)

    @property
    def anime_provider_names(self) -> tuple[str, ...]:
        return tuple(provider.name for provider in self._anime_providers)

    async def search(
        self,
        image: ImageAsset,
        mode: SearchMode,
        *,
        provider_names: Sequence[str] | None = None,
    ) -> SearchResponse:
        """Execute the provider plan for a search intent.

        Args:
            image: Image to search.
            mode: User-selected search intent.

        Returns:
            Aggregated matches, notices, and provider status.
        """
        response = await self._search_plan(image, mode, provider_names=provider_names)
        if self._source_enricher is not None:
            await self._source_enricher.enrich(response)
        return response

    async def _search_plan(
        self, image: ImageAsset, mode: SearchMode,
        *, provider_names: Sequence[str] | None = None,
    ) -> SearchResponse:
        if provider_names is not None:
            if not provider_names or any(
                name not in self.source_provider_names for name in provider_names
            ):
                raise ValueError("Selected source provider is unavailable")
            selected = [p for p in self._source_providers if p.name in provider_names]
            stage = await self._run_providers(selected, image, SearchMode.SOURCE)
            return stage.response
        if mode is SearchMode.SOURCE:
            stage = await self._run_source_plan(image, mode)
            return stage.response
        if mode is SearchMode.ANIME:
            stage = await self._run_providers(
                self._anime_providers,
                image,
                SearchMode.ANIME,
            )
            return stage.response

        stage = await self._run_source_plan(image, SearchMode.AUTO)
        response = stage.response
        if not any(outcome.suggests_anime for outcome in stage.outcomes):
            return response

        followup = await self._run_providers(
            self._anime_followup_providers,
            image,
            SearchMode.ANIME,
        )
        response.matches.extend(followup.response.matches)
        response.attempted_providers.extend(followup.response.attempted_providers)
        response.failed_providers.extend(followup.response.failed_providers)
        response.failure_details.update(followup.response.failure_details)
        response.notices.extend(followup.response.notices)
        response.diagnostics.update(followup.response.diagnostics)
        response.filtered_providers.update(followup.response.filtered_providers)
        return response

    async def _run_source_plan(
        self, image: ImageAsset, mode: SearchMode
    ) -> _StageResult:
        """Prefer SauceNAO; low-similarity follow-up is an explicit option."""
        by_name = {provider.name: provider for provider in self._source_providers}
        stage = _StageResult()
        weak_sauce = False
        for name in ("saucenao", "ascii2d", "google_lens"):
            provider = by_name.get(name)
            if provider is None:
                continue
            if weak_sauce:
                if self._low_similarity_provider == "off":
                    break
                if self._low_similarity_provider not in by_name:
                    stage.response.notices.append("设定的低相似度补搜渠道未启用，请检查搜索服务配置。")
                    break
                if name != self._low_similarity_provider:
                    continue
            elif any(outcome.matches and not outcome.low_accuracy for outcome in stage.outcomes):
                break
            result = await self._run_providers((provider,), image, mode)
            stage.response.matches.extend(result.response.matches)
            stage.response.notices.extend(result.response.notices)
            stage.response.diagnostics.update(result.response.diagnostics)
            stage.response.filtered_providers.update(result.response.filtered_providers)
            stage.response.attempted_providers.extend(
                result.response.attempted_providers
            )
            stage.response.failed_providers.extend(result.response.failed_providers)
            stage.response.failure_details.update(result.response.failure_details)
            stage.outcomes.extend(result.outcomes)
            if weak_sauce:
                break  # A selected low-similarity follow-up never adds another channel.
            if name == "saucenao":
                weak_sauce = any(outcome.low_accuracy for outcome in result.outcomes)
                if (weak_sauce and self._low_similarity_provider != "off"
                        and self._low_similarity_provider not in by_name):
                    stage.response.notices.append("设定的低相似度补搜渠道未启用，请检查搜索服务配置。")
                    break

        # Additional providers require explicit selection; registration alone must
        # never expand the default set of services receiving an image.
        return stage

    @staticmethod
    async def _run_providers(
        providers: Sequence[SearchProvider],
        image: ImageAsset,
        mode: SearchMode,
    ) -> _StageResult:
        """Run one provider stage concurrently with failure isolation.

        Args:
            providers: Providers in this stage.
            image: Image to search.
            mode: Intent passed to every provider in this stage.

        Returns:
            Aggregated stage output including internal provider outcomes.

        Raises:
            asyncio.CancelledError: If the search operation is cancelled.
        """
        results = await asyncio.gather(
            *(provider.search(image, mode) for provider in providers),
            return_exceptions=True,
        )
        stage = _StageResult()
        for provider, result in zip(providers, results, strict=True):
            stage.response.attempted_providers.append(provider.name)
            if isinstance(result, asyncio.CancelledError):
                raise result
            if isinstance(result, BaseException):
                stage.response.failed_providers.append(provider.name)
                stage.response.failure_details[provider.name] = safe_failure_reason(
                    result
                )
                continue
            stage.response.matches.extend(result.matches)
            stage.response.notices.extend(result.notices)
            if result.diagnostics:
                stage.response.diagnostics[provider.name] = list(result.diagnostics)
            if result.filtered_below is not None:
                stage.response.filtered_providers[provider.name] = result.filtered_below
            stage.outcomes.append(result)
        return stage
