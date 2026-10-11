"""Text and result-card presentation for structured search results."""

from __future__ import annotations

from dataclasses import dataclass

if __package__:
    from .core.models import AnimeMatch, SearchResponse, SourceMatch
    from .core.source_results import group_source_matches, source_link_key
else:
    from core.models import AnimeMatch, SearchResponse, SourceMatch
    from core.source_results import group_source_matches, source_link_key


def format_search_help(
    source_names: tuple[str, ...], anime_names: tuple[str, ...],
    aliases: dict[str, str], *, batch_limit: int, cache_enabled: bool,
    search_mode_enabled: bool, mode_timeout: float,
) -> str:
    """Describe only commands backed by currently available services."""
    lines = ["搜图帮助"]
    if not source_names and not anime_names:
        return "搜图帮助\n当前没有可用的搜索服务，请联系管理员配置。"
    if source_names:
        lines.append("/搜图：搜索图片出处，可附图或引用图片。")
        for name in source_names:
            names = sorted((alias for alias, target in aliases.items() if target == name),
                           key=lambda alias: (len(alias), alias))
            lines.append(f"{provider_label(name)}：" + "、".join(names or [name]))
        first = source_names[0]
        example = min((alias for alias, target in aliases.items() if target == first),
                      key=lambda alias: (len(alias), alias), default=first)
        lines.append(f"指定渠道：/搜图 {example}；多个渠道用空格分隔。")
    if anime_names:
        lines.append("/搜番：查找动画名称、集数和时间点。")
    lines.append(f"支持一次多图，最多 {batch_limit} 张；也可先发命令，再发送图片。")
    if cache_enabled:
        command = "搜图" if source_names else "搜番"
        lines.append(f"刷新缓存：/{command} -purge；成功后更新缓存。")
    if search_mode_enabled and source_names:
        lines.append(
            f"连续搜图：/搜图模式，可附渠道；直接发图，{mode_timeout:g} 秒无动作自动退出。\n"
            "模式内发送「退出」结束，也可使用 /退出搜图模式。"
        )
    elif not search_mode_enabled:
        lines.append("搜图模式已关闭。")
    else:
        lines.append("搜图模式不可用：没有可用的来源搜索渠道。")
    return "\n".join(lines)


def format_search_response(response: SearchResponse, *, cached: bool = False) -> str:
    """Render grouped source and anime matches without provider-specific coupling."""
    if response.matches:
        return "\n\n".join(card.text for card in build_result_cards(response, cached=cached))
    sections: list[str] = []
    if not response.attempted_providers:
        sections.append("没有可用的搜索服务，请先检查插件配置。")
    elif len(response.failed_providers) == len(response.attempted_providers):
        sections.append("搜索服务暂时不可用，请稍后重试。")
    else:
        for name in response.attempted_providers:
            if name in response.failed_providers:
                continue
            threshold = response.filtered_providers.get(name)
            sections.append(
                f"{provider_label(name)} 候选相似度低于 {threshold:g}%，未展示。"
                if threshold is not None else
                f"{provider_label(name)} 未找到匹配结果。"
            )

    sections.extend(_response_notes(response))
    return "\n\n".join(sections)


@dataclass(frozen=True, slots=True)
class ResultCard:
    text: str
    preview_urls: tuple[str, ...] = ()
    image_after_lines: int | None = None


@dataclass(frozen=True, slots=True)
class BatchResult:
    image_index: int
    response: SearchResponse | None = None
    message: str = ""
    cached: bool = False


def build_batch_cards(results: list[BatchResult]) -> list[ResultCard]:
    """Keep input order and group sources only within each input image."""
    cards = []
    for result in results:
        group = (build_result_cards(result.response, cached=result.cached) if result.response is not None
                 else [ResultCard(result.message)])
        count = count_search_results(result.response) if result.response is not None else 0
        prefix = f"第 {result.image_index} 张图片\n" if count != 1 else ""
        for index, card in enumerate(group):
            heading = prefix if index == 0 else ""
            cards.append(ResultCard(
                heading + card.text, card.preview_urls,
                card.image_after_lines + 1 if heading and card.image_after_lines is not None
                else card.image_after_lines,
            ))
    return cards


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


def build_result_cards(response: SearchResponse, *, cached: bool = False) -> list[ResultCard]:
    """Cards retain individual previews; delivery groups them by input image."""
    sources = [m for m in response.matches if isinstance(m, SourceMatch)]
    anime = [m for m in response.matches if isinstance(m, AnimeMatch)]
    cards = []
    numbered = count_search_results(response) > 1
    source_groups = group_source_matches(sources)
    lens_index = 0
    for index, group in enumerate(source_groups, start=1):
        previews = tuple(dict.fromkeys(m.thumbnail_url for m in group if m.thumbnail_url))
        lines = _format_source_match(index if numbered else None, group)
        pure_lens = all(m.provider == "google_lens" for m in group)
        if pure_lens and (numbered or cached):
            lens_index += 1
            title = next((m.title for m in group if m.title and m.title != "未提供标题"), "未提供标题")
            scores = list(dict.fromkeys(m.similarity for m in group if m.similarity is not None))
            score_text = " / ".join(f"{score:.2f}%" for score in scores)
            caption = (f"{lens_index}. " if numbered else "") + title + (f" ({score_text})" if score_text else "")
            header = "Google Lens" + ("[缓存]" if cached else "")
            lines = ([header] if lens_index == 1 else []) + [caption]
            lines.extend(_source_links(group, ("url", "author_url", "source_url"), match_first=True))
        image_after = None
        if any(match.provider in {"saucenao", "google_lens"} for match in group):
            image_after = next(
                (i for i, line in enumerate(lines)
                 if line.startswith(("http://", "https://", "Author: "))),
                len(lines),
            )
        text = "\n".join(lines)
        if cached and not pure_lens:
            text = "[缓存] " + text
        cards.append(ResultCard(text, previews, image_after))
    for index, match in enumerate(anime, start=1):
        previews = (match.preview_url,) if match.preview_url and not match.adult else ()
        cards.append(ResultCard(
            ("[缓存] " if cached else "") + "动画场景\n" + "\n".join(_format_anime_match(index if numbered else None, match)), previews,
        ))
    if not cards:
        return [ResultCard(format_search_response(response))]
    notes = _response_notes(response)
    if notes:
        cards.append(ResultCard("\n\n".join(notes)))
    return cards


def _format_source_match(index: int | None, matches: list[SourceMatch]) -> list[str]:
    if any(match.provider in {"saucenao", "google_lens"} for match in matches):
        return _format_compact_match(index, matches)
    providers = list(dict.fromkeys(match.provider for match in matches))
    labels = " / ".join(provider_label(name) for name in providers)
    title = next(
        (match.title for match in matches if match.title and match.title != "未提供标题"),
        "未提供标题",
    )
    lines = [(f"{index}. " if index is not None else "") + f"[{labels}] {title}"]
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


def _format_compact_match(index: int | None, matches: list[SourceMatch]) -> list[str]:
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
    lines = [(f"{index}. " if index is not None else "") + " / ".join(labels)]
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


def _format_anime_match(index: int | None, match: AnimeMatch) -> list[str]:
    lines = [(f"{index}. " if index is not None else "") + f"[{provider_label(match.provider)}] {match.title}"]
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
