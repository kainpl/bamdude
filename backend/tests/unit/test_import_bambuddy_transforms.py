"""The per-row transforms of the upstream-database importer.

``_import_table`` matches source columns to destination columns BY NAME, so a
column that was renamed needs a ``rename=`` and a **value** that was retired
needs a ``transform=``. The second kind is the quiet one: the column still
exists on both sides, so the dead value copies straight through and the row
looks imported until something asks it what its status means.
"""

from backend.app.migrations.import_bambuddy import _transform_project


def test_the_retired_archived_status_becomes_completed():
    """m158 folded ``archived`` into ``completed``. An imported project that
    kept ``archived`` would match no filter, no picker and no validator — and
    ``PATCH`` on it answers 400, because the status it already holds is not one
    this build accepts."""
    assert _transform_project({"name": "Old", "status": "archived"})["status"] == "completed"


def test_every_live_status_is_left_alone():
    for status in ("active", "completed", "cancelled"):
        assert _transform_project({"status": status})["status"] == status


def test_a_row_without_a_status_column_survives_untouched():
    """``_import_table`` selects only the columns both sides have, so a source
    without ``status`` hands the transform a dict that simply lacks the key."""
    row = _transform_project({"name": "No status", "price": 12.5})
    assert row == {"name": "No status", "price": 12.5}
