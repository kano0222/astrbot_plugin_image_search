from __future__ import annotations

import unittest

from core.image_wait import (
    ImageResolution,
    ImageWaitCoordinator,
    WaitOutcome,
)


class ImageWaitCoordinatorTests(unittest.IsolatedAsyncioTestCase):
    async def test_delivers_only_to_matching_session_and_sender(self) -> None:
        coordinator = ImageWaitCoordinator(1)
        first_key = ("session-a", "user")
        second_key = ("session-b", "user")
        first = await coordinator.create(first_key)
        second = await coordinator.create(second_key)
        self.assertIsNotNone(first)
        self.assertIsNotNone(second)

        delivered = await coordinator.deliver(
            second_key,
            ImageResolution(url="https://example.com/image.jpg"),
        )

        self.assertTrue(delivered)
        self.assertFalse(first.future.done())
        result = await coordinator.wait(second_key, second)
        self.assertEqual(result.url, "https://example.com/image.jpg")
        await coordinator.close()

    async def test_rejects_duplicate_wait_for_same_key(self) -> None:
        coordinator = ImageWaitCoordinator(1)
        key = ("session", "user")

        first = await coordinator.create(key)
        duplicate = await coordinator.create(key)

        self.assertIsNotNone(first)
        self.assertIsNone(duplicate)
        await coordinator.close()

    async def test_timeout_removes_wait(self) -> None:
        coordinator = ImageWaitCoordinator(0.01)
        key = ("session", "user")
        pending = await coordinator.create(key)
        self.assertIsNotNone(pending)

        result = await coordinator.wait(key, pending)

        self.assertIs(result, WaitOutcome.TIMED_OUT)
        self.assertFalse(await coordinator.has_pending(key))

    async def test_close_cancels_all_waits(self) -> None:
        coordinator = ImageWaitCoordinator(1)
        key = ("session", "user")
        pending = await coordinator.create(key)
        self.assertIsNotNone(pending)

        await coordinator.close()

        self.assertIs(await pending.future, WaitOutcome.CANCELLED)


if __name__ == "__main__":
    unittest.main()
