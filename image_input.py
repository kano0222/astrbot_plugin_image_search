"""Extract a public image URL from an AstrBot message chain."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from urllib.parse import urlsplit


def find_raw_image_url(event: object) -> str | None:
    """Recover the first OneBot image before AstrBot localized its component."""
    raw = getattr(getattr(event, "message_obj", None), "raw_message", None)
    segments = (
        raw.get("message")
        if isinstance(raw, Mapping)
        else getattr(raw, "message", None)
    )
    if not isinstance(segments, (list, tuple)):
        return None
    for segment in segments:
        if not isinstance(segment, Mapping) or segment.get("type") != "image":
            continue
        data = segment.get("data")
        if not isinstance(data, Mapping):
            return None
        # Do not accidentally search a later image when the first has no URL.
        return first_public_url([data.get("url"), data.get("file")])
    return None


def find_public_image_url(
    components: Iterable[object],
    *,
    image_type: type,
    reply_type: type,
) -> str | None:
    """Return the first direct or quoted HTTP(S) image URL."""
    direct: list[object] = []
    quoted: list[object] = []
    for component in components:
        if isinstance(component, image_type):
            direct.append(component)
        elif isinstance(component, reply_type):
            chain = getattr(component, "chain", None)
            if isinstance(chain, list):
                quoted.extend(item for item in chain if isinstance(item, image_type))

    for image in (*direct, *quoted):
        for attribute in ("url", "file"):
            value = getattr(image, attribute, None)
            if not isinstance(value, str):
                continue
            candidate = value.strip()
            if urlsplit(candidate).scheme.lower() in {"http", "https"}:
                return candidate
    return None


def first_public_url(values: Iterable[str]) -> str | None:
    """Return the first HTTP(S) URL from already resolved image references."""
    for value in values:
        if not isinstance(value, str):
            continue
        candidate = value.strip()
        if urlsplit(candidate).scheme.lower() in {"http", "https"}:
            return candidate
    return None
