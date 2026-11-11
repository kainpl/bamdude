"""Freshness/ownership and buffered-payload boundaries of the interim OQ2 guard."""

import asyncio
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from backend.app.core.auth import CameraKeyIdentity
from backend.app.services import camera_key_access as access
from backend.app.services.camera_fanout import iter_subscriber
from backend.app.services.connector_runtime import connector_runtime

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def clean():
    connector_runtime._reset_for_tests()
    yield
    connector_runtime._reset_for_tests()


def guard(namespace=None):
    return access.CameraKeyStreamGuard(
        CameraKeyIdentity(1, str(uuid.uuid4()), "hash", 1),
        SimpleNamespace(id=1, connector_identity=str(uuid.uuid4())),
        namespace if namespace is not None else (str(uuid.uuid4()), str(uuid.uuid4())),
    )


@pytest.mark.parametrize("namespace", [(None, None), ("", ""), ("bad", "bad"), (str(uuid.uuid4()), None)])
async def test_missing_or_malformed_namespace_never_opens_database(monkeypatch, namespace):
    session = Mock(side_effect=AssertionError("must not open DB"))
    monkeypatch.setattr(access.database, "async_session", session)
    assert not await guard(namespace)._allowed()
    session.assert_not_called()


@pytest.mark.parametrize("printer_identity", [None, "", "malformed"])
async def test_missing_printer_incarnation_cannot_match_a_deleted_printer(monkeypatch, printer_identity):
    value = guard()
    value.printer_identity = printer_identity
    session = Mock(side_effect=AssertionError("must not treat a missing row as matching a NULL incarnation"))
    monkeypatch.setattr(access.database, "async_session", session)
    assert not await value._allowed()
    session.assert_not_called()


async def test_recheck_is_time_bounded_not_per_frame_and_cannot_reopen(monkeypatch):
    clock = [10.0]
    monkeypatch.setattr(access.time, "monotonic", lambda: clock[0])
    value = guard()
    query = AsyncMock(return_value=True)
    monkeypatch.setattr(value, "_allowed", query)
    for _ in range(100):
        assert not await value.closed()
    assert query.await_count == 1
    clock[0] += access.RECHECK_SECONDS
    assert not await value.closed()
    assert query.await_count == 2
    clock[0] += access.RECHECK_SECONDS
    query.return_value = False
    assert await value.closed()
    query.return_value = True
    clock[0] += access.RECHECK_SECONDS
    assert await value.closed() and query.await_count == 3


async def test_delayed_check_never_gets_a_new_full_freshness_window(monkeypatch):
    clock = [10.0]
    monkeypatch.setattr(access.time, "monotonic", lambda: clock[0])
    value = guard()

    async def delayed():
        clock[0] += access.RECHECK_SECONDS + 0.01
        return True

    monkeypatch.setattr(value, "_allowed", delayed)
    assert await value.closed()


async def test_buffered_frame_checked_before_yield_even_when_subscriber_resumes(monkeypatch):
    clock = [10.0]
    monkeypatch.setattr(access.time, "monotonic", lambda: clock[0])
    value = guard()
    allowed = AsyncMock(return_value=True)
    monkeypatch.setattr(value, "_allowed", allowed)
    queue = asyncio.Queue()
    queue.put_nowait(b"first")
    queue.put_nowait(b"private queued frame")
    broadcaster = SimpleNamespace(unsubscribe=AsyncMock(return_value=1))
    viewer = iter_subscriber(broadcaster, queue, before_yield=value.closed)
    assert await anext(viewer) == b"first"
    clock[0] += access.RECHECK_SECONDS
    allowed.return_value = False
    with pytest.raises(StopAsyncIteration):
        await anext(viewer)
    broadcaster.unsubscribe.assert_awaited_once_with(queue)


@pytest.mark.parametrize("cancel", [False, True])
async def test_pending_check_remains_in_restore_drain_after_timeout_or_cancel(monkeypatch, cancel):
    value = guard()
    entered, release = asyncio.Event(), asyncio.Event()
    monkeypatch.setattr(access, "CHECK_TIMEOUT_SECONDS", 0.05)

    async def blocked():
        entered.set()
        await release.wait()
        return True

    monkeypatch.setattr(value, "_allowed", blocked)
    check = asyncio.create_task(value.closed())
    await entered.wait()
    if cancel:
        check.cancel()
        with pytest.raises(asyncio.CancelledError):
            await check
    else:
        assert await check
    assert len(connector_runtime._requests) == 1
    assert not await connector_runtime._drain(0)
    release.set()
    assert await connector_runtime._drain(1)
    assert await value.closed()


async def test_default_freshness_and_deadline_fit_stalled_fanout_poll():
    from backend.app.services.camera_fanout import _DISCONNECT_POLL_SECONDS

    assert access.RECHECK_SECONDS + access.CHECK_TIMEOUT_SECONDS + _DISCONNECT_POLL_SECONDS <= 5
