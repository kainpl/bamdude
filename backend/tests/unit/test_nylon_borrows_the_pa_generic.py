"""Nylon, PA6 and PA12 borrow Generic PA's profile; nothing else moves (G07 Р26).

A spool whose material is one of those names resolved to no system profile, so
the slot was never configured at all. The base generic now lends its profile —
the slot keeps the spool's own type, as for every name that is no catalogue type.
The table is every material the stand-in answered for BEFORE the change, with
the answer it gave: the three names are the only rows that differ.
"""

from __future__ import annotations

import pytest

from backend.app.services.slot_assignment import _stand_in
from backend.app.utils import filament_catalog as catalog

# (material, family_for_material, generic_family_for_material, stand-in family, slot type)
UNCHANGED = [
    ("PLA", "GFL99", "GFL99", "GFL99", "PLA"),
    ("PLA+", "GFL99", None, "GFL99", "PLA"),
    ("PLA Matte", "GFL99", "GFL99", "GFL99", "PLA"),
    ("PLA Silk", "GFL96", "GFL96", "GFL96", "PLA"),
    ("PLA-AERO", "GFA11", "GFL99", "GFA11", "PLA-AERO"),
    ("PLA Aero", "GFA11", "GFL99", "GFA11", "PLA-AERO"),
    ("PLA-CF", "GFL98", "GFL98", "GFL98", "PLA-CF"),
    ("PETG", "GFG99", "GFG99", "GFG99", "PETG"),
    ("PETG HF", "GFG96", "GFG96", "GFG96", "PETG"),
    ("PETG-CF", "GFG98", "GFG98", "GFG98", "PETG-CF"),
    ("PCTG", "GFG97", "GFG97", "GFG97", "PCTG"),
    ("ABS", "GFB99", "GFB99", "GFB99", "ABS"),
    ("ABS-GF", "GFB50", "GFB99", "GFB50", "ABS-GF"),
    ("ASA", "GFB98", "GFB98", "GFB98", "ASA"),
    ("ASA-GF", None, "GFB98", "GFB98", "ASA-GF"),
    ("ASA-CF", "GFB51", "GFB98", "GFB51", "ASA-CF"),
    ("TPU", "GFU99", "GFU99", "GFU99", "TPU"),
    ("TPU-AMS", "GFU98", "GFU99", "GFU98", "TPU-AMS"),
    ("PC", "GFC99", "GFC99", "GFC99", "PC"),
    ("PA", "GFN99", "GFN99", "GFN99", "PA"),
    ("PA-CF", "GFN98", "GFN98", "GFN98", "PA-CF"),
    ("PA-GF", "GFN08", "GFN99", "GFN08", "PA-GF"),
    ("PA6-CF", "GFN05", None, "GFN05", "PA6-CF"),
    ("PA6-GF", None, None, None, "PA6-GF"),
    ("PA12-CF", "GFN98", None, "GFN98", "PA-CF"),
    ("PA11", None, None, None, "PA11"),
    ("PAHT", None, None, None, "PAHT"),
    ("PAHT-CF", "GFN98", None, "GFN98", "PA-CF"),
    ("PPA", None, None, None, "PPA"),
    ("PPA-CF", "GFN97", "GFN97", "GFN97", "PPA-CF"),
    ("PVA", "GFS99", "GFS99", "GFS99", "PVA"),
    ("HIPS", "GFS98", "GFS98", "GFS98", "HIPS"),
    ("PP", "GFP97", "GFP97", "GFP97", "PP"),
    ("PET", None, None, None, "PET"),
    ("PET-CF", "GFT01", None, "GFT01", "PET-CF"),
    ("PPS", "GFT97", "GFT97", "GFT97", "PPS"),
    ("Support", None, None, None, "Support"),
    ("Nylon CF", None, None, None, "Nylon CF"),
    ("PA612", None, None, None, "PA612"),
    ("Unobtainium", None, None, None, "Unobtainium"),
    ("", None, None, None, ""),
]


def _id(family):
    return family.filament_id if family else None


@pytest.mark.parametrize(("material", "exact", "generic", "stand_in", "slot_type"), UNCHANGED)
def test_every_other_material_answers_as_before(material, exact, generic, stand_in, slot_type):
    assert _id(catalog.family_for_material(material)) == exact
    assert _id(catalog.generic_family_for_material(material)) == generic
    family, written = _stand_in(material)
    assert (_id(family), written) == (stand_in, slot_type)


@pytest.mark.parametrize("material", ["Nylon", "NYLON", "nylon ", "PA6", "PA12"])
def test_a_nylon_name_borrows_generic_pa_and_keeps_its_own_type(material):
    family, written = _stand_in(material)

    assert _id(family) == "GFN99"  # Generic PA lends the profile …
    assert written == material.strip()  # … and the slot is told the spool's type
    # No family of that exact material exists, so nothing is persisted as one.
    assert catalog.family_for_material(material) is None
