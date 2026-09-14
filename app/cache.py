"""On-disk cache for coarse price scans.

Only the *grid* is cached, never a refined itinerary. The distinction matters:
a grid scan answers "which dates are worth looking at", where a few hours of
staleness changes nothing, while a refined itinerary is the fare you would
actually book and must be live.

SQLite because this is a personal tool: one file, no server, survives restarts,
and it is the natural place to grow price history later.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
import threading
import time
from dataclasses import asdict
from datetime import date, datetime
from pathlib import Path
from typing import Any

from app.providers.base import GridPrice, GridQuery, ItineraryQuery

logger = logging.getLogger(__name__)

DEFAULT_TTL_SECONDS = 6 * 60 * 60
DEFAULT_PATH = Path(os.environ.get("FLIGHT_CACHE_PATH", ".flight-cache.sqlite3"))


def _json_default(value: Any) -> str:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    raise TypeError(f"cannot serialize {type(value).__name__}")


def grid_key(query: GridQuery) -> str:
    """A stable key for one grid scan.

    Every field that changes the prices is part of the key; anything left out
    would silently serve one search's results to a different search.
    """
    payload = json.dumps(asdict(query), sort_keys=True, default=_json_default)
    return hashlib.sha256(payload.encode()).hexdigest()


class PriceCache:
    """SQLite-backed store of grid scans, keyed by the full query."""

    def __init__(self, path: Path | str = DEFAULT_PATH, ttl_seconds: int = DEFAULT_TTL_SECONDS):
        self.path = Path(path)
        self.ttl_seconds = ttl_seconds
        # One connection guarded by a lock: searches run on a small thread
        # pool, and SQLite connections are not safe to share across threads.
        self._lock = threading.Lock()
        self._connection = sqlite3.connect(self.path, check_same_thread=False)
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS grid_scans (
                key        TEXT PRIMARY KEY,
                fetched_at REAL NOT NULL,
                route      TEXT NOT NULL,
                payload    TEXT NOT NULL
            )
            """
        )
        self._connection.execute(
            "CREATE INDEX IF NOT EXISTS grid_scans_fetched_at ON grid_scans (fetched_at)"
        )
        self._connection.commit()

    def get(self, query: GridQuery) -> list[GridPrice] | None:
        """Return a cached scan, or None if absent or too old."""
        cutoff = time.time() - self.ttl_seconds
        with self._lock:
            row = self._connection.execute(
                "SELECT payload FROM grid_scans WHERE key = ? AND fetched_at >= ?",
                (grid_key(query), cutoff),
            ).fetchone()
        if row is None:
            return None
        try:
            return [
                GridPrice(
                    departure_date=date.fromisoformat(item["departure_date"]),
                    return_date=(
                        date.fromisoformat(item["return_date"]) if item["return_date"] else None
                    ),
                    price=item["price"],
                    currency=item["currency"],
                )
                for item in json.loads(row[0])
            ]
        except (ValueError, KeyError, TypeError):
            # A row written by an older shape is not worth a crash; re-fetch.
            logger.warning("discarding an unreadable cache row")
            return None

    def put(self, query: GridQuery, prices: list[GridPrice]) -> None:
        """Store a scan, replacing any previous entry for the same query."""
        payload = json.dumps([asdict(price) for price in prices], default=_json_default)
        route = f"{'+'.join(query.origins)}-{'+'.join(query.destinations)}"
        with self._lock:
            self._connection.execute(
                "INSERT OR REPLACE INTO grid_scans (key, fetched_at, route, payload) "
                "VALUES (?, ?, ?, ?)",
                (grid_key(query), time.time(), route, payload),
            )
            self._connection.commit()

    def purge_expired(self) -> int:
        """Drop rows past their TTL. Returns how many were removed."""
        with self._lock:
            cursor = self._connection.execute(
                "DELETE FROM grid_scans WHERE fetched_at < ?", (time.time() - self.ttl_seconds,)
            )
            self._connection.commit()
            return cursor.rowcount

    def stats(self) -> dict[str, Any]:
        """What is currently stored, for the CLI and the status endpoint."""
        with self._lock:
            total, oldest = self._connection.execute(
                "SELECT COUNT(*), MIN(fetched_at) FROM grid_scans"
            ).fetchone()
        fresh = 0
        if total:
            cutoff = time.time() - self.ttl_seconds
            with self._lock:
                (fresh,) = self._connection.execute(
                    "SELECT COUNT(*) FROM grid_scans WHERE fetched_at >= ?", (cutoff,)
                ).fetchone()
        return {
            "path": str(self.path),
            "ttl_seconds": self.ttl_seconds,
            "rows": total or 0,
            "fresh_rows": fresh,
            "oldest_age_seconds": round(time.time() - oldest) if oldest else None,
        }

    def close(self) -> None:
        """Close the underlying connection."""
        with self._lock:
            self._connection.close()


class CachedProvider:
    """Wraps a provider so repeated grid scans are served from disk.

    Itinerary lookups always pass through: the grid narrows the field, but the
    fare you are shown for a specific flight should never be a memory.
    """

    def __init__(self, inner, cache: PriceCache):
        self.inner = inner
        self.cache = cache
        self.hits = 0
        self.misses = 0

    @property
    def name(self) -> str:
        """The wrapped provider's name."""
        return f"{self.inner.name}+cache"

    @property
    def supports_date_grid(self) -> bool:
        """Caching cannot add a capability the provider lacks."""
        return self.inner.supports_date_grid

    def scan_date_grid(self, query: GridQuery) -> list[GridPrice]:
        """Serve from cache when fresh, otherwise fetch and store."""
        cached = self.cache.get(query)
        if cached is not None:
            self.hits += 1
            return cached
        self.misses += 1
        prices = self.inner.scan_date_grid(query)
        # Empty results are cached too — "this airline does not fly here" is a
        # real answer, and re-asking it every search wastes the same request.
        self.cache.put(query, prices)
        return prices

    def search_itineraries(self, query: ItineraryQuery) -> list:
        """Always live — this is the bookable fare."""
        return self.inner.search_itineraries(query)
