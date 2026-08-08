"""The A2L's AMS Lite gets its empty-slot cleanup through the VP too (#2699).

The A2L reports its AMS Lite as physical unit id **16** but packs the slot
presence bits at base **24**, so ``bambu_mqtt`` normalises the id to 6 at the
ingest boundary and every internal reader gets the right bits.

The VP bridge is not downstream of that. ``BambuMQTTClient._on_message`` fans raw
payload bytes out to raw-message handlers *before* parsing, so
``mqtt_bridge._on_printer_raw`` does its own ``json.loads`` and still holds 16.
It then calls the shared ``apply_tray_exist_bits``, which computed ``16*4`` =
bits 64-67 — never set — and concluded all four slots were empty, wiping the
filament from the copy sent to the slicer.

Our ``0 <= ams_id <= 15`` range guard (the #2670 port) turned that into the
*other* wrong answer: the unit falls out of the guard entirely and gets no
cleanup at all, so empty slots keep stale filament and BambuStudio paints
phantom loaded spools. Two different wrong answers; folding the id inside the
helper fixes both, and makes the helper's own docstring true for both callers.
"""

from __future__ import annotations

from backend.app.services.bambu_mqtt import apply_tray_exist_bits, normalize_am_unit_id

# Bits 24-27 set: all four AMS-Lite slots present. Bits 64-67 are not
# representable in the value the firmware actually sends.
_ALL_FOUR_PRESENT = f"{0b1111 << 24:x}"
# Slots 0 and 2 present, 1 and 3 empty.
_TWO_PRESENT = f"{0b0101 << 24:x}"


def _lite_unit(ams_id: int) -> list[dict]:
    return [
        {
            "id": str(ams_id),
            "tray": [{"id": str(i), "tray_type": "PLA", "tray_color": "FF0000FF", "remain": 50} for i in range(4)],
        }
    ]


class TestTheIdIsFoldedInsideTheHelper:
    def test_the_physical_id_and_the_normalised_one_agree(self) -> None:
        """The whole point: whichever id the caller happens to hold, the same
        bits are consulted. The VP bridge holds 16, everything else holds 6."""
        physical = _lite_unit(16)
        normalised = _lite_unit(6)

        apply_tray_exist_bits(physical, _TWO_PRESENT)
        apply_tray_exist_bits(normalised, _TWO_PRESENT)

        assert [t.get("tray_type") for t in physical[0]["tray"]] == [t.get("tray_type") for t in normalised[0]["tray"]]

    def test_a_full_lite_keeps_every_slot_when_addressed_as_16(self) -> None:
        units = _lite_unit(16)

        cleared = apply_tray_exist_bits(units, _ALL_FOUR_PRESENT)

        assert cleared == 0
        assert all(t["tray_type"] == "PLA" for t in units[0]["tray"])

    def test_only_the_genuinely_empty_slots_are_cleared(self) -> None:
        """The failure this fixes, stated from the other side: before, id 16
        either wiped all four (pre-guard) or cleaned none of them (post-guard)."""
        units = _lite_unit(16)

        cleared = apply_tray_exist_bits(units, _TWO_PRESENT)

        assert cleared == 2
        # Cleared slots keep the key and are blanked, not removed — downstream
        # readers key off the empty string plus state 9.
        assert [t["tray_type"] for t in units[0]["tray"]] == ["PLA", "", "PLA", ""]

    def test_an_empty_lite_slot_is_marked_with_the_firmware_no_spool_code(self) -> None:
        """State 9 is the one canonical "no spool" signal the AMS card, the VP
        cache and the inventory short-circuits all read."""
        units = _lite_unit(16)

        apply_tray_exist_bits(units, _TWO_PRESENT)

        assert [t.get("state") for t in units[0]["tray"]] == [None, 9, None, 9]


class TestNothingElseMoved:
    def test_only_id_16_is_remapped(self) -> None:
        assert normalize_am_unit_id(16) == 6
        for other in (0, 1, 2, 3, 6, 15, 128, 135):
            assert normalize_am_unit_id(other) == other

    def test_a_regular_ams_still_uses_its_own_bit_base(self) -> None:
        # AMS 1 occupies bits 4-7; only slot 0 present here.
        units = [{"id": "1", "tray": [{"id": str(i), "tray_type": "PETG"} for i in range(4)]}]

        cleared = apply_tray_exist_bits(units, f"{0b0001 << 4:x}")

        assert cleared == 3
        assert [t["tray_type"] for t in units[0]["tray"]] == ["PETG", "", "", ""]

    def test_an_ams_ht_keeps_its_single_consecutive_bit(self) -> None:
        # HT-A is bit 16, not id*4 — the other special case in this helper.
        units = [{"id": "128", "tray": [{"id": "0", "tray_type": "ABS"}]}]

        assert apply_tray_exist_bits(units, f"{1 << 16:x}") == 0
        assert units[0]["tray"][0]["tray_type"] == "ABS"
