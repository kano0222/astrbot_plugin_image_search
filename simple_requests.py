"""Conservative whole-message routing for explicit, simple image searches."""
from __future__ import annotations

import re
from dataclasses import dataclass

_PREFIX = r"(?:请)?(?:帮我|帮忙|麻烦你)?"
_EXPLICIT = (
    r"(?:搜图|反向搜图|搜索图片|搜一下图|"
    r"(?:找|查)(?:一下|下)?(?:这张图片|这张图|图片|图)?的?(?:来源|出处|原图))"
)
_CONTEXTUAL = r"(?:搜一下|搜下)"
_END = "。！？.!?～~"


@dataclass(frozen=True, slots=True)
class SimpleSearchRequest:
    channels: str = ""
    requires_image: bool = False


def parse_simple_request(
    text: str, aliases: dict[str, str],
) -> SimpleSearchRequest | None:
    """Never route negations, embedded quotations, or mixed/complex tasks."""
    if not isinstance(text, str) or len(text) > 200:
        return None
    text = text.strip().rstrip(_END).strip()
    if not text or text.startswith("/") or "\n" in text or "\r" in text:
        return None
    for pattern, requires_image in ((_EXPLICIT, False), (_CONTEXTUAL, True)):
        match = re.fullmatch(_PREFIX + pattern + r"(?:\s+(.+))?", text)
        if match is None:
            continue
        channels = match.group(1) or ""
        tokens = re.split(r"[\s,，、]+", channels.lower()) if channels else []
        # Unknown numeric channel names use the command's existing warning;
        # arbitrary Chinese text must not be interpreted as a channel selection.
        if any(not token or (
            token not in aliases and token not in {"google_lens", "saucenao", "ascii2d"}
            and re.fullmatch(r"[0-9]+", token) is None
        ) for token in tokens):
            return None
        return SimpleSearchRequest(channels=channels, requires_image=requires_image)
    return None


def parse_tool_channels(text: str, aliases: dict[str, str]) -> str | None:
    """Bind exact channels in a whole search request without changing direct routing."""
    request = parse_simple_request(text, aliases)
    if request is not None and request.channels:
        return request.channels
    if not isinstance(text, str) or len(text) > 200:
        return None
    text = text.strip().rstrip(_END).strip()
    if "\n" in text or "\r" in text:
        return None
    match = re.fullmatch(
        _PREFIX + "(?:" + _EXPLICIT + "|" + _CONTEXTUAL
        + r"|搜索一下|搜索下)\s*(.+)", text,
    )
    if match is None:
        return None
    names = set(aliases) | {"google_lens", "saucenao", "ascii2d"}
    token = "(?:" + "|".join(re.escape(name) for name in sorted(names, key=len, reverse=True)) + r"|[0-9]+)"
    tail = match.group(1)
    if re.fullmatch(token + r"(?:(?:[\s,，、]+|\s*和\s*)" + token + ")*", tail,
                    re.IGNORECASE) is None:
        return None
    return " ".join(re.findall(token, tail, re.IGNORECASE))
