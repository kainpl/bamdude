"""Part-render contract: constants, closed reason sets and the packed result (spec §5.3, §5.4, §7, §8).

Imported by main, the worker and the disposable child alike, so it does no I/O of its own beyond the
files a caller names. Every budget lives here (plan E3, R1): one attempt's packed result is at most
``ATTEMPT_BYTES``, and a crashed attempt's leftover plus the next attempt still fit ``BUCKET_BYTES``.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import struct
from pathlib import Path
from typing import Literal

RENDERER_VERSION = 2  # == frontend/src/part-render/protocol.ts; part_render_node takes it from here
INSTANCE_CAP = 256
GCODE_BYTES = 256 * 1024**2
MASTER_SIZE = 512
SMALL_SIZE = 128
PNG_BYTES = 2 * 1024**2  # one master PNG (512x512 RGBA is 1 MiB raw)
SMALL_PNG_BYTES = 80 * 1024  # 128x128 RGBA is 64 KiB raw
NODE_OUTPUT_BYTES = 96 * 1024**2  # all of Node's stdout for one plate: frames, PNGs and the manifest
ATTEMPT_BYTES = 120 * 1024**2  # one packed result in the Object Store
RSS_BYTES = 2 * 1024**3  # sampled Python + Node of one attempt; E1b (task 7): max 563 MB
NODE_HEAP_MB = 1536
PLATE_SECONDS = 300
CHILD_MARGIN_SECONDS = 5  # the child's own deadline for Node ends this much before the worker's
BUCKET_BYTES = 256 * 1024**2
BUCKET_TTL_SECONDS = 900
GC_GRACE_SECONDS = 7 * 24 * 3600
MAX_ATTEMPTS = 3
RETRY_BASE_SECONDS = 60
# A job entry in mode "model" selects by the proven model area and ignores its id (select.ts), so the
# child asks for it under an id no slicer gives an object (plan E3, R3); a file that uses it anyway
# gets no probe.
MODEL_PROBE_ID = 0xFFFFFFFF
ID_MAX = 0xFFFFFFFF  # a slicer's object id is u32; past it no plate names an object (final review C1)

PlateStatus = Literal["pending", "ready", "failed", "unavailable"]
Phase = Literal["render", "fallback"]
InstanceMethod = Literal["toolpath", "model", "top_mask", "missing", "skipped"]
DEGRADED = ("too_large", "memory_limit", "parse_failed", "render_failed", "rerender_failed", "no_runtime")
TRANSIENT = ("timeout", "crashed", "invalid_output", "source_read_failed")
TERMINAL_FAILED = ("source_changed",)  # + any failure in phase "fallback"
UNAVAILABLE = ("no_gcode", "no_objects", "invalid_archive")  # invalid_archive: plan E3 R13, spec 3.4
RUNTIME_UNAVAILABLE = ("runtime_missing", "runtime_failed", "bundle_mismatch")
RUNTIME_DEGRADED = ("no_runtime",)
MISSING_REASONS = ("no_markers", "empty_selection", "empty_render", "no_valid_pair", "over_cap")
METHODS = ("toolpath", "model", "top_mask", "missing", "skipped")
RENDERED = ("toolpath", "model", "top_mask")
# What the worker answers for one attempt. Every outcome but "unavailable", "busy" and "protocol_error"
# is given only after the attempt's whole process tree was proven gone.
SERVICE_OUTCOMES = (
    "done",
    "timeout",
    "memory_limit",
    "crashed",
    "invalid_output",
    "canceled",
    "busy",
    "unavailable",
    "protocol_error",
)

_MAGIC = b"BDPR\x01"
_HEADER_MAX = 1024 * 1024
_FILE_NAME = re.compile(r"(manifest\.json|[0-9]{1,10}\.(lg|sm)\.png)\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_MANIFEST_MAX = 1024 * 1024  # == part_render_node.MANIFEST_MAX; not imported, the child needs no Node module here


class PackError(ValueError):
    """A packed result that does not describe exactly its own bytes."""


def _ceiling(name: str) -> int:
    if name == "manifest.json":
        return _MANIFEST_MAX
    return PNG_BYTES if name.endswith(".lg.png") else SMALL_PNG_BYTES


def pack(files: dict[str, bytes], dest: Path, *, limit: int = ATTEMPT_BYTES) -> int:
    """Write ``files`` as one result object at ``dest`` (created, never replaced); returns its size."""
    names = sorted(files)
    for name in names:
        if not _FILE_NAME.fullmatch(name):
            raise PackError(f"not a result file name: {name!r}")
        if len(files[name]) > _ceiling(name):
            raise PackError(f"{name} exceeds its ceiling")
    header = json.dumps(
        {
            "files": [
                {"name": name, "bytes": len(files[name]), "sha256": hashlib.sha256(files[name]).hexdigest()}
                for name in names
            ]
        },
        separators=(",", ":"),
    ).encode()
    size = len(_MAGIC) + 4 + len(header) + sum(len(files[name]) for name in names)
    if len(header) > _HEADER_MAX or size > limit:
        raise PackError(f"a result of {size} bytes exceeds {limit}")
    with dest.open("xb") as out:
        out.write(_MAGIC + struct.pack(">I", len(header)) + header)
        for name in names:
            out.write(files[name])
        out.flush()
        os.fsync(out.fileno())
    return size


def unpack(source: Path, dest: Path, *, limit: int = ATTEMPT_BYTES) -> dict[str, tuple[int, str]]:
    """Unpack into the empty directory ``dest``, every file against the header; returns name -> (bytes, sha256)."""
    total = source.stat().st_size
    if total > limit:
        raise PackError(f"a result of {total} bytes exceeds {limit}")
    found: dict[str, tuple[int, str]] = {}
    with source.open("rb") as stream:
        if stream.read(len(_MAGIC)) != _MAGIC:
            raise PackError("not a packed part-render result")
        raw = stream.read(4)
        if len(raw) != 4:
            raise PackError("truncated header length")
        header_len = struct.unpack(">I", raw)[0]
        if header_len > _HEADER_MAX:
            raise PackError("header too large")
        try:
            header = json.loads(stream.read(header_len))
        except ValueError as exc:
            raise PackError("header is not JSON") from exc
        entries = header.get("files") if isinstance(header, dict) else None
        if not isinstance(entries, list):
            raise PackError("header has no file list")
        consumed = len(_MAGIC) + 4 + header_len
        for entry in entries:
            if not isinstance(entry, dict) or set(entry) != {"name", "bytes", "sha256"}:
                raise PackError("malformed header entry")
            name, size, digest = entry["name"], entry["bytes"], entry["sha256"]
            if not isinstance(name, str) or not _FILE_NAME.fullmatch(name) or name in found:
                raise PackError(f"not a result file name: {name!r}")
            if type(size) is not int or not 0 <= size <= _ceiling(name):
                raise PackError(f"{name}: size outside its ceiling")
            if not isinstance(digest, str) or not _SHA256.fullmatch(digest):
                raise PackError(f"{name}: malformed digest")
            body = stream.read(size)
            if len(body) != size or hashlib.sha256(body).hexdigest() != digest:
                raise PackError(f"{name} does not match its header")
            with (dest / name).open("xb") as out:  # SEC-PATH-OK: name matched _FILE_NAME above
                out.write(body)
            found[name] = (size, digest)
            consumed += size
        if stream.read(1) or consumed != total:
            raise PackError("bytes after the last file")
    if "manifest.json" not in found:
        raise PackError("no manifest")
    return found
