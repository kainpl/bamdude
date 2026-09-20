"""A completion closes the job it belongs to, or it closes nothing.

Ported from upstream `b5a34b7b` + `a9624d38` (#2819 / #2829), and the same
fault was here: ``on_print_complete`` found the row by printer and
``status='printing'`` alone, then closed ``printing_items[0]``. Nothing in the
MQTT payload identifies a run, so **any** completion delivered for a printer
closed whichever job happened to be printing on it — a calibration run, or the
tail of a previous job.

⚠️ The damage is not a wrong row in a list. The job is marked completed while
the printer is still working, leaves the queue into history, and **strands the
rest of its batch**: ``check_queue`` counts a printing row as a busy printer,
and nothing else ever closes one, so the queue stops until somebody cancels by
hand.

⚠️ Upstream shipped the guard comparing names verbatim and had to fix it a
commit later, because the firmware substitutes underscores for spaces. We were
spared that: ``_subtask_norm`` has folded them since our own X2D closed a
two-hour print on restart. What we did have to add is the **truncation**
tolerance — the printer cuts a long name and marks it ``...``.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from backend.app.main import _completion_belongs_to_item


class _DB:
    """Just enough of a session: ``get`` returns whatever it was handed."""

    def __init__(self, archive=None):
        self._archive = archive

    async def get(self, _model, _pk):
        return self._archive


def _archive(print_name=None, filename=None):
    return SimpleNamespace(print_name=print_name, filename=filename)


def _item(archive_id=7):
    return SimpleNamespace(id=42, archive_id=archive_id)


@pytest.mark.asyncio
class TestItRefusesOnlyOnDisagreement:
    async def test_a_matching_name_closes_the_row(self):
        db = _DB(_archive(filename="Bracket v3.gcode.3mf"))

        assert await _completion_belongs_to_item(db, _item(), {"subtask_name": "Bracket_v3"}) is True

    async def test_a_different_print_leaves_the_row_alone(self):
        db = _DB(_archive(filename="Bracket v3.gcode.3mf"))

        assert await _completion_belongs_to_item(db, _item(), {"subtask_name": "Calibration_Cube"}) is False

    async def test_the_space_underscore_substitution_is_not_a_disagreement(self):
        """The exact shape from upstream #2829: dispatched with spaces, echoed
        with underscores. Comparing verbatim strands the row."""
        db = _DB(_archive(filename="H2D_Carbon_Filter_(V2)_Body & Solid Lid.gcode.3mf"))
        echoed = "H2D_Carbon_Filter_(V2)_Body_&_Solid_Lid"

        assert await _completion_belongs_to_item(db, _item(), {"subtask_name": echoed}) is True

    async def test_a_truncated_echo_still_matches(self):
        db = _DB(_archive(filename="A very long print name that the printer will cut short.gcode.3mf"))

        assert (
            await _completion_belongs_to_item(db, _item(), {"subtask_name": "A_very_long_print_name_that..."}) is True
        )

    async def test_the_archive_side_may_be_the_truncated_one(self):
        """An archive whose name was recorded from an earlier truncated echo."""
        db = _DB(_archive(filename="A_very_long_print_name_that..."))

        full = "A_very_long_print_name_that_the_printer_will_cut_short"
        assert await _completion_belongs_to_item(db, _item(), {"subtask_name": full}) is True

    async def test_print_name_counts_as_well_as_filename(self):
        db = _DB(_archive(print_name="Bracket v3", filename=None))

        assert await _completion_belongs_to_item(db, _item(), {"subtask_name": "Bracket_v3"}) is True

    async def test_a_file_named_only_by_its_extension_closes_its_own_row(self):
        """Live incident 2026-09-06: a library file called just ``.gcode.3mf``,
        uploaded as ``/.3mf``, echoed back as ``.3mf``. Both normalise to the
        empty string — equal, not different — yet the row was left in
        ``printing`` for good, with only the printer's claim released."""
        db = _DB(_archive(print_name="", filename=".gcode.3mf"))

        assert await _completion_belongs_to_item(db, _item(), {"subtask_name": ".3mf"}) is True


@pytest.mark.asyncio
class TestUnverifiableIsNotWrong:
    """Refusing where nothing can be checked would strand the row just as
    thoroughly, only for a different reason."""

    async def test_no_subtask_name_in_the_payload_closes_the_row(self):
        db = _DB(_archive(filename="Bracket v3.gcode.3mf"))

        assert await _completion_belongs_to_item(db, _item(), {}) is True
        assert await _completion_belongs_to_item(db, _item(), {"subtask_name": "   "}) is True

    async def test_a_row_with_no_archive_closes(self):
        assert await _completion_belongs_to_item(_DB(), _item(archive_id=None), {"subtask_name": "x"}) is True

    async def test_a_missing_archive_closes(self):
        assert await _completion_belongs_to_item(_DB(None), _item(), {"subtask_name": "x"}) is True

    async def test_an_archive_with_no_names_closes(self):
        db = _DB(_archive(print_name=None, filename=None))

        assert await _completion_belongs_to_item(db, _item(), {"subtask_name": "x"}) is True
