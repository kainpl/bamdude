"""A K-profile write the printer refused is reported as refused (upstream #2718).

Saving was fire-and-forget: the client published and returned True the moment
the bytes left the process. The printer *does* answer ``extrusion_cali_set`` /
``extrusion_cali_del`` with ``result`` and, on failure, ``reason`` — and that
answer was logged at DEBUG and dropped.

The reason it could not simply be gated on is the interesting half: the answer
itself was wrong. Single-nozzle firmware returned ``result: "fail"``,
``reason: "invalid tray_id"`` on writes that demonstrably applied, because of
the ``tray_id: -1`` **we** put in the payload. The X1C validates that field,
complains, and applies the write anyway; the H2D ignores it. BambuStudio always
sends a real tray_id and defaults it to 0. So the tray_id fix is a precondition
for the ack being usable at all, and both are pinned here.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import MagicMock

import pytest

from backend.app.services.bambu_mqtt import BambuMQTTClient


def _client() -> BambuMQTTClient:
    c = BambuMQTTClient.__new__(BambuMQTTClient)
    # ``topic_publish`` is a read-only property derived from the serial.
    c.serial_number = "01P00A000000000"
    c._sequence_id = 0
    c._pending_cali_acks = {}
    c._client = MagicMock()
    c.state = MagicMock()
    c.state.connected = True
    return c


def _published(c: BambuMQTTClient) -> dict:
    return json.loads(c._client.publish.call_args[0][1])["print"]


class TestPayload:
    def test_a_single_write_sends_tray_id_zero(self):
        # -1 is what made the X1C answer "invalid tray_id" on a write it applied.
        c = _client()
        c.set_kprofile(filament_id="GFA00", name="PLA", k_value="0.020000")
        assert _published(c)["filaments"][0]["tray_id"] == 0

    def test_a_batch_write_sends_tray_id_zero_on_every_entry(self):
        c = _client()
        c.set_kprofiles_batch([{"filament_id": "GFA00", "name": "a"}, {"filament_id": "GFA01", "name": "b"}])
        assert [f["tray_id"] for f in _published(c)["filaments"]] == [0, 0]

    def test_each_writer_returns_the_sequence_id_it_published(self):
        # The routes correlate the verdict on this value, so a writer that
        # returned True would make the ack unattributable.
        c = _client()
        seq = c.set_kprofile(filament_id="GFA00", name="PLA", k_value="0.020000")
        assert seq == _published(c)["sequence_id"]
        seq2 = c.delete_kprofile(cali_idx=3, filament_id="GFA00", nozzle_id="HS00-0.4")
        assert seq2 == _published(c)["sequence_id"]
        assert seq2 != seq  # incremented, not reused

    def test_a_disconnected_client_returns_none_and_publishes_nothing(self):
        c = _client()
        c.state.connected = False
        assert c.set_kprofile(filament_id="GFA00", name="PLA", k_value="0.020000") is None
        assert c.set_kprofiles_batch([{"filament_id": "GFA00"}]) is None
        assert c.delete_kprofile(cali_idx=1, filament_id="GFA00", nozzle_id="HS00-0.4") is None
        c._client.publish.assert_not_called()

    def test_the_slot_is_registered_before_the_publish(self):
        """A reply can land on the MQTT thread before the writer returns.

        Registering after publishing would drop exactly the fastest verdicts,
        which is the failure mode hardest to reproduce and easiest to call flaky.
        """
        c = _client()
        seen: list[bool] = []
        c._client.publish.side_effect = lambda *a, **k: seen.append(bool(c._pending_cali_acks))
        c.set_kprofile(filament_id="GFA00", name="PLA", k_value="0.020000")
        assert seen == [True]


class TestAwaitAck:
    @pytest.mark.asyncio
    async def test_an_explicit_failure_is_reported_with_the_printers_own_reason(self):
        c = _client()
        seq = c.set_kprofile(filament_id="GFA00", name="PLA", k_value="0.020000")
        c._pending_cali_acks[seq] = {"result": "fail", "reason": "invalid nozzle_id"}
        assert await c.await_cali_ack(seq, timeout=0.5) == (False, "invalid nozzle_id")

    @pytest.mark.asyncio
    async def test_success_is_success(self):
        c = _client()
        seq = c.delete_kprofile(cali_idx=1, filament_id="GFA00", nozzle_id="HS00-0.4")
        c._pending_cali_acks[seq] = {"result": "success"}
        assert await c.await_cali_ack(seq, timeout=0.5) == (True, "")

    @pytest.mark.asyncio
    async def test_silence_is_success(self):
        """No answer is not evidence of refusal.

        Firmware that never answers must not turn every save into an error —
        which would be a far louder regression than the bug being fixed.
        """
        c = _client()
        seq = c.set_kprofile(filament_id="GFA00", name="PLA", k_value="0.020000")
        assert await c.await_cali_ack(seq, timeout=0.15) == (True, "")

    @pytest.mark.asyncio
    async def test_a_failure_with_no_reason_still_says_something(self):
        c = _client()
        seq = c.set_kprofile(filament_id="GFA00", name="PLA", k_value="0.020000")
        c._pending_cali_acks[seq] = {"result": "fail"}
        ok, detail = await c.await_cali_ack(seq, timeout=0.5)
        assert ok is False
        assert detail  # an empty error message is worse than a terse one

    @pytest.mark.asyncio
    async def test_the_slot_is_released_afterwards_but_only_its_own(self):
        # Clearing the dict would drop verdicts other in-flight writes are still
        # waiting on — the same bug the K-profile *read* path had.
        c = _client()
        mine = c.set_kprofile(filament_id="GFA00", name="PLA", k_value="0.020000")
        theirs = c.delete_kprofile(cali_idx=1, filament_id="GFA01", nozzle_id="HS00-0.4")
        c._pending_cali_acks[mine] = {"result": "success"}
        await c.await_cali_ack(mine, timeout=0.5)
        assert mine not in c._pending_cali_acks
        assert theirs in c._pending_cali_acks

    @pytest.mark.asyncio
    async def test_a_verdict_arriving_late_is_still_caught(self):
        c = _client()
        seq = c.set_kprofile(filament_id="GFA00", name="PLA", k_value="0.020000")

        async def answer_late():
            await asyncio.sleep(0.1)
            c._pending_cali_acks[seq] = {"result": "fail", "reason": "busy"}

        task = asyncio.create_task(answer_late())
        assert await c.await_cali_ack(seq, timeout=1.0) == (False, "busy")
        await task

    @pytest.mark.asyncio
    async def test_nothing_was_sent_so_there_is_nothing_to_wait_for(self):
        c = _client()
        assert await c.await_cali_ack(None) == (True, "")


class TestAckRouting:
    def test_a_response_fills_the_slot_it_names(self):
        c = _client()
        c._pending_cali_acks = {"7": None, "8": None}
        c._route_ack({"command": "extrusion_cali_set", "sequence_id": "8", "result": "fail", "reason": "nope"})
        assert c._pending_cali_acks["7"] is None
        assert c._pending_cali_acks["8"]["reason"] == "nope"

    def test_an_unrequested_ack_is_not_accumulated(self):
        # Writing every ack into the dict would grow it for the process lifetime.
        c = _client()
        c._route_ack({"command": "extrusion_cali_del", "sequence_id": "99", "result": "success"})
        assert c._pending_cali_acks == {}
