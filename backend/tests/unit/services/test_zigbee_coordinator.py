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

    Now patches the CONSTRUCTOR rather than ``new()``: ``_open_radio`` stopped
    calling ``new()`` so that a failed start leaves us holding the application
    and able to shut it down. The config still reaches zigpy raw, and that is
    still what this asserts.
    """
    coord = ZigbeeCoordinator(data_dir=tmp_path)
    captured = {}

    def _fake_ctor(config):
        captured["config"] = config
        app = MagicMock()
        app._load_db = AsyncMock()
        app.startup = AsyncMock()
        app.shutdown = AsyncMock()
        captured["app"] = app
        return app

    with patch("bellows.zigbee.application.ControllerApplication", _fake_ctor):
        await coord._open_radio("socket://127.0.0.1:1")

    assert set(captured["config"]) == {"database_path", "device"}, (
        "config was pre-validated before zigpy got it — that is the double-validation bug"
    )
    captured["app"].startup.assert_awaited_once_with(auto_form=True)
    # Without a resolver zigpy applies NO quirks at all — ``_resolve_device``
    # returns the bare device — and per-model fixes are not cosmetic here: the
    # plug this was built against keeps reporting the last measured power after
    # its socket is switched off, so BamDude read 33 W from a socket with nothing
    # running. ZHA passes the same resolver.
    captured["app"].register_device_resolver.assert_called_once()
    assert captured["app"].register_device_resolver.call_args.args[0] is not None


class TestPartialStartupIsTornDown:
    """A failed start must not leave bellows' reader thread and zigpy's DB
    worker running.

    ``ControllerApplication.new()`` keeps the application in a local, so when
    ``startup()`` raises the object is unreachable and nothing can shut it down.
    The threads outlived the failure: "Task was destroyed but it is pending",
    a process that would not exit, and another set leaked on every retry.
    """

    @pytest.mark.asyncio
    async def test_shutdown_runs_when_startup_fails(self, tmp_path):
        from backend.app.services.zigbee import coordinator as mod

        app = MagicMock()
        app._load_db = AsyncMock()
        app.startup = AsyncMock(side_effect=OSError("radio went away"))
        app.shutdown = AsyncMock()

        coord = ZigbeeCoordinator(data_dir=tmp_path)
        with (
            patch.object(mod, "_quirk_resolver", return_value=None),
            patch("bellows.zigbee.application.ControllerApplication", return_value=app),
            pytest.raises(OSError, match="radio went away"),
        ):
            await coord._open_radio("socket://127.0.0.1:6638")

        app.shutdown.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_a_raising_teardown_does_not_replace_the_reason(self, tmp_path):
        """The masking bug in miniature: if cleanup throws, the operator must
        still be told why the radio did not come up."""
        from backend.app.services.zigbee import coordinator as mod

        app = MagicMock()
        app._load_db = AsyncMock()
        app.startup = AsyncMock(side_effect=OSError("radio went away"))
        app.shutdown = AsyncMock(side_effect=RuntimeError("teardown also broke"))

        coord = ZigbeeCoordinator(data_dir=tmp_path)
        with (
            patch.object(mod, "_quirk_resolver", return_value=None),
            patch("bellows.zigbee.application.ControllerApplication", return_value=app),
            pytest.raises(OSError, match="radio went away"),
        ):
            await coord._open_radio("socket://127.0.0.1:6638")

    @pytest.mark.asyncio
    async def test_nothing_is_torn_down_on_success(self, tmp_path):
        from backend.app.services.zigbee import coordinator as mod

        app = MagicMock()
        app._load_db = AsyncMock()
        app.startup = AsyncMock()
        app.shutdown = AsyncMock()

        coord = ZigbeeCoordinator(data_dir=tmp_path)
        with (
            patch.object(mod, "_quirk_resolver", return_value=None),
            patch("bellows.zigbee.application.ControllerApplication", return_value=app),
        ):
            assert await coord._open_radio("socket://127.0.0.1:6638") is app

        app.shutdown.assert_not_awaited()


def test_open_radio_mirrors_zigpy_new():
    """``_open_radio`` hand-rolls what ``ControllerApplication.new()`` does, so
    that a failed start leaves us holding the application. That only stays
    correct while ``new()`` does the same steps.

    Guards the drift: if zigpy adds a step to ``new()``, this fails and someone
    decides whether ``_open_radio`` needs it too, instead of silently skipping
    it forever.
    """
    import inspect
    import re

    from zigpy.application import ControllerApplication

    calls = set(re.findall(r"\bapp\.(\w+)\(", inspect.getsource(ControllerApplication.new)))

    assert calls == {
        "register_device_resolver",  # mirrored
        "register_uninitialized_packet_handler",  # deliberately unused — we pass no handler
        "_load_db",  # mirrored
        "startup",  # mirrored
    }, f"ControllerApplication.new() changed shape: {sorted(calls)}"
