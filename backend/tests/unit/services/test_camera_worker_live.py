import asyncio

import pytest

from backend.app.services.camera_worker_live import LiveExternalSubscription, LiveProducerRegistry


@pytest.mark.asyncio
async def test_live_registry_starts_one_producer_and_drops_stale_subscriber_frame():
    registry = LiveProducerRegistry()
    starts = 0
    stopped = asyncio.Event()

    async def producer():
        nonlocal starts
        starts += 1
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    first, first_queue = await registry.subscribe("rtsp://camera", producer)
    second, second_queue = await registry.subscribe("rtsp://camera", producer)
    await asyncio.sleep(0)
    assert starts == 1
    registry.publish("rtsp://camera", b"old")
    registry.publish("rtsp://camera", b"new")
    assert await first_queue.get() == b"new"
    assert await second_queue.get() == b"new"
    await registry.unsubscribe(first)
    assert not stopped.is_set()
    await registry.unsubscribe(second)
    await asyncio.wait_for(stopped.wait(), timeout=1)


def test_live_subscription_rejects_invalid_or_credential_leaking_input():
    subscription = LiveExternalSubscription(
        identity="00000000-0000-4000-8000-000000000001",
        media_session_id="00000000-0000-4000-8000-000000000002",
        url="rtsp://operator:secret@camera.example/live",
        camera_type="rtsp",
        fps=5,
    )
    assert "operator:secret" not in repr(subscription)


@pytest.mark.asyncio
async def test_raw_lease_and_live_producer_are_mutually_exclusive():
    registry = LiveProducerRegistry()
    raw = await registry.acquire_raw("camera-a")
    with pytest.raises(RuntimeError, match="raw lease"):
        await registry.subscribe("camera-a", lambda: asyncio.sleep(0))
    await registry.release_raw(raw)
    live, _queue = await registry.subscribe("camera-a", lambda: asyncio.sleep(0.1))
    with pytest.raises(RuntimeError, match="busy"):
        await registry.acquire_raw("camera-a")
    await registry.unsubscribe(live)
