"""``GFS`` is two things at once: the prefix Bambu Cloud puts on every setting_id
(``GFSA00`` is the preset of family ``GFA00``) AND the first three letters of the
support-material families themselves (``GFS00`` Support W, ``GFS01`` Support G,
``GFS04`` PVA, ``GFSNL02`` SUNLU PLA Matte) — those are filament_ids the printer
reports as ``tray_info_idx``, and their presets are ``GFSS00`` / ``GFSSNL02``.

A converter that strips the S whenever it sees ``GFS`` turns ``GFS00`` into
``GF00``, an id that exists nowhere; that is how support spools ended up with a
family the catalog refuses (2026-09-04). The only safe rule is the SHAPE of a
family id: ``GF`` + one letter + two digits, or ``GF`` + three letters + two
digits — every one of the catalog's families has one of those two shapes,
and no setting_id does.
"""

import pytest

from backend.app.utils.filament_ids import filament_id_to_setting_id, setting_id_to_filament_id


@pytest.mark.parametrize(
    ("setting_id", "expected"),
    [
        ("GFSA00", "GFA00"),
        ("GFSG99", "GFG99"),
        ("GFSL05", "GFL05"),
        ("GFSS00", "GFS00"),  # preset of Bambu Support W
        ("GFSSNL02", "GFSNL02"),  # preset of a five-letter family
        ("GFS00", "GFS00"),  # already the family — the S is the family's own
        ("GFSNL02", "GFSNL02"),
        ("GFA00", "GFA00"),
        ("P1a2b3c4d", "P1a2b3c4d"),
        ("", ""),
    ],
)
def test_setting_id_to_filament_id_only_strips_an_s_that_leaves_a_family_behind(setting_id, expected):
    assert setting_id_to_filament_id(setting_id) == expected


@pytest.mark.parametrize(
    ("filament_id", "expected"),
    [
        ("GFA00", "GFSA00"),
        ("GFL05", "GFSL05"),
        ("GFS00", "GFSS00"),  # a support family gets its second S, like Bambu Cloud does
        ("GFSNL02", "GFSSNL02"),
        ("GFSA00", "GFSA00"),  # already a setting_id
        ("GFSS00", "GFSS00"),
        ("P1a2b3c4d", "P1a2b3c4d"),
        ("", ""),
    ],
)
def test_filament_id_to_setting_id_recognises_a_family_by_its_shape(filament_id, expected):
    assert filament_id_to_setting_id(filament_id) == expected


def test_the_two_converters_round_trip_every_family_shape():
    for family in ("GFA00", "GFS00", "GFSNL02", "GFL99"):
        assert setting_id_to_filament_id(filament_id_to_setting_id(family)) == family
