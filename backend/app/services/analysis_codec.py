"""Bounded, pickle-free artifact for one immutable 3MF analysis result.

Only metadata is JSON. The layer timeline uses little-endian numeric columns
so a million records do not require a million JSON parses on the main loop.
"""

from __future__ import annotations

import hashlib
import json
import math
import mmap
import struct
import sys
from array import array
from pathlib import Path

from backend.app.services.print_file_analysis import PrintFileAnalysis

_MAGIC = b"BDA3MF01"
_VERSION = 1
_PREFIX = struct.Struct("<8sHIII")
_MAX_ARTIFACT = 32 * 1024 * 1024
_MAX_METADATA = 4 * 1024 * 1024
_MAX_ROWS = 1_000_000
_I32_MIN = -(1 << 31)
_I32_MAX = (1 << 31) - 1


class AnalysisCodecError(ValueError):
    """An artifact cannot be trusted or exceeds its transport budget."""


def _integer(value: object, *, signed: bool = True) -> int:
    if type(value) is not int or not (_I32_MIN if signed else 0) <= value <= (_I32_MAX if signed else _MAX_ROWS):
        raise AnalysisCodecError("invalid integer column value")
    return value


def _finite(value: object) -> float:
    if type(value) not in (int, float) or not math.isfinite(float(value)):
        raise AnalysisCodecError("invalid numeric column value")
    return float(value)


def _columns() -> tuple[array, array, array, array]:
    columns = array("i"), array("I"), array("i"), array("d")
    if tuple(column.itemsize for column in columns) != (4, 4, 4, 8):
        raise AnalysisCodecError("unsupported native numeric layout")
    return columns


def _little_bytes(column: array) -> bytes:
    if sys.byteorder == "little":
        return column.tobytes()
    copy = array(column.typecode, column)
    copy.byteswap()
    return copy.tobytes()


def _metadata(analysis: PrintFileAnalysis, identity: str) -> bytes:
    if not isinstance(identity, str) or not 0 < len(identity) <= 256:
        raise AnalysisCodecError("invalid attempt identity")
    properties = [[_integer(key), value] for key, value in sorted(analysis.filament_properties.items())]
    if type(analysis.retained_bytes) is not int or not 0 <= analysis.retained_bytes <= _MAX_ARTIFACT:
        raise AnalysisCodecError("invalid native admission size")
    data = {
        "identity": identity,
        "filament_usage": analysis.filament_usage,
        "filament_properties": properties,
        "slicer_estimates": analysis.slicer_estimates,
        "timeline_error": analysis.timeline_error,
        "timeline_present": analysis.layer_usage is not None,
        "retained_bytes": analysis.retained_bytes,
    }
    try:
        encoded = json.dumps(data, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()
    except (TypeError, ValueError, OverflowError) as exc:
        raise AnalysisCodecError("invalid analysis metadata") from exc
    if len(encoded) > _MAX_METADATA:
        raise AnalysisCodecError("analysis metadata exceeds wire limit")
    return encoded


def encode(analysis: PrintFileAnalysis, *, identity: str) -> bytes:
    """Encode a bounded result; intended for the isolated parser child."""
    metadata = _metadata(analysis, identity)
    layer_ids, offsets, channel_ids, millimetres = _columns()
    offsets.append(0)
    if analysis.layer_usage is not None:
        if len(analysis.layer_usage) > _MAX_ROWS:
            raise AnalysisCodecError("too many layers")
        for layer, channels in sorted(analysis.layer_usage.items()):
            layer_ids.append(_integer(layer))
            if not isinstance(channels, dict):
                raise AnalysisCodecError("invalid layer channels")
            if len(channel_ids) + len(channels) > _MAX_ROWS:
                raise AnalysisCodecError("too many timeline entries")
            for channel, mm in sorted(channels.items()):
                channel_ids.append(_integer(channel))
                millimetres.append(_finite(mm))
            offsets.append(len(channel_ids))
    layers, entries = len(layer_ids), len(channel_ids)
    expected = _PREFIX.size + len(metadata) + 8 * layers + 12 * entries + 4
    if expected > _MAX_ARTIFACT:
        raise AnalysisCodecError("analysis artifact exceeds wire limit")
    return b"".join(
        (
            _PREFIX.pack(_MAGIC, _VERSION, len(metadata), layers, entries),
            metadata,
            _little_bytes(layer_ids),
            _little_bytes(offsets),
            _little_bytes(channel_ids),
            _little_bytes(millimetres),
        )
    )


def _unique_pairs(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise AnalysisCodecError("duplicate metadata key")
        result[key] = value
    return result


def _metadata_depth(value: object, depth: int = 0) -> None:
    if depth > 12:
        raise AnalysisCodecError("metadata nesting exceeds limit")
    if isinstance(value, dict):
        for item in value.values():
            _metadata_depth(item, depth + 1)
    elif isinstance(value, list):
        for item in value:
            _metadata_depth(item, depth + 1)


def _read_metadata(raw: bytes, identity: str) -> dict:
    try:
        data = json.loads(
            raw,
            object_pairs_hook=_unique_pairs,
            parse_constant=lambda _value: (_ for _ in ()).throw(AnalysisCodecError("non-finite metadata")),
        )
    except (ValueError, UnicodeDecodeError, RecursionError) as exc:
        raise AnalysisCodecError("invalid analysis metadata") from exc
    if not isinstance(data, dict) or set(data) != {
        "identity",
        "filament_usage",
        "filament_properties",
        "slicer_estimates",
        "timeline_error",
        "timeline_present",
        "retained_bytes",
    }:
        raise AnalysisCodecError("unknown analysis metadata fields")
    _metadata_depth(data)
    if data["identity"] != identity or not isinstance(identity, str):
        raise AnalysisCodecError("analysis attempt identity mismatch")
    if not isinstance(data["filament_usage"], list) or not all(
        isinstance(item, dict) for item in data["filament_usage"]
    ):
        raise AnalysisCodecError("invalid filament totals")
    if not isinstance(data["slicer_estimates"], dict) or not all(
        isinstance(key, str) and type(value) in (int, float) and math.isfinite(float(value))
        for key, value in data["slicer_estimates"].items()
    ):
        raise AnalysisCodecError("invalid slicer estimates")
    if (
        type(data["timeline_present"]) is not bool
        or data["timeline_error"] is not None
        and not isinstance(data["timeline_error"], str)
    ):
        raise AnalysisCodecError("invalid timeline metadata")
    if type(data["retained_bytes"]) is not int or not 0 <= data["retained_bytes"] <= _MAX_ARTIFACT:
        raise AnalysisCodecError("invalid native admission size")
    properties = {}
    if not isinstance(data["filament_properties"], list):
        raise AnalysisCodecError("invalid filament properties")
    for row in data["filament_properties"]:
        if not isinstance(row, list) or len(row) != 2 or not isinstance(row[1], dict):
            raise AnalysisCodecError("invalid filament property row")
        key = _integer(row[0])
        if key in properties:
            raise AnalysisCodecError("duplicate filament property")
        properties[key] = row[1]
    data["filament_properties"] = properties
    return data


def _decode_view(view: memoryview, *, identity: str) -> PrintFileAnalysis:
    if len(view) < _PREFIX.size + 4 or len(view) > _MAX_ARTIFACT:
        raise AnalysisCodecError("invalid artifact size")
    magic, version, metadata_size, layers, entries = _PREFIX.unpack_from(view)
    if magic != _MAGIC or version != _VERSION or metadata_size > _MAX_METADATA:
        raise AnalysisCodecError("unsupported artifact header")
    if layers > _MAX_ROWS or entries > _MAX_ROWS:
        raise AnalysisCodecError("artifact row count exceeds limit")
    expected = _PREFIX.size + metadata_size + 8 * layers + 12 * entries + 4
    if expected != len(view):
        raise AnalysisCodecError("artifact length mismatch")
    start = _PREFIX.size
    data = _read_metadata(bytes(view[start : start + metadata_size]), identity)
    if not data["timeline_present"] and (layers or entries):
        raise AnalysisCodecError("absent timeline has data")
    start += metadata_size
    layer_end = start + 4 * layers
    offsets_end = layer_end + 4 * (layers + 1)
    channel_end = offsets_end + 4 * entries
    if sys.byteorder != "little":
        raise AnalysisCodecError("big-endian decode requires a supported adapter")
    layer_ids = view[start:layer_end].cast("i")
    offsets = view[layer_end:offsets_end].cast("I")
    channels = view[offsets_end:channel_end].cast("i")
    values = view[channel_end:].cast("d")
    try:
        if len(offsets) != layers + 1 or offsets[0] != 0 or offsets[-1] != entries:
            raise AnalysisCodecError("invalid timeline offsets")
        timeline: dict[int, dict[int, float]] | None = {} if data["timeline_present"] else None
        last_layer = None
        for i, layer in enumerate(layer_ids):
            if last_layer is not None and layer <= last_layer:
                raise AnalysisCodecError("unordered timeline layers")
            first, last = offsets[i], offsets[i + 1]
            if first > last or last > entries:
                raise AnalysisCodecError("invalid timeline offsets")
            row = {}
            previous_channel = None
            for position in range(first, last):
                channel, mm = channels[position], values[position]
                if previous_channel is not None and channel <= previous_channel:
                    raise AnalysisCodecError("unordered timeline channels")
                if not math.isfinite(mm):
                    raise AnalysisCodecError("non-finite timeline usage")
                row[channel] = mm
                previous_channel = channel
            timeline[layer] = row
            last_layer = layer
        return PrintFileAnalysis(
            filament_usage=data["filament_usage"],
            layer_usage=timeline,
            filament_properties=data["filament_properties"],
            slicer_estimates=data["slicer_estimates"],
            timeline_error=data["timeline_error"],
            retained_bytes=data["retained_bytes"],
        )
    finally:
        layer_ids.release()
        offsets.release()
        channels.release()
        values.release()


def decode(data: bytes, *, identity: str, sha256: str | None = None) -> PrintFileAnalysis:
    if sha256 is not None and hashlib.sha256(data).hexdigest() != sha256:
        raise AnalysisCodecError("artifact digest mismatch")
    return _decode_view(memoryview(data), identity=identity)


def decode_file(path: Path, *, identity: str, size: int, sha256: str) -> PrintFileAnalysis:
    """Map a bounded staging file, validate it, then build only the native table."""
    with path.open("rb") as source:
        if source.seek(0, 2) != size or not _PREFIX.size + 4 <= size <= _MAX_ARTIFACT:
            raise AnalysisCodecError("artifact size mismatch")
        source.seek(0)
        with mmap.mmap(source.fileno(), 0, access=mmap.ACCESS_READ) as mapping:
            view = memoryview(mapping)
            try:
                if hashlib.sha256(view).hexdigest() != sha256:
                    raise AnalysisCodecError("artifact digest mismatch")
                return _decode_view(view, identity=identity)
            finally:
                view.release()
