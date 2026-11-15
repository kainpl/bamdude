"""A K command's answer is not the printer's status; an unanswered write is not "confirmed".

Upstream f45cc599f (legs 2, 3) + ab5ccc108 (leg 2).

1. We put ``nozzle_diameter`` on ``extrusion_cali_set`` / ``_del`` / ``_sel``; the
   printer's answer travels in the same ``print`` envelope as a status push, and
   ``_update_state`` took a top-level ``nozzle_diameter`` as the fitted hardware.
   Saving a 0.6 profile on a printer with 0.4 fitted made ``slot_nozzle`` hand
   0.6 to every binder until the next full status. BambuStudio reads only
   ``cali_idx`` from the ``extrusion_cali_sel`` answer (``DevCalib.cpp``) and the
   nozzle only from status. Code parity with BambuStudio and upstream; whether
   current firmware echoes the diameter was not measured — the guard is right
   either way.
2. Silence stays ``ok`` (firmware that never answers must not fail every save),
   but it is now told apart from a confirmed write.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from backend.app.services.bambu_mqtt import BambuMQTTClient


def _real_client() -> BambuMQTTClient:
    client = BambuMQTTClient(ip_address="192.168.1.100", serial_number="TEST123", access_code="12345678")
    client._process_message(
        {"print": {"command": "push_status", "nozzle_diameter": "0.4", "nozzle_type": "stainless_steel"}}
    )
    assert client.state.nozzles[0].nozzle_diameter == "0.4"
    return client


class TestAnswerDoesNotChangeTheNozzle:
    @pytest.mark.parametrize("command", ["extrusion_cali_set", "extrusion_cali_del", "extrusion_cali_sel"])
    def test_the_echoed_diameter_of_a_k_command_is_not_the_fitted_nozzle(self, command):
        client = _real_client()
        client._process_message(
            {
                "print": {
                    "command": command,
                    "nozzle_diameter": "0.6",
                    "nozzle_type": "hardened_steel",
                    "result": "success",
                    "sequence_id": "77",
                }
            }
        )
        assert client.state.nozzles[0].nozzle_diameter == "0.4"
        assert client.state.nozzles[0].nozzle_type == "stainless_steel"

    def test_a_status_push_still_reports_a_changed_nozzle(self):
        client = _real_client()
        client._process_message({"print": {"command": "push_status", "nozzle_diameter": "0.6"}})
        assert client.state.nozzles[0].nozzle_diameter == "0.6"

    def test_a_status_frame_without_a_command_still_reports_it(self):
        """Older firmware omits ``command`` on a status frame."""
        client = _real_client()
        client._process_message({"print": {"nozzle_diameter": "0.8"}})
        assert client.state.nozzles[0].nozzle_diameter == "0.8"


def _ack_client() -> BambuMQTTClient:
    c = BambuMQTTClient.__new__(BambuMQTTClient)
    c.serial_number = "01P00A000000000"
    c._sequence_id = 0
    c._pending_cali_acks = {}
    c._client = MagicMock()
    c.state = MagicMock()
    c.state.connected = True
    return c


@pytest.mark.asyncio
class TestVerdictSaysWhetherItWasConfirmed:
    async def test_silence_is_ok_but_not_confirmed(self):
        c = _ack_client()
        seq = c.set_kprofile(filament_id="GFA00", name="PLA", k_value="0.020000")
        assert await c.await_cali_verdict(seq, timeout=0.15) == (True, "", False)

    async def test_an_answered_success_is_confirmed(self):
        c = _ack_client()
        seq = c.set_kprofile(filament_id="GFA00", name="PLA", k_value="0.020000")
        c._pending_cali_acks[seq] = {"result": "success"}
        assert await c.await_cali_verdict(seq, timeout=0.5) == (True, "", True)

    async def test_a_refusal_is_a_confirmed_failure(self):
        c = _ack_client()
        seq = c.set_kprofile(filament_id="GFA00", name="PLA", k_value="0.020000")
        c._pending_cali_acks[seq] = {"result": "fail", "reason": "busy"}
        assert await c.await_cali_verdict(seq, timeout=0.5) == (False, "busy", True)

    async def test_a_write_that_never_left_is_not_confirmed(self):
        c = _ack_client()
        assert await c.await_cali_verdict(None) == (True, "", False)

    async def test_the_old_two_value_answer_is_unchanged(self):
        """Git restore and every older caller keep reading ``(ok, detail)``."""
        c = _ack_client()
        seq = c.set_kprofile(filament_id="GFA00", name="PLA", k_value="0.020000")
        assert await c.await_cali_ack(seq, timeout=0.15) == (True, "")


class TestWhetherSilenceIsWorthSaying:
    """Firmware that never answers a K write must not have every save called "not confirmed": silence
    is said only on a connection that has answered such a write before."""

    def test_a_connection_that_never_answered_does_not_claim_to(self):
        c = _ack_client()
        c.state.connection_generation = 7
        assert c.answers_cali_writes is False

    def test_an_answer_proves_this_connection_answers(self):
        c = _ack_client()
        c.state.connection_generation = 7
        c._route_ack({"command": "extrusion_cali_set", "result": "success", "sequence_id": "5"})
        assert c.answers_cali_writes is True

    def test_a_new_connection_has_proven_nothing(self):
        c = _ack_client()
        c.state.connection_generation = 7
        c._route_ack({"command": "extrusion_cali_del", "result": "success", "sequence_id": "5"})
        c.state.connection_generation = 8
        assert c.answers_cali_writes is False
