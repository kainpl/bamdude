"""A K-profile on an archived printer is history, not an option.

Archiving retires a printer and hides it everywhere while its history is kept
— so a spool that was once calibrated on it still carries the link, and must
not be presented as if the profile were available.

The PA tab already knows this: it counts only profiles "bound to a
non-archived printer (the same active-printer set the list shows), so a spool
still carrying a K-profile from a since-archived printer doesn't inflate the
badge past the printers actually offered for assignment". The inventory card
and the table's badge were serialised straight off the ORM relationship and
counted everything, so one farm's spool read 11 on the card and 7 in the
dialog — 4 of its printers had been archived.
"""

from datetime import datetime, timezone

import pytest

from backend.app.api.routes.inventory import _spool_to_list_item

_NOW = datetime.now(timezone.utc)


class _KP:
    def __init__(self, kp_id, printer_id):
        self.id = kp_id
        self.spool_id = 1
        self.printer_id = printer_id
        self.extruder = 0
        self.auto_linked = False
        self.created_at = _NOW
        self.nozzle_diameter = "0.4"
        self.nozzle_type = "standard"

    def __getattr__(self, _name):
        return None


class _Spool:
    """Only what ``_spool_to_list_item`` reads.

    Anything it asks for and this stub does not name answers ``None``, so a
    column added to Spool tomorrow does not break a test about K-profiles.
    """

    def __init__(self, profiles):
        self.k_profiles = profiles
        self.id = 1
        self.weight_used = 0.0
        self.label_weight = 1000
        self.material = "PETG"
        self.subtype = "Basic"
        self.brand = "333"
        self.color_name = "Black"
        self.rgba = "000000FF"
        self.filament_diameter = "1.75"
        self.created_at = _NOW
        self.updated_at = _NOW
        # Typed non-optional on the schema, so ``None`` from __getattr__ won't do.
        self.core_weight = 0
        self.weight_used_baseline = 0.0
        self.weight_locked = False

    def __getattr__(self, _name):
        return None


@pytest.fixture
def spool():
    # Two live printers (1, 3) and two archived ones (2, 4) — the shape the
    # farm hit, scaled down.
    return _Spool([_KP(10, 1), _KP(11, 2), _KP(12, 3), _KP(13, 4)])


def test_the_count_skips_profiles_on_archived_printers(spool):
    item = _spool_to_list_item(spool, archived_printer_ids={2, 4})
    assert item.k_profile_count == 2


def test_the_serialized_list_skips_them_too(spool):
    item = _spool_to_list_item(spool, include_k_profiles=True, archived_printer_ids={2, 4})
    assert [kp.id for kp in item.k_profiles] == [10, 12]


def test_nothing_archived_means_nothing_hidden(spool):
    item = _spool_to_list_item(spool, include_k_profiles=True, archived_printer_ids=set())
    assert item.k_profile_count == 4
    assert len(item.k_profiles) == 4
