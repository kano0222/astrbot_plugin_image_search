"""Minimal HTTP boundary used by transport-independent providers."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol

from .models import ImageAsset


@dataclass(frozen=True, slots=True)
class JsonResponse:
    """Status code and decoded JSON returned by an HTTP client."""

    status: int
    data: object


class JsonHttpClient(Protocol):
    """HTTP client capable of returning a decoded JSON response."""

    async def get_json(
        self,
        url: str,
        *,
        params: Mapping[str, str],
        timeout_seconds: float,
    ) -> JsonResponse:
        """Perform one JSON GET request.

        Args:
            url: Endpoint URL without query parameters.
            params: Query parameters kept separate to avoid leaking secrets.
            timeout_seconds: Total request timeout.

        Returns:
            HTTP status and decoded response body.
        """
        ...


class ImageUploadHttpClient(Protocol):
    """Upload a bounded image as a multipart file and return decoded JSON."""

    async def upload_image_json(
        self,
        url: str,
        *,
        image: ImageAsset,
        data: Mapping[str, str],
        timeout_seconds: float,
    ) -> JsonResponse:
        ...
