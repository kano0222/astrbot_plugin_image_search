"""HTTP boundary for providers that consume HTML pages and form redirects."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class TextResponse:
    """Status, final URL, and text body returned by a web request."""

    status: int
    url: str
    text: str


class WebHttpClient(Protocol):
    """Browser-like HTTP transport used by HTML-based providers."""

    async def get_text(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        cookies: Mapping[str, str],
        timeout_seconds: float,
    ) -> TextResponse: ...

    async def post_form(
        self,
        url: str,
        *,
        data: Mapping[str, str],
        headers: Mapping[str, str],
        cookies: Mapping[str, str],
        timeout_seconds: float,
    ) -> TextResponse: ...
