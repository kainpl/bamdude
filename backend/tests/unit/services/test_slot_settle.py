"""After configuring a slot, ask the printer again until it has actually said so.

Reported symptom: assign a spool to the external slot and the printer card keeps
showing the old one — the spool catalogue says it is assigned, BambuStudio shows
the new colour and type, and our card catches up only "after a while", on its own.

Nothing was lost. ``ams_filament_setting`` lands, and the printer echoes the
filament id back within about a tenth of a second — but ``tray_type`` follows
later, and ``tray_type`` is what the card renders and what ``on_ams_change``
compares the assignment fingerprint against. We ask the printer for a full push
exactly once, immediately after publishing, so our single forced push captures
precisely the moment before the type lands. After that nothing asks again and we
wait for the printer's own reporting cadence.

⚠️ The fix is deliberately NOT "verify the type as well". ``register_assignment_
verification`` succeeds on the filament id alone, on purpose — a slot can be
configured without our ever seeing a type, and ``test_match_fires_verified``
pins exactly that. Requiring a type there would turn today's successes into
timeout failures on every such slot. Settling is a separate question from
verification, so it gets a separate mechanism.
"""

import pytest

from backend.app.services.bambu_mqtt import BambuMQTTClient
from backend.app.services.slot_settle import nudge_until_slot_reports_type

# Tiny so the suite doesn't sleep; the production defaults live in the module.
_FAST = (0.001, 0.001, 0.001)


@pytest.fixture
def client():
    return BambuMQTTClient(ip_address="192.168.1.100", serial_number="TEST123", access_code="x")


def _external(**over):
    """The external spool as the printer reports it: vt_tray, global id 254."""
    vt = {"id": "254", "tray_type": "PETG", "tray_color": "FFFF00FF", "tray_info_idx": "GFG99"}
    vt.update(over)
    return vt


class TestReadingWhatTheSlotReports:
    def test_it_reads_the_external_slot(self, client):
        client.state.raw_data["vt_tray"] = [_external()]
        assert client.slot_reported_filament_type(255, 0) == "PETG"

    def test_a_blank_type_reads_as_blank(self, client):
        """The state this whole file is about: id and colour present, type not."""
        client.state.raw_data["vt_tray"] = [_external(tray_type="")]
        assert client.slot_reported_filament_type(255, 0) == ""

    def test_a_slot_we_cannot_find_reads_as_blank(self, client):
        client.state.raw_data["vt_tray"] = []
        assert client.slot_reported_filament_type(255, 0) == ""

    def test_it_reads_an_ams_tray_too(self, client):
        """Same question, the ordinary case — one definition for both shapes."""
        client.state.raw_data["ams"] = [{"id": 0, "tray": [{"id": 1, "tray_type": "PLA"}]}]
        assert client.slot_reported_filament_type(0, 1) == "PLA"


class TestNudging:
    @pytest.mark.asyncio
    async def test_a_slot_that_already_reports_is_not_nudged_at_all(self, client, monkeypatch):
        """⚠️ pushall is a full-state message. Sending one when the answer is
        already on hand is pure noise on a farm that pushes constantly."""
        calls = []
        monkeypatch.setattr(client, "request_status_update", lambda: calls.append(1) or True)
        client.state.raw_data["vt_tray"] = [_external()]

        settled = await nudge_until_slot_reports_type(client, 255, 0, delays=_FAST)

        assert settled is True
        assert calls == []

    @pytest.mark.asyncio
    async def test_it_stops_as_soon_as_the_type_lands(self, client, monkeypatch):
        client.state.raw_data["vt_tray"] = [_external(tray_type="")]

        def _nudge():
            # The printer answers the second ask.
            if len(calls) >= 1:
                client.state.raw_data["vt_tray"] = [_external()]
            calls.append(1)
            return True

        calls: list[int] = []
        monkeypatch.setattr(client, "request_status_update", _nudge)

        settled = await nudge_until_slot_reports_type(client, 255, 0, delays=_FAST)

        assert settled is True
        assert len(calls) == 2, "it kept asking after the printer had answered"

    @pytest.mark.asyncio
    async def test_it_gives_up_after_its_attempts(self, client, monkeypatch):
        """⚠️ Bounded on purpose. A slot the printer never describes is a real
        state — an empty external holder — and must not be nudged for ever."""
        calls = []
        monkeypatch.setattr(client, "request_status_update", lambda: calls.append(1) or True)
        client.state.raw_data["vt_tray"] = [_external(tray_type="")]

        settled = await nudge_until_slot_reports_type(client, 255, 0, delays=_FAST)

        assert settled is False
        assert len(calls) == len(_FAST)

    @pytest.mark.asyncio
    async def test_a_disconnected_printer_ends_it(self, client, monkeypatch):
        """``request_status_update`` returns False when not connected. Carrying
        on would sleep the whole budget for nothing."""
        calls = []
        monkeypatch.setattr(client, "request_status_update", lambda: calls.append(1) or False)
        client.state.raw_data["vt_tray"] = [_external(tray_type="")]

        settled = await nudge_until_slot_reports_type(client, 255, 0, delays=_FAST)

        assert settled is False
        assert len(calls) == 1, "it kept asking a printer that is not there"

    @pytest.mark.asyncio
    async def test_no_client_is_a_noop(self):
        assert await nudge_until_slot_reports_type(None, 255, 0, delays=_FAST) is False
