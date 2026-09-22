"""Lifecycle tests for the shared, per-print immutable 3MF analysis."""

import asyncio
import time
import zipfile

import pytest

from backend.app.services.print_file_analysis import (
    PrintFileAnalysis,
    discard_print_file_analysis,
    get_print_file_analysis,
)


class _Manager:
    # Production PrinterManager enables the child process.  The narrow runner
    # seam keeps these deterministic lifecycle tests in-process.
    _uses_print_file_analysis_process = False


def _runner_factory(calls, delay=0.0):
    def runner(path, plate_id):
        calls.append((path, plate_id))
        if delay:
            time.sleep(delay)
        return PrintFileAnalysis(
            filament_usage=[{"slot_id": 1, "used_g": 10.0}],
            layer_usage={1: {0: 2.0}},
            filament_properties={},
        )

    return runner


@pytest.mark.asyncio
async def test_concurrent_readers_share_one_analysis(tmp_path):
    path = tmp_path / "job.3mf"
    path.write_bytes(b"fixture")
    manager = _Manager()
    calls = []
    manager._print_file_analysis_runner = _runner_factory(calls, delay=0.05)

    results = await asyncio.gather(*[get_print_file_analysis(manager, 7, 42, path, 1) for _ in range(100)])

    assert len(calls) == 1
    assert all(result is results[0] for result in results)


@pytest.mark.asyncio
async def test_timed_out_display_reader_does_not_cancel_shared_analysis(tmp_path):
    path = tmp_path / "job.3mf"
    path.write_bytes(b"fixture")
    manager = _Manager()
    calls = []
    manager._print_file_analysis_runner = _runner_factory(calls, delay=0.05)

    assert await get_print_file_analysis(manager, 7, 42, path, None, timeout=0.001) is None
    ready = await get_print_file_analysis(manager, 7, 42, path, None)

    assert ready is not None
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_new_archive_replaces_old_context_and_discard_is_archive_scoped(tmp_path):
    path = tmp_path / "job.3mf"
    path.write_bytes(b"fixture")
    manager = _Manager()
    calls = []
    manager._print_file_analysis_runner = _runner_factory(calls)

    first = await get_print_file_analysis(manager, 7, 42, path, None)
    second = await get_print_file_analysis(manager, 7, 43, path, None)
    discard_print_file_analysis(manager, 7, 42)

    assert first is not second
    assert manager._print_file_analysis_contexts[7].archive_id == 43
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_changed_source_never_reuses_the_previous_table(tmp_path):
    path = tmp_path / "job.3mf"
    path.write_bytes(b"one")
    manager = _Manager()
    calls = []
    manager._print_file_analysis_runner = _runner_factory(calls)

    first = await get_print_file_analysis(manager, 7, 42, path, None)
    path.write_bytes(b"replacement with a different size")
    second = await get_print_file_analysis(manager, 7, 42, path, None)

    assert first is not second
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_real_printer_manager_uses_the_child_executor(tmp_path):
    """The production manager selects the process path, not the test seam."""
    from backend.app.services import print_file_analysis
    from backend.app.services.printer_manager import PrinterManager

    path = tmp_path / "job.3mf"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(
            "Metadata/slice_info.config",
            '<config><filament id="1" used_g="12.5" type="PLA" color="#FFFFFF" /></config>',
        )
        archive.writestr("Metadata/plate_1.gcode", "M73 L1\nM620 S0\nG1 E2\n")

    result = await get_print_file_analysis(PrinterManager(), 7, 42, path, None)

    assert result is not None
    assert result.filament_usage[0]["used_g"] == 12.5
    assert print_file_analysis._executor is not None
    print_file_analysis.shutdown_print_file_analysis_workers()
