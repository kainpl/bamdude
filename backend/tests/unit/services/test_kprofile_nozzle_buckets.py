"""The printer's calibration table is filed per nozzle diameter (upstream #2854).

An ``extrusion_cali_get`` response is the complete table for ONE nozzle
diameter, and the printer answers whoever asks — BambuStudio's queries land on
the report topic we subscribe to, the git backup probes 0.2/0.4/0.6/0.8 in
turn, the spool dialog asks for each fitted nozzle. Assigning every response
straight to ``state.kprofiles`` let any one answer stand for the whole printer,
and the cost was not only a blank K on the AMS card: the ``on_kprofiles_changed``
sync hard-prunes every cached calibration the live list does not carry, so a
0.6 reply deleted the 0.4 calibrations together with their spool links and
notes.

Responses are therefore bucketed by the diameter they describe and
``state.kprofiles`` is the union. A table is read once per connection for the
diameters actually fitted, not by luck.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.app.services.bambu_mqtt import BambuMQTTClient


def _client(**kwargs) -> BambuMQTTClient:
    """A client with no transport — only the message handling is under test."""
    return BambuMQTTClient(ip_address="192.168.1.100", serial_number="TESTSERIAL0000", access_code="12345678", **kwargs)


def _response(nozzle, *entries: tuple[int, str], seq_id: str | None = None, extruder: int = 0) -> dict:
    """One ``extrusion_cali_get`` payload as a single-nozzle printer sends it:
    the diameter on the envelope, none on the entries."""
    payload: dict = {
        "command": "extrusion_cali_get",
        "filaments": [
            {
                "cali_idx": cali_idx,
                "extruder_id": extruder,
                "filament_id": "GFL99",
                "k_value": k_value,
                "name": f"Profile {cali_idx}",
                "setting_id": "GFSL99",
            }
            for cali_idx, k_value in entries
        ],
    }
    if nozzle is not None:
        payload["nozzle_diameter"] = nozzle
    if seq_id is not None:
        payload["sequence_id"] = seq_id
    return payload


def _by_nozzle(client: BambuMQTTClient) -> dict[str, list[str]]:
    table: dict[str, list[str]] = {}
    for kp in client.state.kprofiles:
        table.setdefault(kp.nozzle_diameter, []).append(kp.k_value)
    return table


class TestTablesAreFiledPerNozzle:
    def test_an_empty_table_clears_only_its_own_nozzle(self):
        """The backup's probe sequence on a 0.4 + 0.6 machine: 0.2 and 0.8 are
        empty, and neither may take the other two nozzles' profiles with it."""
        client = _client()
        client._handle_kprofile_response(_response("0.2"))
        client._handle_kprofile_response(_response("0.4", (3, "0.020000")))
        client._handle_kprofile_response(_response("0.6", (3, "0.018000")))
        client._handle_kprofile_response(_response("0.8"))

        assert _by_nozzle(client) == {"0.4": ["0.020000"], "0.6": ["0.018000"]}

    def test_a_fresh_table_replaces_its_own_nozzle_wholesale(self):
        """A re-read is authoritative for its nozzle: a deletion must stick."""
        client = _client()
        client._handle_kprofile_response(_response("0.4", (3, "0.020000"), (4, "0.021000")))
        client._handle_kprofile_response(_response("0.6", (3, "0.018000")))
        client._handle_kprofile_response(_response("0.4", (3, "0.019000")))

        assert _by_nozzle(client) == {"0.4": ["0.019000"], "0.6": ["0.018000"]}

    def test_an_answer_that_names_no_nozzle_and_holds_nothing_keeps_the_table(self):
        """No envelope diameter and no entries names no bucket to replace."""
        client = _client()
        client._handle_kprofile_response(_response("0.4", (3, "0.020000")))
        client._handle_kprofile_response(_response(None))

        assert _by_nozzle(client) == {"0.4": ["0.020000"]}

    def test_entries_name_their_own_bucket_when_the_envelope_does_not(self):
        client = _client()
        client._handle_kprofile_response(_response("0.4", (3, "0.020000")))
        client._handle_kprofile_response(
            {
                "command": "extrusion_cali_get",
                "filaments": [{"cali_idx": 3, "extruder_id": 0, "k_value": "0.017000", "nozzle_diameter": "0.6"}],
            }
        )

        assert _by_nozzle(client) == {"0.4": ["0.020000"], "0.6": ["0.017000"]}

    @pytest.mark.parametrize("spelling", ["0.40", 0.4])
    def test_one_diameter_spelled_two_ways_is_one_bucket(self, spelling):
        """A bucket that keeps the 0.4 table twice would double every entry."""
        client = _client()
        client._handle_kprofile_response(_response("0.4", (3, "0.020000")))
        client._handle_kprofile_response(_response(spelling, (3, "0.019000")))

        assert [kp.k_value for kp in client.state.kprofiles] == ["0.019000"]

    def test_a_waiter_gets_its_own_table_not_the_union(self):
        """``get_kprofiles(0.6)`` answers for 0.6 — callers render exactly the
        diameter they asked about."""
        client = _client()
        client._handle_kprofile_response(_response("0.4", (3, "0.020000")))
        client._kprofile_waiters["41"] = (asyncio.Event(), "0.6", None)

        client._handle_kprofile_response(_response("0.6", (5, "0.018000"), seq_id="41"))

        delivered = client._kprofile_waiters["41"][2]
        assert [(kp.slot_id, kp.k_value) for kp in delivered] == [(5, "0.018000")]
        assert _by_nozzle(client) == {"0.4": ["0.020000"], "0.6": ["0.018000"]}

    def test_a_table_nobody_here_asked_for_is_still_filed(self):
        """A pending request for another nozzle does not make a stranger's
        answer less true — with buckets it can no longer overwrite anything."""
        client = _client()
        client._kprofile_waiters["41"] = (asyncio.Event(), "0.4", None)

        client._handle_kprofile_response(_response("0.6", (5, "0.018000"), seq_id="999"))

        assert _by_nozzle(client) == {"0.6": ["0.018000"]}
        assert not client._kprofile_waiters["41"][0].is_set()


class TestTheChangeSignalFollowsTheUnion:
    def test_an_empty_table_for_a_nozzle_that_holds_nothing_changes_nothing(self):
        on_changed = MagicMock()
        client = _client(on_kprofiles_changed=on_changed)
        client._handle_kprofile_response(_response("0.4", (3, "0.020000")))
        on_changed.reset_mock()

        client._handle_kprofile_response(_response("0.8"))

        on_changed.assert_not_called()

    def test_the_other_nozzles_table_arriving_is_a_change(self):
        on_changed = MagicMock()
        client = _client(on_kprofiles_changed=on_changed)
        client._handle_kprofile_response(_response("0.4", (3, "0.020000")))
        on_changed.reset_mock()

        client._handle_kprofile_response(_response("0.6", (3, "0.018000")))

        on_changed.assert_called_once()


class TestTheHistoryViewIsFiledPerNozzle:
    def test_it_keeps_every_nozzle_it_has_read(self):
        """Same payload, same defect: the Calibration History modal showed
        whichever diameter was answered last — usually the backup's empty 0.8."""
        client = _client()
        client._handle_extrusion_cali_history(_response("0.4", (3, "0.020000")))
        client._handle_extrusion_cali_history(_response("0.6", (3, "0.018000")))
        client._handle_extrusion_cali_history(_response("0.8"))

        assert sorted((h.nozzle_diameter, h.k_value) for h in client.state.extrusion_cali_history) == [
            (0.4, 0.02),
            (0.6, 0.018),
        ]


class TestTheTableIsReadOnceTheFittedNozzlesAreKnown:
    """Nothing used to read the table on connect for the nozzles actually
    fitted — only a blind 0.4 prime. A printer with a 0.6 nozzle that nobody
    opened the Profiles page for showed no K values at all."""

    def _connected(self, **kwargs) -> tuple[BambuMQTTClient, MagicMock]:
        due = MagicMock()
        client = _client(on_kprofile_tables_due=due, **kwargs)
        client.state.connected = True
        return client, due

    def test_the_fitted_diameter_is_asked_for_once(self):
        client, due = self._connected()

        client._update_state({"nozzle_diameter": "0.6"})
        client._update_state({"nozzle_diameter": "0.6"})

        due.assert_called_once_with(["0.6"])

    def test_nothing_is_asked_before_a_diameter_is_known(self):
        """The first push does not always carry the nozzle fields — spending the
        connection's one attempt there would ask about nothing."""
        client, due = self._connected()

        client._update_state({"nozzle_temper": 210.0})
        due.assert_not_called()

        client._update_state({"nozzle_diameter": "0.4"})
        due.assert_called_once_with(["0.4"])

    def test_a_dual_nozzle_printer_asks_for_each_distinct_diameter(self):
        client, due = self._connected()

        client._update_state({"left_nozzle_diameter": "0.4", "right_nozzle_diameter": "0.6"})

        due.assert_called_once_with(["0.4", "0.6"])

    def test_two_identical_nozzles_are_asked_for_once(self):
        client, due = self._connected()

        client._update_state({"device": {"nozzle": {"info": [{"id": 0, "diameter": 0.4}, {"id": 1, "diameter": 0.4}]}}})

        due.assert_called_once_with(["0.4"])

    def test_a_disconnected_client_asks_nothing(self):
        client, due = self._connected()
        client.state.connected = False

        client._update_state({"nozzle_diameter": "0.4"})

        due.assert_not_called()

    def test_a_reconnect_asks_again(self):
        """The table may have changed while the link was down."""
        client, due = self._connected()
        client._client = MagicMock()
        client._client.subscribe.return_value = (0, 1)  # paho's (result, mid)
        client._update_state({"nozzle_diameter": "0.4"})

        client._on_connect(client._client, None, None, 0)
        client._update_state({"nozzle_diameter": "0.4"})

        assert due.call_count == 2

    def test_a_failing_callback_does_not_break_the_push(self):
        client, due = self._connected()
        due.side_effect = RuntimeError("boom")

        client._update_state({"nozzle_diameter": "0.4", "nozzle_temper": 205.0})

        # raw_data is stored after the nozzle block — the rest of the push landed.
        assert client.state.raw_data.get("nozzle_temper") == 205.0


class TestPrimeKProfileTables:
    async def _prime(self, client, diameters):
        from backend.app.services import printer_manager as pm_module

        with patch.object(pm_module.printer_manager, "get_client", return_value=client):
            return await pm_module._prime_kprofile_tables(7, diameters)

    @pytest.mark.asyncio
    async def test_each_diameter_is_read_with_its_own_request(self):
        client = MagicMock()
        client.state = SimpleNamespace(connected=True)
        client.get_kprofiles = AsyncMock(return_value=[])

        read = await self._prime(client, ["0.4", "0.6"])

        assert read == 2
        assert [c.kwargs["nozzle_diameter"] for c in client.get_kprofiles.await_args_list] == ["0.4", "0.6"]

    @pytest.mark.asyncio
    async def test_one_nozzle_failing_does_not_cost_the_other_its_table(self):
        """This runs on the back of a connection; it may not raise into it."""
        client = MagicMock()
        client.state = SimpleNamespace(connected=True)
        client.get_kprofiles = AsyncMock(side_effect=[RuntimeError("no answer"), []])

        read = await self._prime(client, ["0.4", "0.6"])

        assert read == 1

    @pytest.mark.asyncio
    async def test_a_disconnected_printer_is_left_alone(self):
        client = MagicMock()
        client.state = SimpleNamespace(connected=False)
        client.get_kprofiles = AsyncMock(return_value=[])

        assert await self._prime(client, ["0.4"]) == 0
        client.get_kprofiles.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_the_manager_wires_the_client_to_it(self):
        from backend.app.services.printer_manager import PrinterManager

        manager = PrinterManager()
        manager._schedule_async = MagicMock()
        printer = SimpleNamespace(
            id=7, ip_address="192.168.1.100", serial_number="TESTSERIAL0000", access_code="x", model="X1C", name="P"
        )
        with patch("backend.app.services.printer_manager.BambuMQTTClient") as mock_client_cls:
            mock_client_cls.return_value = MagicMock(state=MagicMock(connected=True))
            await manager.connect_printer(printer)

        callback = mock_client_cls.call_args.kwargs["on_kprofile_tables_due"]
        callback(["0.4"])

        scheduled = manager._schedule_async.call_args.args[0]
        assert scheduled.cr_code.co_name == "_prime_kprofile_tables"
        scheduled.close()
