"""A K-profile table that was not READ is not an empty table (upstream 080d866fb).

``get_kprofiles`` used to answer ``[]`` both for a nozzle that holds no
profiles and for a printer that never answered. Every caller then acted on
"empty": the Configure Slot dialog cleared the slot's K, a Git restore sent
every profile as new beside its original, the connect-time prime counted the
nozzle as read. No answer is now a typed error and each caller decides.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.app.services import printer_manager as pm_module
from backend.app.services.bambu_mqtt import BambuMQTTClient, KProfileReadError


def _client() -> BambuMQTTClient:
    c = BambuMQTTClient.__new__(BambuMQTTClient)
    c.serial_number = "01P00A000000000"
    c._sequence_id = 0
    c._kprofile_waiters = {}
    c._kprofiles_by_nozzle = {}
    c._client = MagicMock()
    c._loop = None
    c.state = MagicMock()
    c.state.connected = True
    c.state.kprofiles = []
    c.on_kprofiles_changed = None
    c._maybe_notify_kprofiles_changed = lambda _profiles: None
    return c


@pytest.mark.asyncio
class TestReadIsTyped:
    async def test_a_disconnected_printer_is_a_read_error(self) -> None:
        c = _client()
        c.state.connected = False
        with pytest.raises(KProfileReadError):
            await c.get_kprofiles(nozzle_diameter="0.4")

    async def test_a_printer_that_never_answers_is_a_read_error(self) -> None:
        c = _client()
        with pytest.raises(KProfileReadError):
            await c.get_kprofiles(nozzle_diameter="0.4", timeout=0.05, max_retries=2)
        # Both attempts were really made, and nothing of them is left behind.
        assert c._client.publish.call_count == 2
        assert c._kprofile_waiters == {}

    async def test_an_answered_empty_table_is_an_empty_list(self) -> None:
        """A nozzle that holds no profiles answers — that IS an empty table."""
        c = _client()
        c._loop = asyncio.get_running_loop()

        async def answer() -> None:
            for _ in range(200):
                if c._kprofile_waiters:
                    break
                await asyncio.sleep(0.005)
            seq = next(iter(c._kprofile_waiters))
            c._handle_kprofile_response({"nozzle_diameter": "0.8", "sequence_id": seq, "filaments": []})

        profiles, _ = await asyncio.gather(c.get_kprofiles(nozzle_diameter="0.8", timeout=3.0), answer())
        assert profiles == []

    async def test_an_unanswered_read_leaves_the_filed_tables_alone(self) -> None:
        """The cache sync and its prune read the filed union, never the reply — a
        timeout must not empty a bucket (that would prune the nozzle's rows)."""
        c = _client()
        c._kprofiles_by_nozzle = {"0.4": ["kept"]}
        with pytest.raises(KProfileReadError):
            await c.get_kprofiles(nozzle_diameter="0.4", timeout=0.05, max_retries=1)
        assert c._kprofiles_by_nozzle == {"0.4": ["kept"]}


@pytest.mark.asyncio
class TestConnectPrime:
    async def _prime(self, client, diameters):
        with patch.object(pm_module.printer_manager, "get_client", return_value=client):
            return await pm_module._prime_kprofile_tables(7, diameters)

    async def test_an_unread_nozzle_is_not_counted_and_the_read_is_owed_again(self) -> None:
        client = MagicMock()
        client.state = SimpleNamespace(connected=True)
        client.get_kprofiles = AsyncMock(side_effect=[KProfileReadError("no answer"), []])

        read = await self._prime(client, ["0.4", "0.6"])

        assert read == 1
        client.rearm_fitted_kprofile_tables.assert_called_once_with()

    async def test_every_nozzle_read_owes_nothing(self) -> None:
        client = MagicMock()
        client.state = SimpleNamespace(connected=True)
        client.get_kprofiles = AsyncMock(return_value=[])

        assert await self._prime(client, ["0.4"]) == 1
        client.rearm_fitted_kprofile_tables.assert_not_called()


class TestRearmIsBounded:
    """The re-arm must not become a request storm against a printer that never answers."""

    def _latched(self) -> tuple[BambuMQTTClient, list]:
        c = _client()
        c.state = SimpleNamespace(connected=True, nozzles=[SimpleNamespace(nozzle_diameter="0.4")])
        asked: list = []
        c.on_kprofile_tables_due = asked.append
        c._kprofile_tables_requested = False
        c._kprofile_table_attempts = 0
        return c, asked

    def test_a_rearmed_latch_asks_again_on_the_next_push(self) -> None:
        c, asked = self._latched()
        c._request_fitted_kprofile_tables()
        c._request_fitted_kprofile_tables()  # latched: the second push asks nothing
        assert len(asked) == 1
        c.rearm_fitted_kprofile_tables()
        c._request_fitted_kprofile_tables()
        assert len(asked) == 2

    def test_the_attempts_of_one_connection_are_limited(self) -> None:
        c, asked = self._latched()
        for _ in range(10):
            c._request_fitted_kprofile_tables()
            c.rearm_fitted_kprofile_tables()
        assert len(asked) == BambuMQTTClient.KPROFILE_TABLE_ATTEMPTS_PER_CONNECTION
