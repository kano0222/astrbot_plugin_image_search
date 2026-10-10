from __future__ import annotations

import unittest
from dataclasses import dataclass, field

from types import SimpleNamespace

from image_input import find_public_image_url, find_raw_image_url, first_public_url, image_candidates_from_message, image_cache_identity, image_identities_from_message


@dataclass
class FakeImage:
    file: str = ""
    url: str = ""


@dataclass
class FakeReply:
    chain: list[object] = field(default_factory=list)


class ImageInputTests(unittest.TestCase):
    def test_qq_identity_ignores_changing_url_and_normalizes_md5(self):
        digest = "ABCDEF0123456789ABCDEF0123456789"
        first = image_cache_identity({"file": digest + ".image", "url": "https://img.test/a?token=1"})
        second = image_cache_identity({"md5": digest.lower(), "url": "https://img.test/b?token=2"})
        self.assertEqual(first, second)
        self.assertEqual(first, "qq-md5:" + digest.lower())
        self.assertEqual(image_cache_identity({"file_unique": "stable-id", "file": digest}), "qq-unique:stable-id")

    def test_known_qq_paths_only_and_unknown_names_fall_back(self):
        digest = "a" * 32
        self.assertEqual(image_cache_identity({"url": f"https://gchat.qpic.cn/gchatpic_new/123/456-789-{digest}/0?rkey=one"}),
                         image_cache_identity({"url": f"https://c2cpicdw.qpic.cn/offpic_new/999/111-222-{digest}/0?rkey=two"}))
        for data in ({"file": "image.jpg"}, {"file": "/tmp/" + digest + ".jpg"},
                     {"md5": "wrong"}, {"url": f"https://other.test/gchatpic_new/123/456-789-{digest}/0"},
                     {"url": "https://multimedia.nt.qq.com.cn/download?fileid=a_b&rkey=x"}):
            self.assertIsNone(image_cache_identity(data), data)

    def test_identity_extraction_preserves_invalid_slots(self):
        self.assertEqual(image_identities_from_message({"data": {"message": [
            {"type": "image", "data": None}, {"type": "text", "data": {}},
            {"type": "image", "data": {"file_unique": "id-two"}},
        ]}}), [None, "qq-unique:id-two"])
    def test_groups_alternatives_by_original_segment(self):
        segments = [
            {"type": "image", "data": {"url": "https://img.test/a", "file": "/tmp/a.jpg"}},
            {"type": "text", "data": {"text": "search"}},
            {"type": "image", "data": {"url": "https://img.test/b", "file": "id-b"}},
        ]
        expected = [("https://img.test/a", "/tmp/a.jpg"), ("https://img.test/b", "id-b")]
        self.assertEqual(image_candidates_from_message({"message": segments}), expected)
        self.assertEqual(image_candidates_from_message({"data": {"message": segments}}), expected)

    def test_invalid_image_preserves_its_slot(self):
        self.assertEqual(image_candidates_from_message({"message": [
            {"type": "image", "data": None},
            {"type": "image", "data": {"url": "https://img.test/a"}},
        ]}), [(), ("https://img.test/a",)])
        self.assertIsNone(image_candidates_from_message({"message": "unparsed CQ string"}))

    def test_recovers_raw_image_url_from_mapping_and_object(self) -> None:
        segments = [{"type": "image", "data": {"url": "https://example.com/raw.jpg"}}]
        for raw in ({"message": segments}, SimpleNamespace(message=segments)):
            event = SimpleNamespace(message_obj=SimpleNamespace(raw_message=raw))
            self.assertEqual(find_raw_image_url(event), "https://example.com/raw.jpg")

    def test_raw_extraction_does_not_skip_first_image(self) -> None:
        event = SimpleNamespace(
            message_obj=SimpleNamespace(
                raw_message={
                    "message": [
                        {"type": "image", "data": {"file": "base64://data"}},
                        {
                            "type": "image",
                            "data": {"url": "https://example.com/later.jpg"},
                        },
                    ]
                }
            )
        )
        self.assertIsNone(find_raw_image_url(event))

    def test_missing_raw_message_is_supported(self) -> None:
        self.assertIsNone(find_raw_image_url(SimpleNamespace()))

    def test_prefers_direct_image_over_quoted_image(self) -> None:
        components = [
            FakeReply([FakeImage(url="https://example.com/quoted.jpg")]),
            FakeImage(file="https://example.com/direct.jpg"),
        ]

        result = find_public_image_url(
            components,
            image_type=FakeImage,
            reply_type=FakeReply,
        )

        self.assertEqual(result, "https://example.com/direct.jpg")

    def test_rejects_local_and_base64_sources(self) -> None:
        components = [
            FakeImage(file="base64://data"),
            FakeImage(file="file:///tmp/image.jpg"),
        ]

        result = find_public_image_url(
            components,
            image_type=FakeImage,
            reply_type=FakeReply,
        )

        self.assertIsNone(result)

    def test_selects_public_url_from_resolved_reply_images(self) -> None:
        result = first_public_url(
            ["file:///tmp/image.jpg", "https://example.com/resolved.jpg"]
        )

        self.assertEqual(result, "https://example.com/resolved.jpg")


if __name__ == "__main__":
    unittest.main()
