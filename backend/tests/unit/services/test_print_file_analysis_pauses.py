"""The print-file analysis carries the plate's pauses and starts as soon as its source is bound."""

import asyncio
import struct
import zipfile
from types import SimpleNamespace

import pytest

from backend.app.services import print_file_analysis
from backend.app.services.analysis_codec import AnalysisCodecError, decode, encode
from backend.app.services.print_file_analysis import (
    PrintFileAnalysis,
    bind_print_file_analysis,
    current_pause_points,
    discard_print_file_analysis,
    notify_print_file_analysis_source_ready,
    start_print_file_analysis,
)


class _Manager:
    _uses_print_file_analysis_service = False


def _plate_gcode(pause_layer: int | None) -> str:
    lines = ["M83", "M620 S0A", "M73 P0 R20"]
    for layer in range(1, 11):
        lines += [f"M73 P{layer * 10} R{20 - (layer - 1) * 2}", f"M73 L{layer}"]
        if layer == pause_layer:
            lines.append("M400 U1")
        lines.append("G1 E1.0")
    return "\n".join(lines) + "\n"


def _three_mf(path, plates):
    with zipfile.ZipFile(path, "w") as archive:
        for number, pause_layer in plates.items():
            archive.writestr(f"Metadata/plate_{number}.gcode", _plate_gcode(pause_layer))
    return path


def test_the_parser_reads_the_pauses_of_the_archives_plate(tmp_path):
    path = _three_mf(tmp_path / "job.3mf", {1: None, 2: 6})
    assert print_file_analysis._parse_3mf(str(path), 2).pause_points == ((6, 10),)
    assert print_file_analysis._parse_3mf(str(path), 1).pause_points == ()


def test_the_artifact_round_trips_pauses():
    source = PrintFileAnalysis([], {1: {0: 2.0}}, {}, retained_bytes=8, pause_points=((4, 14), (9, None)))
    assert decode(encode(source, identity="a"), identity="a").pause_points == ((4, 14), (9, None))
    assert (
        decode(encode(PrintFileAnalysis([], {}, {}, retained_bytes=8), identity="a"), identity="a").pause_points == ()
    )


def test_an_artifact_of_the_previous_format_is_refused():
    artifact = encode(PrintFileAnalysis([], {}, {}, retained_bytes=8), identity="a")
    prefix = struct.Struct("<8sHIII")
    magic, version, metadata_size, layers, entries = prefix.unpack_from(artifact)
    assert version == 2
    old = prefix.pack(magic, 1, metadata_size, layers, entries) + artifact[prefix.size :]
    with pytest.raises(AnalysisCodecError, match="unsupported artifact header"):
        decode(old, identity="a")


@pytest.mark.parametrize("points", [((0, 5),), ((4, -1),), ((4, 5), (4, 6)), ((5, 1), (4, 2)), ((4, 1.5),)])
def test_the_artifact_refuses_malformed_pauses(points):
    with pytest.raises(AnalysisCodecError):
        encode(PrintFileAnalysis([], {}, {}, retained_bytes=8, pause_points=points), identity="a")


async def _settle(manager, printer_id=7):
    context = manager._print_file_analysis_contexts[printer_id]
    for _ in range(200):
        if context.state in {"ready", "unavailable"}:
            return context
        await asyncio.sleep(0.01)
    raise AssertionError(f"analysis never settled: {context.state}")


@pytest.mark.asyncio
async def test_a_bound_source_is_analysed_without_any_reader(tmp_path):
    path = _three_mf(tmp_path / "job.3mf", {1: 6})
    manager = _Manager()
    bind_print_file_analysis(manager, 7, 42, path, 1, expected_identity="run-a")
    assert current_pause_points(manager, 7, 42, "run-a") is None

    assert start_print_file_analysis(manager, 7, 42) is True
    await _settle(manager)

    assert current_pause_points(manager, 7, 42, "run-a") == ((6, 10),)
    # Another archive, or another incarnation of the same row, reads nothing.
    assert current_pause_points(manager, 7, 43, "run-a") is None
    assert current_pause_points(manager, 7, 42, "run-b") is None


@pytest.mark.asyncio
async def test_a_print_without_a_file_starts_nothing_until_the_file_arrives(tmp_path):
    manager = _Manager()
    bind_print_file_analysis(manager, 7, 42, None, 1, expected_identity="run-a")
    assert start_print_file_analysis(manager, 7, 42) is False
    assert manager._print_file_analysis_contexts[7].state == "waiting_source"

    path = _three_mf(tmp_path / "job.3mf", {1: 3})
    manager._print_run_bindings = {7: SimpleNamespace(archive_id=42, archive_identity="run-a")}
    assert notify_print_file_analysis_source_ready(manager, 7, 42, path, 1, expected_identity="run-a")
    assert start_print_file_analysis(manager, 7, 42) is True
    await _settle(manager)
    assert current_pause_points(manager, 7, 42, "run-a") == ((3, 16),)


@pytest.mark.asyncio
async def test_the_early_start_parses_once_and_shares_with_later_readers(tmp_path):
    path = tmp_path / "job.3mf"
    path.write_bytes(b"fixture")
    manager = _Manager()
    calls = []

    def runner(path_text, plate_id):
        calls.append(plate_id)
        return PrintFileAnalysis([], {1: {0: 2.0}}, {}, pause_points=((5, None),))

    manager._print_file_analysis_runner = runner
    bind_print_file_analysis(manager, 7, 42, path, 3)
    assert start_print_file_analysis(manager, 7, 42) is True
    assert start_print_file_analysis(manager, 7, 42) is True
    await _settle(manager)
    analysis = await print_file_analysis.get_print_file_analysis(manager, 7, 42, path, 3)
    assert analysis.pause_points == ((5, None),)
    assert calls == [3]


@pytest.mark.asyncio
async def test_a_finished_print_stops_answering(tmp_path):
    path = _three_mf(tmp_path / "job.3mf", {1: 6})
    manager = _Manager()
    bind_print_file_analysis(manager, 7, 42, path, 1, expected_identity="run-a")
    start_print_file_analysis(manager, 7, 42)
    await _settle(manager)
    discard_print_file_analysis(manager, 7, 42)
    assert current_pause_points(manager, 7, 42, "run-a") is None


@pytest.mark.asyncio
async def test_the_ready_analysis_asks_for_a_status_publication(tmp_path):
    path = _three_mf(tmp_path / "job.3mf", {1: 6})
    manager = _Manager()
    published = []
    manager._layer_projection_callback = published.append
    bind_print_file_analysis(manager, 7, 42, path, 1)
    start_print_file_analysis(manager, 7, 42)
    await _settle(manager)
    for _ in range(50):
        if published:
            break
        await asyncio.sleep(0.01)
    assert published == [7]


def test_main_starts_the_analysis_where_it_binds_the_archive(tmp_path, monkeypatch):
    from backend.app import main

    started = []
    manager = _Manager()
    monkeypatch.setattr(main, "printer_manager", manager)
    monkeypatch.setattr(main.app_settings, "base_dir", tmp_path)
    monkeypatch.setattr(print_file_analysis, "start_print_file_analysis", lambda *args: started.append(args[1:]))
    (tmp_path / "job.3mf").write_bytes(b"fixture")
    archive = SimpleNamespace(id=42, file_path="job.3mf", plate_index=1)

    main._bind_print_file_analysis_context(7, archive)
    assert started == [(7, 42)]

    # A late 3MF: the attach paths start it too.
    late = SimpleNamespace(id=43, file_path=None, plate_index=1)
    main._bind_print_file_analysis_context(8, late)
    late.file_path = "job.3mf"
    main._notify_print_file_analysis_source_ready(8, late)
    assert started == [(7, 42), (8, 43), (8, 43)]
