"""Conservative source-link grouping without changing search plans or scores."""
from __future__ import annotations

import re
from collections.abc import Sequence
from urllib.parse import unquote, urlsplit, urlunsplit

from .models import SourceMatch

_TRACKING_KEYS = {"fbclid", "gclid", "msclkid"}


def source_link_key(url: str) -> str:
    """Normalize only known identity-preserving URL differences; never fetch."""
    try:
        parsed = urlsplit(url.strip())
        scheme = parsed.scheme.lower()
        host = (parsed.hostname or "").lower()
        port = parsed.port
    except (AttributeError, ValueError):
        return ""
    if scheme not in {"http", "https"} or not host or parsed.username is not None:
        return ""
    default_port = 80 if scheme == "http" else 443
    query = "&".join(
        part for part in parsed.query.split("&")
        if not (
            unquote(part.split("=", 1)[0]).lower().startswith("utm_")
            or unquote(part.split("=", 1)[0]).lower() in _TRACKING_KEYS
        )
    )
    # Match the modern Pixiv URL forms already recognized by the provider.
    # Unknown query parameters/fragments may identify a distinct view: retain them.
    if host in {"pixiv.net", "www.pixiv.net"} and port in {None, default_port}:
        artwork = re.fullmatch(
            r"/(?:(?:[a-z]{2}/)?artworks|i)/(\d+)/?", parsed.path
        )
        if artwork and not query and not parsed.fragment:
            return "https://www.pixiv.net/artworks/" + artwork.group(1)
    authority = "[" + host + "]" if ":" in host else host
    if port is not None and port != default_port:
        authority += ":" + str(port)
    return urlunsplit((scheme, authority, parsed.path, query, parsed.fragment))


def group_source_matches(matches: Sequence[SourceMatch]) -> list[list[SourceMatch]]:
    """Group preferred sources, then attach unambiguous mirror-only results.

    Different explicit sources never merge via a shared mirror. Build the entire
    alias index first so conflicts and result order cannot affect that decision.
    Missing or invalid links are not evidence for merging.
    """
    primary_keys = [
        source_link_key(match.source_url) or source_link_key(match.url)
        for match in matches
    ]
    explicit_sources = {
        source_link_key(match.source_url)
        for match in matches if source_link_key(match.source_url)
    }
    alias_sources: dict[str, set[str]] = {}
    for match in matches:
        source = source_link_key(match.source_url)
        if not source:
            continue
        for key in (source, source_link_key(match.url)):
            if key:
                alias_sources.setdefault(key, set()).add(source)

    groups: list[list[SourceMatch]] = []
    positions: dict[str, int] = {}
    for match, key in zip(matches, primary_keys):
        # An explicit source identity takes precedence over mirror aliases.
        if key and key not in explicit_sources:
            targets = alias_sources.get(key, set())
            if len(targets) == 1:
                key = next(iter(targets))
        if key and key in positions:
            groups[positions[key]].append(match)
        else:
            if key:
                positions[key] = len(groups)
            groups.append([match])
    return groups
