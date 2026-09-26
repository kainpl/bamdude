"""The printer's own calibration runs are not a user's print (upstream 9c938843,
a4a1f4c5, 164382b3).

Bambu firmware reports its own jobs through the same print-start and
print-complete events a real print uses. Only the ``/usr/`` path was
recognised — bed levelling reports as ``/usr/etc/print/auto_cali_for_user.gcode``
— but the pressure-advance line arrives as a bare SUBTASK name with no path:
automatically before a print as ``auto_pa_line_calib_mode``, by hand as
``pa_line_calib_mode`` or ``pa_pattern_calib_mode``. Each fell past the guard:
an FTP sweep for a 3MF that cannot exist, a no-3MF archive named after the
calibration, a "Print started" — and, worse, the completion took the no-archive
path, which attributes it to any queue item the printer finished in the last
five minutes and emails its owner.

Matched exactly after normalising (directory, one print suffix, case) — "auto",
"calib" and "pa_" are ordinary words in a user's own filenames, and BamDude's own
calibration wizard dispatches ``auto_pa_line_single.3mf`` / ``auto_pa_line_dual.3mf``,
which are prints and must stay archived.
"""

from __future__ import annotations

import inspect
import re

import pytest

from backend.app import main
from backend.app.utils.print_jobs import is_internal_printer_job


class TestWhatCountsAsInternal:
    def test_the_system_partition_path(self):
        assert is_internal_printer_job("/usr/etc/print/auto_cali_for_user.gcode", None)

    @pytest.mark.parametrize(
        "subtask",
        ["auto_pa_line_calib_mode", "pa_line_calib_mode", "pa_pattern_calib_mode", "auto_cali_for_user"],
    )
    def test_a_calibration_reported_by_name_only(self, subtask):
        assert is_internal_printer_job(None, subtask)
        assert is_internal_printer_job("", subtask)

    def test_the_name_is_normalised(self):
        assert is_internal_printer_job("/data/Metadata/AUTO_PA_LINE_CALIB_MODE.gcode.3mf", None)
        assert is_internal_printer_job(None, "Pa_Pattern_Calib_Mode.gcode")

    @pytest.mark.parametrize(
        ("filename", "subtask"),
        [
            ("auto_pa_line_single.3mf", "auto_pa_line_single"),  # BamDude's own calibration print
            ("auto_pa_line_dual.3mf", "auto_pa_line_dual"),
            ("pa_bracket.3mf", "pa_bracket"),
            ("calib_cube.gcode.3mf", "calib_cube"),
            ("Lamp.gcode.3mf", "auto_calibration_holder"),
            (None, None),
        ],
    )
    def test_a_users_print_is_not(self, filename, subtask):
        assert not is_internal_printer_job(filename, subtask)


class TestWhereItIsAsked:
    @staticmethod
    def _source(fn) -> str:
        return re.sub(r"\s+", " ", inspect.getsource(fn))

    def test_print_start_skips_it_and_says_nothing(self):
        source = self._source(main._on_print_start_impl)
        guard = source.index("if is_internal_printer_job(filename, subtask_name):")
        branch = source[guard : source.index("return", guard)]
        assert "_send_print_start_notification" not in branch
        assert 'startswith("/usr/")' not in source

    def test_the_adoption_of_a_running_print_skips_it(self):
        source = self._source(main._adopt_running_print)
        assert "if is_internal_printer_job(filename, subtask_name):" in source
        assert 'startswith("/usr/")' not in source

    def test_its_completion_sends_no_no_archive_notification(self):
        source = self._source(main._on_print_complete_impl)
        notify = source.index("async def _notify_no_archive():")
        branch = source[source.rindex("if not archive_id:", 0, notify) : notify]
        assert "if is_internal_printer_job(filename, subtask_name):" in branch
