"""LLM tool contract derived from registered search services."""

from __future__ import annotations


def available_modes(
    source_names: tuple[str, ...], anime_names: tuple[str, ...]
) -> tuple[str, ...]:
    modes = ("auto", "source") if source_names else ()
    return modes + (("anime",) if anime_names else ())


def tool_description(
    source_names: tuple[str, ...], anime_names: tuple[str, ...]
) -> str:
    parts = [
        "Search an attached or replied image only after an explicit user request "
        "to find its source or identify an anime screenshot. Do not call merely "
        "because an image is present or discussed.",
    ]
    if source_names:
        parts.append("Enabled source services: " + ", ".join(source_names) + ".")
        parts.append(
            "auto uses the conditional source search plan; source searches image "
            "provenance only. Enabled services are candidates, not all called "
            "on every search."
        )
        parts.append(
            "When the user explicitly selects source channels (for example Google), "
            "channels is fixed by the plugin from the current user message. Do not choose extra channels. For an unspecified default search, "
            "pass an empty string. Explicit channels search only the selected services "
            "without fallback or anime follow-up."
        )
        if anime_names:
            parts.append(
                "auto may add anime search only after a structured anime signal."
            )
    if anime_names:
        parts.append("Enabled anime services: " + ", ".join(anime_names) + ".")
        parts.append("anime searches for title, episode, and timestamp.")
    modes = available_modes(source_names, anime_names)
    if modes:
        parts.append(
            "Allowed modes: " + ", ".join(modes) + ". Default: " + modes[0] + "."
        )
    parts.append(
        "Search all attached images in one call within the batch limit. "
        "Set refresh=true only for an explicit request to search again without cache (-purge). "
        "Enabled does not guarantee service availability. Do not claim to use "
        "disabled services or claim searches succeeded before receiving results. "
        "The tool sends formatted results directly to the user. Do not add "
        "search narration before calling it. After it finishes, follow its "
        "returned instruction for a single brief response in the current persona; "
        "do not repeat the results or links."
    )
    return "\n".join(parts)


def completion_instruction(result_count: int) -> str:
    """Continue the existing model conversation after direct result delivery."""
    outcome = (
        "插件已向用户发送搜索结果。"
        if result_count else
        "插件已向用户发送无结果或搜索失败说明，没有可展示的结果。"
    )
    return (
        outcome
        + "请沿用当前会话的人格语气，只回复一句简短反馈，表明你已使用搜图工具。"
        "有结果时简短确认已发出；无结果时说明未找到符合条件的结果，不声称找到了出处。"
        "渠道事实以 attempted_providers 和 result_providers 为准，不把结果归因于用户提到但未返回结果的渠道。"
        "cached_images 大于零表示使用过缓存，不把缓存结果说成刚刚进行的外部搜索。"
        "不要重复结果、标题、链接或错误详情，不要追问，也不要再次调用搜图工具。"
    )
