"""Test EventBus (pub/sub trong tiến trình) dùng cho realtime SSE.

Theo cùng phong cách các test khác: bọc coroutine rồi `asyncio.run`.
"""

from __future__ import annotations

import asyncio

from app.services.event_bus import (
    EVENT_NVR_CHANGE,
    EventBus,
)


def test_publish_reaches_all_subscribers():
    """Mỗi subscriber nhận đúng event đã publish (broadcast)."""

    async def run():
        bus = EventBus()
        q1 = bus.subscribe()
        q2 = bus.subscribe()
        assert bus.subscriber_count == 2

        bus.publish(EVENT_NVR_CHANGE, {"nvr_id": 7})

        e1 = await asyncio.wait_for(q1.get(), timeout=1)
        e2 = await asyncio.wait_for(q2.get(), timeout=1)
        assert e1 == {"type": EVENT_NVR_CHANGE, "data": {"nvr_id": 7}}
        assert e2 == e1

    asyncio.run(run())


def test_unsubscribe_stops_delivery():
    """Sau khi unsubscribe, queue không còn nhận event mới."""

    async def run():
        bus = EventBus()
        q = bus.subscribe()
        bus.unsubscribe(q)
        assert bus.subscriber_count == 0

        bus.publish(EVENT_NVR_CHANGE)
        assert q.empty()

    asyncio.run(run())


def test_publish_defaults_empty_data():
    """publish không kèm data -> event.data là dict rỗng (không None)."""

    async def run():
        bus = EventBus()
        q = bus.subscribe()
        bus.publish(EVENT_NVR_CHANGE)
        event = await asyncio.wait_for(q.get(), timeout=1)
        assert event["data"] == {}

    asyncio.run(run())


def test_full_queue_skips_without_blocking():
    """Queue đầy (client chậm) -> bỏ qua event đó, KHÔNG raise/treo collector."""

    async def run():
        bus = EventBus(max_queue=1)
        q = bus.subscribe()
        bus.publish(EVENT_NVR_CHANGE, {"n": 1})  # lấp đầy queue (maxsize=1)
        bus.publish(EVENT_NVR_CHANGE, {"n": 2})  # phải bị bỏ qua, không lỗi

        first = await asyncio.wait_for(q.get(), timeout=1)
        assert first["data"] == {"n": 1}
        assert q.empty()  # event thứ 2 đã bị drop

    asyncio.run(run())


def test_camera_scan_outcome_changed_default():
    """CameraScanOutcome mặc định changed=False (tương thích ngược nơi gọi cũ)."""
    from app.services.status_service import CameraScanOutcome

    outcome = CameraScanOutcome(ok=True, offline_alertable=[], recovered=[])
    assert outcome.changed is False
