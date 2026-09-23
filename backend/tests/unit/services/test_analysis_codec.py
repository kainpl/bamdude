import asyncio
import hashlib
import json
import os
import struct

import pytest

from backend.app.services.analysis_codec import AnalysisCodecError, decode, decode_file, encode
from backend.app.services.preview_artifacts import disk
from backend.app.services.print_file_analysis import PrintFileAnalysis


@pytest.mark.parametrize("timeline", [None, {}, {8: {}}, {8: {2: 1.125, 1: 0.5}, 2: {}, 10: {1: 4.25}}])
def test_analysis_artifact_preserves_timeline_and_metadata(tmp_path, timeline):
    source = PrintFileAnalysis(
        filament_usage=[{"slot_id": 2, "used_g": 1.25, "type": "PETG", "color": "#FF00AA"}],
        layer_usage=timeline,
        filament_properties={2: {"density": 1.24, "diameter": 1.75, "type": "PETG"}},
        slicer_estimates={"print_time_seconds": 42, "filament_used_grams": 1.25},
        timeline_error="missing gcode" if timeline is None else None,
        retained_bytes=1234,
    )
    artifact = encode(source, identity="attempt:1")
    expected = decode(artifact, identity="attempt:1", sha256=hashlib.sha256(artifact).hexdigest())
    assert expected.filament_usage == source.filament_usage
    assert expected.layer_usage == source.layer_usage
    assert expected.filament_properties == source.filament_properties
    assert expected.slicer_estimates == source.slicer_estimates
    assert expected.timeline_error == source.timeline_error
    assert expected.retained_bytes == source.retained_bytes
    assert expected.layer_numbers == source.layer_numbers
    path = tmp_path / "result.bda"
    path.write_bytes(artifact)
    assert (
        decode_file(path, identity="attempt:1", size=len(artifact), sha256=hashlib.sha256(artifact).hexdigest())
        == expected
    )


def test_artifact_rejects_trailing_data_wrong_identity_and_digest():
    artifact = encode(PrintFileAnalysis([], {}, {}, retained_bytes=8), identity="a")
    with pytest.raises(AnalysisCodecError, match="length mismatch"):
        decode(artifact + b"x", identity="a")
    with pytest.raises(AnalysisCodecError, match="identity mismatch"):
        decode(artifact, identity="b")
    with pytest.raises(AnalysisCodecError, match="digest mismatch"):
        decode(artifact, identity="a", sha256="0" * 64)


def test_artifact_rejects_duplicate_metadata_key_and_invalid_offsets():
    artifact = encode(PrintFileAnalysis([], {1: {0: 2.0}}, {}, retained_bytes=8), identity="a")
    prefix = struct.Struct("<8sHIII")
    magic, version, metadata_size, layers, entries = prefix.unpack_from(artifact)
    metadata = artifact[prefix.size : prefix.size + metadata_size]
    duplicate = metadata[:-1] + b',"identity":"a"}'
    mutated = (
        prefix.pack(magic, version, len(duplicate), layers, entries)
        + duplicate
        + artifact[prefix.size + metadata_size :]
    )
    with pytest.raises(AnalysisCodecError, match="metadata"):
        decode(mutated, identity="a")

    offsets_start = prefix.size + metadata_size + 4 * layers
    corrupt = bytearray(artifact)
    struct.pack_into("<I", corrupt, offsets_start + 4, entries + 1)
    with pytest.raises(AnalysisCodecError, match="offsets"):
        decode(bytes(corrupt), identity="a")


def test_artifact_rejects_nonfinite_and_bool_channel():
    with pytest.raises(AnalysisCodecError, match="integer"):
        encode(PrintFileAnalysis([], {1: {True: 2.0}}, {}, retained_bytes=8), identity="a")
    with pytest.raises(AnalysisCodecError, match="numeric"):
        encode(PrintFileAnalysis([], {1: {0: float("nan")}}, {}, retained_bytes=8), identity="a")
    artifact = encode(PrintFileAnalysis([], {1: {0: 1.0}}, {}, retained_bytes=8), identity="a")
    corrupt = bytearray(artifact)
    struct.pack_into("<d", corrupt, len(corrupt) - 8, float("inf"))
    with pytest.raises(AnalysisCodecError, match="non-finite"):
        decode(bytes(corrupt), identity="a")


@pytest.mark.skipif(os.environ.get("BAMDUDE_LARGE_TESTS") != "1", reason="run serial with BAMDUDE_LARGE_TESTS=1")
@pytest.mark.asyncio
async def test_one_million_timeline_entries_fit_binary_wire_and_roundtrip(tmp_path):
    timeline = {layer: {channel: layer + channel / 8 for channel in range(1000)} for layer in range(1000)}
    source = PrintFileAnalysis([], timeline, {}, retained_bytes=31 * 1024 * 1024)
    artifact = encode(source, identity="million")
    assert len(artifact) < 20 * 1024 * 1024
    path = tmp_path / "analysis.bin"
    path.write_bytes(artifact)
    gaps = []
    completed = False

    async def heartbeat():
        previous = asyncio.get_running_loop().time()
        while not completed:
            await asyncio.sleep(0.01)
            now = asyncio.get_running_loop().time()
            gaps.append(now - previous)
            previous = now

    ticker = asyncio.create_task(heartbeat())
    try:
        result = await disk(
            decode_file,
            path,
            identity="million",
            size=len(artifact),
            sha256=hashlib.sha256(artifact).hexdigest(),
        )
    finally:
        completed = True
        await ticker
    assert result.layer_usage == timeline
    assert result.layer_numbers == tuple(range(1000))
    max_gap = max(gaps, default=0)
    print(f"million-entry analysis artifact={len(artifact)} bytes, loop max gap={max_gap:.4f}s")
    assert max_gap < 1.0
