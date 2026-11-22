from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.state import EventBus, StateStore

NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


def test_state_store_tracks_values_and_age(fake_clock) -> None:
    store = StateStore(clock=fake_clock, now=lambda: NOW)

    assert store.get("cpu") is None
    assert store.age_ms("cpu") is None
    assert store.updated_at("cpu") is None

    store.update("cpu", {"power": 42.0})
    assert store.get("cpu") == {"power": 42.0}
    assert store.updated_at("cpu") == NOW
    assert store.age_ms("cpu") == 0

    fake_clock.advance(1.5)
    assert store.age_ms("cpu") == 1500
    assert store.section("cpu").present is True

    fake_clock.advance(2.0)
    store.update("cpu", {"power": 43.0})
    assert store.age_ms("cpu") == 0

    assert store.snapshot() == {"cpu": {"power": 43.0}}
    store.clear()
    assert store.snapshot() == {}


def test_state_store_missing_section_is_not_present() -> None:
    store = StateStore()
    section = store.section("nope")
    assert section.present is False
    assert section.value is None


async def test_event_bus_slow_subscriber_keeps_only_latest() -> None:
    bus = EventBus()
    subscription = bus.subscribe()
    assert bus.subscriber_count == 1

    bus.publish({"seq": 1})
    bus.publish({"seq": 2})
    bus.publish({"seq": 3})

    assert subscription.queue.qsize() == 1
    assert await subscription.queue.get() == {"seq": 3}

    bus.unsubscribe(subscription)
    assert bus.subscriber_count == 0
    bus.publish({"seq": 4})
    assert subscription.queue.empty()


async def test_event_bus_fans_out_to_every_subscriber() -> None:
    bus = EventBus()
    first = bus.subscribe()
    second = bus.subscribe(maxsize=4)

    bus.publish({"seq": 1})
    bus.publish({"seq": 2})

    assert await first.queue.get() == {"seq": 2}
    assert [await second.queue.get(), await second.queue.get()] == [{"seq": 1}, {"seq": 2}]

    bus.close()
    assert bus.subscriber_count == 0
    assert first.closed is True
    assert second.closed is True


def test_subscription_publish_after_close_is_ignored() -> None:
    bus = EventBus()
    subscription = bus.subscribe()
    subscription.close()
    bus.publish({"seq": 1})
    assert subscription.queue.empty()


@pytest.mark.parametrize("maxsize", [1, 8])
async def test_subscription_queue_never_exceeds_maxsize(maxsize: int) -> None:
    bus = EventBus()
    subscription = bus.subscribe(maxsize=maxsize)
    for index in range(50):
        bus.publish({"seq": index})
    assert subscription.queue.qsize() == maxsize
    last = None
    while not subscription.queue.empty():
        last = await subscription.queue.get()
    assert last == {"seq": 49}


async def test_event_bus_unsubscribe_is_idempotent() -> None:
    bus = EventBus()
    subscription = bus.subscribe()
    bus.unsubscribe(subscription)
    bus.unsubscribe(subscription)
    assert bus.subscriber_count == 0
