"""Lifecycle tests for the shared, per-print immutable 3MF analysis."""

import asyncio
import concurrent.futures
import logging
import os
import time
import zipfile
from dataclasses import asdict
from types import SimpleNamespace

import pytest

from backend.app.services.print_file_analysis import (
    PrintFileAnalysis,
    begin_historical_print_file_analysis_finishing,
    begin_print_file_analysis_finishing,
    bind_print_file_analysis,
    discard_print_file_analysis,
    discard_printer_print_file_analysis,
    get_print_file_analysis,
    get_print_file_analysis_diagnostics,
    notify_print_file_analysis_source_ready,
)


class _Manager:
    # Production PrinterManager enables the analysis service. The narrow runner
    # seam keeps these deterministic lifecycle tests in-process.
    _uses_print_file_analysis_service = False


def test_main_lifecycle_wiring_waits_for_the_late_archive_file(tmp_path, monkeypatch):
    """A start without a 3MF becomes readable only after the attach path says so."""
    from backend.app import main

    manager = _Manager()
    archive = SimpleNamespace(id=42, file_path=None, plate_index=1)
    monkeypatch.setattr(main, "printer_manager", manager)
    monkeypatch.setattr(main.app_settings, "base_dir", tmp_path)

    main._bind_print_file_analysis_context(7, archive)
    context = manager._print_file_analysis_contexts[7]
    assert context.state == "waiting_source"

    # A no-file archive can be revisited freely but must not make a source up.
    main._notify_print_file_analysis_source_ready(7, archive)
    assert manager._print_file_analysis_contexts[7] is context

    archive.file_path = "archive/job.3mf"
    (tmp_path / "archive").mkdir()
    (tmp_path / archive.file_path).write_bytes(b"fixture")
    main._notify_print_file_analysis_source_ready(7, archive)
    ready_context = manager._print_file_analysis_contexts[7]
    assert ready_context is not context
    assert ready_context.state == "empty"
    assert ready_context.source is not None
    assert ready_context.source[0] == str(tmp_path / archive.file_path)


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
async def test_analysis_diagnostics_record_one_parse_many_readers_and_release(tmp_path, caplog):
    caplog.set_level(logging.INFO)
    path = tmp_path / "job.3mf"
    path.write_bytes(b"fixture")
    manager = _Manager()
    calls = []
    manager._print_file_analysis_runner = _runner_factory(calls)

    assert await get_print_file_analysis(manager, 7, 42, path, 1) is not None
    assert await get_print_file_analysis(manager, 7, 42, path, 1) is not None

    diagnostics = get_print_file_analysis_diagnostics(manager)
    assert diagnostics["requests"] == 2
    assert diagnostics["cache_misses"] == 1
    assert diagnostics["cache_hits"] == 1
    assert diagnostics["parses_started"] == 1
    assert diagnostics["parses_ready"] == 1
    assert diagnostics["parse_duration_ms"]["samples"] == 1
    assert diagnostics["last_parse"]["archive_id"] == 42
    assert diagnostics["last_parse"]["source_bytes"] == path.stat().st_size
    assert diagnostics["contexts"][0]["state"] == "ready"

    discard_print_file_analysis(manager, 7, 42)
    released = get_print_file_analysis_diagnostics(manager)
    assert released["contexts_released"] == 1
    assert released["contexts"] == []
    assert "[3MF ANALYSIS] started printer_id=7 archive_id=42" in caplog.text
    assert "[3MF ANALYSIS] ready printer_id=7 archive_id=42" in caplog.text
    assert "[3MF ANALYSIS] released printer_id=7 archive_id=42" in caplog.text


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
    bind_print_file_analysis(manager, 7, 43, path, None)
    second = await get_print_file_analysis(manager, 7, 43, path, None)
    discard_print_file_analysis(manager, 7, 42)

    assert first is not second
    assert manager._print_file_analysis_contexts[7].archive_id == 43
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_start_bind_waits_for_a_matching_source_without_parsing(tmp_path):
    path = tmp_path / "job.3mf"
    path.write_bytes(b"fixture")
    manager = _Manager()
    calls = []
    manager._print_file_analysis_runner = _runner_factory(calls)

    bind_print_file_analysis(manager, 7, 42)
    waiting = manager._print_file_analysis_contexts[7]

    assert waiting.state == "waiting_source"
    assert not notify_print_file_analysis_source_ready(manager, 7, 41, path, 1)
    assert manager._print_file_analysis_contexts[7] is waiting
    assert not calls

    assert notify_print_file_analysis_source_ready(manager, 7, 42, path, 1)
    assert manager._print_file_analysis_contexts[7].state == "empty"
    assert not calls

    assert await get_print_file_analysis(manager, 7, 42, path, 1) is not None
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_failed_analysis_retries_once_after_its_context_backoff(tmp_path):
    path = tmp_path / "job.3mf"
    path.write_bytes(b"fixture")
    manager = _Manager()
    calls = []

    def fails_once(path_text, plate_id):
        calls.append((path_text, plate_id))
        if len(calls) == 1:
            raise ValueError("bad temporary read")
        return PrintFileAnalysis([], None, {})

    manager._print_file_analysis_runner = fails_once

    assert await get_print_file_analysis(manager, 7, 42, path, None) is None
    context = manager._print_file_analysis_contexts[7]
    assert context.state == "unavailable"
    assert len(calls) == 1
    assert await get_print_file_analysis(manager, 7, 42, path, None) is None
    assert len(calls) == 1

    context.retry_at = 0.0
    assert await get_print_file_analysis(manager, 7, 42, path, None) == PrintFileAnalysis([], None, {})
    assert len(calls) == 2


def test_late_source_ready_never_revives_a_retired_or_replaced_print(tmp_path):
    path = tmp_path / "job.3mf"
    path.write_bytes(b"fixture")
    manager = _Manager()

    bind_print_file_analysis(manager, 7, 42)
    discard_print_file_analysis(manager, 7, 42)
    assert not notify_print_file_analysis_source_ready(manager, 7, 42, path, 1)

    bind_print_file_analysis(manager, 7, 43)
    assert not notify_print_file_analysis_source_ready(manager, 7, 42, path, 1)
    assert manager._print_file_analysis_contexts[7].archive_id == 43


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
async def test_same_size_and_mtime_replacement_invalidates_the_table(tmp_path):
    path = tmp_path / "job.3mf"
    path.write_bytes(b"old")
    original = path.stat()
    manager = _Manager()
    calls = []
    manager._print_file_analysis_runner = _runner_factory(calls)
    first = await get_print_file_analysis(manager, 7, 42, path, None)
    replacement = tmp_path / "next.3mf"
    replacement.write_bytes(b"new")
    os.utime(replacement, ns=(original.st_atime_ns, original.st_mtime_ns))
    os.replace(replacement, path)
    assert path.stat().st_ino != original.st_ino
    second = await get_print_file_analysis(manager, 7, 42, path, None)
    assert second is not first
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_ready_analysis_survives_later_source_deletion_until_context_release(tmp_path):
    path = tmp_path / "job.3mf"
    path.write_bytes(b"fixture")
    manager = _Manager()
    calls = []
    manager._print_file_analysis_runner = _runner_factory(calls)

    ready = await get_print_file_analysis(manager, 7, 42, path, None)
    path.unlink()

    assert await get_print_file_analysis(manager, 7, 42, path, None) is ready
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_finishing_context_keeps_old_completion_from_replacing_new_print(tmp_path):
    path = tmp_path / "job.3mf"
    path.write_bytes(b"fixture")
    manager = _Manager()
    calls = []
    manager._print_file_analysis_runner = _runner_factory(calls)

    bind_print_file_analysis(manager, 7, 42, path, 1)
    assert begin_print_file_analysis_finishing(manager, 7, 42)
    bind_print_file_analysis(manager, 7, 43, path, 1)

    old = await get_print_file_analysis(manager, 7, 42, path, 1)
    new = await get_print_file_analysis(manager, 7, 43, path, 1)
    discard_print_file_analysis(manager, 7, 42)

    assert old is not None
    assert new is not None
    assert old is not new
    assert manager._print_file_analysis_contexts[7].archive_id == 43
    assert (7, 42) not in manager._print_file_analysis_finishing_contexts


@pytest.mark.asyncio
async def test_historical_finishing_context_never_replaces_the_current_print(tmp_path):
    path = tmp_path / "old.3mf"
    path.write_bytes(b"fixture")
    manager = _Manager()
    calls = []
    manager._print_file_analysis_runner = _runner_factory(calls)

    bind_print_file_analysis(manager, 7, 43, path, 1)
    assert begin_historical_print_file_analysis_finishing(manager, 7, 42, path, 1)

    old = await get_print_file_analysis(manager, 7, 42, path, 1)
    assert old is not None
    assert manager._print_file_analysis_contexts[7].archive_id == 43
    assert (7, 42) in manager._print_file_analysis_finishing_contexts

    discard_print_file_analysis(manager, 7, 42)
    assert manager._print_file_analysis_contexts[7].archive_id == 43


@pytest.mark.asyncio
async def test_releasing_finishing_context_cancels_its_unneeded_waiter(tmp_path):
    path = tmp_path / "job.3mf"
    path.write_bytes(b"fixture")
    manager = _Manager()
    started = asyncio.Event()

    def slow_runner(path_text, plate_id):
        started_loop.call_soon_threadsafe(started.set)
        release_future.result(timeout=1)
        return PrintFileAnalysis([], None, {})

    started_loop = asyncio.get_running_loop()
    release_future: concurrent.futures.Future[None] = concurrent.futures.Future()
    manager._print_file_analysis_runner = slow_runner
    bind_print_file_analysis(manager, 7, 42, path, None)
    reader = asyncio.create_task(get_print_file_analysis(manager, 7, 42, path, None))
    await asyncio.wait_for(started.wait(), timeout=1)

    assert begin_print_file_analysis_finishing(manager, 7, 42)
    discard_print_file_analysis(manager, 7, 42)
    release_future.set_result(None)

    assert await reader is None
    assert not manager._print_file_analysis_contexts
    assert not manager._print_file_analysis_finishing_contexts


@pytest.mark.asyncio
async def test_admission_cap_rejects_a_new_context_without_starting_a_runner(tmp_path, monkeypatch):
    from backend.app.services import print_file_analysis

    path = tmp_path / "job.3mf"
    path.write_bytes(b"fixture")
    manager = _Manager()
    calls = []
    manager._print_file_analysis_runner = _runner_factory(calls)
    monkeypatch.setattr(print_file_analysis, "_MAX_PENDING_CONTEXTS", 0)

    assert await get_print_file_analysis(manager, 7, 42, path, None) is None
    context = manager._print_file_analysis_contexts[7]
    assert context.state == "unavailable"
    assert context.error == "analysis admission is full"
    assert not calls


@pytest.mark.asyncio
async def test_deadline_marks_context_unavailable_without_leaving_preparing(tmp_path, monkeypatch):
    from backend.app.services import print_file_analysis

    path = tmp_path / "job.3mf"
    path.write_bytes(b"fixture")
    manager = _Manager()
    manager._print_file_analysis_runner = _runner_factory([], delay=0.05)
    monkeypatch.setattr(print_file_analysis, "_ANALYSIS_DEADLINE_SECONDS", 0.001)

    assert await get_print_file_analysis(manager, 7, 42, path, None) is None
    assert manager._print_file_analysis_contexts[7].state == "unavailable"
    assert "deadline" in manager._print_file_analysis_contexts[7].error


def test_parser_rejects_an_oversized_gcode_member_before_extracting(tmp_path, monkeypatch):
    from backend.app.services import print_file_analysis

    path = tmp_path / "job.3mf"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("Metadata/plate_1.gcode", "G1 E12\n")
    monkeypatch.setattr(print_file_analysis, "_MAX_GCODE_BYTES", 1)

    with pytest.raises(print_file_analysis.AnalysisResourceError, match="G-code"):
        print_file_analysis._parse_3mf(str(path), 1)


def test_analysis_progress_index_matches_the_last_layer_at_or_before_target():
    analysis = PrintFileAnalysis(
        [],
        {2: {0: 5.0}, 7: {0: 20.0}, 10: {0: 40.0}},
        {},
    )

    assert analysis.layer_numbers == (2, 7, 10)
    assert analysis.progress_fraction(0, 1) is None
    assert analysis.progress_fraction(0, 8) == pytest.approx(0.5)
    assert analysis.progress_fraction(0, 99) == pytest.approx(1.0)


def test_metadata_totals_survive_an_unavailable_layer_timeline(tmp_path, monkeypatch):
    from backend.app.services import print_file_analysis
    from backend.app.utils import threemf_tools

    path = tmp_path / "job.3mf"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(
            "Metadata/slice_info.config",
            '<config><plate><metadata key="index" value="5"/>'
            '<metadata key="prediction" value="900"/>'
            '<metadata key="weight" value="12.5"/></plate></config>',
        )
        archive.writestr("Metadata/plate_5.gcode", "M73 L1\n")
    monkeypatch.setattr(threemf_tools, "extract_filament_usage_from_3mf", lambda path, plate: [{"used_g": 12.5}])
    monkeypatch.setattr(threemf_tools, "extract_filament_properties_from_3mf", lambda path: {})

    def broken_timeline(path, plate):
        raise ValueError("M82 timeline unavailable")

    monkeypatch.setattr(threemf_tools, "extract_layer_filament_usage_from_3mf", broken_timeline)

    analysis = print_file_analysis._parse_3mf(str(path), 5)

    assert analysis.slicer_estimates == {"print_time_seconds": 900, "filament_used_grams": 12.5}
    assert analysis.layer_usage is None
    assert analysis.timeline_error == "M82 timeline unavailable"


@pytest.mark.asyncio
async def test_retained_analysis_budget_applies_to_contexts_not_readers(tmp_path, monkeypatch):
    from backend.app.services import print_file_analysis

    path = tmp_path / "job.3mf"
    path.write_bytes(b"fixture")
    manager = _Manager()

    def runner(path_text, plate_id):
        return PrintFileAnalysis([], None, {}, retained_bytes=100)

    manager._print_file_analysis_runner = runner
    monkeypatch.setattr(print_file_analysis, "_MAX_RETAINED_ANALYSIS_BYTES", 100)

    assert await get_print_file_analysis(manager, 7, 42, path, None) is not None
    assert begin_print_file_analysis_finishing(manager, 7, 42)
    assert await get_print_file_analysis(manager, 8, 43, path, None) is None
    assert manager._print_file_analysis_contexts[8].state == "unavailable"


def test_stale_completion_cannot_create_or_replace_a_new_current_context(tmp_path):
    path = tmp_path / "job.3mf"
    path.write_bytes(b"fixture")
    manager = _Manager()

    bind_print_file_analysis(manager, 7, 43, path, 1)

    assert not begin_print_file_analysis_finishing(manager, 7, 42)
    assert manager._print_file_analysis_contexts[7].archive_id == 43
    assert not manager._print_file_analysis_finishing_contexts


def test_deleting_a_printer_releases_current_and_finishing_contexts(tmp_path):
    path = tmp_path / "job.3mf"
    path.write_bytes(b"fixture")
    manager = _Manager()

    bind_print_file_analysis(manager, 7, 42, path, None)
    assert begin_print_file_analysis_finishing(manager, 7, 42)
    bind_print_file_analysis(manager, 7, 43, path, None)

    discard_printer_print_file_analysis(manager, 7)

    assert not manager._print_file_analysis_contexts
    assert not manager._print_file_analysis_finishing_contexts


def test_analysis_context_never_enters_printer_state_serialization(tmp_path):
    from backend.app.services.bambu_mqtt import PrinterState

    path = tmp_path / "job.3mf"
    path.write_bytes(b"fixture")
    manager = _Manager()
    bind_print_file_analysis(manager, 7, 42, path, None)

    state = PrinterState()
    assert "_print_file_analysis_contexts" in manager.__dict__
    assert "analysis" not in state.__dict__
    assert "analysis" not in asdict(state)


@pytest.mark.asyncio
async def test_completion_wrapper_releases_its_context_after_an_unexpected_error(tmp_path, monkeypatch):
    from backend.app import main

    path = tmp_path / "job.3mf"
    path.write_bytes(b"fixture")
    manager = _Manager()
    bind_print_file_analysis(manager, 7, 42, path, None)
    assert begin_print_file_analysis_finishing(manager, 7, 42)
    monkeypatch.setattr(main, "printer_manager", manager)

    async def broken_impl(printer_id, data, completion):
        completion["archive_id"] = 42
        raise RuntimeError("late completion failure")

    monkeypatch.setattr(main, "_on_print_complete_impl", broken_impl)
    with pytest.raises(RuntimeError, match="late completion failure"):
        await main.on_print_complete(7, {})

    assert not manager._print_file_analysis_contexts
    assert not manager._print_file_analysis_finishing_contexts
