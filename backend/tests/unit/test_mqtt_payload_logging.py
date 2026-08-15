"""What a command payload may and may not carry into a log file.

The logging exists for one open question — whether a command is fresh or a
replay of one already answered — and only ``sequence_id`` separates those, so
the payload has to be written down whole.

⚠️ Which makes masking load-bearing. A debug log is the thing an operator
pastes into an issue, and ``url`` carries userinfo on some transfer commands.
A leak here is silent: nobody reads their own logs closely enough to notice a
password in them until it is already public.
"""

from __future__ import annotations

import json

from backend.app.services.bambu_mqtt import _loggable


class TestWhatGetsMasked:
    def test_a_credential_key_never_reaches_the_log(self) -> None:
        out = _loggable({"command": "project_file", "access_code": "12345678", "password": "hunter2"})

        assert "12345678" not in out
        assert "hunter2" not in out
        assert out.count("***") == 2

    def test_masking_is_by_key_name_at_any_depth(self) -> None:
        """Nested, because a printer payload is nested — ``print.upload.password``
        is exactly the shape that a top-level-only sweep would miss."""
        out = _loggable({"print": {"upload": {"password": "hunter2", "host": "192.168.1.10"}}})

        assert "hunter2" not in out
        assert "192.168.1.10" in out, "masking must not swallow the fields being investigated"

    def test_userinfo_in_a_url_is_stripped_but_the_host_survives(self) -> None:
        """⚠️ The host is the point of logging a URL at all: it says whether the
        job came from us, from the cloud, or from somebody's Studio."""
        out = _loggable({"url": "ftp://bblp:secretcode@192.168.1.10/model.3mf"})

        assert "secretcode" not in out
        assert "bblp" not in out
        assert "192.168.1.10/model.3mf" in out

    def test_an_ordinary_payload_passes_through_intact(self) -> None:
        payload = {"command": "extrusion_cali_get", "sequence_id": "2031", "filament_id": ""}

        assert json.loads(_loggable(payload)) == payload


class TestKeepingTheLogReadable:
    def test_a_huge_payload_is_truncated_and_says_so(self) -> None:
        """A single pushall response is tens of kilobytes. Left whole it buries
        the line the log is being read for."""
        out = _loggable({"data": ["x" * 100 for _ in range(200)]})

        assert len(out) < 2200
        assert "chars)" in out

    def test_something_json_cannot_encode_is_still_logged(self) -> None:
        """⚠️ Falls back to repr rather than raising. This runs inside the MQTT
        message handler: an exception here would cost the message, and losing
        state updates to a *logging* call is a far worse trade than an ugly line.
        """
        out = _loggable({"command": "x", "when": object()})

        assert "command" in out
