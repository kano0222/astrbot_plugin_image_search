"""Concurrency-safe handoff for commands waiting on a later image."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum


@dataclass(frozen=True, slots=True)
class ImageResolution:
    """Resolved public image URL or a user-facing resolution error."""

    url: str | None = None
    error: str | None = None


class WaitOutcome(Enum):
    """Terminal states that do not contain an image."""

    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"


WaitResult = ImageResolution | WaitOutcome


@dataclass(slots=True)
class PendingImage:
    """One active wait owned by a conversation and sender."""

    deadline: float
    future: asyncio.Future[WaitResult]


class ImageWaitCoordinator:
    """Route later images to the matching command without cross-session leakage."""

    def __init__(
        self,
        timeout_seconds: float,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be greater than zero")
        self.timeout_seconds = timeout_seconds
        self._clock = clock
        self._pending: dict[tuple[str, str], PendingImage] = {}
        self._lock = asyncio.Lock()

    async def create(self, key: tuple[str, str]) -> PendingImage | None:
        """Create a wait unless the same sender already has one in this session."""
        async with self._lock:
            self._expire_locked()
            if key in self._pending:
                return None
            pending = PendingImage(
                deadline=self._clock() + self.timeout_seconds,
                future=asyncio.get_running_loop().create_future(),
            )
            self._pending[key] = pending
            return pending

    async def has_pending(self, key: tuple[str, str]) -> bool:
        async with self._lock:
            self._expire_locked()
            return key in self._pending

    async def deliver(
        self,
        key: tuple[str, str],
        resolution: ImageResolution,
    ) -> bool:
        """Resolve and remove a matching wait atomically."""
        async with self._lock:
            self._expire_locked()
            pending = self._pending.pop(key, None)
            if pending is None:
                return False
            if not pending.future.done():
                pending.future.set_result(resolution)
            return True

    async def wait(
        self,
        key: tuple[str, str],
        pending: PendingImage,
    ) -> WaitResult:
        remaining = max(0.0, pending.deadline - self._clock())
        try:
            return await asyncio.wait_for(
                asyncio.shield(pending.future),
                timeout=remaining,
            )
        except TimeoutError:
            async with self._lock:
                if self._pending.get(key) is pending:
                    self._pending.pop(key, None)
                    if not pending.future.done():
                        pending.future.set_result(WaitOutcome.TIMED_OUT)
            return await asyncio.shield(pending.future)
        except asyncio.CancelledError:
            await self._remove(key, pending, WaitOutcome.CANCELLED)
            raise

    async def close(self) -> None:
        async with self._lock:
            pending_items = list(self._pending.values())
            self._pending.clear()
            for pending in pending_items:
                if not pending.future.done():
                    pending.future.set_result(WaitOutcome.CANCELLED)

    async def _remove(
        self,
        key: tuple[str, str],
        expected: PendingImage,
        outcome: WaitOutcome,
    ) -> None:
        async with self._lock:
            if self._pending.get(key) is expected:
                self._pending.pop(key, None)
                if not expected.future.done():
                    expected.future.set_result(outcome)

    def _expire_locked(self) -> None:
        now = self._clock()
        expired = [
            (key, pending)
            for key, pending in self._pending.items()
            if pending.deadline <= now
        ]
        for key, pending in expired:
            self._pending.pop(key, None)
            if not pending.future.done():
                pending.future.set_result(WaitOutcome.TIMED_OUT)
