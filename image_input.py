"""Extract a public image URL from an AstrBot message chain."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
import re
from urllib.parse import urlsplit


def image_cache_identity(data: object) -> str | None:
    """Use OneBot unique IDs or recognizable QQ image digests, never arbitrary names."""
    if not isinstance(data, Mapping):
        return None
    unique = data.get("file_unique")
    if isinstance(unique, str) and unique.strip() and len(unique) <= 512:
        return "qq-unique:" + unique.strip()
    digest = data.get("md5")
    if isinstance(digest, str) and re.fullmatch(r"[0-9a-fA-F]{32}", digest):
        return "qq-md5:" + digest.lower()
    file = data.get("file")
    if isinstance(file, str):
        match = re.fullmatch(r"([0-9a-fA-F]{32})(?:\.(?:image|jpg|jpeg|png|gif|webp))?", file.strip(), re.IGNORECASE)
        if match:
            return "qq-md5:" + match[1].lower()
    for value in (data.get("url"), file):
        if not isinstance(value, str):
            continue
        try:
            parsed = urlsplit(value)
            if parsed.scheme not in {"http", "https"} or parsed.hostname not in {"gchat.qpic.cn", "c2cpicdw.qpic.cn"}:
                continue
            match = re.fullmatch(r"/(?:gchatpic|offpic)_new/\d+/\d+-\d+-([0-9a-fA-F]{32})/\d+", parsed.path)
            if match:
                return "qq-md5:" + match[1].lower()
        except ValueError:
            continue
    return None


def image_identities_from_message(payload: object) -> list[str | None]:
    if not isinstance(payload, Mapping):
        return []
    inner = payload.get("data")
    message = inner if isinstance(inner, Mapping) else payload
    segments = message.get("message")
    if not isinstance(segments, (list, tuple)):
        return []
    return [image_cache_identity(segment.get("data")) for segment in segments
            if isinstance(segment, Mapping) and segment.get("type") == "image"]


def image_candidates_from_message(payload: object) -> list[tuple[str, ...]] | None:
    """Keep alternative references grouped by the original OneBot image segment."""
    if not isinstance(payload, Mapping):
        return None
    data = payload.get("data")
    message = data if isinstance(data, Mapping) else payload
    segments = message.get("message")
    if not isinstance(segments, (list, tuple)):
        return None
    images = []
    for segment in segments:
        if not isinstance(segment, Mapping) or segment.get("type") != "image":
            continue
        data = segment.get("data")
        candidates = tuple(dict.fromkeys(
            value.strip() for key in ("url", "file", "path")
            if isinstance(data, Mapping) and isinstance(value := data.get(key), str) and value.strip()
        ))
        images.append(candidates)
    return images


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
