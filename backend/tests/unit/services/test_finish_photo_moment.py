"""Tests for the stage-driven finish-photo trigger (#1721).

Replaces the removed force-timelapse machinery. ``bambu_mqtt.py`` fires
``on_finish_photo_moment`` when ``stg_cur`` transitions INTO 22 ("Filament
unloading") at end-of-print (progress>=99 / layer>=total / remaining<=0),
with a FINISH-state fallback for prints that skip stage 22. These tests
drive the client through ``_process_message`` the same way the timelapse
full-flow tests do.
"""

import pytest


class TestFinishPhotoMoment:
    @pytest.fixture
    def mqtt_client(self):
        from backend.app.services.bambu_mqtt import BambuMQTTClient

        return BambuMQTTClient(
            ip_address="192.168.1.100",
            serial_number="TEST123",
            access_code="12345678",
        )

    def _start_running(self, client):
        """Drive an IDLE->RUNNING transition so ``_was_running`` is True."""
        client._previous_gcode_state = "IDLE"
        client._process_message(
            {
                "print": {
                    "gcode_state": "RUNNING",
                    "gcode_file": "/data/Metadata/test.gcode",
                    "subtask_name": "Test",
                    "total_layer_num": 100,
                }
            }
        )
        assert client._was_running is True

    def test_stage_22_end_of_print_fires_callback(self, mqtt_client):
        """stg_cur -> 22 at progress>=99 fires on_finish_photo_moment."""
        moments = []
        mqtt_client.on_finish_photo_moment = lambda data: moments.append(data)

        self._start_running(mqtt_client)

        # End of print: high progress, all layers done, then stage 22.
        mqtt_client._process_message(
            {
                "print": {
                    "gcode_state": "RUNNING",
                    "gcode_file": "/data/Metadata/test.gcode",
                    "subtask_name": "Test",
                    "mc_percent": 100,
                    "layer_num": 100,
                    "total_layer_num": 100,
                    "stg_cur": 22,
                }
            }
        )

        assert len(moments) == 1
        assert moments[0]["trigger"] == "stage_22"
        assert mqtt_client._finish_photo_captured is True

    def test_mid_print_stage_22_does_not_fire(self, mqtt_client):
        """A mid-print color swap transits stage 22 at progress<99 — must NOT fire."""
        moments = []
        mqtt_client.on_finish_photo_moment = lambda data: moments.append(data)

        self._start_running(mqtt_client)

        # Mid-print swap: low progress, plenty of layers/time left.
        mqtt_client._process_message(
            {
                "print": {
                    "gcode_state": "RUNNING",
                    "gcode_file": "/data/Metadata/test.gcode",
                    "subtask_name": "Test",
                    "mc_percent": 40,
                    "layer_num": 20,
                    "total_layer_num": 100,
                    "mc_remaining_time": 30,
                    "stg_cur": 22,
                }
            }
        )

        assert moments == []
        assert mqtt_client._finish_photo_captured is False

    def test_finish_fallback_fires_once_when_stage_22_never_arrives(self, mqtt_client):
        """No stage 22 seen — the FINISH-state fallback fires exactly once."""
        moments = []
        mqtt_client.on_finish_photo_moment = lambda data: moments.append(data)
        # Completion detection is gated on a wired on_print_complete callback.
        mqtt_client.on_print_complete = lambda data: None

        self._start_running(mqtt_client)

        # Jump straight to FINISH without ever passing stage 22.
        mqtt_client._process_message(
            {
                "print": {
                    "gcode_state": "FINISH",
                    "gcode_file": "/data/Metadata/test.gcode",
                    "subtask_name": "Test",
                }
            }
        )

        assert len(moments) == 1
        assert moments[0]["trigger"] == "finish_state"
        assert mqtt_client._finish_photo_captured is True

    def test_stage_22_then_finish_fires_only_once(self, mqtt_client):
        """If the stage-22 edge already fired, the FINISH fallback must not refire."""
        moments = []
        mqtt_client.on_finish_photo_moment = lambda data: moments.append(data)
        mqtt_client.on_print_complete = lambda data: None

        self._start_running(mqtt_client)

        # End-of-print stage 22 fires the stage_22 trigger.
        mqtt_client._process_message(
            {
                "print": {
                    "gcode_state": "RUNNING",
                    "gcode_file": "/data/Metadata/test.gcode",
                    "subtask_name": "Test",
                    "mc_percent": 100,
                    "layer_num": 100,
                    "total_layer_num": 100,
                    "stg_cur": 22,
                }
            }
        )
        # Then the print reaches FINISH — no second callback.
        mqtt_client._process_message(
            {
                "print": {
                    "gcode_state": "FINISH",
                    "gcode_file": "/data/Metadata/test.gcode",
                    "subtask_name": "Test",
                }
            }
        )

        assert len(moments) == 1
        assert moments[0]["trigger"] == "stage_22"

    def test_finish_photo_captured_flag_initializes_false(self, mqtt_client):
        assert mqtt_client._finish_photo_captured is False
