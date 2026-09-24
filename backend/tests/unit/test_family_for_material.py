"""A bare material name stands for a family of THAT material, or for none (upstream #2902, 88e8ca81).

A spool that names no family — every Spoolman spool without a linked K-profile,
a quick-added or legacy one — used to be configured as ``Generic <base>``: the
name was cut at the first hyphen or space. For a filled or foamed material that
is a different material. ``PLA-AERO`` went out as Generic PLA, so the slot said
PLA: routing compares that type and nothing else under the default base-material
matching, so PLA plates were sent to foaming filament and a PLA Aero plate found
no slot; AMS Backup could group it with ordinary PLA of the same colour.

Now: the family of the exact type — the generic one when the catalogue has it,
otherwise the catalogue's own family of that type — and a name that is no type
at all resolves to nothing, which refuses the slot instead of guessing.
"""

from __future__ import annotations

import pytest

from backend.app.utils import filament_catalog as catalog


@pytest.mark.parametrize(
    ("material", "family_id"),
    [
        ("PLA", "GFL99"),
        ("pla", "GFL99"),
        ("PLA+", "GFL99"),  # a qualifier, not a material (88e8ca81)
        ("PolyTerra PLA", "GFL01"),  # the name IS a catalogue family (Polymaker, type PLA)
        ("Esun PLA", "GFL99"),  # a brand word before the type: the type's generic
        ("PLA Matte", "GFL99"),
        ("PLA Silk", "GFL96"),  # a Generic family of that name exists — take it
        ("PETG HF", "GFG96"),
        ("PETG-HF", "GFG96"),
        ("PLA-AERO", "GFA11"),  # no Generic PLA-AERO: the catalogue's own PLA-AERO
        ("PLA Aero", "GFA11"),  # the words join to a type exactly
        ("ASA-CF", "GFB51"),
        ("ABS-GF", "GFB50"),
        ("PA-GF", "GFN08"),  # Bambu before Polymaker, deterministically
        ("PA6-CF", "GFN05"),
        ("TPU-AMS", "GFU98"),  # Generic TPU for AMS — its name is not "Generic TPU-AMS"
        ("PA12-CF", "GFN98"),  # equivalent to PA-CF for routing, so the same family
        ("PA-CF", "GFN98"),
    ],
)
def test_a_material_name_finds_a_family_of_that_material(material, family_id):
    family = catalog.family_for_material(material)

    assert family is not None and family.filament_id == family_id


@pytest.mark.parametrize("material", ["ASA-GF", "PLA-Matte", "Unobtainium", "", "   "])
def test_a_name_that_is_no_known_type_resolves_to_nothing(material):
    """No family of that exact material exists: the answer is none, not the base."""
    assert catalog.family_for_material(material) is None


def test_the_family_found_has_the_type_the_name_said():
    """The invariant the whole change exists for: never a different material."""
    for material in ("PLA-AERO", "ASA-AERO", "ASA-CF", "ABS-GF", "PA-GF", "TPU-AMS", "PET-CF"):
        family = catalog.family_for_material(material)
        assert family is not None and family.filament_type == material, material


@pytest.mark.asyncio
async def test_a_legacy_material_name_resolves_to_a_family_of_that_material(db_session):
    """``resolve_raw``'s last resort — also what a spool save persists as its family."""
    from backend.app.services.filament_identity import resolve_raw

    aero = await resolve_raw(db_session, "PLA Aero")
    unknown = await resolve_raw(db_session, "ASA-GF")

    assert aero.family is not None and aero.family.filament_id == "GFA11"
    assert unknown.family is None  # no family of that material: an honest NULL, not Generic ASA
