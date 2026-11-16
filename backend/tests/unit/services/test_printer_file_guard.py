"""May a file on the printer be deleted now? (G10 B, M07/M08)."""

from types import SimpleNamespace

import pytest

from backend.app.services import printer_file_guard as guard


@pytest.fixture
def printer(monkeypatch):
    """One observed printer; the test sets its state, freshness and expected prints."""
    box = SimpleNamespace(
        state=SimpleNamespace(connected=True, state="IDLE", gcode_file=None, subtask_name=None),
        received_at=1.0,
        stale=False,
        expected={},
    )
    monkeypatch.setattr(guard.printer_manager, "peek_status", lambda _pid: (box.state, box.received_at, box.stale))
    monkeypatch.setattr(guard, "_expected_archive", lambda _pid, path: box.expected.get(path.rsplit("/", 1)[-1]))
    monkeypatch.setattr(guard, "_printer_expects_a_print", lambda _pid: bool(box.expected))
    return box


def _printing(box, *, state="RUNNING", gcode_file="/sdcard/Cube.3mf", subtask_name="Cube"):
    box.state = SimpleNamespace(connected=True, state=state, gcode_file=gcode_file, subtask_name=subtask_name)


class TestOneFile:
    @pytest.mark.parametrize("state", ["IDLE", "FINISH", "FAILED"])
    def test_an_idle_printer_gives_its_files_up(self, printer, state):
        printer.state.state = state
        # The file of the print that has just finished is still named by the state.
        printer.state.gcode_file = "Cube.3mf"
        assert guard.file_refusal(1, "/Cube.3mf") is None

    @pytest.mark.parametrize("state", ["RUNNING", "PAUSE", "PREPARE", "SLICING"])
    @pytest.mark.parametrize(
        "path",
        [
            "/Cube.3mf",
            "/Cube.gcode.3mf",
            "/cache/Cube.gcode.3mf",
            "/userdata/model/history/Cube.gcode.3mf",
            "/CUBE.3MF",
            "/cache/1_Cube.bbl",
            "/cache/anything.gcode",
        ],
    )
    def test_the_loaded_job_keeps_its_file_and_its_working_copies(self, printer, state, path):
        _printing(printer, state=state)
        assert guard.file_refusal(1, path) == guard.ACTIVE_PRINT

    def test_another_file_is_deleted_while_a_print_runs(self, printer):
        _printing(printer)
        assert guard.file_refusal(1, "/Benchy.3mf") is None
        assert guard.file_refusal(1, "/timelapse/video_2026.mp4") is None
        assert guard.file_refusal(1, "/cache/2_Benchy.bbl") is None

    def test_a_job_named_with_spaces_keeps_its_underscored_cache_copy(self, printer):
        _printing(printer, gcode_file="My Part.3mf", subtask_name="My Part")
        assert guard.file_refusal(1, "/cache/1_My_Part.bbl") == guard.ACTIVE_PRINT
        assert guard.file_refusal(1, "/My Part.3mf") == guard.ACTIVE_PRINT

    def test_only_one_name_reported_is_enough(self, printer):
        _printing(printer, gcode_file=None, subtask_name="Cube")
        assert guard.file_refusal(1, "/Cube.3mf") == guard.ACTIVE_PRINT
        _printing(printer, gcode_file="/sdcard/Cube.gcode.3mf", subtask_name=None)
        assert guard.file_refusal(1, "/Cube.3mf") == guard.ACTIVE_PRINT

    def test_a_job_that_names_no_file_keeps_every_print_file(self, printer):
        _printing(printer, gcode_file=None, subtask_name=None)
        assert guard.file_refusal(1, "/Benchy.3mf") == guard.ACTIVE_PRINT
        assert guard.file_refusal(1, "/cache/x.bbl") == guard.ACTIVE_PRINT
        assert guard.file_refusal(1, "/timelapse/video.mp4") is None

    def test_the_plate_member_inside_the_archive_names_no_file(self, printer):
        """Some firmware reports ``Metadata/plate_1.gcode``: the job's name is then the only name."""
        _printing(printer, gcode_file="/data/Metadata/plate_1.gcode", subtask_name="Cube")
        assert guard.file_refusal(1, "/Cube.3mf") == guard.ACTIVE_PRINT
        assert guard.file_refusal(1, "/Benchy.3mf") is None
        _printing(printer, gcode_file="Metadata/plate_2.gcode", subtask_name=None)
        assert guard.file_refusal(1, "/Benchy.3mf") == guard.ACTIVE_PRINT  # nothing to tell it from

    def test_a_file_that_only_looks_like_a_plate_member_keeps_its_protection(self, printer):
        """Only the exact member form is set aside; a real ``plate_1.gcode`` on the card is a file."""
        _printing(printer, gcode_file="/plate_1.gcode", subtask_name="Cube")
        assert guard.file_refusal(1, "/plate_1.gcode") == guard.ACTIVE_PRINT
        assert guard.file_refusal(1, "/Cube.3mf") == guard.ACTIVE_PRINT
        _printing(printer, gcode_file="/sdcard/metadata/My Part.gcode", subtask_name=None)
        assert guard.file_refusal(1, "/My Part.3mf") == guard.ACTIVE_PRINT
        assert guard.file_refusal(1, "/Benchy.3mf") is None
        _printing(printer, gcode_file="/metadata/plate_x.gcode", subtask_name=None)
        assert guard.file_refusal(1, "/plate_x.gcode") == guard.ACTIVE_PRINT

    def test_a_disconnected_printer_is_not_an_idle_one(self, printer):
        printer.state.connected = False
        assert guard.file_refusal(1, "/Cube.3mf") == guard.STATE_UNKNOWN

    def test_a_silent_printer_is_not_an_idle_one(self, printer):
        printer.stale = True
        assert guard.file_refusal(1, "/Cube.3mf") == guard.STATE_UNKNOWN

    def test_a_printer_that_has_not_reported_is_not_an_idle_one(self, printer):
        printer.received_at = None
        assert guard.file_refusal(1, "/Cube.3mf") == guard.STATE_UNKNOWN
        printer.received_at = 1.0
        printer.state = None
        assert guard.file_refusal(1, "/Cube.3mf") == guard.STATE_UNKNOWN

    def test_a_state_never_seen_is_no_permission(self, printer):
        printer.state.state = "unknown"
        assert guard.file_refusal(1, "/Cube.3mf") == guard.STATE_UNKNOWN

    def test_the_next_prints_file_is_kept_on_an_idle_printer(self, printer):
        printer.expected = {"Cube.3mf": 7}
        assert guard.file_refusal(1, "/Cube.3mf") == guard.EXPECTED_PRINT

    def test_a_dispatcher_clears_its_own_expected_file_only(self, printer):
        printer.expected = {"Cube.3mf": 7}
        assert guard.file_refusal(1, "/Cube.3mf", own_archive_id=7) is None
        assert guard.file_refusal(1, "/Cube.3mf", own_archive_id=8) == guard.EXPECTED_PRINT


class TestBulk:
    @pytest.mark.parametrize("state", ["IDLE", "FINISH", "FAILED"])
    def test_an_idle_printer_may_be_cleared(self, printer, state):
        printer.state.state = state
        assert guard.wide_refusal(1) is None

    @pytest.mark.parametrize("state", ["RUNNING", "PAUSE", "PREPARE", "SLICING"])
    def test_a_printer_with_a_job_is_never_cleared(self, printer, state):
        _printing(printer, state=state)
        assert guard.wide_refusal(1) == guard.ACTIVE_PRINT

    def test_an_unobserved_printer_is_never_cleared(self, printer):
        printer.state.connected = False
        assert guard.wide_refusal(1) == guard.STATE_UNKNOWN
        printer.state.connected = True
        printer.state.state = "unknown"
        assert guard.wide_refusal(1) == guard.STATE_UNKNOWN

    def test_a_print_about_to_start_stops_a_clear(self, printer):
        printer.expected = {"Cube.3mf": 7}
        assert guard.wide_refusal(1) == guard.EXPECTED_PRINT


class TestTheCheckIsAskedAgainAtTheCommand:
    def test_a_print_that_starts_during_the_lane_wait_keeps_its_file(self, printer):
        may_run = guard.may_delete(1, "/Cube.3mf")
        assert may_run() is True
        _printing(printer, state="PREPARE")
        assert may_run() is False

    def test_the_callers_own_condition_is_asked_first(self, printer):
        asked = []
        may_run = guard.may_delete(1, "/Cube.3mf", also=lambda: asked.append(1) or False)
        assert may_run() is False and asked == [1]

    def test_a_walk_stops_when_a_print_becomes_expected(self, printer):
        may_run = guard.may_delete_wide(1)
        assert may_run() is True
        printer.expected = {"Cube.3mf": 7}
        assert may_run() is False


class TestTheRegistryLookup:
    """The two functions main offers the guard, on the real registry."""

    def test_the_archive_of_an_expected_file_is_found_by_every_name_form(self):
        from backend.app import main

        main.register_expected_print(991, "Cube.3mf", 41)
        try:
            assert main.expected_print_archive(991, "/Cube.3mf") == 41
            assert main.expected_print_archive(991, "/cache/Cube.gcode.3mf") == 41
            assert main.expected_print_archive(991, "/Other.3mf") is None
            assert main.expected_print_archive(992, "/Cube.3mf") is None
            assert main.printer_has_expected_print(991) is True
            assert main.printer_has_expected_print(992) is False
            assert main.is_expected_print_file(991, "/Cube.3mf") is True
        finally:
            main.withdraw_expected_print(991, "Cube.3mf")
        assert main.printer_has_expected_print(991) is False
