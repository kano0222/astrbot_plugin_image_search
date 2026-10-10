"""Validate source-channel aliases independently from AstrBot."""

from __future__ import annotations

import re

_CHANNELS = {"google_lens", "saucenao", "ascii2d"}
_BUILTINS = {
    "google": "google_lens",
    "lens": "google_lens",
    "sauce": "saucenao",
    "2d": "ascii2d",
}


def build_channel_aliases(providers: object) -> tuple[dict[str, str], int]:
    """Read each service's alias list; builtin routing cannot be overridden."""
    aliases = dict(_BUILTINS)
    defaults = {"google_lens": ["g", "谷歌"], "saucenao": ["s"], "ascii2d": ["a"]}
    if not isinstance(providers, dict):
        providers = {}
    invalid = 0
    for target, default in defaults.items():
        section = providers.get(target, {})
        entries = (
            section.get("aliases", default) if isinstance(section, dict) else default
        )
        if not isinstance(entries, list):
            invalid += 1
            continue
        for entry in entries:
            if not isinstance(entry, str):
                invalid += 1
                continue
            name = entry.strip().lower()
            if (
                not name
                or re.search(r"[\s,，、/=]", name)
                or name in _CHANNELS
                or name in _BUILTINS
            ):
                invalid += 1
                continue
            if name in aliases and aliases[name] != target:
                invalid += 1
                continue
            aliases[name] = target
    return aliases, invalid
