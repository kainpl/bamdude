"""Per-slot merge in ``_archive_types_from_spools`` (#2563).

The archive's ``filament_type`` was stamped from the 3MF at creation — the
material the plate was *sliced for*. Once usage tracking has resolved each used
slot to an inventory (or Spoolman) spool, the spool's declared material is the
one actually consumed, so it wins per slot. A slot with no matched spool keeps
its sliced type rather than dropping out.

Mirrors ``test_archive_colors_from_spools`` deliberately — the two fields come
from the same ``slice_info`` and must be refined the same way. The one thing
that must NOT be mirrored is the separator: ``filament_type`` is comma-SPACE
joined (see ``archive.py``) and the frontend material graphs split on ``', '``.
"""

from backend.app.services.usage_tracker import _archive_types_from_spools


def _usage(*slots):
    # slots: (slot_id, used_g, mf_type)
    return [{"slot_id": s, "used_g": g, "type": t} for s, g, t in slots]


def _results(*slots):
    # slots: (slot_id, spool_material)
    return [{"slot_id": s, "material": m} for s, m in slots]


def test_all_slots_matched_uses_spool_materials():
    out = _archive_types_from_spools(
        _usage((1, 10, "PLA"), (2, 5, "PLA")),
        _results((1, "PETG"), (2, "ABS")),
    )
    assert out == ["PETG", "ABS"]


def test_the_bug_a_pla_slice_routed_to_a_petg_spool():
    """The reported case: a single-slot PLA slice hand-mapped to the only loaded
    PETG spool. The deduction hit PETG; the archive must say PETG too."""
    out = _archive_types_from_spools(_usage((1, 12, "PLA")), _results((1, "PETG")))
    assert out == ["PETG"]


def test_partial_match_falls_back_per_slot():
    """Our divergence from upstream's all-or-nothing gate: slot 2 has no spool,
    so it keeps its sliced type instead of discarding slot 1's resolved one.
    Note this is also strictly more correct than upstream on a de-duplicated
    archive: ``archive.py`` collapses PLA/PLA to a single "PLA", which upstream's
    gate would leave untouched even when slot 1 really ran PETG."""
    out = _archive_types_from_spools(
        _usage((1, 10, "PLA"), (2, 5, "TPU")),
        _results((1, "PETG")),
    )
    assert out == ["PETG", "TPU"]


def test_no_match_keeps_sliced_types():
    out = _archive_types_from_spools(_usage((1, 10, "PLA"), (2, 5, "PETG")), _results())
    assert out == ["PLA", "PETG"]


def test_duplicates_collapse_in_slot_order():
    out = _archive_types_from_spools(
        _usage((2, 5, "PLA"), (1, 10, "PLA"), (3, 2, "ABS")),
        _results((1, "PETG"), (2, "PETG")),
    )
    assert out == ["PETG", "ABS"]


def test_unused_slots_are_ignored():
    out = _archive_types_from_spools(
        _usage((1, 10, "PLA"), (2, 0, "TPU")),
        _results((1, "PETG"), (2, "ABS")),
    )
    assert out == ["PETG"]


def test_non_string_spool_material_is_declined():
    """The Spoolman caller feeds ``filament.material`` straight from external
    JSON, so a non-string must fall back to the sliced type rather than reach
    the caller's ``", ".join``."""
    out = _archive_types_from_spools(
        _usage((1, 10, "PLA")),
        [{"slot_id": 1, "material": {"unexpected": "json"}}],
    )
    assert out == ["PLA"]


def test_blank_spool_material_is_declined():
    out = _archive_types_from_spools(_usage((1, 10, "PLA")), _results((1, "   ")))
    assert out == ["PLA"]


def test_materials_are_stripped():
    out = _archive_types_from_spools(_usage((1, 10, "PLA")), _results((1, " PETG ")))
    assert out == ["PETG"]


def test_nothing_usable_returns_none():
    assert _archive_types_from_spools([], _results((1, "PETG"))) is None
    assert _archive_types_from_spools(_usage((1, 10, None)), _results()) is None
