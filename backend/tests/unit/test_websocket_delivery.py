"""Synthetic farm: one stalled viewer must not stall other viewers or producers."""

import asyncio
import json

import pytest

from backend.app.core.websocket import ConnectionManager


class Browser:
    def __init__(self, *, blocked=False):
        self.sent = []
        self.received = asyncio.Event()
        self.release = asyncio.Event()
        if not blocked:
            self.release.set()
        self.closed = []

    async def accept(self):
        pass

    async def send_text(self, data):
        await self.release.wait()
        self.sent.append(json.loads(data))
        self.received.set()

    async def close(self, code=1000):
        self.closed.append(code)


@pytest.mark.asyncio
async def test_stalled_first_browser_does_not_block_a_healthy_viewer():
    manager = ConnectionManager()
    stalled, healthy = Browser(blocked=True), Browser()
    await manager.connect(stalled)
    await manager.connect(healthy)
    broadcast = asyncio.create_task(manager.send_printer_status(1, {"progress": 42}))
    try:
        await asyncio.wait_for(healthy.received.wait(), 0.2)
        assert healthy.sent[0]["data"]["progress"] == 42
        assert not stalled.sent
        assert broadcast.done(), "MQTT producer must not wait for a browser socket"
    finally:
        stalled.release.set()
        await broadcast
        await manager.disconnect(stalled)
        await manager.disconnect(healthy)


@pytest.mark.asyncio
async def test_fifty_printers_keep_snapshot_live_events_and_pong_in_order():
    manager = ConnectionManager()
    browser = Browser()
    await manager.connect(browser)
    try:
        for printer_id in range(50):
            await manager.send(browser, {"type": "printer_status", "printer_id": printer_id, "data": {"progress": 1}})
        await manager.send(browser, {"type": "initial_status_complete"})
        await manager.send_print_complete(3, {"status": "completed"})
        await manager.send_printer_status(3, {"progress": 100})
        browser.received.clear()
        await manager.send(browser, {"type": "pong"})
        await asyncio.wait_for(browser.received.wait(), 0.2)
        assert [m["printer_id"] for m in browser.sent[:50]] == list(range(50))
        assert [m["type"] for m in browser.sent[50:]] == [
            "initial_status_complete",
            "print_complete",
            "printer_status",
            "pong",
        ]
    finally:
        await manager.shutdown()
    assert not manager._outboxes


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", ["MAX_PENDING_MESSAGES", "MAX_PENDING_BYTES"])
async def test_overflow_evicts_only_the_slow_client_and_releases_owners(monkeypatch, limit):
    import backend.app.core.websocket as module

    monkeypatch.setattr(module, limit, 2 if limit.endswith("MESSAGES") else 190)
    manager = ConnectionManager()
    stalled, healthy = Browser(blocked=True), Browser()
    await manager.connect(stalled)
    await manager.connect(healthy)
    try:
        for printer_id in range(8):
            healthy.received.clear()
            await manager.send_printer_status(printer_id, {"progress": 42})
            await asyncio.wait_for(healthy.received.wait(), 0.2)
        assert stalled not in manager.active_connections
        assert len(healthy.sent) == 8
        stalled.release.set()
        await asyncio.wait_for(manager._outboxes[stalled].task, 0.2)
        assert stalled.closed == [1013]
        assert stalled not in manager._outboxes
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_hung_send_is_bounded_and_releases_client(monkeypatch):
    import backend.app.core.websocket as module

    monkeypatch.setattr(module, "SEND_TIMEOUT_SECONDS", 0.01)
    manager = ConnectionManager()
    browser = Browser(blocked=True)
    await manager.connect(browser)
    writer = manager._outboxes[browser].task
    await manager.send(browser, {"type": "pong"})
    await asyncio.wait_for(writer, 0.2)
    assert browser.closed == [1013]
    assert not manager.active_connections and not manager._outboxes


@pytest.mark.asyncio
async def test_disconnect_before_writer_starts_releases_pending_messages():
    manager = ConnectionManager()
    browser = Browser()
    await manager.connect(browser, user_id=7)
    await manager.send(browser, {"type": "printer_status", "printer_id": 1})
    outbox = manager._outboxes[browser]
    await manager.disconnect(browser)
    assert not manager.active_connections and not manager._user_by_conn and not manager._outboxes
    assert not outbox.messages and outbox.pending_bytes == 0
    assert outbox.task.done()


@pytest.mark.asyncio
async def test_shutdown_interrupts_a_stalled_close(monkeypatch):
    import backend.app.core.websocket as module

    monkeypatch.setattr(module, "SEND_TIMEOUT_SECONDS", 0.01)
    manager = ConnectionManager()
    browser = Browser(blocked=True)
    closing = asyncio.Event()

    async def stalled_close(code):
        closing.set()
        await asyncio.Event().wait()

    browser.close = stalled_close
    await manager.connect(browser)
    await manager.send(browser, {"type": "pong"})
    await asyncio.wait_for(closing.wait(), 0.2)
    await asyncio.wait_for(manager.shutdown(), 0.2)
    assert not manager._outboxes and not manager.active_connections


@pytest.mark.asyncio
async def test_shutdown_releases_a_snapshot_waiting_for_its_slow_viewer():
    manager = ConnectionManager()
    browser = Browser(blocked=True)
    await manager.connect(browser)

    async def snapshot():
        for printer_id in range(50):
            await manager.send(browser, {"type": "printer_status", "printer_id": printer_id})

    producer = asyncio.create_task(snapshot())
    await asyncio.sleep(0)
    assert not producer.done()
    await manager.shutdown()
    await asyncio.wait_for(producer, 0.2)
    assert not manager._outboxes and not browser.sent
