from __future__ import annotations

import unittest
from collections.abc import Mapping
from dataclasses import dataclass, field

from core.models import ImageAsset, SearchMode
from core.provider import ProviderError, safe_failure_reason
from core.providers.ascii2d import Ascii2dProvider
from core.web import TextResponse

_HOME_HTML = """
<form>
  <input value="token-value" type="hidden" name="authenticity_token">
</form>
"""

_RESULT_HTML = """
<div class="row item-box">
  <div><img src="/query.jpg"></div>
  <div><h6><a href="/query">query</a></h6></div>
</div>
<div class="row item-box">
  <div><img src="/thumb.jpg"></div>
  <div>
    <h6><a href="/relative">Artwork title</a></h6>
    <small><a href="https://example.com/artwork">source</a></small>
  </div>
</div>
"""


@dataclass
class FakeWebClient:
    get_responses: dict[str, TextResponse]
    post_response: TextResponse
    get_calls: list[str] = field(default_factory=list)
    post_data: Mapping[str, str] | None = None

    async def get_text(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        cookies: Mapping[str, str],
        timeout_seconds: float,
    ) -> TextResponse:
        del headers, cookies, timeout_seconds
        self.get_calls.append(url)
        return self.get_responses[url]

    async def post_form(
        self,
        url: str,
        *,
        data: Mapping[str, str],
        headers: Mapping[str, str],
        cookies: Mapping[str, str],
        timeout_seconds: float,
    ) -> TextResponse:
        del url, headers, cookies, timeout_seconds
        self.post_data = dict(data)
        return self.post_response


class Ascii2dProviderTests(unittest.IsolatedAsyncioTestCase):
    async def test_submission_challenge_has_specific_safe_diagnostic(self) -> None:
        client = self.make_client()
        client.post_response = TextResponse(
            403,
            "https://ascii2d.net/search/uri",
            '<script src="/cdn-cgi/challenge-platform/test"></script>'
            "Cloudflare cf-chl secret-cookie-value",
        )
        with self.assertRaises(ProviderError) as caught:
            await Ascii2dProvider(client).search(
                ImageAsset(url="https://images.example/query.jpg"), SearchMode.SOURCE
            )
        reason = safe_failure_reason(caught.exception)
        self.assertEqual(reason, "提交搜索返回 HTTP 403，Cloudflare 验证拦截")
        self.assertNotIn("secret-cookie-value", reason)
        self.assertEqual(client.get_calls, ["https://ascii2d.net"])

    async def test_generic_submission_403_is_not_labelled_cloudflare_challenge(
        self,
    ) -> None:
        client = self.make_client()
        client.post_response = TextResponse(
            403, "https://ascii2d.net/search/uri", "Cloudflare access denied"
        )
        with self.assertRaises(ProviderError) as caught:
            await Ascii2dProvider(client).search(
                ImageAsset(url="https://images.example/query.jpg"), SearchMode.SOURCE
            )
        self.assertEqual(
            safe_failure_reason(caught.exception),
            "提交搜索返回 HTTP 403，访问受限",
        )

    def make_client(self) -> FakeWebClient:
        color_url = "https://ascii2d.net/search/color/id"
        bovw_url = "https://ascii2d.net/search/bovw/id"
        return FakeWebClient(
            get_responses={
                "https://ascii2d.net": TextResponse(
                    200,
                    "https://ascii2d.net",
                    _HOME_HTML,
                ),
                color_url: TextResponse(200, color_url, _RESULT_HTML),
                bovw_url: TextResponse(200, bovw_url, _RESULT_HTML),
            },
            post_response=TextResponse(200, color_url, ""),
        )

    async def test_runs_both_modes_and_parses_external_result(self) -> None:
        client = self.make_client()
        provider = Ascii2dProvider(
            client,
            session_id="session",
            cf_clearance="clearance",
        )

        outcome = await provider.search(
            ImageAsset(url="https://images.example/query.jpg"),
            SearchMode.SOURCE,
        )

        self.assertEqual(len(outcome.matches), 2)
        self.assertEqual(
            [match.site_name for match in outcome.matches],
            ["Ascii2d BOVW", "Ascii2d color"],
        )
        self.assertEqual(outcome.matches[0].title, "Artwork title")
        self.assertEqual(outcome.matches[0].url, "https://example.com/artwork")
        self.assertEqual(
            outcome.matches[0].thumbnail_url,
            "https://ascii2d.net/thumb.jpg",
        )
        self.assertEqual(client.post_data["authenticity_token"], "token-value")
        self.assertEqual(client.post_data["uri"], "https://images.example/query.jpg")

    async def test_one_mode_failure_preserves_other_mode(self) -> None:
        client = self.make_client()
        bovw_url = "https://ascii2d.net/search/bovw/id"
        client.get_responses[bovw_url] = TextResponse(503, bovw_url, "")
        provider = Ascii2dProvider(client)

        outcome = await provider.search(
            ImageAsset(url="https://images.example/query.jpg"),
            SearchMode.SOURCE,
        )

        self.assertEqual(len(outcome.matches), 1)
        self.assertEqual(outcome.matches[0].site_name, "Ascii2d color")
        self.assertEqual(outcome.notices, ["Ascii2d 部分搜索模式失败：BOVW"])

    async def test_missing_token_is_error(self) -> None:
        client = self.make_client()
        client.get_responses["https://ascii2d.net"] = TextResponse(
            200,
            "https://ascii2d.net",
            "<html></html>",
        )
        provider = Ascii2dProvider(client)

        with self.assertRaisesRegex(ProviderError, "token"):
            await provider.search(
                ImageAsset(url="https://images.example/query.jpg"),
                SearchMode.SOURCE,
            )

    async def test_non_result_redirect_is_error(self) -> None:
        client = self.make_client()
        client.post_response = TextResponse(200, "https://ascii2d.net/", "")
        provider = Ascii2dProvider(client)

        with self.assertRaisesRegex(ProviderError, "result page"):
            await provider.search(
                ImageAsset(url="https://images.example/query.jpg"),
                SearchMode.SOURCE,
            )

    async def test_challenge_page_is_not_an_empty_success(self) -> None:
        client = self.make_client()
        for mode in ("color", "bovw"):
            url = f"https://ascii2d.net/search/{mode}/id"
            client.get_responses[url] = TextResponse(
                200, url, "<html><title>Just a moment...</title></html>"
            )

        with self.assertRaisesRegex(ProviderError, "both failed"):
            await Ascii2dProvider(client).search(
                ImageAsset(url="https://images.example/query.jpg"),
                SearchMode.SOURCE,
            )

    async def test_external_result_redirect_is_rejected(self) -> None:
        client = self.make_client()
        client.post_response = TextResponse(
            200, "https://other.example/search/color/id", ""
        )

        with self.assertRaisesRegex(ProviderError, "result page"):
            await Ascii2dProvider(client).search(
                ImageAsset(url="https://images.example/query.jpg"),
                SearchMode.SOURCE,
            )
        self.assertEqual(client.get_calls, ["https://ascii2d.net"])


if __name__ == "__main__":
    unittest.main()
