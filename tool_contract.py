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
        "Enabled does not guarantee service availability. Do not claim to use "
        "disabled services or claim searches succeeded before receiving results. "
        "The tool sends formatted results directly to the user."
    )
    return "\n".join(parts)
