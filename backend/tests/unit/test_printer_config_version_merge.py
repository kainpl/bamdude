"""Firmware-version blocks are merged, the way BambuStudio merges them.

``resources/printers/<code>.json`` is keyed by firmware version. BS
(``json_diff::load_compatible_settings``) clears its accumulator and merges
**every block whose key is <= the printer's OTA version**, in key order, and
that merged tree — not the base block — is what its capability parsers read.

We read ``"00.00.00.00"`` and stopped. That understates a printer by every flag
a later firmware turned on, and it is not a rounding error: thirteen flags on an
X1C, seven on a P1P.

The one with teeth is lidar calibration. ``device_calibration_availability``
gates it on ``support_lidar_calibration AND support_ai_monitoring``. The X1C's
base block has lidar **true** and ai_monitoring **false** — so the AND collapsed
and the row was hidden on every X1C and X1 in existence.

(Worth recording: the audit that found this described the cause as "the base
block says lidar=False". It does not — lidar is true there. The mechanism was
real, the stated example was not, which is why the assertion below checks both
halves rather than the headline.)
"""

from __future__ import annotations

from backend.app.utils.printer_configs import (
    _deep_merge,
    _merged_block,
    device_calibration_availability,
    get_device_support_flags,
    load_printer_config,
)


class TestTheMergeItself:
    def test_later_blocks_win(self) -> None:
        data = {
            "00.00.00.00": {"print": {"a": False, "b": 1}},
            "01.01.01.00": {"print": {"a": True}},
        }
        assert _merged_block(data, "01.02.00.00")["print"] == {"a": True, "b": 1}

    def test_blocks_newer_than_the_printer_are_not_applied(self) -> None:
        """A flag a future firmware turns on is not a flag this printer has."""
        data = {
            "00.00.00.00": {"print": {"a": False}},
            "09.00.00.00": {"print": {"a": True}},
        }
        assert _merged_block(data, "01.00.00.00")["print"] == {"a": False}

    def test_the_printers_own_version_is_included(self) -> None:
        """BS breaks on ``key > version``, so an exact match still merges."""
        data = {"00.00.00.00": {"print": {"a": False}}, "01.01.01.00": {"print": {"a": True}}}
        assert _merged_block(data, "01.01.01.00")["print"] == {"a": True}

    def test_no_version_means_base_only(self) -> None:
        """Before the first push we do not know the version. Understating for a
        few seconds beats guessing a capability."""
        data = {"00.00.00.00": {"print": {"a": False}}, "01.01.01.00": {"print": {"a": True}}}
        assert _merged_block(data, None)["print"] == {"a": False}

    def test_merge_is_deep_for_objects(self) -> None:
        """BS ``merge_objects`` recurses into objects and overwrites scalars —
        a later block adding one key must not drop its siblings."""
        dst = {"print": {"keep": 1, "nested": {"x": 1, "y": 2}}}
        _deep_merge({"print": {"nested": {"y": 9, "z": 3}}}, dst)
        assert dst == {"print": {"keep": 1, "nested": {"x": 1, "y": 9, "z": 3}}}

    def test_a_version_older_than_every_block_still_answers(self) -> None:
        data = {"01.00.00.00": {"print": {"a": True}}}
        assert _merged_block(data, "00.00.00.01") is not None


class TestAgainstTheShippedConfigs:
    def test_x1c_gains_ai_monitoring_from_its_own_firmware_block(self) -> None:
        assert get_device_support_flags("X1C").get("support_ai_monitoring") is False
        assert get_device_support_flags("X1C", "01.08.02.00").get("support_ai_monitoring") is True

    def test_lidar_calibration_is_unblocked_on_x1c(self) -> None:
        """Both halves, because the headline claim about this was wrong: lidar
        itself is already true in the base block; ai_monitoring is what was
        false, and the gate ANDs them."""
        base = load_printer_config("X1C")["print"]
        assert base["support_lidar_calibration"] is True
        assert base["support_ai_monitoring"] is False

        assert device_calibration_availability("X1C")["lidar"] is False
        assert device_calibration_availability("X1C", "01.08.02.00")["lidar"] is True

    def test_the_x1c_understatement_is_thirteen_flags_not_one(self) -> None:
        base = get_device_support_flags("X1C")
        merged = get_device_support_flags("X1C", "99.99.99.99")
        differing = {k for k, v in merged.items() if base.get(k) != v}
        assert len(differing) >= 13, sorted(differing)
        # Spot-check the ones an operator would actually notice.
        for flag in ("support_ai_monitoring", "support_filament_backup", "support_send_to_sd"):
            assert base.get(flag) is False and merged.get(flag) is True, flag

    def test_a_model_with_no_later_blocks_is_unchanged(self) -> None:
        """H2D ships one block; merging must be a no-op rather than a surprise."""
        assert get_device_support_flags("H2D") == get_device_support_flags("H2D", "99.99.99.99")

    def test_an_unknown_model_still_answers_none(self) -> None:
        assert load_printer_config("DefinitelyNotABambu", "01.00.00.00") is None
