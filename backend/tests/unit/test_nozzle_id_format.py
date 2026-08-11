"""The nozzle id we sent was four characters where BambuStudio sends eight.

Registry N8. Every calibration payload carries a ``nozzle_id``, and BS builds it
in ``_generate_nozzle_id`` as::

    "H" + flow letter + material + "-" + diameter      ->  "HS00-0.4"

⚠️ **Characters 2-3 are the MATERIAL, not the diameter.** We packed a diameter
code into them and stopped — ``HS20`` for a standard 0.4 — which put a diameter
where a material belongs and left the id four characters long.

That is not cosmetic. BS's reverse parser opens with::

    if (str.size() < 8) { assert(false); return NozzleVolumeType::nvtStandard; }

so a short id is not rejected loudly, it reads back as **Standard** — the same
silent downgrade of a High Flow profile that registry N7 fixed at the other end
of the wire. Our own read side already spoke the long form: the k-profile
schemas document ``HS00-0.4``, the printer sends it, and only this encoder
disagreed.

⚠️ The diameter never depended on the id anyway. Every payload that carries
``nozzle_id`` carries a ``nozzle_diameter`` field beside it, which is why an
unnameable diameter degrades to "0" here instead of raising: the label goes
vague, the command stays correct.
"""

from __future__ import annotations

import pytest

from backend.app.services.bambu_mqtt import _parse_nozzle_type
from backend.app.services.calibration_constants import (
    NozzleVolumeType,
    generate_nozzle_id,
    to_string_nozzle_diameter,
)


class TestTheShapeIsBsOwn:
    @pytest.mark.parametrize(
        ("vol_type", "diameter", "expected"),
        [
            (NozzleVolumeType.STANDARD, 0.4, "HS00-0.4"),
            (NozzleVolumeType.HIGH_FLOW, 0.4, "HH00-0.4"),
            (NozzleVolumeType.HIGH_FLOW, 0.8, "HH00-0.8"),
            (NozzleVolumeType.TPU_HIGH_FLOW, 0.2, "HU00-0.2"),
            (NozzleVolumeType.E3D_HIGH_FLOW, 0.6, "HB00-0.6"),
        ],
    )
    def test_it_spells_what_generate_nozzle_id_spells(
        self, vol_type: NozzleVolumeType, diameter: float, expected: str
    ) -> None:
        assert generate_nozzle_id(vol_type, diameter) == expected

    def test_it_clears_the_length_guard_bs_applies(self) -> None:
        """⚠️ The whole defect in one assertion. ``convert_to_nozzle_type``
        returns Standard for anything under eight characters, so a four-char id
        does not fail — it lies."""
        for diameter in (0.2, 0.4, 0.6, 0.8):
            assert len(generate_nozzle_id(NozzleVolumeType.HIGH_FLOW, diameter)) >= 8

    def test_the_material_slot_holds_a_material(self) -> None:
        """Positions 2-3 are read as the material code by our own parser. They
        used to hold "20" for a 0.4 nozzle, which names no material at all."""
        material, _ = _parse_nozzle_type(generate_nozzle_id(NozzleVolumeType.STANDARD, 0.4))

        assert material == "stainless_steel"


class TestHybridHasNoLetterOfItsOwn:
    def test_it_takes_the_switch_default(self) -> None:
        """⚠️ BS's switch has no ``nvtHybrid`` case; it lands on ``default:``,
        which emits "H"."""
        assert generate_nozzle_id(NozzleVolumeType.HYBRID, 0.4) == "HH00-0.4"

    def test_the_letter_we_invented_is_gone(self) -> None:
        """A "Y" used to be written here and could be read back by nobody —
        not BS, and not our own ``_NOZZLE_FLOW_LETTER_MAP``. A nozzle encoded
        that way came back with no flow class at all."""
        _, flow = _parse_nozzle_type(generate_nozzle_id(NozzleVolumeType.HYBRID, 0.4))

        assert flow == "high_flow"


class TestTheDiameterString:
    @pytest.mark.parametrize(("value", "expected"), [(0.2, "0.2"), (0.4, "0.4"), (0.6, "0.6"), (0.8, "0.8")])
    def test_the_four_bs_will_name(self, value: float, expected: str) -> None:
        assert to_string_nozzle_diameter(value) == expected

    def test_float_noise_still_lands(self) -> None:
        """BS compares with a 1e-3 tolerance rather than equality, because the
        value arrives as a float from a config."""
        assert to_string_nozzle_diameter(0.4001) == "0.4"

    def test_an_unnameable_diameter_degrades_instead_of_raising(self) -> None:
        """⚠️ BS answers "0" and sends the command anyway. Raising would abort a
        calibration over a label, while the true diameter travels in its own
        ``nozzle_diameter`` field regardless."""
        assert to_string_nozzle_diameter(1.0) == "0"
        assert generate_nozzle_id(NozzleVolumeType.STANDARD, 1.0) == "HS00-0"


class TestTheRoundTripHolds:
    @pytest.mark.parametrize(
        "vol_type",
        [
            NozzleVolumeType.STANDARD,
            NozzleVolumeType.HIGH_FLOW,
            NozzleVolumeType.TPU_HIGH_FLOW,
            NozzleVolumeType.E3D_HIGH_FLOW,
        ],
    )
    def test_the_class_encoded_is_the_class_parsed(self, vol_type: NozzleVolumeType) -> None:
        _, flow = _parse_nozzle_type(generate_nozzle_id(vol_type, 0.4))

        assert flow == vol_type.value
