"""Per-slot merge in ``_archive_colors_from_spools``.

The archive ``filament_color`` takes each used slot's inventory-spool colour
when matched, else that slot's own 3MF colour (was all-or-nothing before).
"""

from backend.app.services.usage_tracker import _archive_colors_from_spools


def _usage(*slots):
    # slots: (slot_id, used_g, mf_color)
    return [{"slot_id": s, "used_g": g, "color": c} for s, g, c in slots]


def _results(*slots):
    # slots: (slot_id, spool_hex) -- already #RRGGBB normalised
    return [{"slot_id": s, "color": c} for s, c in slots]


def test_all_slots_matched_uses_spool_colours():
    out = _archive_colors_from_spools(
        _usage((1, 10, "#161616"), (2, 5, "#001122")),
        _results((1, "#000000"), (2, "#00FF00")),
    )
    assert out == ["#000000", "#00FF00"]


def test_partial_match_falls_back_per_slot():
    # slot 1 has a spool, slot 2 does not -> spool colour + slot-2 3MF colour
    out = _archive_colors_from_spools(
        _usage((1, 10, "#161616"), (2, 5, "#0011FF")),
        _results((1, "#000000")),
    )
    assert out == ["#000000", "#0011FF"]


def test_no_match_normalises_3mf_colours():
    out = _archive_colors_from_spools(
        _usage((1, 10, "#abcdefff"), (2, 5, "0011ff")),
        _results(),
    )
    assert out == ["#ABCDEF", "#0011FF"]


def test_duplicate_colours_deduped_slot_order():
    out = _archive_colors_from_spools(
        _usage((1, 10, "#111111"), (2, 5, "#222222"), (3, 5, "#111111")),
        _results((1, "#FF0000"), (3, "#FF0000")),
    )
    assert out == ["#FF0000", "#222222"]


def test_spool_without_colour_falls_back_to_3mf():
    # results carries a null colour for slot 1 -> ignored -> 3MF used
    out = _archive_colors_from_spools(
        _usage((1, 10, "#334455")),
        [{"slot_id": 1, "color": None}],
    )
    assert out == ["#334455"]


def test_unused_slots_ignored_and_empty_returns_none():
    assert _archive_colors_from_spools(_usage((1, 0, "#111111")), _results()) is None
    assert _archive_colors_from_spools([], _results()) is None


def test_slot_with_no_colour_anywhere_skipped():
    out = _archive_colors_from_spools(
        _usage((1, 10, None), (2, 5, "#123456")),
        _results(),
    )
    assert out == ["#123456"]
