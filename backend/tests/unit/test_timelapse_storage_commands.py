"""Asking the printer about timelapse room, and refusing to promise one it
cannot keep.

Registry N2. Two commands on BambuStudio's third MQTT envelope — ``camera``,
beside ``print`` and ``system`` — plus the veto that keeps the checkbox honest
when the request does not come from the browser.

⚠️ **``ipcam_get_media_info`` is a nudge, not a query.** The answer arrives in
the next status push as ``device.cam.tl_*_free_kb``; nothing comes back as a
reply. A caller that treats it as a request/response waits for a message that
never arrives.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from backend.app.services.background_dispatch import _timelapse_or_off
from backend.app.services.bambu_mqtt import BambuMQTTClient
from backend.app.services.print_scheduler import _timelapse_storage_full


def _client(model: str = "X2D") -> BambuMQTTClient:
    c = BambuMQTTClient(ip_address="1.2.3.4", serial_number="TL1", access_code="12345678", model=model)
    c._client = MagicMock()
    c.state.connected = True
    return c


def _published(c: BambuMQTTClient) -> dict:
    return json.loads(c._client.publish.call_args[0][1])


class TestTheCameraEnvelope:
    def test_the_storage_question_is_bs_shape(self) -> None:
        c = _client()
        assert c.check_timelapse_storage("internal", 250) is True

        p = _published(c)["camera"]
        assert p["command"] == "ipcam_get_media_info"
        assert p["sub_command"] == "is_timelapse_storage_enough"
        assert p["storage"] == "internal"
        assert p["total_layer"] == 250
        assert "sequence_id" in p

    def test_the_delete_is_bs_shape(self) -> None:
        c = _client()
        assert c.delete_oldest_timelapse("external", 40) is True

        p = _published(c)["camera"]
        assert p["command"] == "ipcam_delete_oldest_timelapse"
        assert p["storage"] == "external"
        assert p["total_layer"] == 40

    def test_it_is_not_the_print_envelope(self) -> None:
        """⚠️ A third namespace. Putting these under ``print`` would be accepted
        by nothing."""
        c = _client()
        c.check_timelapse_storage("internal", 10)

        assert "print" not in _published(c)

    def test_nothing_is_sent_while_disconnected(self) -> None:
        c = _client()
        c.state.connected = False

        assert c.check_timelapse_storage("internal", 10) is False
        c._client.publish.assert_not_called()


class TestTheParsedInputs:
    def test_the_kit_is_aux_bit_26(self) -> None:
        c = _client()
        c._update_state({"aux": hex(1 << 26)})

        assert c.state.has_timelapse_kit is True

    def test_internal_support_is_fun_bit_28(self) -> None:
        # The capability bits live in their own parser, not in _update_state.
        c = _client()
        c._parse_print_option_support({"fun": hex(1 << 28)})

        assert c.state.print_option_support["internal_timelapse"] is True

    def test_free_space_comes_from_the_camera_block(self) -> None:
        c = _client()
        c._update_state({"device": {"cam": {"tl_internal_free_kb": 512, "tl_internal_total_kb": 4096}}})

        assert c.state.timelapse_storage["tl_internal_free_kb"] == 512
        assert c.state.timelapse_storage["tl_internal_total_kb"] == 4096

    def test_a_push_without_the_camera_block_leaves_it_alone(self) -> None:
        """Sparse pushes are the norm; a missing block is not zero free space."""
        c = _client()
        c._update_state({"device": {"cam": {"tl_internal_free_kb": 512}}})
        c._update_state({"bed_temper": 60})

        assert c.state.timelapse_storage["tl_internal_free_kb"] == 512


class TestTheDispatcherVeto:
    """⚠️ The browser disabling the checkbox is not the guard: this path is also
    reached by API key, by the Telegram bot, and by a queue item created before
    somebody pulled the card out."""

    def test_a_request_the_printer_cannot_keep_is_turned_off(self) -> None:
        client = _client(model="P1S")
        client.state.sdcard_state = 0  # card pulled
        with patch("backend.app.services.printer_manager.printer_manager") as pm:
            pm.get_client.return_value = client

            assert _timelapse_or_off(1, SimpleNamespace(model="P1S"), True) is False

    def test_a_request_it_can_keep_goes_through(self) -> None:
        client = _client(model="P1S")
        client.state.sdcard_state = 1
        with patch("backend.app.services.printer_manager.printer_manager") as pm:
            pm.get_client.return_value = client

            assert _timelapse_or_off(1, SimpleNamespace(model="P1S"), True) is True

    def test_it_never_turns_anything_on(self) -> None:
        """The veto only subtracts. A print nobody asked to record must not
        start recording because the printer happens to be able to."""
        client = _client(model="X2D")
        with patch("backend.app.services.printer_manager.printer_manager") as pm:
            pm.get_client.return_value = client

            assert _timelapse_or_off(1, SimpleNamespace(model="X2D"), False) is False

    def test_an_unknown_printer_is_left_as_asked(self) -> None:
        """⚠️ No client means no evidence, and no evidence is not a refusal —
        the printer is the one that would ignore the flag anyway."""
        with patch("backend.app.services.printer_manager.printer_manager") as pm:
            pm.get_client.return_value = None

            assert _timelapse_or_off(1, SimpleNamespace(model="P1S"), True) is True


class TestTheQueuePause:
    """⚠️ A queue that pauses is a farm decision, not BambuStudio's. Studio
    offers to delete the oldest recording or to untick the box — both need
    somebody present. Unattended, the only choice that does not quietly discard
    what was asked for is to stop and say so.

    ⚠️ And only the SPACE question pauses. Whether the machine can record at all
    is asked in the scheduling dialog, where a person is picking printers:
    pausing for a missing card would strand work that nothing here can unstick.
    """

    def _with(self, client):
        return patch("backend.app.services.printer_manager.printer_manager", **{"get_client.return_value": client})

    def test_a_full_internal_store_pauses(self) -> None:
        c = _client(model="X2D")
        c.state.print_option_support["internal_timelapse"] = True
        c.state.timelapse_storage = {"tl_internal_free_kb": 1024}
        with self._with(c):
            assert _timelapse_storage_full(1) is True

    def test_room_to_spare_does_not(self) -> None:
        c = _client(model="X2D")
        c.state.print_option_support["internal_timelapse"] = True
        c.state.timelapse_storage = {"tl_internal_free_kb": 999999}
        with self._with(c):
            assert _timelapse_storage_full(1) is False

    def test_a_figure_nobody_reported_does_not_pause(self) -> None:
        """⚠️ The important one. Stranding a farm on a number nobody has is
        worse than the missing video the pause exists to protect."""
        c = _client(model="P1S")
        with self._with(c):
            assert _timelapse_storage_full(1) is False

    def test_an_offline_printer_does_not_pause(self) -> None:
        with self._with(None):
            assert _timelapse_storage_full(1) is False

    def test_each_machine_is_measured_where_it_writes(self) -> None:
        """An SD-card machine is judged on the external figure, not the internal
        one — reading the wrong field would pause on somebody else's disk."""
        c = _client(model="P1S")
        c.state.timelapse_storage = {"tl_internal_free_kb": 10, "tl_external_free_kb": 999999}
        with self._with(c):
            assert _timelapse_storage_full(1) is False


class TestTheGuardSitsWhereDispatchIsDecided:
    def test_check_queue_pauses_rather_than_dropping_the_timelapse(self) -> None:
        import inspect

        from backend.app.services import print_scheduler

        src = inspect.getsource(print_scheduler.PrintScheduler.check_queue)
        src = chr(10).join(ln for ln in src.splitlines() if not ln.lstrip().startswith("#"))
        assert "_timelapse_storage_full" in src
        assert "is_paused = True" in src
