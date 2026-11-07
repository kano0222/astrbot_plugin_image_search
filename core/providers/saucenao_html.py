"""Parse anonymous SauceNAO result cards into the shared candidate format."""

from __future__ import annotations

from dataclasses import dataclass, field
from html.parser import HTMLParser
import math
import re
from urllib.parse import urljoin, urlsplit

from ..provider import ProviderError


@dataclass
class _Node:
    tag: str
    attrs: dict[str, str | None] = field(default_factory=dict)
    children: list = field(default_factory=list)

    def text(self, *, exclude_results: bool = False) -> str:
        if exclude_results and "result" in (self.attrs.get("class") or "").split():
            return ""
        return "".join(
            child.text(exclude_results=exclude_results) if isinstance(child, _Node) else child
            for child in self.children
        )

    def find(self, *, css_class: str = "", tag: str = "") -> list[_Node]:
        found = []
        for child in self.children:
            if not isinstance(child, _Node):
                continue
            if ((not css_class or css_class in (child.attrs.get("class") or "").split())
                    and (not tag or child.tag == tag)):
                found.append(child)
            found.extend(child.find(css_class=css_class, tag=tag))
        return found


class _ResultPage(HTMLParser):
    """Keep nested markup, including field labels and links, within each card."""

    _VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input",
             "link", "meta", "param", "source", "track", "wbr"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = _Node("root")
        self.stack = [self.root]

    def handle_starttag(self, tag, attrs):
        if tag == "br":
            self.stack[-1].children.append("\n")
            return
        node = _Node(tag, dict(attrs))
        self.stack[-1].children.append(node)
        if tag not in self._VOID:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in self._VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                del self.stack[index:]
                break

    def handle_data(self, data):
        if self.stack[-1].tag not in {"script", "style"}:
            self.stack[-1].children.append(re.sub(r"\s+", " ", data))


def _url(value: str) -> str:
    if not value.strip():
        return ""
    try:
        url = urljoin("https://saucenao.com/", value.strip())
        parsed = urlsplit(url)
        parsed.port  # Validate a supplied port before retaining the link.
        if (parsed.scheme in {"https", "http"} and parsed.hostname
                and parsed.username is None and parsed.password is None):
            return url
    except ValueError:
        pass
    return ""


def _links(node: _Node) -> list[tuple[str, str]]:
    links = []
    for anchor in node.find(tag="a"):
        url = _url(anchor.attrs.get("href") or "")
        if not url:
            continue
        host = urlsplit(url).hostname
        if host == "saucenao.com" or host.endswith(".saucenao.com"):
            continue
        links.append((url, " ".join(anchor.text().split())))
    return links


def _parse_card(card: _Node) -> dict | None:
    similarities = card.find(css_class="resultsimilarityinfo")
    if not similarities:
        return None
    score = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*%\s*", similarities[0].text())
    if not score or not math.isfinite(float(score[1])) or not 0 <= float(score[1]) <= 100:
        return None
    titles = card.find(css_class="resulttitle")
    title = " ".join(titles[0].text().split()) if titles else ""
    author = author_url = source = ""
    content_links = []
    for column in card.find(css_class="resultcontentcolumn"):
        links = _links(column)
        content_links.extend(links)
        for line in column.text().splitlines():
            label, separator, value = line.strip().partition(":")
            if not separator:
                continue
            value = value.strip()
            if label.lower() in {"member", "author", "artist", "creator"}:
                author = value
                author_url = next((url for url, text in links if text == value), "")
            elif label.lower() == "source":
                source = next((url for url, text in links if text == value), value)
    misc_links = [link for node in card.find(css_class="resultmiscinfo") for link in _links(node)]
    urls = list(dict.fromkeys(url for url, _ in misc_links + content_links if url != author_url))
    if not title and not urls and not source:
        return None
    images = [image for node in card.find(css_class="resultimage") for image in node.find(tag="img")]
    thumbnail = _url(images[0].attrs.get("src") or "") if images else ""
    index_id = None
    # Infer only from explicit source identities, never from a guessed title.
    if any(urlsplit(url).hostname in {"pixiv.net", "www.pixiv.net"}
           and re.match(r"/(?:[a-z]{2}/)?(?:artworks/|i/)\d+", urlsplit(url).path)
           for url in urls):
        index_id = 5
    elif any(urlsplit(url).hostname in {"anidb.net", "www.anidb.net"} for url in urls):
        index_id = 21
    return {
        "header": {"similarity": score[1], "index_id": index_id,
                   "thumbnail": thumbnail, "index_name": ""},
        "data": {"title": title, "source": source, "ext_urls": urls,
                 "author_name": author, "author_url": author_url},
    }


def parse_anonymous_results(html: str) -> tuple[list[dict], bool]:
    page = _ResultPage()
    page.feed(html)
    page.close()
    cards = page.root.find(css_class="result")
    text = " ".join(page.root.text(exclude_results=True).split()).lower()
    if (re.search(r"(?:daily|short|long) (?:search )?limit (?:has been )?(?:reached|exceeded)", text)
            or re.search(r"exceeded (?:your |the )?(?:daily |short |long )?(?:search )?limit", text)
            or "too many searches" in text or "too many requests" in text
            or "search rate too high" in text):
        raise ProviderError("SauceNAO rate limit was exceeded")
    if ((not cards and ("captcha" in html.lower() or "cf-chl-" in html.lower()
                        or "challenge-platform" in html.lower()))
            or "verify you are human" in text):
        raise ProviderError("SauceNAO anonymous access was blocked")
    if not cards:
        if re.search(r"\bno (?:results|matches|similar images) (?:found|were found)\b", text):
            return [], False
        raise ProviderError("SauceNAO returned an unrecognized page")
    results = [result for card in cards if (result := _parse_card(card)) is not None]
    if not results:
        raise ProviderError("SauceNAO returned an unrecognized page")
    return results, len(results) != len(cards)
