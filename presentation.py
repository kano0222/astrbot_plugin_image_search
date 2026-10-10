"""Text and result-card presentation for structured search results."""

from __future__ import annotations

from dataclasses import dataclass

if __package__:
    from .core.models import AnimeMatch, SearchResponse, SourceMatch
    from .core.source_results import group_source_matches, source_link_key
else:
    from core.models import AnimeMatch, SearchResponse, SourceMatch
    from core.source_results import group_source_matches, source_link_key


def format_search_response(response: SearchResponse) -> str:
    """Render grouped source and anime matches without provider-specific coupling."""
    source_matches = [
        match for match in response.matches if isinstance(match, SourceMatch)
    ]
    anime_matches = [
        match for match in response.matches if isinstance(match, AnimeMatch)
    ]

    sections: list[str] = []
    if source_matches:
        lines = []
        for index, match in enumerate(group_source_matches(source_matches), start=1):
            lines.extend(_format_source_match(index, match))
        sections.append("\n".join(lines))

    if anime_matches:
        lines = ["动画场景"]
        for index, match in enumerate(anime_matches, start=1):
            lines.extend(_format_anime_match(index, match))
        sections.append("\n".join(lines))

    if not sections:
        if not response.attempted_providers:
            sections.append("没有可用的搜索服务，请先检查插件配置。")
        elif len(response.failed_providers) == len(response.attempted_providers):
            sections.append("搜索服务暂时不可用，请稍后重试。")
        else:
            sections.append("未找到可靠结果，请尝试更换更清晰、裁剪更准确的图片。")

    sections.extend(_response_notes(response))
    return "\n\n".join(sections)


@dataclass(frozen=True, slots=True)
class ResultCard:
    text: str
    preview_urls: tuple[str, ...] = ()
    image_after_lines: int | None = None


def _response_notes(response: SearchResponse) -> list[str]:
    sections: list[str] = []
    if response.notices:
        sections.append("\n".join(response.notices))
    if response.failed_providers and len(response.failed_providers) < len(
        response.attempted_providers
    ):
        sections.append("部分搜索服务失败：" + "、".join(provider_label(name) for name in response.failed_providers))
    if response.failure_details:
        sections.append(
            "\n".join(
                f"{provider_label(name)}：{reason}" for name, reason in response.failure_details.items()
            )
        )
    return sections


def count_search_results(response: SearchResponse) -> int:
    """Count grouped sources and anime results, excluding diagnostic cards."""
    sources = [m for m in response.matches if isinstance(m, SourceMatch)]
    return len(group_source_matches(sources)) + sum(
        isinstance(m, AnimeMatch) for m in response.matches
    )


def build_result_cards(response: SearchResponse) -> list[ResultCard]:
    """One node per grouped result; notices/failures remain visible in a final node."""
    sources = [m for m in response.matches if isinstance(m, SourceMatch)]
    anime = [m for m in response.matches if isinstance(m, AnimeMatch)]
    cards = []
    for index, group in enumerate(group_source_matches(sources), start=1):
        previews = tuple(dict.fromkeys(m.thumbnail_url for m in group if m.thumbnail_url))
        lines = _format_source_match(index, group)
        image_after = None
        if any(match.provider in {"saucenao", "google_lens"} for match in group):
            image_after = next(
                (i for i, line in enumerate(lines)
                 if line.startswith(("http://", "https://", "Author: "))),
                len(lines),
            )
        cards.append(ResultCard("\n".join(lines), previews, image_after))
    for index, match in enumerate(anime, start=1):
        previews = (match.preview_url,) if match.preview_url and not match.adult else ()
        cards.append(ResultCard(
            "动画场景\n" + "\n".join(_format_anime_match(index, match)), previews,
        ))
    if not cards:
        return [ResultCard(format_search_response(response))]
    notes = _response_notes(response)
    if notes:
        cards.append(ResultCard("\n\n".join(notes)))
    return cards


def _format_source_match(index: int, matches: list[SourceMatch]) -> list[str]:
    if any(match.provider in {"saucenao", "google_lens"} for match in matches):
        return _format_compact_match(index, matches)
    providers = list(dict.fromkeys(match.provider for match in matches))
    labels = " / ".join(provider_label(name) for name in providers)
    title = next(
        (match.title for match in matches if match.title and match.title != "未提供标题"),
        "未提供标题",
    )
    lines = [f"{index}. [{labels}] {title}"]
    details = []
    for field in ("site_name", "author"):
        value = next((getattr(match, field) for match in matches if getattr(match, field)), "")
        if value:
            details.append(value)
    scores = list(dict.fromkeys(
        (match.provider, match.similarity)
        for match in matches if match.similarity is not None
    ))
    for provider, score in scores:
        label = provider_label(provider) + "：" if len(providers) > 1 else ""
        details.append(f"{label}{score:.2f}%")
    if details:
        lines.append(" · ".join(details))

    lines.extend(_source_links(matches, ("source_url", "url"), source_label=True))
    return lines


def _source_links(
    matches: list[SourceMatch], fields: tuple[str, ...], *,
    source_label: bool = False, match_first: bool = False,
) -> list[str]:
    lines = []
    seen = set()
    pairs = (
        ((match, field) for match in matches for field in fields)
        if match_first else
        ((match, field) for field in fields for match in matches)
    )
    for match, field in pairs:
        url = getattr(match, field)
        if not url:
            continue
        key = source_link_key(url) or url
        if key in seen:
            continue
        seen.add(key)
        prefix = "Author: " if field == "author_url" else (
            "来源页：" if field == "source_url" and source_label else ""
        )
        lines.append(prefix + url)
    return lines


def _format_compact_match(index: int, matches: list[SourceMatch]) -> list[str]:
    """Compact Lens/CQPS layout, retaining merged channels and independent scores."""
    providers = list(dict.fromkeys(match.provider for match in matches))
    labels = []
    for provider in providers:
        scores = list(dict.fromkeys(
            match.similarity for match in matches
            if match.provider == provider and match.similarity is not None
        ))
        score_text = " / ".join(f"{score:.2f}%" for score in scores)
        label = provider_label(provider)
        labels.append(label + (f" ({score_text})" if score_text else ""))
    lines = [f"{index}. " + " / ".join(labels)]
    preferred = [match for match in matches if match.provider == "saucenao"]
    preferred += [match for match in matches if match.provider != "saucenao"]
    title = next(
        (match.title for match in preferred
         if match.title and match.title != "未提供标题"), "",
    )
    author = next((match.author for match in preferred if match.author), "")
    if author or any(match.provider == "saucenao" for match in matches):
        caption = "/".join(f"「{value}」" for value in (title, author) if value)
    else:
        caption = title
    if caption:
        lines.append(caption)
    lines.extend(_source_links(preferred, ("url", "author_url", "source_url"), match_first=True))
    return lines


def _format_anime_match(index: int, match: AnimeMatch) -> list[str]:
    lines = [f"{index}. [{provider_label(match.provider)}] {match.title}"]
    details = []
    if match.episode:
        details.append(f"第 {match.episode} 集")
    if match.timestamp_start is not None:
        timestamp = _format_timestamp(match.timestamp_start)
        if match.timestamp_end is not None:
            timestamp += f"–{_format_timestamp(match.timestamp_end)}"
        details.append(timestamp)
    if match.similarity is not None:
        details.append(f"{match.similarity:.2f}%")
    if match.adult:
        details.append("成人内容")
    if details:
        lines.append(" · ".join(details))
    if match.preview_url and not match.adult:
        lines.append(match.preview_url)
    return lines


def provider_label(name: str) -> str:
    return {
        "google_lens": "Google Lens",
        "saucenao": "SauceNAO",
        "ascii2d": "Ascii2d",
        "trace_moe": "trace.moe",
    }.get(name, name)


def _format_timestamp(seconds: float) -> str:
    total_seconds = max(0, int(seconds))
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}"
    return f"{minutes:02d}:{seconds:02d}"
