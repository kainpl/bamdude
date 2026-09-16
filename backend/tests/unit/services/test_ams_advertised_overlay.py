"""The overlay tells routing what a slot REALLY holds — and only while the printer still shows what we advertised."""

from backend.app.services import ams_advertised_overlay as overlay
from backend.app.services.ams_backup_compatibility import SlotProjection
from backend.app.services.slot_assignment import SlotAssignmentPlan


def _plan(color, idx="GFG00", cols=None):
    return SlotAssignmentPlan(
        tray_info_idx=idx,
        setting_id="x",
        tray_type="PETG",
        tray_color=color,
        cols=cols or [],
        ctype=1 if cols else 0,
    )


def _projection():
    return SlotProjection(
        _plan("FF0000FF", cols=["FF0000FF", "00FF00FF"]),
        _plan("000000FF", idx="GFG99"),
        applied=("generic", "color"),
    )


def setup_function():
    overlay.forget_all()


def test_effective_only_while_live_matches_advertised():
    overlay.remember(7, 0, 1, _projection(), "internal")
    live_before_echo = {"tray_info_idx": "GFG00", "tray_color": "FF0000FF"}
    assert overlay.effective(7, 0, 1, live_before_echo) is None  # printer has not echoed yet — stay silent, keep entry
    echoed = {"tray_info_idx": "GFG99", "tray_color": "000000FF"}
    entry = overlay.effective(7, 0, 1, echoed)
    assert entry is not None and (entry.actual_color, entry.actual_variant, entry.actual_material) == (
        "FF0000FF",
        "GFG00",
        "PETG",
    )
    assert entry.actual_cols == ("FF0000FF", "00FF00FF")
    reconfigured_on_screen = {"tray_info_idx": "GFA00", "tray_color": "000000FF"}
    assert overlay.effective(7, 0, 1, reconfigured_on_screen) is None
    assert (0, 1) in overlay.entries_for(7)  # dormant, not deleted: the echo may still be on its way


def test_colour_compare_ignores_alpha_and_case():
    overlay.remember(7, 0, 1, _projection(), "internal")
    assert overlay.effective(7, 0, 1, {"tray_info_idx": "gfg99", "tray_color": "#000000"}) is not None


def test_an_unprojected_projection_forgets_and_unassign_forgets():
    overlay.remember(7, 0, 1, _projection(), "internal")
    overlay.remember(7, 0, 1, SlotProjection(_plan("FF0000FF"), _plan("FF0000FF")), "internal")
    assert overlay.entries_for(7) == {}
    overlay.remember(7, 0, 2, _projection(), "spoolman")
    overlay.forget(7, 0, 2)
    assert overlay.entries_for(7) == {}
    overlay.remember(7, 0, 3, _projection(), "internal")
    overlay.forget_printer(7)
    assert overlay.entries_for(7) == {}


def test_replace_printer_is_atomic_and_entries_for_is_a_copy():
    overlay.replace_printer(7, {(0, 0): overlay.entry_from(_projection(), "internal")})
    snapshot = overlay.entries_for(7)
    snapshot.clear()
    assert (0, 0) in overlay.entries_for(7)
