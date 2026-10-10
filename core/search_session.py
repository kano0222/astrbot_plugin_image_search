"""Ephemeral, sender-scoped sessions for consecutive image searches."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass


@dataclass(slots=True)
class SearchSession:
    providers: tuple[str, ...] | None
    deadline: float
    timer: asyncio.Task | None = None


class SearchSessions:
    def __init__(self, timeout: float) -> None:
        self.timeout = timeout
        self._sessions: dict[tuple[str, str], SearchSession] = {}
        self._tasks: set[asyncio.Task] = set()

    def get(self, key: tuple[str, str]) -> SearchSession | None:
        session = self._sessions.get(key)
        if session is None or session.deadline <= time.monotonic():
            return None
        return session

    def start(self, key: tuple[str, str], providers: tuple[str, ...] | None,
              expired: Callable[[], Awaitable[None]]) -> None:
        self.stop(key)
        session = SearchSession(providers, time.monotonic() + self.timeout)
        self._sessions[key] = session

        async def expire() -> None:
            await asyncio.sleep(self.timeout)
            if self._sessions.get(key) is session:
                self._sessions.pop(key)
                await expired()

        session.timer = asyncio.create_task(expire())
        self._tasks.add(session.timer)
        session.timer.add_done_callback(self._tasks.discard)

    def stop(self, key: tuple[str, str]) -> bool:
        session = self._sessions.pop(key, None)
        if session is None:
            return False
        if session.timer is not None:
            session.timer.cancel()
        return session.deadline > time.monotonic()

    async def close(self) -> None:
        self._sessions.clear()
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
