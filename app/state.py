"""In-memory latest state and the realtime event fan-out.

``StateStore`` holds the most recent value of every section with its freshness.
``EventBus`` broadcasts *complete* snapshots; each subscriber owns a bounded
queue of size one, so a slow WebSocket client simply misses intermediate
snapshots instead of growing memory or delaying the collector.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Callable


@dataclass(slots=True)
class Section:
    value: Any = None
    updated_at: datetime | None = None
    updated_mono: float | None = None

    @property
    def present(self) -> bool:
        return self.updated_at is not None


class StateStore:
    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._clock = clock
        self._now = now or (lambda: datetime.now(UTC))
        self._sections: dict[str, Section] = {}
        self._lock = asyncio.Lock()

    def update(self, name: str, value: Any) -> None:
        """Store a new section value.

        No ``await`` happens inside, so under asyncio this is atomic with
        respect to readers; the lock only protects future threaded writers.
        """
        self._sections[name] = Section(
            value=value,
            updated_at=self._now(),
            updated_mono=self._clock(),
        )

    def get(self, name: str, default: Any = None) -> Any:
        section = self._sections.get(name)
        return default if section is None else section.value

    def section(self, name: str) -> Section:
        return self._sections.get(name, Section())

    def age_ms(self, name: str) -> int | None:
        section = self._sections.get(name)
        if section is None or section.updated_mono is None:
            return None
        return max(0, int((self._clock() - section.updated_mono) * 1000))

    def updated_at(self, name: str) -> datetime | None:
        section = self._sections.get(name)
        return None if section is None else section.updated_at

    def snapshot(self) -> dict[str, Any]:
        return {name: section.value for name, section in self._sections.items()}

    def clear(self) -> None:
        self._sections.clear()


@dataclass(eq=False, slots=True)
class Subscription:
    queue: asyncio.Queue[dict[str, Any]]
    closed: bool = False

    def publish(self, item: dict[str, Any]) -> None:
        if self.closed:
            return
        while True:
            try:
                self.queue.put_nowait(item)
                return
            except asyncio.QueueFull:
                try:
                    self.queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass

    def close(self) -> None:
        self.closed = True


class EventBus:
    def __init__(self) -> None:
        self._subscribers: set[Subscription] = set()

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    def subscribe(self, *, maxsize: int = 1) -> Subscription:
        subscription = Subscription(queue=asyncio.Queue(maxsize=maxsize))
        self._subscribers.add(subscription)
        return subscription

    def unsubscribe(self, subscription: Subscription) -> None:
        subscription.close()
        self._subscribers.discard(subscription)

    def publish(self, item: dict[str, Any]) -> None:
        for subscription in tuple(self._subscribers):
            subscription.publish(item)

    def close(self) -> None:
        for subscription in tuple(self._subscribers):
            subscription.close()
        self._subscribers.clear()
