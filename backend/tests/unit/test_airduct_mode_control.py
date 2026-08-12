"""The air-duct mode and its "Filter" sub-mode are governed differently, and
neither rule may be copied onto the other.

Registry F4 + F5. BS ``FanControlPopupNew``:

* **mode, mid-print — refused outright.** ``on_mode_changed`` shows an OK-only
  dialog ("The selected material only supports the current fan mode, and it
  can't be changed during printing") and ``return``\\ s without publishing.
  There is no "anyway";
* **filtration on, mid-print — a warning** with "Change Anyway". It costs
  cooling rather than contradicting the material;
* **filtration off — not warned about at all.** Turning it off gives cooling
  back.

⚠️ And the modes offered are the ones the printer listed, never the four names
in the enum: BS builds one button per entry in the reported ``modeList``
(``CreateDuct``). A mode that exists in the protocol is not necessarily a mode
this machine has.
"""

from __future__ import annotations

import inspect
import json
from unittest.mock import MagicMock

import pytest

from backend.app.api.routes import printers as printers_routes
from backend.app.services.bambu_mqtt import (
    AIRDUCT_COOLING_FILT,
    AIRDUCT_HEATING_INTERNAL_FILT,
    BambuMQTTClient,
)


def _client() -> BambuMQTTClient:
    c = BambuMQTTClient(ip_address="1.2.3.4", serial_number="X2D0001", access_code="12345678", model="X2D")
    c._client = MagicMock()
    c.state.connected = True
    return c


def _published(c: BambuMQTTClient) -> dict:
    return json.loads(c._client.publish.call_args[0][1])["print"]


class TestTheWireFormat:
    def test_it_is_bs_set_airduct(self) -> None:
        c = _client()
        assert c.set_airduct_mode(AIRDUCT_HEATING_INTERNAL_FILT) is True

        p = _published(c)
        assert p["command"] == "set_airduct"
        assert p["modeId"] == AIRDUCT_HEATING_INTERNAL_FILT
        assert "sequence_id" in p

    def test_the_submode_rides_along(self) -> None:
        c = _client()
        c.set_airduct_mode(AIRDUCT_COOLING_FILT, submode=1)

        assert _published(c)["submode"] == 1

    def test_minus_one_means_leave_it_alone(self) -> None:
        c = _client()
        c.set_airduct_mode(AIRDUCT_COOLING_FILT)

        assert _published(c)["submode"] == -1

    @pytest.mark.parametrize(("word", "expected"), [("cooling", 0), ("heating", 1)])
    def test_the_legacy_words_still_work(self, word: str, expected: int) -> None:
        """``services/preheat.py`` picks a mode from the filament, not from a
        list someone clicked, and speaks in these terms."""
        c = _client()
        c.set_airduct_mode(word)

        assert _published(c)["modeId"] == expected

    def test_a_disconnected_printer_is_refused(self) -> None:
        c = _client()
        c.state.connected = False

        assert c.set_airduct_mode(AIRDUCT_COOLING_FILT) is False


class TestTheRouteGoverns:
    """Pinned as source: the route needs a live MQTT client, and what matters
    here is which refusal applies to which action."""

    @pytest.fixture
    def body(self) -> str:
        return inspect.getsource(printers_routes.set_airduct_mode)

    def test_only_modes_the_printer_listed_are_accepted(self, body: str) -> None:
        assert "if mode_id not in available:" in body

    def test_a_printer_with_no_airduct_is_told_so(self, body: str) -> None:
        """Distinct from "mode 2 not offered" — one is a machine without the
        feature, the other a machine with a shorter list."""
        assert "does not report an air duct" in body

    def test_changing_mode_mid_print_is_absolute(self, body: str) -> None:
        """⚠️ No ``confirm`` on this branch. BS returns without publishing, so
        an override would be a button that contradicts the loaded material."""
        assert "if changing_mode and busy:" in body
        branch = body.split("if changing_mode and busy:")[1].split("\n\n")[0]
        assert "confirm" not in branch

    def test_setting_the_same_mode_again_is_not_a_change(self, body: str) -> None:
        """Only a *different* mode is refused mid-print — re-sending the current
        one is how the sub-mode is carried, and BS allows exactly that."""
        assert "changing_mode = mode_id != client.state.airduct_mode" in body

    def test_filtration_needs_the_hardware(self, body: str) -> None:
        assert '"cooling_filter"' in body

    def test_filtration_belongs_to_the_cooling_mode(self, body: str) -> None:
        assert "if mode_id != AIRDUCT_COOLING_FILT:" in body

    def test_only_turning_filtration_on_is_warned_about(self, body: str) -> None:
        """Off gives cooling back; BS does not ask."""
        assert "if submode == 1 and busy and not confirm:" in body

    def test_that_one_names_its_own_remedy(self, body: str) -> None:
        assert "confirm=true" in body
