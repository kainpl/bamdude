"""Coordinator lifecycle. Pairing is phase 2, control is phase 3 — neither is here.

The test that matters most is the failing-radio one. Every other guarantee is a
convenience; that one is the difference between "the dongle has a problem" and
"BamDude will not start". A $20 USB stick must not be able to take the farm down,
so nothing may escape ``start()``.

All of these run without hardware: the single seam that touches zigpy is
``_open_radio``, and it is patched throughout.
"""

from unittest.mock import AsyncMock, MagicMock, patch

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


@pytest.mark.asyncio
async def test_open_radio_hands_zigpy_an_unvalidated_config(tmp_path):
    """The regression from the first run against a real dongle.

    We used to build the config with ``ControllerApplication.SCHEMA(...)`` and
    then hand the result to ``new()``, which validates again. The first pass
    turns the OTA entries into ``ZigpyOtaProvider`` objects and the second calls
    ``.get()`` on them, so it surfaced as "'ZigpyOtaProvider' object has no
    attribute 'get'" — a message naming neither config nor the radio, which is
    why it read as a hardware fault.

    Asserted by shape rather than by connecting: a validated config carries ~20
    keys of filled-in defaults, ours carries exactly the two we set. That is a
    precise, instant check for double validation.

    An earlier version of this test called the real ``new()`` against a closed
    local port. It caught the bug, but bellows leaves a thread behind on a
    failed connect and the pytest process then never exited — every full-suite
    run left a hung shell. The guarantee was worth keeping; the mechanism was
    not.
    """
    coord = ZigbeeCoordinator(data_dir=tmp_path)
    captured = {}

    async def _fake_new(config, **kwargs):
        captured["config"] = config
        captured["kwargs"] = kwargs
        return AsyncMock()

    with patch("bellows.zigbee.application.ControllerApplication.new", _fake_new):
        await coord._open_radio("socket://127.0.0.1:1")

    assert set(captured["config"]) == {"database_path", "device"}, (
        "config was pre-validated before new() — that is the double-validation bug"
    )
    assert captured["kwargs"]["auto_form"] is True
    assert captured["kwargs"]["start_radio"] is True
    # Without a resolver zigpy applies NO quirks at all — ``_resolve_device``
    # returns the bare device — and per-model fixes are not cosmetic here: the
    # plug this was built against keeps reporting the last measured power after
    # its socket is switched off, so BamDude read 33 W from a socket with nothing
    # running. ZHA passes the same resolver.
    assert captured["kwargs"]["device_resolver"] is not None


class TestTheReasonIsNeverEmpty:
    """`reason` is the whole explanation, so it must always say something.

    Measured on hardware: pointing the coordinator at a closed port produced
    `state: error` with `reason: ""`, because the exception bellows raised
    stringifies to nothing. Every consumer built in phase 4 — the settings card,
    the status badge, the toast — falls back to a generic label in that case, so
    the operator is told "the radio is down" and nothing about why.

    An exception class name is a poor explanation. It is still infinitely better
    than an empty string.
    """

    def test_an_exception_with_no_message_still_yields_a_reason(self):
        from backend.app.services.zigbee.coordinator import _describe_exception

        assert _describe_exception(TimeoutError()) == "TimeoutError"

    def test_a_message_is_preferred_when_there_is_one(self):
        from backend.app.services.zigbee.coordinator import _describe_exception

        assert _describe_exception(OSError("no such device")) == "no such device"

    def test_whitespace_counts_as_empty(self):
        from backend.app.services.zigbee.coordinator import _describe_exception

        assert _describe_exception(OSError("   ")) == "OSError"

    def test_none_is_described_rather_than_printed(self):
        """`connection_lost(None)` is what bellows actually passed on a dropped
        socket, and "Connection to the Zigbee radio was lost: None" is not an
        explanation."""
        from backend.app.services.zigbee.coordinator import _describe_exception

        assert _describe_exception(None) == "the connection closed without an error"
