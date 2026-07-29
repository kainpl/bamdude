"""Coordinator lifecycle. Pairing is phase 2, control is phase 3 — neither is here.

The test that matters most is the failing-radio one. Every other guarantee is a
convenience; that one is the difference between "the dongle has a problem" and
"BamDude will not start". A $20 USB stick must not be able to take the farm down,
so nothing may escape ``start()``.

All of these run without hardware: the single seam that touches zigpy is
``_open_radio``, and it is patched throughout.
"""

from unittest.mock import AsyncMock, patch

import pytest

from backend.app.services.zigbee.coordinator import CoordinatorState, ZigbeeCoordinator


def _settings(enabled=True, mode="ethernet", path="1.2.3.4:6638"):
    return {
        "zigbee_enabled": "true" if enabled else "false",
        "zigbee_transport": mode,
        "zigbee_path": path,
    }


@pytest.mark.asyncio
async def test_disabled_does_not_touch_the_radio(tmp_path):
    """An install that never wants Zigbee pays nothing but an import."""
    coord = ZigbeeCoordinator(data_dir=tmp_path)
    with patch.object(coord, "_open_radio", AsyncMock()) as radio:
        await coord.start(_settings(enabled=False))

    radio.assert_not_awaited()
    assert coord.status.state is CoordinatorState.DISABLED


@pytest.mark.asyncio
async def test_radio_failure_leaves_the_app_running(tmp_path):
    """Unplugged, wrong mode, unreachable IP — all the same: report, do not raise."""
    coord = ZigbeeCoordinator(data_dir=tmp_path)
    with patch.object(coord, "_open_radio", AsyncMock(side_effect=OSError("no such device"))):
        await coord.start(_settings())

    assert coord.status.state is CoordinatorState.ERROR
    assert "no such device" in coord.status.reason


@pytest.mark.asyncio
async def test_radio_failure_releases_the_lock(tmp_path):
    """A failed start must not leave the radio marked as ours."""
    coord = ZigbeeCoordinator(data_dir=tmp_path)
    with patch.object(coord, "_open_radio", AsyncMock(side_effect=OSError("boom"))):
        await coord.start(_settings())

    other = ZigbeeCoordinator(data_dir=tmp_path)
    with patch.object(other, "_open_radio", AsyncMock()):
        await other.start(_settings())
    assert other.status.state is CoordinatorState.UP


@pytest.mark.asyncio
async def test_bad_config_reports_rather_than_raises(tmp_path):
    coord = ZigbeeCoordinator(data_dir=tmp_path)
    await coord.start(_settings(path=""))

    assert coord.status.state is CoordinatorState.ERROR
    assert "not set" in coord.status.reason


@pytest.mark.asyncio
async def test_bad_config_never_reaches_the_lock(tmp_path):
    """Misconfiguration must not leave a lock file behind."""
    coord = ZigbeeCoordinator(data_dir=tmp_path)
    await coord.start(_settings(path=""))

    assert not (tmp_path / "zigbee" / "radio.lock").exists()


@pytest.mark.asyncio
async def test_start_is_idempotent(tmp_path):
    coord = ZigbeeCoordinator(data_dir=tmp_path)
    with patch.object(coord, "_open_radio", AsyncMock()) as radio:
        await coord.start(_settings())
        await coord.start(_settings())

    assert radio.await_count == 1
    assert coord.status.state is CoordinatorState.UP
    await coord.stop()


@pytest.mark.asyncio
async def test_busy_radio_names_the_likely_holder(tmp_path):
    """'Someone else has the dongle' is the least guessable cause, so say it."""
    holder = ZigbeeCoordinator(data_dir=tmp_path)
    second = ZigbeeCoordinator(data_dir=tmp_path)

    with patch.object(holder, "_open_radio", AsyncMock()), patch.object(second, "_open_radio", AsyncMock()) as radio:
        await holder.start(_settings())
        await second.start(_settings())

    radio.assert_not_awaited()
    assert second.status.state is CoordinatorState.ERROR
    assert "Zigbee2MQTT" in second.status.reason
    await holder.stop()


@pytest.mark.asyncio
async def test_stop_without_start_is_safe(tmp_path):
    await ZigbeeCoordinator(data_dir=tmp_path).stop()


@pytest.mark.asyncio
async def test_stop_releases_the_radio_for_a_second_instance(tmp_path):
    """--reload must be able to hand the radio over."""
    first, second = ZigbeeCoordinator(data_dir=tmp_path), ZigbeeCoordinator(data_dir=tmp_path)

    with patch.object(first, "_open_radio", AsyncMock()), patch.object(second, "_open_radio", AsyncMock()):
        await first.start(_settings())
        await first.stop()
        await second.start(_settings())

    assert second.status.state is CoordinatorState.UP
    await second.stop()


@pytest.mark.asyncio
async def test_stop_closes_the_zigpy_application(tmp_path):
    coord = ZigbeeCoordinator(data_dir=tmp_path)
    app = AsyncMock()
    with patch.object(coord, "_open_radio", AsyncMock(return_value=app)):
        await coord.start(_settings())
    await coord.stop()

    app.shutdown.assert_awaited_once()
    assert coord.status.state is CoordinatorState.DISABLED


@pytest.mark.asyncio
async def test_stop_survives_a_shutdown_that_raises(tmp_path):
    """Shutdown runs during app teardown; it must not take the teardown with it."""
    coord = ZigbeeCoordinator(data_dir=tmp_path)
    app = AsyncMock()
    app.shutdown.side_effect = OSError("radio already gone")
    with patch.object(coord, "_open_radio", AsyncMock(return_value=app)):
        await coord.start(_settings())

    await coord.stop()  # must not raise

    second = ZigbeeCoordinator(data_dir=tmp_path)
    with patch.object(second, "_open_radio", AsyncMock()):
        await second.start(_settings())
    assert second.status.state is CoordinatorState.UP


def test_database_lands_in_its_own_subdirectory(tmp_path):
    """Never beside bamdude.db.

    zigpy keeps SQLite even on PostgreSQL installs — that is its design, not our
    choice — so the path has to make clear this is not our application database
    and never enters our migration chain.
    """
    coord = ZigbeeCoordinator(data_dir=tmp_path)
    assert coord.database_path == tmp_path / "zigbee" / "zigbee.db"
