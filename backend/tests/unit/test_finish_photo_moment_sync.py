"""Regression tests for the #1790 producer→consumer synchronization.

``on_finish_photo_moment`` (producer) and ``_background_finish_photo``
(consumer, inside ``on_print_complete``) are dispatched back-to-back on the
FINISH-state fallback path. Before #1790 the consumer ran a single ``pop()``
on ``_stage22_finish_frames`` with no wait — racing past the producer with an
empty result, then doing its own RTSP grab that collided with the producer's
still-in-flight grab (Bambu printers allow one RTSP client). Net result: a
captured frame was logged, the cache was populated ~1s later, but the
notification went text-only.

The fix is an ``asyncio.Event`` per printer registered in
``_stage22_finish_in_flight`` by the producer (before its first await) and
set in a ``finally`` on every exit; the consumer awaits it (with a 20s
timeout) before the cache pop. These tests pin the producer side of that
contract: the event is registered before the first await, and set() runs on
every exit path (captured / no-frame / disabled / exception), while the
timelapse branch never registers one.
"""

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from backend.app import main as main_module
from backend.app.main import on_finish_photo_moment


@asynccontextmanager
async def _fake_session(printer):
    """async_session() stub whose execute().scalar_one_or_none() returns `printer`."""
    result = SimpleNamespace(scalar_one_or_none=lambda: printer)
    session = SimpleNamespace(execute=AsyncMock(return_value=result))
    yield session


@pytest.fixture
def fake_printer():
    return SimpleNamespace(
        id=7,
        ip_address="192.0.2.7",
        access_code="x",
        model="X1C",
        external_camera_enabled=False,
        external_camera_url=None,
        external_camera_type=None,
        external_camera_snapshot_url=None,
    )


@pytest.fixture(autouse=True)
def _clean_state():
    """Don't leak event / cache dict entries across tests."""
    main_module._stage22_finish_in_flight.clear()
    main_module._stage22_finish_frames.clear()
    main_module._inprint_frame_bank.clear()
    main_module._inprint_frame_bank_ts.clear()
    main_module._inprint_bank_in_flight.clear()
    yield
    main_module._stage22_finish_in_flight.clear()
    main_module._stage22_finish_frames.clear()
    main_module._inprint_frame_bank.clear()
    main_module._inprint_frame_bank_ts.clear()
    main_module._inprint_bank_in_flight.clear()


@pytest.fixture
def patched_env(fake_printer, monkeypatch):
    monkeypatch.setattr(main_module, "async_session", lambda: _fake_session(fake_printer))

    async def _get_setting(_db, key):
        if key == "capture_finish_photo":
            return "true"
        return None

    monkeypatch.setattr("backend.app.api.routes.settings.get_setting", _get_setting)
    monkeypatch.setattr("backend.app.api.routes.camera.get_buffered_frame", lambda _pid: None)
    return fake_printer


async def test_event_registered_before_first_await(patched_env, monkeypatch):
    """The consumer needs to find the event the moment it polls — registration
    must complete BEFORE any await yields control back to the loop."""
    seen_during_capture = {}

    async def _slow_capture(**_kwargs):
        seen_during_capture["registered"] = patched_env.id in main_module._stage22_finish_in_flight
        await asyncio.sleep(0)
        return b"\xff\xd8frame"

    monkeypatch.setattr("backend.app.services.camera.capture_camera_frame_bytes", _slow_capture)

    await on_finish_photo_moment(patched_env.id, {"trigger": "finish_state"})

    assert seen_during_capture["registered"] is True


async def test_event_set_after_successful_capture(patched_env, monkeypatch):
    async def _capture(**_kwargs):
        return b"\xff\xd8frame"

    monkeypatch.setattr("backend.app.services.camera.capture_camera_frame_bytes", _capture)

    await on_finish_photo_moment(patched_env.id, {"trigger": "finish_state"})

    event = main_module._stage22_finish_in_flight[patched_env.id]
    assert event.is_set()
    assert main_module._stage22_finish_frames[patched_env.id] == b"\xff\xd8frame"


async def test_event_set_when_capture_returns_no_frame(patched_env, monkeypatch):
    """Producer gives up (RTSP timeout, no buffered frame) — consumer must NOT
    wait the full 20s for nothing."""

    async def _capture(**_kwargs):
        return None

    monkeypatch.setattr("backend.app.services.camera.capture_camera_frame_bytes", _capture)

    await on_finish_photo_moment(patched_env.id, {"trigger": "finish_state"})

    event = main_module._stage22_finish_in_flight[patched_env.id]
    assert event.is_set()
    assert patched_env.id not in main_module._stage22_finish_frames


async def test_event_set_even_when_capture_raises(patched_env, monkeypatch):
    """Producer hit a bug or network error — `finally` still releases the consumer."""

    async def _capture(**_kwargs):
        raise RuntimeError("camera went away")

    monkeypatch.setattr("backend.app.services.camera.capture_camera_frame_bytes", _capture)

    await on_finish_photo_moment(patched_env.id, {"trigger": "finish_state"})

    event = main_module._stage22_finish_in_flight[patched_env.id]
    assert event.is_set()


async def test_no_event_when_timelapse_was_active(patched_env):
    """On the timelapse-on path the producer early-returns before registering,
    so the consumer's `is not None` guard skips the wait — no hang."""
    await on_finish_photo_moment(
        patched_env.id,
        {"trigger": "stage_22", "timelapse_was_active": True},
    )

    assert patched_env.id not in main_module._stage22_finish_in_flight


async def test_event_set_when_capture_setting_disabled(patched_env, monkeypatch):
    """Even on the early-return-before-capture path (setting disabled), the
    event must be released so the consumer doesn't hang on a no-op producer."""

    async def _disabled_setting(_db, _key):
        return "false"

    monkeypatch.setattr("backend.app.api.routes.settings.get_setting", _disabled_setting)

    await on_finish_photo_moment(patched_env.id, {"trigger": "finish_state"})

    event = main_module._stage22_finish_in_flight[patched_env.id]
    assert event.is_set()


async def test_consumer_wait_unblocked_when_producer_completes(patched_env, monkeypatch):
    """End-to-end sync check: a consumer-style waiter awaiting the event
    finishes promptly once the producer's finally fires."""

    async def _capture(**_kwargs):
        await asyncio.sleep(0.05)
        return b"\xff\xd8frame"

    monkeypatch.setattr("backend.app.services.camera.capture_camera_frame_bytes", _capture)

    producer = asyncio.create_task(on_finish_photo_moment(patched_env.id, {"trigger": "finish_state"}))

    await asyncio.sleep(0)  # let the producer register

    event = main_module._stage22_finish_in_flight[patched_env.id]
    await asyncio.wait_for(event.wait(), timeout=1.0)

    assert main_module._stage22_finish_frames[patched_env.id] == b"\xff\xd8frame"
    await producer


# --- #1867 follow-on: in-print frame bank -----------------------------------
#
# The FINISH-state fallback fires only AFTER the printer's End G-code has run
# (park / plate swap / clear), so a live grab there photographs the aftermath.
# A rolling in-print frame, banked on layer changes, is the pre-swap image it
# falls back on. The other two triggers fire before the swap and stay live.


async def test_finish_state_prefers_banked_frame(patched_env, monkeypatch):
    main_module._inprint_frame_bank[patched_env.id] = b"\xff\xd8banked"
    live_called = {"n": 0}

    async def _live(**_kwargs):
        live_called["n"] += 1
        return b"\xff\xd8live-post-swap"

    monkeypatch.setattr("backend.app.services.camera.capture_camera_frame_bytes", _live)
    await on_finish_photo_moment(patched_env.id, {"trigger": "finish_state"})
    assert main_module._stage22_finish_frames[patched_env.id] == b"\xff\xd8banked"
    assert live_called["n"] == 0


async def test_finish_state_falls_back_to_live_when_no_bank(patched_env, monkeypatch):
    async def _live(**_kwargs):
        return b"\xff\xd8live"

    monkeypatch.setattr("backend.app.services.camera.capture_camera_frame_bytes", _live)
    await on_finish_photo_moment(patched_env.id, {"trigger": "finish_state"})
    assert main_module._stage22_finish_frames[patched_env.id] == b"\xff\xd8live"


async def test_last_layer_trigger_ignores_bank(patched_env, monkeypatch):
    """The last-layer edge fires before the End G-code, so a live grab there is
    better framed than anything banked mid-print."""
    main_module._inprint_frame_bank[patched_env.id] = b"\xff\xd8banked"

    async def _live(**_kwargs):
        return b"\xff\xd8live"

    monkeypatch.setattr("backend.app.services.camera.capture_camera_frame_bytes", _live)
    await on_finish_photo_moment(patched_env.id, {"trigger": "last_layer"})
    assert main_module._stage22_finish_frames[patched_env.id] == b"\xff\xd8live"


# --- the banking helper itself ----------------------------------------------


def _bank_env(
    monkeypatch,
    *,
    state="RUNNING",
    sub_stage=0,
    total_layers=10,
    finish_photo_captured=False,
    printer=None,
):
    """Wire printer_manager.get_client + the snapshot capture for the bank
    helper. Capture returns a distinct frame per call so updates are visible."""
    if printer is None:
        printer = SimpleNamespace(external_camera_enabled=False, external_camera_url=None)
    client = SimpleNamespace(
        state=SimpleNamespace(state=state, mc_print_sub_stage=sub_stage, total_layers=total_layers),
        _finish_photo_captured=finish_photo_captured,
    )
    monkeypatch.setattr(main_module.printer_manager, "get_client", lambda _pid: client)
    monkeypatch.setattr(main_module, "async_session", lambda: _fake_session(printer))
    monkeypatch.setattr("backend.app.api.routes.camera.is_stream_active", lambda _pid: False)

    counter = {"n": 0}

    async def _capture(_pid, _printer, _logger):
        counter["n"] += 1
        return f"frame-{counter['n']}".encode()

    monkeypatch.setattr(main_module, "_capture_snapshot_for_notification", _capture)
    return counter


async def test_bank_stores_frame_while_printing(monkeypatch):
    _bank_env(monkeypatch)
    await main_module._maybe_bank_inprint_frame(3, 5)
    assert main_module._inprint_frame_bank[3] == b"frame-1"


async def test_bank_throttles_within_interval(monkeypatch):
    counter = _bank_env(monkeypatch)
    await main_module._maybe_bank_inprint_frame(3, 5)
    await main_module._maybe_bank_inprint_frame(3, 6)
    assert counter["n"] == 1
    assert main_module._inprint_frame_bank[3] == b"frame-1"


async def test_bank_always_refreshes_on_last_layer(monkeypatch):
    counter = _bank_env(monkeypatch, total_layers=10)
    await main_module._maybe_bank_inprint_frame(3, 5)
    await main_module._maybe_bank_inprint_frame(3, 10)
    assert counter["n"] == 2
    assert main_module._inprint_frame_bank[3] == b"frame-2"


async def test_bank_skips_when_not_running(monkeypatch):
    _bank_env(monkeypatch, state="FINISH")
    await main_module._maybe_bank_inprint_frame(3, 10)
    assert 3 not in main_module._inprint_frame_bank


async def test_bank_skips_during_calibration_substage(monkeypatch):
    _bank_env(monkeypatch, sub_stage=14)
    await main_module._maybe_bank_inprint_frame(3, 2)
    assert 3 not in main_module._inprint_frame_bank


async def test_bank_skips_once_a_finish_photo_was_taken(monkeypatch):
    """BamDude divergence: the same layer_num packet that triggers the final
    banking attempt also fires the last-layer finish photo, as an independent
    task. The camera service has no capture lock, so banking on top of it would
    open a competing socket and degrade the path that already works. Once the
    finish photo is captured, nothing will read the bank anyway."""
    counter = _bank_env(monkeypatch, finish_photo_captured=True)
    await main_module._maybe_bank_inprint_frame(3, 10)
    assert counter["n"] == 0
    assert 3 not in main_module._inprint_frame_bank


async def test_bank_skips_while_a_finish_grab_is_in_flight(monkeypatch):
    counter = _bank_env(monkeypatch)
    main_module._stage22_finish_in_flight[3] = asyncio.Event()
    await main_module._maybe_bank_inprint_frame(3, 10)
    assert counter["n"] == 0
    assert 3 not in main_module._inprint_frame_bank


async def test_bank_skips_when_viewer_attached_and_buffer_empty(monkeypatch):
    """BamDude divergence: never open a competing camera socket while a viewer
    is attached but the fan-out buffer is momentarily empty (X2D / port-6000
    single-connection firmware). Mirrors obico_detection._capture_frame."""
    counter = _bank_env(monkeypatch)
    monkeypatch.setattr("backend.app.api.routes.camera.is_stream_active", lambda _pid: True)
    monkeypatch.setattr("backend.app.api.routes.camera.get_buffered_frame", lambda _pid: None)
    await main_module._maybe_bank_inprint_frame(3, 5)
    assert counter["n"] == 0
    assert 3 not in main_module._inprint_frame_bank
