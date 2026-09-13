"""One reader for a queue row's print time — the queue response and the farm
forecast must agree on it, so it lives in a service, not in the route."""

from pathlib import Path

from backend.app.models.archive import PrintArchive
from backend.app.models.library import LibraryFile
from backend.app.services import queue_times
from backend.app.services.queue_source_descriptor import QueueSourceDescriptor


def a_descriptor(path: Path, *, plate_fallback: int | None = None) -> QueueSourceDescriptor:
    return QueueSourceDescriptor(
        path=path,
        format="3mf",
        sha256="c" * 64,
        size_bytes=1,
        display_filename="lamp.gcode.3mf",
        plate_fallback=plate_fallback,
        queue_source_id=4,
    )


def test_archive_time_wins_and_a_plate_overrides_it(monkeypatch, tmp_path):
    archive = PrintArchive(filename="a", file_path="a.3mf", file_size=1, status="completed", print_time_seconds=100)
    monkeypatch.setattr(queue_times.settings, "base_dir", tmp_path)
    assert queue_times.print_time_for_row(archive=archive, library_file=None, plate_id=None) == 100
    # A plate of a multi-plate archive: the cached per-plate reader answers.
    (tmp_path / "a.3mf").write_bytes(b"not a zip")
    monkeypatch.setattr(queue_times, "plate_metadata_cached", lambda path, plate: (250, 0.0, None))
    assert queue_times.print_time_for_row(archive=archive, library_file=None, plate_id=2) == 250


def test_a_library_row_reads_its_metadata_and_its_plate(monkeypatch, tmp_path):
    lib = LibraryFile(
        filename="l.gcode.3mf",
        file_path="l.gcode.3mf",
        file_size=1,
        file_type="gcode",
        file_metadata={"print_time_seconds": 400},
    )
    monkeypatch.setattr(queue_times.settings, "base_dir", tmp_path)
    assert queue_times.print_time_for_row(archive=None, library_file=lib, plate_id=None) == 400
    (tmp_path / "l.gcode.3mf").write_bytes(b"x")
    monkeypatch.setattr(queue_times, "plate_metadata_cached", lambda path, plate: (90, 0.0, None))
    assert queue_times.print_time_for_row(archive=None, library_file=lib, plate_id=1) == 90


def test_a_missing_file_falls_back_to_the_row_level_estimate(monkeypatch, tmp_path):
    lib = LibraryFile(
        filename="l.gcode.3mf",
        file_path="gone.gcode.3mf",
        file_size=1,
        file_type="gcode",
        file_metadata={"print_time_seconds": 400},
    )
    monkeypatch.setattr(queue_times.settings, "base_dir", tmp_path)
    assert queue_times.print_time_for_row(archive=None, library_file=lib, plate_id=3) == 400
    assert queue_times.print_time_for_row(archive=None, library_file=None, plate_id=None) is None


def test_a_library_row_reads_its_plate_filaments(monkeypatch, tmp_path):
    lib = LibraryFile(
        filename="l.gcode.3mf",
        file_path="l.gcode.3mf",
        file_size=1,
        file_type="gcode",
        file_metadata={
            "plates": [
                {
                    "index": 1,
                    "filaments": [{"slot_id": 1, "type": "PETG", "color": "#000000", "used_g": 12.5}],
                },
                {"index": 2, "filaments": [{"slot_id": 1, "type": "PLA", "used_g": 3.0}]},
            ]
        },
    )
    monkeypatch.setattr(queue_times.settings, "base_dir", tmp_path)
    assert [f["type"] for f in queue_times.filaments_for_row(archive=None, library_file=lib, plate_id=2)] == ["PLA"]
    assert [f["type"] for f in queue_times.filaments_for_row(archive=None, library_file=lib, plate_id=None)] == [
        "PETG",
        "PLA",
    ]
    empty = LibraryFile(filename="e", file_path="e", file_size=1, file_type="gcode", file_metadata={})
    assert queue_times.filaments_for_row(archive=None, library_file=empty, plate_id=None) is None


def test_an_archive_row_reads_the_3mf_or_answers_none(monkeypatch, tmp_path):
    archive = PrintArchive(filename="a", file_path="a.3mf", file_size=1, status="completed")
    monkeypatch.setattr(queue_times.settings, "base_dir", tmp_path)
    assert queue_times.filaments_for_row(archive=archive, library_file=None, plate_id=1) is None
    (tmp_path / "a.3mf").write_bytes(b"x")
    monkeypatch.setattr(
        queue_times,
        "extract_filament_usage_from_3mf",
        lambda path, plate: [{"type": "PETG", "used_g": 9.0}],
    )
    assert queue_times.filaments_for_row(archive=archive, library_file=None, plate_id=1) == [
        {"type": "PETG", "used_g": 9.0}
    ]


# --------------------------------------------------------------------------- #
# m173 — the snapshot answers for the card, and the rows are not consulted
# --------------------------------------------------------------------------- #


def test_the_snapshot_outranks_both_rows_for_the_time(monkeypatch, tmp_path):
    """A job that prints a frozen copy is timed from that copy (spec §4, A09).

    The archive row is present here and holds a DIFFERENT estimate on purpose: a
    reader that preferred the row would pass a test where the row is gone and
    still be wrong about which bytes the number describes.
    """
    archive = PrintArchive(filename="a", file_path="a.3mf", file_size=1, status="completed", print_time_seconds=100)
    monkeypatch.setattr(queue_times.settings, "base_dir", tmp_path)
    snapshot = tmp_path / "object.3mf"
    snapshot.write_bytes(b"x")
    monkeypatch.setattr(queue_times, "plate_metadata_cached", lambda path, plate: (777, 3.5, "Textured PEI Plate"))
    assert (
        queue_times.print_time_for_row(
            archive=archive, library_file=None, plate_id=2, descriptor=a_descriptor(snapshot)
        )
        == 777
    )


def test_the_snapshots_plate_metadata_is_read_once_for_the_whole_card(monkeypatch, tmp_path):
    """One tuple, three answers — the same cache the original path has always used."""
    snapshot = tmp_path / "object.3mf"
    snapshot.write_bytes(b"x")
    monkeypatch.setattr(queue_times, "plate_metadata_cached", lambda path, plate: (60, 12.0, "Cool Plate"))
    assert queue_times.plate_metadata_for_row(plate_id=2, descriptor=a_descriptor(snapshot)) == (
        60,
        12.0,
        "Cool Plate",
    )
    assert queue_times.plate_metadata_for_row(plate_id=2, descriptor=None) == (None, 0.0, None)


def test_a_snapshot_with_no_plate_prediction_falls_back_to_the_rows_own_estimate(monkeypatch, tmp_path):
    """The row's recorded estimate came from the same bytes, so it is not a guess."""
    archive = PrintArchive(filename="a", file_path="a.3mf", file_size=1, status="completed", print_time_seconds=100)
    monkeypatch.setattr(queue_times.settings, "base_dir", tmp_path)
    snapshot = tmp_path / "object.3mf"
    snapshot.write_bytes(b"x")
    monkeypatch.setattr(queue_times, "plate_metadata_cached", lambda path, plate: (None, 0.0, None))
    descriptor = a_descriptor(snapshot)
    assert queue_times.print_time_for_row(archive=archive, library_file=None, plate_id=2, descriptor=descriptor) == 100
    # No row at all: the honest answer is nothing, never an invented number.
    assert queue_times.print_time_for_row(archive=None, library_file=None, plate_id=2, descriptor=descriptor) is None


def test_the_snapshot_uses_the_archive_plate_fallback_when_the_job_names_no_plate(monkeypatch, tmp_path):
    snapshot = tmp_path / "object.3mf"
    snapshot.write_bytes(b"x")
    seen: list[int | None] = []

    def spy(path, plate):
        seen.append(plate)
        return (11, 0.0, None)

    monkeypatch.setattr(queue_times, "plate_metadata_cached", spy)
    descriptor = a_descriptor(snapshot, plate_fallback=15)
    assert queue_times.print_time_for_row(archive=None, library_file=None, plate_id=None, descriptor=descriptor) == 11
    assert queue_times.print_time_for_row(archive=None, library_file=None, plate_id=2, descriptor=descriptor) == 11
    assert seen == [15, 2]


def test_the_snapshot_answers_the_plate_filaments(monkeypatch, tmp_path):
    lib = LibraryFile(
        filename="l.gcode.3mf",
        file_path="l.gcode.3mf",
        file_size=1,
        file_type="gcode",
        file_metadata={"plates": [{"index": 1, "filaments": [{"slot_id": 1, "type": "PETG", "used_g": 1.0}]}]},
    )
    monkeypatch.setattr(queue_times.settings, "base_dir", tmp_path)
    snapshot = tmp_path / "object.3mf"
    snapshot.write_bytes(b"x")
    monkeypatch.setattr(
        queue_times, "extract_filament_usage_from_3mf", lambda path, plate: [{"type": "ABS", "used_g": 9.0}]
    )
    assert queue_times.filaments_for_row(
        archive=None, library_file=lib, plate_id=1, descriptor=a_descriptor(snapshot)
    ) == [{"type": "ABS", "used_g": 9.0}]


def test_a_missing_snapshot_object_answers_nothing_and_never_the_original(monkeypatch, tmp_path):
    """S7: a bad spool fails its own job; it never makes a reader open another file."""
    archive = PrintArchive(filename="a", file_path="a.3mf", file_size=1, status="completed", print_time_seconds=100)
    monkeypatch.setattr(queue_times.settings, "base_dir", tmp_path)
    (tmp_path / "a.3mf").write_bytes(b"x")
    calls: list = []
    monkeypatch.setattr(queue_times, "extract_filament_usage_from_3mf", lambda path, plate: calls.append(path) or [])
    gone = a_descriptor(tmp_path / "not-there.3mf")
    assert queue_times.filaments_for_row(archive=archive, library_file=None, plate_id=1, descriptor=gone) is None
    assert calls == []
