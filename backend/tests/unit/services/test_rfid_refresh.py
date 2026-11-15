"""RFID re-read: the command BambuStudio would send, its answer read, a fallback on an explicit refusal.

Upstream 1cc4bae5d (#3206), owner's decision Р10 (option D). BambuStudio
chooses by capability BEFORE sending (``StatusPanel.cpp``,
``AmsControlWeb/ViewModelActions.cpp``): ``is_enable_np || is_enable_ams_np``
→ ``ams_get_rfid`` with ``ams_id`` / ``slot_id`` / ``mode`` and a real
``sequence_id`` (``command_ams_refresh_rfid2``); otherwise the gcode
``M620 R<ams*4+slot> \\n`` (``command_ams_refresh_rfid``). Ours used to send
``ams_get_rfid`` with sequence id "0" to every printer and say "Refreshing"
whatever came back.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.app.services.bambu_mqtt import BambuMQTTClient
from backend.app.utils.printer_models import uses_legacy_rfid_refresh

NEW_PROTOCOL = {"cfg": "0", "fun": "0", "aux": "0", "stat": "0"}


def _client(model: str, *, state: str = "IDLE", new_protocol: bool = False) -> BambuMQTTClient:
    c = BambuMQTTClient(ip_address="192.168.1.100", serial_number="TEST123", access_code="12345678")
    c.model = model
    c._client = MagicMock()
    c.state.connected = True
    c.state.state = state
    c.state.tray_now = 255
    c.state.enable_np = new_protocol
    return c


def _sent(client: BambuMQTTClient) -> list[dict]:
    return [json.loads(call[0][1])["print"] for call in client._client.publish.call_args_list]


def _answer(client: BambuMQTTClient, command: str, seq: str, result: str, reason: str = "") -> None:
    client._process_message(
        {"print": {"command": command, "sequence_id": seq, "result": result, "reason": reason, "param": ""}}
    )


async def _refresh(client: BambuMQTTClient, ams_id: int, tray_id: int, *answers: tuple[str, str, str]):
    """Send the refresh, then feed one printer answer per published command."""
    ok, message = client.ams_refresh_tray(ams_id, tray_id)
    assert ok, message
    waiter = asyncio.ensure_future(client.await_rfid_refresh(ams_id, tray_id, timeout=0.4))
    for command, result, reason in answers:
        await asyncio.sleep(0.02)
        sent = [s for s in _sent(client) if s["command"] == command]
        _answer(client, command, sent[-1]["sequence_id"], result, reason)
        await asyncio.sleep(0.08)
    return await waiter


class TestTheFirstCommandIsBambuStudios:
    def test_a_new_protocol_printer_gets_ams_get_rfid_with_mode_and_a_real_sequence(self):
        client = _client("H2D", new_protocol=True)

        assert client.ams_refresh_tray(1, 2) == (True, "Refreshing AMS 1 tray 2")

        (sent,) = _sent(client)
        assert (sent["command"], sent["ams_id"], sent["slot_id"], sent["mode"]) == ("ams_get_rfid", 1, 2, 0)
        assert sent["sequence_id"] not in ("0", "")
        assert client._is_our_sequence_id(sent["sequence_id"])

    def test_the_ams_np_bit_alone_means_the_new_command(self):
        """``flag3`` bit 9 — BambuStudio's ``is_enable_ams_np``."""
        client = _client("P1S")
        client._process_message({"print": {"command": "push_status", "flag3": 1 << 9}})
        assert client.state.enable_ams_np is True

        client.ams_refresh_tray(0, 1)

        assert _sent(client)[-1]["command"] == "ams_get_rfid"

    def test_a_flag3_without_bit_9_latches_nothing(self):
        client = _client("P1S")
        client._process_message({"print": {"command": "push_status", "flag3": (1 << 3) | (1 << 13)}})

        assert client.state.enable_ams_np is False

    def test_a_classic_printer_gets_the_gcode_exactly_as_bambustudio_writes_it(self):
        client = _client("P1S")

        client.ams_refresh_tray(1, 2)

        (sent,) = _sent(client)
        assert (sent["command"], sent["param"]) == ("gcode_line", "M620 R6 \n")

    def test_a_printer_that_has_not_reported_yet_gets_the_command_we_always_sent(self):
        """Not-latched may only mean not-reported-yet right after a connect."""
        client = _client("P1S", state="unknown")

        client.ams_refresh_tray(0, 0)

        assert _sent(client)[-1]["command"] == "ams_get_rfid"

    @pytest.mark.parametrize("state", ["RUNNING", "PAUSE", "PREPARE", "SLICING"])
    def test_the_gcode_is_never_sent_with_a_job_loaded(self, state):
        client = _client("P1S", state=state)

        client.ams_refresh_tray(0, 0)

        assert [s["command"] for s in _sent(client)] == ["ams_get_rfid"]

    @pytest.mark.parametrize(("ams_id", "tray_id"), [(128, 0), (6, 1), (255, 0), (4, 0)])
    def test_the_gcode_names_only_the_regular_units(self, ams_id, tray_id):
        client = _client("P1S")

        client.ams_refresh_tray(ams_id, tray_id)

        assert [s["command"] for s in _sent(client)] == ["ams_get_rfid"]

    def test_a_loaded_filament_refuses_before_anything_is_sent(self):
        client = _client("P1S")
        client.state.tray_now = 5

        ok, message = client.ams_refresh_tray(0, 0)

        assert not ok and "unload" in message.lower()
        client._client.publish.assert_not_called()


@pytest.mark.asyncio
class TestTheAnswerIsRead:
    async def test_silence_is_accepted(self):
        client = _client("H2D", new_protocol=True)

        assert await _refresh(client, 0, 1) == (True, "")
        assert client._pending_rfid_acks == {}

    async def test_a_success_is_accepted(self):
        client = _client("H2D", new_protocol=True)

        assert await _refresh(client, 0, 1, ("ams_get_rfid", "success", "")) == (True, "")

    async def test_a_refusal_on_a_new_protocol_printer_is_reported_with_the_printers_reason(self):
        client = _client("H2D", new_protocol=True)

        assert await _refresh(client, 0, 1, ("ams_get_rfid", "fail", "tray is busy")) == (False, "tray is busy")
        assert [s["command"] for s in _sent(client)] == ["ams_get_rfid"]

    async def test_the_answer_to_another_sequence_is_not_this_ones(self):
        client = _client("H2D", new_protocol=True)
        client.ams_refresh_tray(0, 1)
        waiter = asyncio.ensure_future(client.await_rfid_refresh(0, 1, timeout=0.3))
        await asyncio.sleep(0.02)

        _answer(client, "ams_get_rfid", "1", "fail", "somebody else's")

        assert await waiter == (True, "")

    async def test_the_verdict_is_said_in_the_log(self, caplog):
        client = _client("H2D", new_protocol=True)
        with caplog.at_level("INFO", logger="backend.app.services.bambu_mqtt"):
            await _refresh(client, 0, 1, ("ams_get_rfid", "fail", "tray is busy"))

        assert any("ams_get_rfid response" in r.message and "tray is busy" in r.message for r in caplog.records)


@pytest.mark.asyncio
class TestTheFallbackOnAnExplicitRefusal:
    async def test_a_refused_ams_get_rfid_falls_back_to_the_gcode_on_a_legacy_model(self):
        """The X1Plus case: the printer reports the new protocol and refuses the new command."""
        client = _client("X1C", new_protocol=True)

        verdict = await _refresh(client, 0, 3, ("ams_get_rfid", "fail", "not supported"), ("gcode_line", "success", ""))

        assert verdict == (True, "")
        commands = _sent(client)
        assert [s["command"] for s in commands] == ["ams_get_rfid", "gcode_line"]
        assert commands[1]["param"] == "M620 R3 \n"

    async def test_both_refused_reports_the_last_reason(self):
        client = _client("X1C", new_protocol=True)

        verdict = await _refresh(
            client, 0, 3, ("ams_get_rfid", "fail", "not supported"), ("gcode_line", "fail", "busy")
        )

        assert verdict == (False, "busy")

    async def test_no_gcode_fallback_while_a_job_is_loaded(self):
        client = _client("X1C", state="RUNNING", new_protocol=True)

        verdict = await _refresh(client, 0, 3, ("ams_get_rfid", "fail", "not supported"))

        assert verdict == (False, "not supported")
        assert [s["command"] for s in _sent(client)] == ["ams_get_rfid"]

    async def test_no_gcode_fallback_for_a_unit_the_gcode_cannot_name(self):
        client = _client("X1C", new_protocol=True)

        verdict = await _refresh(client, 128, 0, ("ams_get_rfid", "fail", "nope"))

        assert verdict == (False, "nope")
        assert [s["command"] for s in _sent(client)] == ["ams_get_rfid"]

    async def test_a_refused_gcode_falls_back_to_ams_get_rfid(self):
        client = _client("P1S")

        verdict = await _refresh(
            client, 0, 1, ("gcode_line", "fail", "unknown command"), ("ams_get_rfid", "success", "")
        )

        assert verdict == (True, "")
        assert [s["command"] for s in _sent(client)] == ["gcode_line", "ams_get_rfid"]

    async def test_a_silent_gcode_is_accepted_and_leaves_no_listener(self):
        client = _client("P1S")

        assert await _refresh(client, 0, 1) == (True, "")
        assert client._ack_listeners == {} and client._rfid_gcode_acks == {}


def test_the_legacy_models():
    assert all(uses_legacy_rfid_refresh(m) for m in ("X1C", "X1", "X1E", "P1P", "P1S", "A1", "A1 Mini", "C12", "N1"))
    assert not any(uses_legacy_rfid_refresh(m) for m in ("H2D", "H2C", "X2D", "P2S", "A2L", "", None))


# ---------------------------------------------------------------- the route


async def _post(async_client, printer_id: int, client, spawned: list):
    manager = MagicMock()
    manager.get_client.return_value = client
    manager.ensure_fresh_connection_for_printer = AsyncMock(return_value=True)

    def spawn(coro, name=None):
        spawned.append(name)
        coro.close()

    with (
        patch("backend.app.api.routes.printers.printer_manager", manager),
        patch("backend.app.api.routes.printers.spawn_background_task", spawn),
    ):
        return await async_client.post(f"/api/v1/printers/{printer_id}/ams/0/slot/1/refresh")


@pytest.mark.asyncio
async def test_the_route_reports_a_refusal_and_schedules_no_pa_reapply(async_client, printer_factory):
    printer = await printer_factory(model="H2D")
    client = MagicMock()
    client.ams_refresh_tray.return_value = (True, "Refreshing AMS 0 tray 1")
    client.await_rfid_refresh = AsyncMock(return_value=(False, "tray is busy"))
    spawned: list = []

    response = await _post(async_client, printer.id, client, spawned)

    assert response.status_code == 400
    assert "tray is busy" in response.json()["detail"]
    assert spawned == []


@pytest.mark.asyncio
async def test_the_route_accepts_and_schedules_the_pa_reapply(async_client, printer_factory):
    printer = await printer_factory(model="H2D")
    client = MagicMock()
    client.ams_refresh_tray.return_value = (True, "Refreshing AMS 0 tray 1")
    client.await_rfid_refresh = AsyncMock(return_value=(True, ""))
    spawned: list = []

    response = await _post(async_client, printer.id, client, spawned)

    assert response.status_code == 200, response.text
    assert len(spawned) == 1
    client.await_rfid_refresh.assert_awaited_once_with(0, 1)
