"""Two firmware states in which the printer takes no work, and we showed neither.

Registry N3, split in half after reconnaissance. BS sends ``upgrade_confirm``
and ``consistency_confirm`` to answer these, and the confirmations themselves are
mostly reachable only through a cloud-pushed firmware — which a LAN-only farm
never receives, so they were nearly written off as irrelevant.

⚠️ **The flags are not.** ``DevUpgrade::ParseV1_0`` reads both from
``print.upgrade_state``, the same ordinary MQTT push everything else here comes
from. No account, no cloud.

And one of them is reachable exactly on an offline farm.
``consistency_request`` is a module version MISMATCH — BS's wording is *"The
firmware version is abnormal. Repairing and updating are required before
printing"* — which is what an SD-card update can leave behind when one module
takes the new firmware and another does not. That is the path BamDude's own
bulk-firmware feature uses.

Reading them does not require being able to answer them. Before this, a printer
in either state accepted no jobs and its card looked entirely ordinary.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from backend.app.services.bambu_mqtt import BambuMQTTClient


def _client() -> BambuMQTTClient:
    c = BambuMQTTClient(ip_address="1.2.3.4", serial_number="P1S001", access_code="12345678", model="P1S")
    c._client = MagicMock()
    c.state.connected = True
    return c


class TestTheFlagsAreRead:
    def test_a_consistency_request_is_seen(self) -> None:
        c = _client()
        c._update_state({"upgrade_state": {"status": "IDLE", "consistency_request": True}})

        assert c.state.firmware_consistency_request is True

    def test_a_forced_upgrade_is_seen(self) -> None:
        c = _client()
        c._update_state({"upgrade_state": {"status": "IDLE", "force_upgrade": True}})

        assert c.state.firmware_force_upgrade is True

    def test_they_are_independent(self) -> None:
        """Different situations with different wording in BS — a repair versus an
        update the printer insists on."""
        c = _client()
        c._update_state({"upgrade_state": {"status": "IDLE", "consistency_request": True, "force_upgrade": False}})

        assert c.state.firmware_consistency_request is True
        assert c.state.firmware_force_upgrade is False

    def test_clearing_is_honoured(self) -> None:
        """A repaired printer must stop being flagged, or the badge becomes
        furniture."""
        c = _client()
        c._update_state({"upgrade_state": {"status": "IDLE", "consistency_request": True}})
        c._update_state({"upgrade_state": {"status": "IDLE", "consistency_request": False}})

        assert c.state.firmware_consistency_request is False


class TestTheyDefaultToCalm:
    def test_a_printer_that_says_nothing_is_not_flagged(self) -> None:
        """The card shows an error badge on these, so a missing field must not
        read as trouble."""
        c = _client()
        c._update_state({"upgrade_state": {"status": "IDLE"}})

        assert c.state.firmware_consistency_request is False
        assert c.state.firmware_force_upgrade is False

    def test_a_push_without_upgrade_state_leaves_them_alone(self) -> None:
        c = _client()
        c._update_state({"upgrade_state": {"status": "IDLE", "consistency_request": True}})
        c._update_state({"gcode_state": "RUNNING"})

        assert c.state.firmware_consistency_request is True

    @pytest.mark.parametrize("junk", ["true", 1, None, {}])
    def test_a_non_boolean_is_ignored(self, junk) -> None:
        """Same discipline as the other named support bools: only a real boolean
        is an answer."""
        c = _client()
        c._update_state({"upgrade_state": {"status": "IDLE", "consistency_request": junk}})

        assert c.state.firmware_consistency_request is False


class TestTheStatusAlreadyParsedStillWorks:
    def test_the_status_string_is_untouched(self) -> None:
        """``firmware_upgrade_status`` gates the AMS-firmware refusals; adding
        neighbours to its block must not disturb it."""
        c = _client()
        c._update_state({"upgrade_state": {"status": "DOWNLOADING", "consistency_request": True}})

        assert c.state.firmware_upgrade_status == "DOWNLOADING"
