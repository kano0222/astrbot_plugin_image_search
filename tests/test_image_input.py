from __future__ import annotations

import unittest
from dataclasses import dataclass, field

from types import SimpleNamespace

from image_input import find_public_image_url, find_raw_image_url, first_public_url


@dataclass
class FakeImage:
    file: str = ""
    url: str = ""


@dataclass
class FakeReply:
    chain: list[object] = field(default_factory=list)


class ImageInputTests(unittest.TestCase):
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
