"""Best-effort provenance from explicit booru source fields, never link guessing."""
from __future__ import annotations

import asyncio
import re
from dataclasses import replace
from html.parser import HTMLParser
from typing import Protocol
from urllib.parse import parse_qs, urlsplit

from .models import SearchResponse, SourceMatch
from .source_results import source_link_key

_PAGE_TIMEOUT_SECONDS = 3
_TOTAL_TIMEOUT_SECONDS = 6

_HOSTS = {"danbooru.donmai.us", "gelbooru.com", "konachan.com", "yande.re"}


def post_page_url(value: str) -> str:
    """Allow only known public post pages; strip unrelated query parameters."""
    try:
        parsed = urlsplit(value.strip())
        host = parsed.hostname
        if (parsed.scheme not in {"http", "https"} or host not in _HOSTS
                or parsed.username is not None or parsed.password is not None
                or parsed.port not in {None, 80, 443}):
            return ""
        if host == "gelbooru.com":
            query = parse_qs(parsed.query)
            if (parsed.path != "/index.php" or query.get("page") != ["post"]
                    or query.get("s") != ["view"] or len(query.get("id", [])) != 1):
                return ""
            post_id = query["id"][0]
            if not re.fullmatch(r"[1-9]\d*", post_id):
                return ""
            return f"https://{host}/index.php?page=post&s=view&id={post_id}"
        pattern = r"/(?:posts|post/show)/([1-9]\d*)/?" if host == "danbooru.donmai.us" else r"/post/show/([1-9]\d*)/?"
        match = re.fullmatch(pattern, parsed.path)
        if not match:
            return ""
        path = "posts" if host == "danbooru.donmai.us" else "post/show"
        return f"https://{host}/{path}/{match.group(1)}"
    except (AttributeError, ValueError):
        return ""


def source_page_url(value: str) -> str:
    """Accept URL provenance only, and convert Pixiv image URLs to artwork pages."""
    value = value.strip()
    if not source_link_key(value) or any(ord(char) < 32 for char in value):
        return ""
    parsed = urlsplit(value)
    if parsed.password is not None:
        return ""
    if parsed.hostname in {"i.pximg.net", "pximg.net"}:
        match = re.search(r"/(\d+)(?:_p\d+)?(?:\.[a-zA-Z0-9]+)?$", parsed.path)
        return f"https://www.pixiv.net/artworks/{match.group(1)}" if match else ""
    return value


class _SourceParser(HTMLParser):
    def __init__(self, host: str) -> None:
        super().__init__(convert_charrefs=True)
        self.host = host
        self.sources: set[str] = set()

    def handle_starttag(self, tag, attrs) -> None:
        values = dict(attrs)
        candidate = ""
        if self.host in {"danbooru.donmai.us", "gelbooru.com"}:
            if "image-container" in (values.get("class") or "").split():
                candidate = values.get("data-normalized-source") or ""
        elif tag == "input" and values.get("id") == "post_source":
            candidate = values.get("value") or ""
        source = source_page_url(candidate)
        if source:
            self.sources.add(source)


def parse_source(page_url: str, html: str) -> str:
    canonical = post_page_url(page_url)
    if not canonical:
        return ""
    parser = _SourceParser(urlsplit(canonical).hostname)
    parser.feed(html)
    # Conflicting page fields do not prove a unique original.
    if len(parser.sources) != 1:
        return ""
    source = next(iter(parser.sources))
    return "" if post_page_url(source) == canonical else source


class SourcePageLoader(Protocol):
    async def load(self, url: str) -> str | None: ...


class SourceEnricher:
    """Resolve at most six distinct pages with one six-second overall budget."""

    def __init__(self, loader: SourcePageLoader) -> None:
        self._loader = loader

    async def enrich(self, response: SearchResponse) -> None:
        pages: dict[str, list[int]] = {}
        for index, match in enumerate(response.matches):
            if not isinstance(match, SourceMatch) or match.source_url:
                continue
            page = post_page_url(match.url)
            if page and (page in pages or len(pages) < 6):
                pages.setdefault(page, []).append(index)
        if not pages:
            return
        semaphore = asyncio.Semaphore(3)

        async def resolve(page: str) -> str:
            async with semaphore:
                try:
                    html = await asyncio.wait_for(self._loader.load(page), timeout=_PAGE_TIMEOUT_SECONDS)
                    return parse_source(page, html) if html else ""
                except Exception:
                    # Enrichment is not a provider failure; retain its search result.
                    return ""

        tasks = {asyncio.create_task(resolve(page)): page for page in pages}
        try:
            done, _ = await asyncio.wait(tasks, timeout=_TOTAL_TIMEOUT_SECONDS)
            for task in done:
                source = task.result()
                if source:
                    for index in pages[tasks[task]]:
                        response.matches[index] = replace(response.matches[index], source_url=source)
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
