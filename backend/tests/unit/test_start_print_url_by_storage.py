"""The print URL is chosen by the medium the file actually went to.

⚠️ The two forms are not one form with the storage substituted: internal is
``brtc://<storage>/<name>`` with no path at all, external is ``ftp://<name>``.
``brtc://udisk/…`` does not exist — BambuStudio sends ``file:///media/usb0/…``
for a removable medium, and we never print from one over that channel.
"""

import json
from unittest.mock import MagicMock

import pytest

from backend.app.services.bambu_mqtt import BambuMQTTClient


def _client_capturing() -> tuple[BambuMQTTClient, list[dict]]:
    client = BambuMQTTClient.__new__(BambuMQTTClient)
    # ``topic_publish`` is a read-only property derived from the serial.
    client.serial_number = "TEST123"
    client.model = "X2D"
    client._is_dual_nozzle = False
    client.state = type(
        "S",
        (),
        {
            "state": "IDLE",
            "connected": True,
            "gcode_state": "IDLE",
            "nozzle_diameter": 0.4,
        },
    )()
    sent: list[dict] = []
    publisher = MagicMock()
    publisher.publish.side_effect = lambda _topic, payload, qos=1: sent.append(json.loads(payload))
    client._client = publisher
    return client, sent


@pytest.mark.parametrize(
    ("storage", "expected_url", "expected_md5"),
    [
        ("external", "ftp://job.gcode.3mf", ""),
        ("internal", "brtc://emmc/job.gcode.3mf", "ABC123"),
    ],
)
def test_the_url_scheme_follows_the_storage(storage, expected_url, expected_md5):
    client, sent = _client_capturing()

    client.start_print("job.gcode.3mf", storage=storage, file_md5="abc123")

    payload = sent[-1]["print"]
    assert payload["url"] == expected_url
    assert payload["md5"] == expected_md5


def test_the_internal_md5_is_uppercase_and_the_upload_one_was_not():
    """One digest, two spellings, decided by which channel carries it."""
    client, sent = _client_capturing()
    client.start_print("job.gcode.3mf", storage="internal", file_md5="deadbeef")
    assert sent[-1]["print"]["md5"] == "DEADBEEF"


def test_external_keeps_todays_empty_md5():
    """Unchanged on every printer that has a card — this stage must not alter
    the FTP payload in any way."""
    client, sent = _client_capturing()
    client.start_print("job.gcode.3mf")
    assert sent[-1]["print"]["md5"] == ""
    assert sent[-1]["print"]["url"] == "ftp://job.gcode.3mf"


def test_the_file_field_is_the_bare_name_on_both_media():
    """`url` differs; `file` does not. Changing both is the natural mistake."""
    for storage in ("external", "internal"):
        client, sent = _client_capturing()
        client.start_print("job.gcode.3mf", storage=storage, file_md5="abc")
        assert sent[-1]["print"]["file"] == "job.gcode.3mf"


def test_an_internal_print_with_no_digest_still_sends_the_field():
    """⚠️ Empty rather than absent. Older firmware rejects the command when a
    key it expects is missing, which is why md5 is sent even on the FTP path."""
    client, sent = _client_capturing()
    client.start_print("job.gcode.3mf", storage="internal")
    assert sent[-1]["print"]["md5"] == ""
