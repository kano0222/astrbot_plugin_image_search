"""Data models shared by search providers and orchestration."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path


class SearchMode(str, Enum):
    """User intent for an image search."""

    AUTO = "auto"
    SOURCE = "source"
    ANIME = "anime"


@dataclass(frozen=True, slots=True)
class ImageAsset:
    """An image that can be accessed by URL, local path, or both."""

    url: str | None = None
    path: Path | None = None
    refresh: bool = False

    def __post_init__(self) -> None:
        """Reject assets without any usable image representation.

        Raises:
            ValueError: If neither a URL nor a local path is provided.
        """
        if not self.url and self.path is None:
            raise ValueError("an image URL or local path is required")


@dataclass(frozen=True, slots=True)
class SourceMatch:
    """A web or creator source associated with an image."""

    provider: str
    title: str
    site_name: str = ""
    url: str = ""
    author: str = ""
    author_url: str = ""
    thumbnail_url: str = ""
    similarity: float | None = None
    source_url: str = ""


@dataclass(frozen=True, slots=True)
class AnimeMatch:
    """An anime scene match with optional episode and time information."""

    provider: str
    title: str
    episode: str = ""
    timestamp_start: float | None = None
    timestamp_end: float | None = None
    similarity: float | None = None
    preview_url: str = ""
    adult: bool | None = None


SearchMatch = SourceMatch | AnimeMatch


@dataclass(slots=True)
class ProviderOutcome:
    """Structured output from one provider invocation."""

    matches: list[SearchMatch] = field(default_factory=list)
    notices: list[str] = field(default_factory=list)
    suggests_anime: bool = False
    low_accuracy: bool = False
    filtered_below: float | None = None


@dataclass(slots=True)
class SearchResponse:
    """Aggregated output from a complete search plan."""

    matches: list[SearchMatch] = field(default_factory=list)
    attempted_providers: list[str] = field(default_factory=list)
    failed_providers: list[str] = field(default_factory=list)
    failure_details: dict[str, str] = field(default_factory=dict)
    notices: list[str] = field(default_factory=list)
    filtered_providers: dict[str, float] = field(default_factory=dict)
