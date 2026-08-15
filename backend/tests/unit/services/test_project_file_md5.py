"""The digest BamDude puts in ``project_file``, on both media.

⚠️ **Why this changed.** The FTP path sent `"md5": ""` for as long as the code
existed, and the reasoning was recorded and reasonable: Bambu's own captures put
the literal ``"from_sd_card"`` there for a print off removable media, so there
was no digest to copy and no evidence the field was read at all.

Orca disproves it. Captured off a P1S on 2026-08-16, its own ``project_file``
carries a real digest against an ``ftp://`` url — the same command, the same
medium and the same field we were sending empty:

    "url": "ftp://W71997_….gcode_plate_3.gcode.3mf",
    "md5": "B80E980FC407C0D09B69C81B01D5100E"

⚠️ Uppercase in this command, lowercase in the file tunnel's own upload frame.
Same digest, two spellings, and each channel wants its own — so the case is
applied at the command rather than at the source.
"""

from __future__ import annotations

import hashlib

import pytest


def _digest(path) -> str:
    from backend.app.services.background_dispatch import _file_digest

    return _file_digest(str(path))


class TestTheDigestWeCompute:
    def test_it_is_the_md5_of_the_bytes_we_uploaded(self, tmp_path) -> None:
        f = tmp_path / "plate.3mf"
        f.write_bytes(b"whatever went up the wire")

        assert _digest(f) == hashlib.md5(b"whatever went up the wire", usedforsecurity=False).hexdigest()

    def test_it_comes_back_lowercase(self, tmp_path) -> None:
        """⚠️ Deliberate: the tunnel's upload frame wants it this way and the
        MQTT command upper-cases at the point of use. Flipping it here would
        silently change the other channel."""
        f = tmp_path / "plate.3mf"
        f.write_bytes(b"x" * 4096)

        assert _digest(f) == _digest(f).lower()

    def test_a_large_file_is_read_in_blocks_not_slurped(self, tmp_path) -> None:
        """A 3MF is tens of megabytes and this runs on the dispatch path."""
        f = tmp_path / "big.3mf"
        f.write_bytes(b"ab" * (3 * 1024 * 1024))

        assert _digest(f) == hashlib.md5(b"ab" * (3 * 1024 * 1024), usedforsecurity=False).hexdigest()


class TestWhatTheCommandCarries:
    """Driven through ``start_print`` and read off the publish, because the
    question is what reaches the printer — not what an expression evaluates to."""

    @staticmethod
    def _published(storage: str, file_md5: str) -> dict:
        import json
        from unittest.mock import MagicMock

        from backend.app.services.bambu_mqtt import BambuMQTTClient

        client = BambuMQTTClient(ip_address="192.168.0.9", serial_number="01P00A000000000", access_code="00000000")
        client._client = MagicMock()
        client.state.connected = True
        client.start_print("plate.gcode.3mf", plate_id=1, storage=storage, file_md5=file_md5)

        for call in client._client.publish.call_args_list:
            payload = json.loads(call.args[1])
            if payload.get("print", {}).get("command") == "project_file":
                return payload["print"]
        raise AssertionError("no project_file command was published")

    @pytest.mark.parametrize("storage", ["internal", "external"])
    def test_both_media_carry_the_digest_uppercased(self, storage: str) -> None:
        """⚠️ ``external`` is the FTP path — the one that used to send "". Orca
        sends a real digest there, which is what reopened this."""
        cmd = self._published(storage, "b80e980fc407c0d09b69c81b01d5100e")

        assert cmd["md5"] == "B80E980FC407C0D09B69C81B01D5100E"

    def test_the_key_is_present_even_with_no_digest(self) -> None:
        """⚠️ Never omit it. Older firmware rejects a command missing a key it
        expects, which presents as a print that silently never starts."""
        cmd = self._published("external", "")

        assert "md5" in cmd
        assert cmd["md5"] == ""
