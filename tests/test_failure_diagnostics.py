from __future__ import annotations

import unittest

from core.models import ImageAsset, ProviderOutcome, SearchMode, SourceMatch
from core.provider import ProviderError, safe_failure_reason
from core.service import SearchService


class FailureDiagnosticTests(unittest.TestCase):
    def test_known_failures_have_specific_safe_messages(self):
        cases = {
            "Google Lens request timed out": "请求超时",
            "SauceNAO request timed out": "请求超时",
            "SauceNAO search quota was exceeded": "搜索额度已用尽",
            "SauceNAO API key was rejected": "API Key 认证失败",
            "SauceNAO rate limit was exceeded": "请求频率受限",
            "all SerpAPI credentials or quotas were rejected": "全部 SerpAPI Key 认证失败或额度受限",
            "SerpAPI returned HTTP 500": "搜索接口返回 HTTP 500，服务端异常",
            "SauceNAO returned HTTP 502": "搜索接口返回 HTTP 502，服务端异常",
            "SerpAPI returned HTTP 400": "搜索接口返回 HTTP 400",
        }
        for error, expected in cases.items():
            with self.subTest(error=error):
                self.assertEqual(safe_failure_reason(ProviderError(error)), expected)

    def test_similar_untrusted_messages_are_not_echoed(self):
        for message in ("SerpAPI returned HTTP 500 secret=token",
                        "SauceNAO API key was rejected: secret",
                        "https://private.test/image?token=secret"):
            self.assertEqual(safe_failure_reason(ProviderError(message)),
                             "搜索服务请求失败")


class FailureIsolationTests(unittest.IsolatedAsyncioTestCase):
    async def test_sauce_timeout_still_falls_back_to_lens(self):
        class Sauce:
            name = "saucenao"

            async def search(self, image, mode):
                raise ProviderError("SauceNAO request timed out")

        class Lens:
            name = "google_lens"

            async def search(self, image, mode):
                return ProviderOutcome(matches=[
                    SourceMatch(provider="google_lens", title="Art",
                                url="https://site.test/a"),
                ])

        response = await SearchService(source_providers=[Sauce(), Lens()]).search(
            ImageAsset(url="https://images.test/a"), SearchMode.AUTO,
        )
        self.assertEqual(response.attempted_providers, ["saucenao", "google_lens"])
        self.assertEqual(response.failure_details, {"saucenao": "请求超时"})
        self.assertEqual(len(response.matches), 1)
