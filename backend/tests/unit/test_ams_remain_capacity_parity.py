"""The four AMS Settings rows, each asked the way BambuStudio asks it.

Registry item B3. One ``_HAS_RFID_AMS`` set was answering four different
questions, and BS answers each differently (``AMSSetting.cpp``):

* ``insertion_update`` — did the printer report the setting at all, is the AMS
  *selected* to run the Lite personality, is the model's ``use_ams_type`` "f1";
* ``power_on_update`` — BS never gates it; the dialog's existence is the gate;
* ``remain_capacity`` — ``support_update_remain`` AND NOT ``fun2`` bit 6, with an
  override forcing it on for a non-Lite AMS personality;
* ``auto_switch_filament`` — ``support_filament_backup``.

⚠️ The override is the part a model list cannot express, and the part that made
the first attempt at this wrong. Reading the config alone said "the A1 does not
support remaining capacity" — true of the AMS Lite it ships with, false the
moment a real AMS 2 or AMS HT is attached. A config answers what ships with a
model, never what the machine in front of you can do.

⚠️ These use a real ``PrinterState``, never ``MagicMock``. A mock returns a
truthy object for ``print_option_support``, which silently makes the
hide-display condition true and every row vanish — a stub that confirms whatever
model you already hold.
"""

from __future__ import annotations

import pytest

from backend.app.services.ams_capabilities import (
    AMS_FIRMWARE_IDX_AMS,
    AMS_FIRMWARE_IDX_LITE,
    compute_ams_supports,
)
from backend.app.services.bambu_mqtt import PrinterState

KNOWN_FW = "01.08.00.00"


def _state(firmware: str | None = KNOWN_FW, **kw) -> PrinterState:
    """A connected printer whose AMS has pushed its user settings."""
    s = PrinterState()
    s.firmware_version = firmware
    s.ams_insertion_update = False
    s.ams_power_on_update = False
    for k, v in kw.items():
        setattr(s, k, v)
    return s


class TestRemainingCapacity:
    def test_the_a1_follows_its_config_with_the_stock_lite_unit(self) -> None:
        """``support_update_remain`` is false for the A1, and we offered the row
        anyway — the original B3 defect."""
        assert compute_ams_supports(_state(), "A1")["remain_capacity"] is False

    def test_but_a_real_ams_on_that_same_a1_brings_it_back(self) -> None:
        """BS forces support on when the AMS is *running* the non-Lite
        personality. Without this, attaching an AMS 2 Pro to an A1 would lose a
        control the machine genuinely has."""
        s = _state(ams_firmware_idx_run=AMS_FIRMWARE_IDX_AMS)

        assert compute_ams_supports(s, "A1")["remain_capacity"] is True

    def test_the_lite_personality_does_not_force_it(self) -> None:
        s = _state(ams_firmware_idx_run=AMS_FIRMWARE_IDX_LITE)

        assert compute_ams_supports(s, "A1")["remain_capacity"] is False

    @pytest.mark.parametrize("model", ["X1", "X1C", "X1E", "P1P", "P1S", "P2S", "X2D", "H2D", "H2C", "H2S"])
    def test_every_other_rfid_model_keeps_it(self, model: str) -> None:
        assert compute_ams_supports(_state(), model)["remain_capacity"] is True

    def test_the_live_report_beats_the_config(self) -> None:
        """A config describes the model; the printer describes itself."""
        s = _state()
        s.print_option_support = {"update_remain": True}

        assert compute_ams_supports(s, "A1")["remain_capacity"] is True

    def test_hide_display_is_a_second_condition_not_a_restatement(self) -> None:
        """``fun2`` bit 6. A machine can support the feature and still be told
        not to offer it."""
        s = _state()
        s.print_option_support = {"update_remain": True, "update_remain_hide_display": True}

        assert compute_ams_supports(s, "X1C")["remain_capacity"] is False

    def test_hide_display_beats_even_the_personality_override(self) -> None:
        s = _state(ams_firmware_idx_run=AMS_FIRMWARE_IDX_AMS)
        s.print_option_support = {"update_remain_hide_display": True}

        assert compute_ams_supports(s, "A1")["remain_capacity"] is False


class TestTheFirmwareWindow:
    """The X1 family gains this flag in a later firmware block, so the 2023 base
    block says False. Reading the config before ``get_version`` answers would
    take a working control away from them."""

    @pytest.mark.parametrize("model", ["X1", "X1C"])
    def test_they_keep_it_before_the_version_is_known(self, model: str) -> None:
        assert compute_ams_supports(_state(firmware=None), model)["remain_capacity"] is True

    @pytest.mark.parametrize("model", ["X1", "X1C"])
    def test_and_after(self, model: str) -> None:
        assert compute_ams_supports(_state(), model)["remain_capacity"] is True

    def test_the_a1_keeps_the_old_answer_until_the_version_is_known(self) -> None:
        """An incomplete config view must not decide. A row shown a few seconds
        too long is harmless; one taken away is not."""
        assert compute_ams_supports(_state(firmware=None), "A1")["remain_capacity"] is True


class TestInsertionRead:
    def test_a_printer_that_reported_nothing_gets_no_row(self) -> None:
        """BS hides on an empty ``std::optional``; ``None`` says the same."""
        s = _state()
        s.ams_insertion_update = None

        assert compute_ams_supports(s, "X1C")["insertion_update"] is False

    def test_a_reporting_printer_gets_it(self) -> None:
        assert compute_ams_supports(_state(), "X1C")["insertion_update"] is True

    def test_the_a1_family_config_hides_it(self) -> None:
        """``use_ams_type: "f1"`` — the A1's own AMS type has no insertion read.
        Checked only when the machine offers no firmware switch."""
        assert compute_ams_supports(_state(), "A1")["insertion_update"] is False
        assert compute_ams_supports(_state(), "A1 Mini")["insertion_update"] is False

    def test_a_selected_lite_personality_hides_it(self) -> None:
        s = _state(
            ams_firmwares=[{"id": 0, "name": "AMS Lite", "version": ""}],
            ams_firmware_idx_sel=AMS_FIRMWARE_IDX_LITE,
        )

        assert compute_ams_supports(s, "A1")["insertion_update"] is False

    def test_a_selected_ams_personality_shows_it(self) -> None:
        """⚠️ And this is why the firmware-switch branch is checked *before* the
        ``use_ams_type`` one: the A1's config says "f1", but a switched AMS makes
        that stale."""
        s = _state(
            ams_firmwares=[{"id": 1, "name": "AMS", "version": ""}],
            ams_firmware_idx_sel=AMS_FIRMWARE_IDX_AMS,
        )

        assert compute_ams_supports(s, "A1")["insertion_update"] is True


class TestPowerOnRead:
    def test_a_reporting_printer_gets_it(self) -> None:
        assert compute_ams_supports(_state(), "A1 Mini")["power_on_update"] is True

    def test_a_printer_that_reported_nothing_falls_back_to_the_model(self) -> None:
        """BS does not gate this at all — its dialog only exists where an AMS
        does. Ours is an HTTP surface, so "the printer reported it" stands in for
        that, with the old heuristic covering the window before it does."""
        s = _state()
        s.ams_power_on_update = None

        assert compute_ams_supports(s, "X1C")["power_on_update"] is True
        assert compute_ams_supports(s, "A1 Mini")["power_on_update"] is False


class TestAutoSwitchFilament:
    def test_it_follows_support_filament_backup(self) -> None:
        """Every mirrored config carries this flag as true, including the A1 Mini
        and the A2L, where the RFID set said false."""
        assert compute_ams_supports(_state(), "A1 Mini")["auto_switch_filament"] is True

    def test_the_live_report_beats_the_config(self) -> None:
        s = _state()
        s.print_option_support = {"filament_backup": False}

        assert compute_ams_supports(s, "X1C")["auto_switch_filament"] is False

    def test_it_is_not_mapped_onto_the_ams_switch_key(self) -> None:
        """``support_command_ams_switch`` is a different question that happens to
        be true everywhere; mapping onto it would have looked identical here and
        been wrong for the right reason."""
        s = _state()
        s.print_option_support = {"filament_backup": False}

        assert compute_ams_supports(s, "A1")["auto_switch_filament"] is False


class TestUnknownModels:
    def test_an_unknown_model_offers_nothing_it_cannot_justify(self) -> None:
        s = _state()
        sup = compute_ams_supports(s, "DefinitelyNotABambu")

        assert sup["remain_capacity"] is False
        assert sup["auto_switch_filament"] is False

    def test_but_a_reported_insertion_setting_is_still_honoured(self) -> None:
        """No config, no firmware switch, no "f1" — nothing refuses it, so the
        printer's own report stands."""
        assert compute_ams_supports(_state(), "DefinitelyNotABambu")["insertion_update"] is True
