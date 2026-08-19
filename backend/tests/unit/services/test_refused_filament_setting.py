"""A refused AMS filament setting must leave a trace support can read.

Ported from upstream #2756. Configuring a slot publishes ``ams_filament_setting``
and the printer answers with a verdict. The answer was received and dropped at
DEBUG, so a refusal left nothing at the level support bundles are collected at:
six Configure Slot attempts on an X1C all reported success, all read back as
still holding the previous profile, and there was no record of what the printer
had said about any of them.

⚠️ Refusals only. Unlike the drying reply — rare, user-initiated — this command
goes out on every spool assignment and every K-profile re-apply, so promoting
each acknowledgement would bury the interesting line under the routine ones.
"""

from __future__ import annotations

import logging

import pytest

import backend.app.models.printer_location  # noqa: F401
from backend.app.services.bambu_mqtt import BambuMQTTClient


@pytest.fixture
def client():
    return BambuMQTTClient(
        ip_address="192.168.1.100",
        serial_number="TEST123",
        access_code="12345678",
    )


def _reply(client, **fields) -> None:
    payload = {"print": {"command": "ams_filament_setting", "sequence_id": "0"}}
    payload["print"].update(fields)
    client._process_message(payload)


def _refusals(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if "ams_filament_setting refused" in r.getMessage()]


class TestARefusalIsReported:
    def test_at_a_level_a_support_bundle_collects(self, client, caplog):
        with caplog.at_level(logging.INFO, logger="backend.app.services.bambu_mqtt"):
            _reply(client, result="fail", reason="invalid tray_id", ams_id=0, tray_id=2)

        assert len(_refusals(caplog)) == 1

    def test_it_carries_what_the_printer_said(self, client, caplog):
        """The verdict alone is not diagnosable — the reason and the slot are
        what tell one refusal from another."""
        with caplog.at_level(logging.INFO, logger="backend.app.services.bambu_mqtt"):
            _reply(client, result="fail", reason="invalid tray_id", ams_id=1, tray_id=3)

        message = _refusals(caplog)[0]
        assert "result=fail" in message
        assert "invalid tray_id" in message
        assert "ams_id=1" in message
        assert "tray_id=3" in message

    def test_a_missing_reason_does_not_swallow_the_line(self, client, caplog):
        with caplog.at_level(logging.INFO, logger="backend.app.services.bambu_mqtt"):
            _reply(client, result="fail")

        assert len(_refusals(caplog)) == 1


class TestTheRoutineCaseStaysQuiet:
    def test_a_success_is_not_promoted(self, client, caplog):
        """⚠️ Every spool assignment and K-profile re-apply sends one of these."""
        with caplog.at_level(logging.INFO, logger="backend.app.services.bambu_mqtt"):
            _reply(client, result="success", ams_id=0, tray_id=0)

        assert _refusals(caplog) == []

    def test_the_verdict_is_matched_case_insensitively(self, client, caplog):
        with caplog.at_level(logging.INFO, logger="backend.app.services.bambu_mqtt"):
            _reply(client, result="SUCCESS")

        assert _refusals(caplog) == []

    def test_a_reply_with_no_verdict_at_all_is_not_a_refusal(self, client, caplog):
        """Absence of a verdict is not a verdict — inventing one would put a
        false refusal in the bundle every time the shape changed."""
        with caplog.at_level(logging.INFO, logger="backend.app.services.bambu_mqtt"):
            _reply(client, ams_id=0)

        assert _refusals(caplog) == []


class TestTheDeveloperModeProbeIsExcluded:
    def test_its_refusal_is_a_reading_not_a_fault(self, client, caplog):
        """⚠️ The probe sends this exact command to the external slot precisely
        to watch it be refused on P1 firmware. Promoting that would put an
        alarming line in every P1 bundle on every reconnect."""
        client._dev_mode_probe_seq = "77"

        with caplog.at_level(logging.INFO, logger="backend.app.services.bambu_mqtt"):
            _reply(client, result="fail", reason="not supported", sequence_id="77")

        assert _refusals(caplog) == []

    def test_a_user_command_is_still_reported_while_a_probe_is_outstanding(self, client, caplog):
        """A user command cannot be mistaken for the probe: those publish a
        hardcoded sequence id of "0"."""
        client._dev_mode_probe_seq = "77"

        with caplog.at_level(logging.INFO, logger="backend.app.services.bambu_mqtt"):
            _reply(client, result="fail", reason="invalid tray_id", sequence_id="0")

        assert len(_refusals(caplog)) == 1
