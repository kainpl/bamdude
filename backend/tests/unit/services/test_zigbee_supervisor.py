"""The radio comes back on its own.

⚠️ Reported from a farm: the dongle was unplugged to be carried to another room,
plugged back in, and nothing happened until BamDude was restarted. The log says
why — ``connection_lost`` fired at 15:45:47, and between then and the restart two
minutes later there is not one further Zigbee line. Nothing retried, because
nothing existed to retry.

⚠️ The other half of that report was "or the UI does not receive the status".
It does: there is a WebSocket handler and a badge query. But the server never
changed the status back, so there was nothing to receive — which is why the
supervisor broadcasts on recovery rather than leaving it to the next refresh.

No hardware: the coordinator is a stub throughout, and the restart sequence is
patched so these test the supervisor's decisions, not zigpy's.
"""

import asyncio
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, patch

import pytest

from backend.app.services.zigbee.coordinator import CoordinatorState, CoordinatorStatus
from backend.app.services.zigbee.supervisor import RadioSupervisor, read_settings

pytestmark = pytest.mark.asyncio


class _Coordinator:
    """Just the two things the supervisor reads."""

    def __init__(self, state: CoordinatorState, reason: str = ""):
        self.status = CoordinatorStatus(state, reason)

    def becomes(self, state: CoordinatorState, reason: str = "") -> None:
        self.status = CoordinatorStatus(state, reason)


@asynccontextmanager
async def _no_session():
    yield object()


def _sessions():
    return _no_session


async def _tick(supervisor, coordinator, restart, *, ticks=1):
    """Run the loop for ``ticks`` cycles with time collapsed to nothing."""
    with (
        patch("backend.app.services.zigbee.supervisor.zigbee_coordinator", coordinator),
        patch("backend.app.services.zigbee.supervisor.restart_radio", restart),
        patch("backend.app.services.zigbee.supervisor._TICK_SECONDS", 0),
        patch("backend.app.services.zigbee.supervisor.ws_manager.broadcast", AsyncMock()) as broadcast,
    ):
        supervisor.start(_sessions())
        for _ in range(ticks):
            await asyncio.sleep(0)
        await asyncio.sleep(0.02)
        await supervisor.stop()
    return broadcast


class TestItRetriesOnlyWhenItShould:
    async def test_a_healthy_radio_is_left_alone(self):
        coordinator = _Coordinator(CoordinatorState.UP)
        restart = AsyncMock()

        await _tick(RadioSupervisor(), coordinator, restart)

        restart.assert_not_awaited()

    async def test_a_disabled_install_is_left_alone(self):
        """⚠️ ``disabled`` is a correct configuration, not a fault to repair.

        Retrying it would open a radio the operator switched off.
        """
        coordinator = _Coordinator(CoordinatorState.DISABLED)
        restart = AsyncMock()

        await _tick(RadioSupervisor(), coordinator, restart)

        restart.assert_not_awaited()

    async def test_a_starting_radio_is_left_alone(self):
        """Transient. Restarting it would abort the start already in flight."""
        coordinator = _Coordinator(CoordinatorState.STARTING)
        restart = AsyncMock()

        await _tick(RadioSupervisor(), coordinator, restart)

        restart.assert_not_awaited()

    async def test_a_down_radio_is_retried(self):
        coordinator = _Coordinator(CoordinatorState.ERROR, "the connection closed without an error")
        restart = AsyncMock()

        await _tick(RadioSupervisor(), coordinator, restart)

        assert restart.await_count >= 1


class TestComingBack:
    async def test_recovery_is_broadcast(self):
        """⚠️ Nobody else will say so.

        The restart route answers over HTTP and the UI reads the response; a
        retry has no response to carry. Without this the operator keeps the
        radio-down badge until something unrelated invalidates the query.
        """
        coordinator = _Coordinator(CoordinatorState.ERROR, "gone")

        async def _restart(_db):
            coordinator.becomes(CoordinatorState.UP)

        broadcast = await _tick(RadioSupervisor(), coordinator, AsyncMock(side_effect=_restart))

        assert broadcast.await_count == 1
        sent = broadcast.await_args.args[0]
        assert sent["type"] == "zigbee_status_changed"
        assert sent["state"] == "up"

    async def test_a_failed_retry_says_nothing(self):
        """Still down is not news, and the badge already says it."""
        coordinator = _Coordinator(CoordinatorState.ERROR, "gone")

        broadcast = await _tick(RadioSupervisor(), coordinator, AsyncMock())

        broadcast.assert_not_awaited()


class TestTheLoopSurvivesItself:
    async def test_a_raising_restart_does_not_end_the_supervisor(self):
        """⚠️ The one failure mode that would restore the original bug.

        A supervisor whose task died leaves exactly what was reported: a radio
        that never comes back without an application restart.
        """
        coordinator = _Coordinator(CoordinatorState.ERROR, "gone")
        supervisor = RadioSupervisor()

        await _tick(supervisor, coordinator, AsyncMock(side_effect=RuntimeError("port vanished")), ticks=3)

        # stop() cleared it; what matters is that nothing propagated out.
        assert supervisor._task is None


class TestSettingsAreReadFresh:
    async def test_missing_rows_become_empty_strings(self):
        """``start`` reads three keys and must get three, present or not."""

        class _Result:
            @staticmethod
            def all():
                return [("zigbee_enabled", "true")]

        class _DB:
            async def execute(self, _q):
                return _Result()

        assert await read_settings(_DB()) == {
            "zigbee_enabled": "true",
            "zigbee_transport": "",
            "zigbee_path": "",
        }

    async def test_a_null_value_becomes_an_empty_string(self):
        class _Result:
            @staticmethod
            def all():
                return [("zigbee_path", None)]

        class _DB:
            async def execute(self, _q):
                return _Result()

        assert (await read_settings(_DB()))["zigbee_path"] == ""
