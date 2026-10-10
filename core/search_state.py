"""Plugin-owned result cache and atomic daily quota reservations."""
from __future__ import annotations

from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import sqlite3
import time

from .models import AnimeMatch, SearchResponse, SourceMatch


def cache_key(identity: str, mode: str, providers: tuple[str, ...], config: dict) -> str:
    value = json.dumps([identity, mode, sorted(providers), config],
                       ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(value.encode()).hexdigest()


class SearchState:
    def __init__(self, path: Path | None = None) -> None:
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path) if path else ":memory:")
        self._db.executescript("""
            CREATE TABLE IF NOT EXISTS quota (
                user TEXT NOT NULL, day TEXT NOT NULL, count INTEGER NOT NULL,
                PRIMARY KEY(user, day));
            CREATE TABLE IF NOT EXISTS cache (
                key TEXT PRIMARY KEY, created REAL NOT NULL, result TEXT NOT NULL);
        """)

    def reserve(self, user: str, day: str, limit: int) -> bool:
        with self._db:
            self._db.execute("DELETE FROM quota WHERE day < ?", (day,))
            self._db.execute("INSERT OR IGNORE INTO quota VALUES (?, ?, 0)", (user, day))
            cursor = self._db.execute(
                "UPDATE quota SET count=count+1 WHERE user=? AND day=? AND (?=0 OR count<?)",
                (user, day, limit, limit),
            )
            return cursor.rowcount == 1

    def refund(self, user: str, day: str) -> None:
        with self._db:
            self._db.execute("UPDATE quota SET count=max(0,count-1) WHERE user=? AND day=?",
                             (user, day))

    def count(self, user: str, day: str) -> int:
        row = self._db.execute("SELECT count FROM quota WHERE user=? AND day=?", (user, day)).fetchone()
        return row[0] if row else 0

    def get(self, key: str, ttl: float) -> tuple[SearchResponse, float] | None:
        with self._db:
            self._db.execute("DELETE FROM cache WHERE created<=?", (time.time() - ttl,))
        row = self._db.execute("SELECT result,created FROM cache WHERE key=?", (key,)).fetchone()
        if row is None:
            return None
        try:
            value = json.loads(row[0])
            value["matches"] = [
                (AnimeMatch if item.pop("kind") == "anime" else SourceMatch)(**item)
                for item in value["matches"]
            ]
            return SearchResponse(**value), row[1]
        except (TypeError, ValueError, KeyError):
            with self._db:
                self._db.execute("DELETE FROM cache WHERE key=?", (key,))
            return None

    def put(self, key: str, response: SearchResponse) -> None:
        value = asdict(response)
        for item, match in zip(value["matches"], response.matches, strict=True):
            item["kind"] = "anime" if isinstance(match, AnimeMatch) else "source"
        with self._db:
            self._db.execute("INSERT OR REPLACE INTO cache VALUES (?, ?, ?)",
                             (key, time.time(), json.dumps(value, ensure_ascii=False)))
            # Bound storage without introducing another user setting.
            self._db.execute("DELETE FROM cache WHERE key NOT IN "
                             "(SELECT key FROM cache ORDER BY created DESC LIMIT 1000)")

    def close(self) -> None:
        self._db.close()
