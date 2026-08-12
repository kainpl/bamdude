"""zigpy calls these; they translate into the app's own WebSocket events.

Two traps here, both verified in zigpy's source rather than assumed, and both
completely silent when got wrong:

* ``listener_event`` invokes callbacks with ``method(*args)`` and never awaits,
  so an ``async def`` callback returns a coroutine nobody runs — every broadcast
  would simply never happen, with nothing logged anywhere.
* zigpy wraps callbacks in ``except Exception`` and logs at DEBUG, so a raising
  callback does not break the stack; it disappears.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from backend.app.services.zigbee.coordinator import CoordinatorState, ZigbeeCoordinator
from backend.app.services.zigbee.devices import ELECTRICAL_MEASUREMENT, METERING, ON_OFF


def _device(clusters=(ON_OFF, METERING, ELECTRICAL_MEASUREMENT)):
    return SimpleNamespace(
        ieee="34:8d:13:ff:fe:11:e4:6f",
        nwk=0x1234,
        manufacturer="SONOFF",
        model="S60ZBTPF",
        endpoints={1: SimpleNamespace(in_clusters={c: object() for c in clusters})},
    )


def _settings():
    return {"zigbee_enabled": "true", "zigbee_transport": "ethernet", "zigbee_path": "1.2.3.4:6638"}


async def _drain():
    """Let the spawned broadcast tasks run."""
    for _ in range(3):
        await asyncio.sleep(0)


def test_callbacks_are_not_coroutine_functions():
    """The trap that would make every broadcast silently vanish.

    Cheapest possible guard against the worse of the two silent failures: if one
    of these ever becomes ``async def``, zigpy will call it, get a coroutine
    back, drop it, and nothing will ever reach the UI.
    """
    coord = ZigbeeCoordinator(data_dir="unused")
    for name in ("device_joined", "device_initialized", "device_left", "connection_lost"):
        assert not asyncio.iscoroutinefunction(getattr(coord, name)), f"{name} must stay a plain def"


@pytest.mark.asyncio
async def test_joining_is_announced_before_the_interview(tmp_path):
    """Interviewing takes tens of seconds; silence for that long reads as broken."""
    coord = ZigbeeCoordinator(data_dir=tmp_path)

    with patch("backend.app.services.zigbee.coordinator.ws_manager.broadcast", AsyncMock()) as bc:
        coord.device_joined(_device())
        await _drain()

    assert bc.await_args[0][0]["type"] == "zigbee_device_joining"


@pytest.mark.asyncio
async def test_a_plug_is_reported_paired_with_its_capabilities(tmp_path):
    coord = ZigbeeCoordinator(data_dir=tmp_path)

    with patch("backend.app.services.zigbee.coordinator.ws_manager.broadcast", AsyncMock()) as bc:
        coord.device_initialized(_device())
        await _drain()

    payload = bc.await_args[0][0]
    assert payload["type"] == "zigbee_device_paired"
    assert payload["device"]["model"] == "S60ZBTPF"
    assert payload["device"]["has_metering"] is True


@pytest.mark.asyncio
async def test_a_non_plug_is_rejected_and_removed_from_the_network(tmp_path):
    """Left joined it would occupy an address and reappear in every device list,
    indistinguishable from a plug that failed for some other reason."""
    coord = ZigbeeCoordinator(data_dir=tmp_path)
    app = AsyncMock()
    coord._app = app

    with patch("backend.app.services.zigbee.coordinator.ws_manager.broadcast", AsyncMock()) as bc:
        coord.device_initialized(_device(clusters=(METERING,)))
        await _drain()

    payload = bc.await_args[0][0]
    assert payload["type"] == "zigbee_device_rejected"
    assert "On/Off" in payload["device"]["reject_reason"]
    app.remove.assert_awaited_once()


@pytest.mark.asyncio
async def test_rejection_without_a_live_app_does_not_explode(tmp_path):
    """connection_lost can land between the interview and our removal."""
    coord = ZigbeeCoordinator(data_dir=tmp_path)
    coord._app = None

    with patch("backend.app.services.zigbee.coordinator.ws_manager.broadcast", AsyncMock()):
        coord.device_initialized(_device(clusters=()))
        await _drain()


@pytest.mark.asyncio
async def test_device_left_is_announced(tmp_path):
    coord = ZigbeeCoordinator(data_dir=tmp_path)

    with patch("backend.app.services.zigbee.coordinator.ws_manager.broadcast", AsyncMock()) as bc:
        coord.device_left(_device())
        await _drain()

    assert bc.await_args[0][0]["type"] == "zigbee_device_left"


@pytest.mark.asyncio
async def test_connection_lost_flips_the_status(tmp_path):
    """Closes the phase-1 gap: status used to be a startup snapshot, not liveness."""
    coord = ZigbeeCoordinator(data_dir=tmp_path)
    with patch.object(coord, "_open_radio", AsyncMock()):
        await coord.start(_settings())
    assert coord.status.state is CoordinatorState.UP

    with patch("backend.app.services.zigbee.coordinator.ws_manager.broadcast", AsyncMock()) as bc:
        coord.connection_lost(OSError("radio gone"))
        await _drain()

    assert coord.status.state is CoordinatorState.ERROR
    assert "radio gone" in coord.status.reason
    assert bc.await_args[0][0]["type"] == "zigbee_status_changed"
    await coord.stop()


@pytest.mark.asyncio
async def test_a_failing_callback_does_not_raise(tmp_path):
    """zigpy would swallow this at DEBUG. We log it — but either way it must not
    escape into zigpy's dispatch."""
    coord = ZigbeeCoordinator(data_dir=tmp_path)
    broken = SimpleNamespace()  # no .ieee, no .endpoints

    coord.device_initialized(broken)  # must not raise
    coord.device_joined(broken)  # must not raise
    coord.device_left(broken)  # must not raise
    await _drain()
