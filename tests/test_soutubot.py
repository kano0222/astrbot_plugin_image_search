from __future__ import annotations

import unittest
from unittest.mock import AsyncMock

from core.http import JsonResponse
from core.models import ImageAsset, SearchMode
from core.provider import ProviderError
from core.providers.soutubot import SoutubotProvider
from core.source_results import group_source_matches


def hit(source="nhentai", score=90, page=5, metadata=None):
    return {"score": score, "path_segments": [{
        "source_key": source, "external_id": "123", "page_no": page,
        "source_url": f"https://{source}.test/g/123",
        "page_url": f"https://{source}.test/g/123/{page}/",
        "thumbnail_url": "https://images.test/thumb.jpg",
        "metadata": metadata,
    }]}


def payload(results=None, *, partial=False, statuses=("ok",)):
    return {"status": "ok", "schema_version": "2.2", "partial": partial,
            "api_results": [{"status": status} for status in statuses],
            "results": [] if results is None else results}


class SoutubotTests(unittest.IsolatedAsyncioTestCase):
    async def test_fourth_candidate_without_public_links_is_not_malformed(self):
        candidates = [hit(page=page, score=95-page) for page in (1, 2, 3, 4)]
        missing = candidates[-1]["path_segments"][0]
        missing.pop("page_url")
        missing.pop("source_url")
        result, _ = await self.search(payload(candidates))
        self.assertEqual(len(result.matches), 3)
        self.assertFalse(result.notices)
        self.assertFalse(result.diagnostics)
        result, _ = await self.search(payload([candidates[-1]]))
        self.assertEqual(len(result.matches), 1)
        self.assertEqual(result.matches[0].url, "")
        self.assertEqual(result.matches[0].source_url, "")
        self.assertEqual(result.matches[0].thumbnail_url, missing["thumbnail_url"])
        self.assertIn("第 4 页", result.matches[0].title)
        self.assertFalse(result.notices)

    async def test_chapter_link_is_used_when_page_link_is_unavailable(self):
        candidate = hit()
        segment = candidate["path_segments"][0]
        segment.pop("page_url")
        segment["links"] = {"chapter_url": "https://site.test/chapter/5"}
        result, _ = await self.search(payload([candidate]))
        self.assertEqual(result.matches[0].url, segment["links"]["chapter_url"])
        self.assertFalse(result.notices)

    async def test_nested_links_are_valid_candidates_without_diagnostic_notice(self):
        candidate = hit()
        segment = candidate["path_segments"][0]
        segment["links"] = {key: segment.pop(key) for key in ("page_url", "source_url", "thumbnail_url")}
        result, _ = await self.search(payload([candidate]))
        self.assertEqual(result.matches[0].url, segment["links"]["page_url"])
        self.assertEqual(result.matches[0].source_url, segment["links"]["page_url"])
        self.assertEqual(result.matches[0].thumbnail_url, segment["links"]["thumbnail_url"])
        self.assertFalse(result.notices)
        self.assertFalse(result.diagnostics)

    async def test_nested_page_links_precede_gallery_and_preserve_distinct_pages(self):
        candidates = [hit(page=page) for page in (5, 6)]
        for candidate in candidates:
            segment = candidate["path_segments"][0]
            segment["links"] = {"page_url": segment.pop("page_url")}
        result, _ = await self.search(payload(candidates))
        self.assertEqual(len(group_source_matches(result.matches)), 2)
        self.assertTrue(result.matches[0].url.endswith("/5/"))
        self.assertFalse(result.notices)

    async def search(self, body, *, status=200, max_results=3):
        http = AsyncMock()
        http.upload_image_json.return_value = JsonResponse(status, body)
        provider = SoutubotProvider(http, max_results=max_results)
        result = await provider.search(ImageAsset(url="https://images.test/input"),
                                       SearchMode.SOURCE)
        return result, http

    async def test_all_sources_sorted_and_limited_without_threshold(self):
        result, http = await self.search(payload([
            hit("nhentai", 30), hit("gelbooru", 42), hit("danbooru", 40),
            hit("pixiv", 35),
        ]))
        self.assertEqual([item.site_name for item in result.matches],
                         ["gelbooru", "danbooru", "pixiv"])
        self.assertFalse(result.notices)
        call = http.upload_image_json.call_args
        self.assertEqual(call.args, ("https://soutubot.moe/api/search",))
        self.assertEqual(call.kwargs["data"], {
            "factor": "1.2", "metadata_mode": "display", "top_k": "3",
        })

    async def test_metadata_and_distinct_page_identity(self):
        metadata = {"title": {"primary": "Book"},
                    "creators": [{"name": "Artist"}],
                    "source": {"name": "NHentai"}}
        result, _ = await self.search(payload([
            hit(page=5, metadata=metadata), hit(page=6, metadata=metadata),
        ]))
        self.assertEqual(result.matches[0].title, "Book · 第 5 页")
        self.assertEqual(result.matches[0].author, "Artist")
        self.assertEqual(result.matches[0].site_name, "NHentai")
        self.assertEqual(len(group_source_matches(result.matches)), 2)

    async def test_booru_artists_and_string_title(self):
        result, _ = await self.search(payload([hit(metadata={
            "title": "Illustration", "tags": {"artists": [{"text": "Alice"}]},
        })]))
        self.assertEqual(result.matches[0].author, "Alice")
        self.assertTrue(result.matches[0].title.startswith("Illustration"))

    async def test_missing_metadata_keeps_source_and_page(self):
        result, _ = await self.search(payload([hit()]))
        self.assertEqual(result.matches[0].title, "nhentai 123 · 第 5 页")
        self.assertEqual(result.matches[0].author, "")

    async def test_partial_or_failed_library_adds_notice(self):
        for body in (payload([hit()], partial=True),
                     payload([hit()], statuses=("ok", "error"))):
            with self.subTest(body=body):
                result, _ = await self.search(body)
                self.assertTrue(result.notices)
                self.assertEqual(len(result.matches), 1)

    async def test_all_libraries_failed_is_error(self):
        with self.assertRaisesRegex(ProviderError, "all libraries failed"):
            await self.search(payload(statuses=("error", "timeout")))

    async def test_empty_success_is_not_failure(self):
        result, _ = await self.search(payload())
        self.assertEqual(result.matches, [])
        self.assertEqual(result.notices, [])

    async def test_malformed_data_cannot_appear_as_empty_success(self):
        for body in ([], {}, payload([{}]), payload([hit(score=float("nan"))]),
                     {**payload(), "api_results": []},
                     {**payload(), "results": {}},
                     {**payload(), "partial": "false"}):
            with self.subTest(body=body), self.assertRaises(ProviderError):
                await self.search(body)

    async def test_valid_candidates_survive_malformed_hit_with_notice(self):
        result, _ = await self.search(payload([hit(), {}]))
        self.assertEqual(len(result.matches), 1)
        self.assertTrue(result.notices)

    async def test_partial_status_preserves_successful_library(self):
        body = payload([hit()], partial=True, statuses=("ok", "error"))
        body["status"] = "partial"
        result, _ = await self.search(body)
        self.assertEqual(len(result.matches), 1)
        self.assertTrue(result.notices)

    async def test_service_warnings_are_sanitized_and_prevent_clean_result(self):
        body = payload([hit()])
        body["warnings"] = ["private-token"]
        result, _ = await self.search(body)
        self.assertTrue(result.notices)
        self.assertNotIn("private-token", " ".join(result.notices))
        self.assertNotIn("private-token", " ".join(result.diagnostics))
        self.assertIn("codes=unknown", result.diagnostics[0])

    async def test_warning_diagnostics_keep_codes_without_remote_messages(self):
        body = payload([hit()])
        body["warnings"] = [{"code": "no_search_features", "message": "private message",
                             "url": "https://private.test/image"}]
        result, _ = await self.search(body)
        self.assertTrue(result.notices)
        self.assertEqual(result.diagnostics, [
            "status=ok partial=False libraries=1 failed=0 warnings=1 codes=no_search_features malformed=False"])
        self.assertNotIn("private", str(result))

    async def test_malformed_segment_alongside_valid_segment_adds_notice(self):
        candidate = hit()
        candidate["path_segments"].append({})
        result, _ = await self.search(payload([candidate]))
        self.assertEqual(len(result.matches), 1)
        self.assertTrue(result.notices)
        self.assertIn("hit=1", result.diagnostics[1])
        self.assertIn("segments=2 valid=1", result.diagnostics[1])
        self.assertIn("segment=2:no_usable_url", result.diagnostics[1])

    async def test_parse_diagnostics_explain_skipped_fields_without_content(self):
        bad = hit()
        bad["path_segments"][0].update(page_url="file:///private", source_url=123)
        result, _ = await self.search(payload([hit(), bad, hit(score="90"), None]))
        self.assertEqual(len(result.matches), 1)
        text = " ".join(result.diagnostics)
        self.assertIn("page_url=unsupported_scheme,source_url=not_string,metadata_source_url=missing", text)
        self.assertIn("score_type=str", text)
        self.assertIn("score_not_number", text)
        self.assertIn("hit_not_object", text)
        self.assertNotIn("private", text)
        self.assertNotIn("https://", text)

    async def test_http_failure_does_not_echo_payload(self):
        with self.assertRaisesRegex(ProviderError, "^Soutubot returned HTTP 429$"):
            await self.search({"error": "private-token"}, status=429)

    async def test_transport_timeout_and_failure_are_sanitized(self):
        for error, message in ((TimeoutError("secret"), "request timed out"),
                               (ValueError("secret"), "request failed")):
            http = AsyncMock()
            http.upload_image_json.side_effect = error
            with self.assertRaisesRegex(ProviderError, message) as caught:
                await SoutubotProvider(http).search(
                    ImageAsset(url="https://image.test/a"), SearchMode.SOURCE,
                )
            self.assertNotIn("secret", str(caught.exception))

    async def test_invalid_source_urls_are_rejected(self):
        bad = hit()
        bad["path_segments"][0].update(source_url="javascript:bad", page_url="file:///secret")
        with self.assertRaises(ProviderError):
            await self.search(payload([bad]))


if __name__ == "__main__":
    unittest.main()
