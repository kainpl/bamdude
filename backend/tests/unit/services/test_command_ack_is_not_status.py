"""A command's acknowledgement is not the printer's status (upstream #3040, 0b830ac3).

The printer echoes a command's fields back in its ack on the report topic, and
the ack went through the same parser as a status push. Our ``project_file`` —
and Bambu Studio's — carries ``cfg`` (the per-job storage bit: "4" or "0") and
the per-job ``timelapse`` request, so ~25 ms after every dispatch the parser
read OUR request back as the printer's device configuration:

* the AMS settings in ``cfg`` bits 0/1/17/18 (detect on insert / power-on /
  remaining capacity / auto-refill) all read OFF;
* every print option BamDude decodes from ``cfg`` (door check, sound, tangle
  and blob detection, save-sent-files, snapshot, AI monitoring, …) read OFF;
* support for "store sent files" turned ON — P1 and A1 send no ``cfg`` in their
  status at all, which is how that row stays hidden for them;
* the recorder's state became whatever timelapse the job asked for.

Printers that repeat ``cfg`` in their periodic status corrected themselves a
second later; the P1 and A1 families send it only in a full dump, so the wrong
values stuck.
"""

from __future__ import annotations

import json

import pytest

from backend.app.services.bambu_mqtt import BambuMQTTClient, is_printer_status_frame


class _Msg:
    """A report-topic frame, as paho hands it over."""

    def __init__(self, payload: dict):
        self.topic = ""
        self.payload = json.dumps(payload).encode("utf-8")


@pytest.fixture
def client() -> BambuMQTTClient:
    c = BambuMQTTClient(ip_address="192.168.1.100", serial_number="TESTACK0001", access_code="12345678")
    c.state.connected = True
    return c


def _ack(sequence_id: str = "20000", **fields) -> dict:
    """The printer's echo of a project_file — ours ("20000") or a slicer's."""
    return {
        "print": {
            "command": "project_file",
            "sequence_id": sequence_id,
            "result": "success",
            "cfg": "0",
            "timelapse": False,
            "subtask_name": "cube",
            **fields,
        }
    }


def _feed(client: BambuMQTTClient, payload: dict) -> None:
    client._on_message(None, None, _Msg(payload))


@pytest.mark.parametrize("sequence_id", ["20000", "123456"], ids=["ours", "a slicer's"])
def test_the_ams_settings_survive_a_dispatch_ack(client, sequence_id):
    client.state.ams_insertion_update = True
    client.state.ams_power_on_update = True
    client.state.ams_remain_capacity = True
    client.state.ams_auto_switch_filament = True

    _feed(client, _ack(sequence_id))

    assert client.state.ams_insertion_update is True
    assert client.state.ams_power_on_update is True
    assert client.state.ams_remain_capacity is True
    assert client.state.ams_auto_switch_filament is True


def test_the_print_options_decoded_from_cfg_survive_a_dispatch_ack(client):
    po = client.state.print_options
    po.sound_enable = True
    po.open_door_check = 2
    po.ai_monitoring_sensitivity = "high"
    po.snapshot_enabled = True

    _feed(client, _ack())

    assert (po.sound_enable, po.open_door_check, po.ai_monitoring_sensitivity, po.snapshot_enabled) == (
        True,
        2,
        "high",
        True,
    )


def test_a_dispatch_ack_does_not_advertise_store_sent_files_on_a_p1(client):
    """P1 and A1 send no top-level cfg; the row stays hidden for them."""
    _feed(client, _ack(cfg="4", timelapse=True))

    assert "save_remote_to_storage" not in client.state.print_option_support


def test_the_recorder_state_is_not_the_timelapse_the_job_asked_for(client):
    client.state.timelapse = True

    _feed(client, _ack(timelapse=False))

    assert client.state.timelapse is True


def test_a_status_push_is_still_read(client):
    _feed(client, {"print": {"command": "push_status", "cfg": "40000", "timelapse": True}})

    assert client.state.ams_auto_switch_filament is True
    assert client.state.timelapse is True


def test_a_status_frame_without_a_command_is_still_read(client):
    """Some firmware omits ``command`` on a status frame."""
    _feed(client, {"print": {"cfg": "40000"}})

    assert client.state.ams_auto_switch_filament is True


@pytest.mark.parametrize(
    ("frame", "is_status"),
    [
        ({"command": "push_status"}, True),
        ({"gcode_state": "RUNNING"}, True),
        ({"command": "project_file", "result": "success"}, False),
        ({"command": "print_option", "sound_enable": True}, False),
    ],
)
def test_which_frames_describe_the_printer(frame, is_status):
    assert is_printer_status_frame(frame) is is_status
