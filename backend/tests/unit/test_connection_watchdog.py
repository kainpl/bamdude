"""MQTT sessions that stop reconnecting get rebuilt (upstream #2732).

A printer lost its session at 02:19 and did not return until 11:24 — nine hours
offline with the UI open. ``check_staleness()`` opens with
``if self.state.connected and self.is_stale()``, so it only handles the
half-broken session that is *still connected but quiet*; a client with
``connected=False`` returns immediately and paho's retry is the only thing left
watching. Ours has the same first line and is not even on a timer — it is called
from ``get_status()``, so a printer nobody is looking at has nothing watching it.

Four conditions, and **the fourth is what makes the sweep safe to run**: the MQTT
port must still answer. Without it, a farm powered down overnight would churn
clients and spam the log once a minute for every machine.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from backend.app.services import connection_watchdog as cw


def _printer(pid: int = 1, ip: str = "192.168.1.50"):
    return SimpleNamespace(id=pid, name=f"printer-{pid}", ip_address=ip)


def _client(*, connected: bool, silent_for: float | None):
    """``silent_for=None`` means the client never heard anything at all."""
    import time

    last = 0 if silent_for is None else time.time() - silent_for
    return SimpleNamespace(
        state=SimpleNamespace(connected=connected),
        _last_message_time=last,
        _last_connect_error=None,
    )


@pytest.fixture(autouse=True)
def _clear_cooldowns():
    cw._last_rebuild.clear()
    yield
    cw._last_rebuild.clear()


@pytest.mark.asyncio
class TestTheFourConditions:
    async def test_rebuilds_a_dead_session_whose_port_still_answers(self) -> None:
        """The reported case."""
        with patch.object(cw, "check_port", AsyncMock(return_value=True)):
            assert await cw.should_rebuild(_printer(), _client(connected=False, silent_for=600)) is True

    async def test_leaves_a_connected_client_alone(self) -> None:
        with patch.object(cw, "check_port", AsyncMock(return_value=True)) as probe:
            assert await cw.should_rebuild(_printer(), _client(connected=True, silent_for=600)) is False
        probe.assert_not_awaited(), "a connected client should not even cost a probe"

    async def test_leaves_a_printer_that_never_connected_alone(self) -> None:
        """Never had a working session — that is a configuration problem, not a
        session to rebuild."""
        with patch.object(cw, "check_port", AsyncMock(return_value=True)):
            assert await cw.should_rebuild(_printer(), _client(connected=False, silent_for=None)) is False

    async def test_leaves_a_session_inside_the_grace_period_alone(self) -> None:
        """The grace sits past the 60s staleness timeout and the 30s max
        reconnect backoff, so a session recovering by itself is not cut short."""
        with patch.object(cw, "check_port", AsyncMock(return_value=True)):
            assert await cw.should_rebuild(_printer(), _client(connected=False, silent_for=120)) is False

    async def test_leaves_a_switched_off_printer_to_paho(self) -> None:
        """The condition that keeps this from becoming a nuisance: a farm
        powered down overnight must cause no client churn and no log spam."""
        with patch.object(cw, "check_port", AsyncMock(return_value=False)):
            assert await cw.should_rebuild(_printer(), _client(connected=False, silent_for=600)) is False

    async def test_a_printer_with_no_ip_is_skipped_without_probing(self) -> None:
        with patch.object(cw, "check_port", AsyncMock(return_value=True)) as probe:
            assert await cw.should_rebuild(_printer(ip=""), _client(connected=False, silent_for=600)) is False
        probe.assert_not_awaited()

    async def test_no_client_at_all_is_not_a_rebuild(self) -> None:
        assert await cw.should_rebuild(_printer(), None) is False

    async def test_the_port_probe_is_the_last_check(self) -> None:
        """It is the only condition that costs a network round trip, so the free
        ones must rule the printer out first."""
        with patch.object(cw, "check_port", AsyncMock(return_value=True)) as probe:
            await cw.should_rebuild(_printer(), _client(connected=False, silent_for=10))
        probe.assert_not_awaited()


@pytest.mark.asyncio
class TestRateLimiting:
    async def test_a_second_rebuild_is_suppressed_within_the_cooldown(self) -> None:
        """A printer whose session dies immediately must not be rebuilt every
        sweep."""
        import time

        cw._last_rebuild[1] = time.time()
        with patch.object(cw, "check_port", AsyncMock(return_value=True)):
            assert await cw.should_rebuild(_printer(), _client(connected=False, silent_for=600)) is False

    async def test_the_cooldown_expires(self) -> None:
        import time

        cw._last_rebuild[1] = time.time() - (cw.REBUILD_COOLDOWN_SECONDS + 1)
        with patch.object(cw, "check_port", AsyncMock(return_value=True)):
            assert await cw.should_rebuild(_printer(), _client(connected=False, silent_for=600)) is True


@pytest.mark.asyncio
class TestTheSweep:
    async def _run(self, printers, clients, *, port_open=True, connect_ok=True):
        with (
            patch.object(cw, "check_port", AsyncMock(return_value=port_open)),
            patch.object(cw.printer_manager, "get_client", side_effect=lambda pid: clients.get(pid)),
            patch.object(cw.printer_manager, "connect_printer", AsyncMock(return_value=connect_ok)) as connect,
            patch.object(cw, "async_session") as session,
        ):
            ctx = session.return_value
            ctx.__aenter__ = AsyncMock(return_value=_db_with(printers))
            ctx.__aexit__ = AsyncMock(return_value=False)
            count = await cw.sweep_once()
        return count, connect

    async def test_a_dead_session_is_rebuilt(self) -> None:
        printers = [_printer(1)]
        clients = {1: _client(connected=False, silent_for=600)}
        count, connect = await self._run(printers, clients)

        assert count == 1
        connect.assert_awaited_once()

    async def test_one_bad_client_does_not_end_the_sweep(self) -> None:
        """A farm must not lose its watchdog because one printer threw."""

        class _Explodes:
            @property
            def state(self):
                raise RuntimeError("boom")

        printers = [_printer(1), _printer(2)]
        clients = {1: _Explodes(), 2: _client(connected=False, silent_for=600)}
        count, connect = await self._run(printers, clients)

        assert count == 1, "the second printer must still be rebuilt"
        connect.assert_awaited_once()

    async def test_a_printer_that_came_back_clears_its_cooldown(self) -> None:
        """So its next failure is judged on its own merits rather than being
        suppressed by an old one."""
        cw._last_rebuild[1] = 12345.0
        printers = [_printer(1)]
        clients = {1: _client(connected=True, silent_for=1)}

        await self._run(printers, clients)

        assert 1 not in cw._last_rebuild

    async def test_a_failed_rebuild_is_not_counted(self) -> None:
        printers = [_printer(1)]
        clients = {1: _client(connected=False, silent_for=600)}
        count, _ = await self._run(printers, clients, connect_ok=False)
        assert count == 0


def _db_with(printers):
    from unittest.mock import MagicMock

    db = MagicMock()
    result = MagicMock()
    result.scalars.return_value.all.return_value = printers
    db.execute = AsyncMock(return_value=result)
    return db
