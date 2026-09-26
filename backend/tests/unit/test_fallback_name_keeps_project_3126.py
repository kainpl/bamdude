"""The wrong-plate guard keeps the project name (upstream ebc72e1d, #3126).

#1204's guard refuses a downloaded 3MF that holds another plate than the one
running, and renames the provisional row after the plate that IS running. It
then blanked the name whenever ``swap_plate_suffix`` returned None — but None
also means "this name carries no plate suffix", and such a name holds no stale
plate number to be wrong about. The project name was dropped, the rename fell
through to the gcode_file path, and the archive was titled ``plate_1``.

The name is kept for the TITLE only; for lookups it stays disowned — it has
just fetched another plate's file.
"""

from __future__ import annotations

import inspect
import re

from backend.app import main
from backend.app.main import _name_after_plate_reject


def test_a_name_without_a_plate_suffix_keeps_the_project():
    assert _name_after_plate_reject("Lamp shade", None, "/data/Metadata/plate_1.gcode") == "Lamp shade"


def test_a_suffixed_name_takes_the_running_plate():
    assert (
        _name_after_plate_reject("Lamp - Plate 4", "Lamp - Plate 1", "/data/Metadata/plate_1.gcode") == "Lamp - Plate 1"
    )


def test_with_no_name_at_all_the_gcode_file_is_what_is_left():
    assert _name_after_plate_reject("", None, "/data/Metadata/plate_1.gcode") == "plate_1"
    assert _name_after_plate_reject("", None, "Lamp.gcode.3mf") == "Lamp"


def test_nothing_to_go_on_is_an_empty_name():
    assert _name_after_plate_reject("", None, "") == ""


def test_the_reject_branch_renames_through_the_helper_and_disowns_the_name_for_lookups():
    source = re.sub(r"\(\s+", "(", inspect.getsource(main._on_print_start_impl))
    branch = source[source.index("Could not re-download correct plate") :]
    branch = branch[: branch.index("Post-download content-hash adoption")]
    assert "_name_after_plate_reject(subtask_name, corrected_subtask, filename)" in branch
    # Lookups: only a name that points at the running plate survives.
    assert 'subtask_name = corrected_subtask or ""' in branch
