"""The few readings the one numbering changed on purpose for every printer (spec r2 §3.2, journal).

Each was a hand copy that named the wrong slot; going through ``utils/tray_ids``
fixed it on the way.
"""

from backend.app.services.archive_colors import _global_to_ams_tray
from backend.app.utils.tray_ids import usage_slot_label


def test_print_summary_names_an_ams_ht_as_itself():
    assert usage_slot_label(128, 0) == "HT-A"  # it read "Ext"
    assert usage_slot_label(0, 1) == "AMS-A T2"
    assert usage_slot_label(255, 0) == "Ext"
    assert usage_slot_label(32, 5) == "AMS-3H T6"
    assert usage_slot_label(255, 4) == "Ext-7"


def test_archive_colour_finds_an_external_spool():
    # External 254 / 255 are stored as (255, 0) / (255, 1); the old inverse gave (254, 0) / (255, 0).
    assert _global_to_ams_tray(254) == (255, 0)
    assert _global_to_ams_tray(255) == (255, 1)
    assert _global_to_ams_tray(130) == (130, 0)
    assert _global_to_ams_tray(6) == (1, 2)
