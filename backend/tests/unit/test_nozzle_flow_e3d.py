"""A nozzle whose flow class we did not know was labelled Standard, and saying
so wrote it back.

Registry N7. BambuStudio has five flow classes; we had four, and the missing one
turned a read-only mislabel into an edit: the card showed an ``HB00`` nozzle as
Standard, the dropdown offered no other reading of it, and touching the field
sent Standard back to the printer — degrading a profile the machine had right.

⚠️ **The letters read backwards from what you would guess**, and this is the
whole of the defect. BS's ``_str2_nozzle_flow_type``::

    S, A, X -> S_FLOW    (Standard)
    H, E    -> H_FLOW    (High Flow)   <- E is PLAIN high flow
    U       -> U_FLOW    (TPU High Flow)
    B       -> E_FLOW    (E3D High Flow)  <- E3D is B

So ``E`` was already correct here and needed no touching; ``B`` was absent
entirely. An earlier note in the audit had this backwards.

⚠️ The enum numbering has a hole too: ``nvtStandard`` 0, ``nvtHighFlow`` 1,
``nvtHybrid`` 2, ``nvtTPUHighFlow`` 3, ``nvtE3DHighFlow`` **5**. Four is unused,
so nothing may treat the set as contiguous — a ``value <= 3`` guard drops E3D
silently.
"""

from __future__ import annotations

import pytest

from backend.app.services.bambu_mqtt import _parse_nozzle_type
from backend.app.services.calibration_constants import NozzleVolumeType, generate_nozzle_id


class TestTheLettersAreBsOwn:
    @pytest.mark.parametrize(
        ("code", "expected"),
        [
            ("HS00", "standard"),
            ("HA00", "standard"),
            ("HX00", "standard"),
            ("HH00", "high_flow"),
            ("HE00", "high_flow"),
            ("HU00", "tpu_high_flow"),
            ("HB00", "e3d_high_flow"),
        ],
    )
    def test_each_letter_maps_the_way_bs_maps_it(self, code: str, expected: str) -> None:
        _, flow = _parse_nozzle_type(code)

        assert flow == expected

    def test_e_is_plain_high_flow_not_e3d(self) -> None:
        """⚠️ The trap in one assertion. The letter looks like it should be the
        E3D one and is not; an audit note said otherwise."""
        _, flow = _parse_nozzle_type("HE00")

        assert flow == "high_flow"

    def test_b_is_the_e3d_one(self) -> None:
        """The nozzle that used to fall through to the default and read as
        Standard — a wrong label the UI would then write back."""
        _, flow = _parse_nozzle_type("HB00")

        assert flow == "e3d_high_flow"


class TestTheEncoderRoundTrips:
    @pytest.mark.parametrize(
        ("vol_type", "letter"),
        [
            (NozzleVolumeType.STANDARD, "S"),
            (NozzleVolumeType.HIGH_FLOW, "H"),
            (NozzleVolumeType.TPU_HIGH_FLOW, "U"),
            (NozzleVolumeType.E3D_HIGH_FLOW, "B"),
        ],
    )
    def test_it_spells_the_letter_bs_spells(self, vol_type: NozzleVolumeType, letter: str) -> None:
        assert generate_nozzle_id(vol_type, 0.4)[1] == letter

    def test_an_e3d_nozzle_survives_a_round_trip(self) -> None:
        """Encode then parse: the class the user picked is the class that comes
        back. Before this, E3D had no letter to encode to at all."""
        encoded = generate_nozzle_id(NozzleVolumeType.E3D_HIGH_FLOW, 0.4)
        _, flow = _parse_nozzle_type(encoded)

        assert flow == NozzleVolumeType.E3D_HIGH_FLOW.value


class TestTheMaterialIsUnaffected:
    @pytest.mark.parametrize(("code", "material"), [("HB00", "stainless_steel"), ("HB01", "hardened_steel")])
    def test_the_flow_letter_does_not_disturb_positions_2_3(self, code: str, material: str) -> None:
        """Flow is character 1, material is characters 2-3 — separate questions
        read from separate places, as in ``s_parse_nozzle_type``."""
        mat, _ = _parse_nozzle_type(code)

        assert mat == material


class TestUnknownLettersStayUnknown:
    def test_a_letter_bs_does_not_define_yields_no_flow(self) -> None:
        """Better an empty flow than a guessed one: a wrong class here is what
        the UI writes back to the printer."""
        _, flow = _parse_nozzle_type("HZ00")

        assert flow == ""
