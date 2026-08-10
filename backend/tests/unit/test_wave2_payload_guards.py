"""Three guards BS puts on a write that we published without: the drying
ceiling, end-of-print air purification, and a sensitivity nobody asked for.

Audit item 17. Each is a case of a support flag being read as though it were an
answer to a narrower question than the one it answers. ``support_purify_air``
says the machine can purify; it does not say it may do so right now, nor that
it owns the fan the second mode needs. ``supports_drying`` says the unit dries;
it does not say how hot.

The fourth sub-item — ``auto_recovery``'s legacy ``option`` int — was already
correct: ``_set_print_option`` sends both the named bool and ``option``.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

from backend.app.api.routes.printer_settings import _guard_purify_air
from backend.app.api.routes.printers import _AMS_DRY_MAX_TEMP, _AMS_DRY_MAX_TEMP_FALLBACK


class TestTheDryingCeilingIsPerUnit:
    """BS ``AMSDryControl.cpp``: ``{N3F, 45, 65, "AMS2"}``, ``{N3S, 45, 85, "AMS-S"}``."""

    def test_an_ams_2_pro_stops_at_sixty_five(self) -> None:
        assert _AMS_DRY_MAX_TEMP["n3f"] == 65

    def test_an_ams_ht_reaches_eighty_five(self) -> None:
        assert _AMS_DRY_MAX_TEMP["n3s"] == 85

    def test_the_two_units_do_not_share_a_ceiling(self) -> None:
        """The regression in one line: a flat range makes these equal, and an
        AMS 2 Pro is then reachable at 85 °C — twenty degrees over its rating."""
        assert _AMS_DRY_MAX_TEMP["n3f"] != _AMS_DRY_MAX_TEMP["n3s"]

    def test_an_unknown_unit_falls_back_to_the_lower_ceiling(self) -> None:
        """Only the AMS HT reaches 85. The frontend has always capped an
        unrecognised unit at 65; the backend allowed 85, so the two disagreed
        about the same unit and an API key got the permissive answer."""
        assert _AMS_DRY_MAX_TEMP.get("ams", _AMS_DRY_MAX_TEMP_FALLBACK) == 65

    def test_the_fallback_matches_the_frontend(self) -> None:
        """``PrintersPage.tsx``: ``moduleType === 'n3s' ? 85 : 65``."""
        assert _AMS_DRY_MAX_TEMP["n3f"] == _AMS_DRY_MAX_TEMP_FALLBACK


def _state(*part_ids: int) -> MagicMock:
    s = MagicMock()
    s.airduct_parts = {pid: {} for pid in part_ids}
    return s


class TestPurifyAirIsRefusedMidPrint:
    """BS disables the control under ``is_in_printing()`` and answers a click
    with "Unavailable during the task" (``m_print_option_disable``)."""

    def test_a_running_printer_refuses(self) -> None:
        with pytest.raises(HTTPException) as exc:
            _guard_purify_air(True, _state(3), 1)

        assert exc.value.status_code == 409
        assert "while a print is running" in str(exc.value.detail)

    def test_an_idle_printer_passes(self) -> None:
        _guard_purify_air(False, _state(3), 1)

    def test_turning_it_off_is_refused_too(self) -> None:
        """BS greys out the whole control, not just the on direction."""
        with pytest.raises(HTTPException):
            _guard_purify_air(True, _state(3), 0)


class TestModeTwoNeedsAnExhaustFan:
    """BS ``AirDuctData::IsExaustFanExit()`` — a part with id
    ``FAN_CHAMBER_0_IDX`` (3). Without one the mode switch is hidden and the
    option is a plain on/off."""

    def test_mode_two_passes_with_a_chamber_exhaust_part(self) -> None:
        _guard_purify_air(False, _state(0, 1, 3), 2)

    def test_mode_two_is_refused_without_one(self) -> None:
        with pytest.raises(HTTPException) as exc:
            _guard_purify_air(False, _state(0, 1), 2)

        assert exc.value.status_code == 409
        assert "chamber exhaust fan" in str(exc.value.detail)

    @pytest.mark.parametrize("value", [0, 1])
    def test_off_and_mode_one_pass_without_a_fan(self, value: int) -> None:
        """Only the second mode depends on the fan. Gating the whole option on
        it would hide purification from every machine that circulates
        internally — which is what BS does on exactly those machines."""
        _guard_purify_air(False, _state(0, 1), value)

    def test_a_printer_with_no_airduct_data_still_allows_the_plain_modes(self) -> None:
        s = MagicMock()
        s.airduct_parts = {}
        _guard_purify_air(False, s, 1)


class TestTheStraySensitivityIsGone:
    def test_the_old_writer_no_longer_exists(self) -> None:
        """``set_xcam_option`` always appended ``halt_print_sensitivity``, so
        toggling a detector that owns no sensitivity — first-layer inspection,
        the buildplate marker — shipped a value that landed on whichever
        detector does. It had no callers; the fix is its absence."""
        from backend.app.services import bambu_mqtt

        assert not hasattr(bambu_mqtt.BambuMQTTClient, "set_xcam_option")

    def test_the_surviving_writer_omits_sensitivity_when_none(self) -> None:
        import json

        from backend.app.services.bambu_mqtt import BambuMQTTClient

        c = BambuMQTTClient(ip_address="192.168.1.100", serial_number="TESTSERIAL", access_code="12345678", model="H2D")
        c._client = MagicMock()
        c.state.connected = True

        c.xcam_control_for_settings("first_layer_inspector", enabled=True, sensitivity=None)

        payload = json.loads(c._client.publish.call_args[0][1])
        assert "halt_print_sensitivity" not in payload["xcam"]
