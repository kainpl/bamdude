"""A dispatch without a plan sends the LOWEST-numbered plate, and patches the plate it sends (G03 E9).

Every 3MF with a routing plan carries its resolved plate, so only a job without one reaches the fallback —
calibration assets, raw G-code. The fallback took the FIRST ``Metadata/plate_N.gcode`` member in ZIP order
(upstream's pre-fix bug: member order is the writer's, ``plate_10`` can come before ``plate_2``), and the G-code
injection was built from ``plate_id or 1`` before that fallback ran, so the two could name different plates.
"""

from __future__ import annotations

import inspect
import zipfile

from backend.app.services.background_dispatch import BackgroundDispatchService


def _sliced(tmp_path, *plates: int):
    path = tmp_path / "multi.gcode.3mf"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("Metadata/model_settings.config", "<config/>")
        for plate in plates:
            zf.writestr(f"Metadata/plate_{plate}.gcode", b"G1 X0")
    return path


def test_the_fallback_takes_the_lowest_numbered_plate_not_the_first_member(tmp_path):
    assert BackgroundDispatchService._resolve_plate_id(_sliced(tmp_path, 10, 3, 2), None) == 2


def test_a_requested_plate_still_wins(tmp_path):
    assert BackgroundDispatchService._resolve_plate_id(_sliced(tmp_path, 10, 2), 10) == 10


def test_a_file_with_no_sliced_plate_or_no_zip_is_plate_one(tmp_path):
    assert BackgroundDispatchService._resolve_plate_id(_sliced(tmp_path), None) == 1
    raw = tmp_path / "raw.gcode"
    raw.write_text("G28")
    assert BackgroundDispatchService._resolve_plate_id(raw, None) == 1


def test_both_runners_inject_into_the_plate_they_start():
    """One resolution feeds the injection and the start: no ``plate_id or 1`` of its own beside it."""
    for runner in (BackgroundDispatchService._run_reprint_archive, BackgroundDispatchService._run_print_library_file):
        source = inspect.getsource(runner)
        assert source.count("dispatch_plate_id = await source_probe(") == 1, runner.__name__
        assert "plate_id=dispatch_plate_id" in source or "plate_id=int(dispatch_plate_id)" in source
        assert "plate_id = dispatch_plate_id" in source
        assert 'job.options.get("plate_id") or 1' not in source
