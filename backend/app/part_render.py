"""One attempt at one plate, in a disposable process (spec §5.6; plan E3, task 16).

The ONLY reader of the source. It opens the file, checks its identity and SHA-256, reads the ZIP,
streams the plate's G-code into Node and reads the top/pick pair. Nothing above this process touches
the path, so a hung mount freezes this process and nothing else. The worker that owns the attempt ends
the read by killing this process's tree, Node included, and proves it gone before it answers.

Bootstrap: one JSON line on stdin, which the guardian closes afterwards. Writes into its attempt
directory: child.pid, node.pid (when Node ran), result.bin (when ok) and result.json, LAST. Exits hard
when a reader thread is still stuck in the source (plan E3, R9).
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import re
import stat
import sys
import time
import zipfile
import zlib
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

import psutil
from PIL import Image

from backend.app.services import part_render_fallback as fallback
from backend.app.services.analysis_source import _open_shared_readonly
from backend.app.services.part_names import tally_objects
from backend.app.services.part_render_node import (
    NodeRenderError,
    NodeRenderResult,
    node_command,
    node_env,
    run_frames,
    verify_bundle,
)
from backend.app.services.part_render_protocol import (
    CHILD_MARGIN_SECONDS,
    GCODE_BYTES,
    INSTANCE_CAP,
    MASTER_SIZE,
    METHODS,
    MODEL_PROBE_ID,
    NODE_OUTPUT_BYTES,
    PNG_BYTES,
    RENDERER_VERSION,
    RSS_BYTES,
    PackError,
    pack,
)
from backend.app.services.part_render_tree import launch, record
from backend.app.services.threemf_parser_core import discover_plate_objects

_BOOT_KEYS = {"root", "task", "deadline_ns", "node"}
# ASCII digits, at most ten: a plate number, never a 5000-digit int() (final review M1)
_PLATE_GCODE = re.compile(r"Metadata/plate_([0-9]{1,10})\.gcode\Z")
_ID_MAX = 0xFFFFFFFF  # a slicer's object id is u32; the instance row is BIGINT (final review C1)
# what zipfile can open here without a password (spec §5.4)
_COMPRESSIONS = frozenset({zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED, zipfile.ZIP_BZIP2, zipfile.ZIP_LZMA})
# An id is at most 10 digits (u32): a longer run is no id at all, and never reaches int(), whose 4300-digit
# limit would raise out of the feed (security review). A non-digit must FOLLOW the id, so digits cut by a
# chunk boundary wait for the next chunk, where MarkerScan's tail sees the marker whole.
_START_MARKER = re.compile(rb"; start printing object, unique label id: *(\d{1,10})(?=\D)")
_MODEL_LABEL = re.compile(rb"; model label id: *([\d,]+)")
_CHUNK = 1024 * 1024
_HEAD_BYTES = 65536


@dataclass(frozen=True)
class Limits:
    instance_cap: int = INSTANCE_CAP
    gcode_bytes: int = GCODE_BYTES
    node_output_bytes: int = NODE_OUTPUT_BYTES
    rss_bytes: int = RSS_BYTES
    zip_entries: int = 10_000  # preview_artifacts.validate's ceilings
    zip_bytes: int = 1024**3
    zip_ratio: int = 1000
    side_bytes: int = 16 * 1024**2  # slice_info.config, top_N.png, pick_N.png, plate_N.json


class Outcome(Exception):
    """The attempt ends with this result: a property of the file, never a crash."""

    def __init__(self, outcome: str, reason: str) -> None:
        super().__init__(f"{outcome}: {reason}")
        self.outcome = outcome
        self.reason = reason


@dataclass
class PlatePlan:
    objects: dict[int, str]
    selected: list[int]
    skipped: list[int]
    gcode_size: int
    too_large: bool = False
    pair: tuple | None = None
    model_bbox: list[float] | None = None


@dataclass
class Instance:
    identify_id: int
    method: str
    reason: str | None = None
    master: bytes | None = None
    tools: list[int] = field(default_factory=list)


class MarkerScan:
    """Which of ``wanted`` had their start marker pass by in the stream that goes to Node (plan E3, R4), and
    whether any marker did (R15). Only the wanted ids are kept: a 256 MiB plate may name millions of them,
    and the scan's memory stays the instance cap's (security review)."""

    def __init__(self, wanted: Iterable[int]) -> None:
        self.wanted = frozenset(wanted)
        self.marked: set[int] = set()
        self.any_marker = False
        self._tail = b""

    def feed(self, chunk: bytes) -> bytes:
        data = self._tail + chunk
        for match in _START_MARKER.finditer(data):
            self.any_marker = True
            if (identify_id := int(match.group(1))) in self.wanted:
                self.marked.add(identify_id)
        self._tail = data[-96:]  # longer than one marker line, so a marker split across chunks is seen whole
        return chunk


def select_instances(objects: dict[int, str], cap: int) -> tuple[list[int], list[int]]:
    """Spec §5.4: the smallest id of every name first, names in key order; then the rest by id."""
    chosen: list[int] = []
    for tally in sorted(tally_objects(objects), key=lambda t: t.name_key):
        if len(chosen) == cap:
            break
        chosen.append(min(tally.identify_ids))
    rest = sorted(set(objects) - set(chosen))
    chosen += rest[: cap - len(chosen)]
    return sorted(chosen), sorted(set(objects) - set(chosen))


def job_for(plan: PlatePlan, limits: Limits) -> dict:
    objects: list[dict] = [{"id": oid, "mode": "toolpath"} for oid in plan.selected]
    if plan.model_bbox is not None and len(plan.objects) == 1 and MODEL_PROBE_ID not in plan.objects:
        objects.append({"id": MODEL_PROBE_ID, "mode": "model", "bbox": plan.model_bbox})
    return {"size": MASTER_SIZE, "outputBytes": limits.node_output_bytes, "objects": objects}


def _same_file(before: os.stat_result, now: os.stat_result) -> bool:
    return stat.S_ISREG(now.st_mode) and (before.st_size, before.st_mtime_ns, before.st_dev, before.st_ino) == (
        now.st_size,
        now.st_mtime_ns,
        now.st_dev,
        now.st_ino,
    )


def open_source(task: dict):
    """The task's file, opened shared read-only, hashed against its key; the only open of the source."""
    root = Path(task["root"]).resolve(strict=True)
    resolved = Path(task["path"]).resolve(strict=True)
    if resolved != root and root not in resolved.parents:
        raise Outcome("failed", "source_changed")  # the row names a file outside its library root
    handle = _open_shared_readonly(str(resolved))
    try:
        before = os.fstat(handle.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_size != task["size"]:
            raise Outcome("failed", "source_changed")
        digest = hashlib.sha256()
        while block := handle.read(_CHUNK):
            digest.update(block)
        if digest.hexdigest() != task["sha256"] or not _same_file(before, os.fstat(handle.fileno())):
            raise Outcome("failed", "source_changed")
        handle.seek(0)
    except BaseException:
        handle.close()
        raise
    return handle, before


def _read_entry(zf: zipfile.ZipFile, name: str, limit: int | None = None) -> bytes:
    """The one way an entry is read here, and strictly: an I/O error goes on as source_read_failed instead
    of turning into "no objects" (consilium E3-R5)."""
    with zf.open(name) as src:
        return src.read(limit) if limit is not None else src.read()


def check_archive(zf: zipfile.ZipFile, limits: Limits) -> dict[str, zipfile.ZipInfo]:
    """Spec §5.4: the archive is judged on its directory before ANY entry is decompressed (consilium E3-R6).
    Too many entries, a name twice, too many bytes in all, or an entry this process cannot open (encrypted,
    an unknown compression -- final review M1): unavailable/invalid_archive, nothing read."""
    infos = zf.infolist()
    names = [info.filename for info in infos]
    if (
        len(infos) > limits.zip_entries
        or len(set(names)) != len(names)
        or sum(info.file_size for info in infos) > limits.zip_bytes
        or any(info.flag_bits & 0x1 or info.compress_type not in _COMPRESSIONS for info in infos)
    ):
        raise Outcome("unavailable", "invalid_archive")
    return {info.filename: info for info in infos}


def _within(info: zipfile.ZipInfo, ceiling: int, ratio: int) -> bool:
    """May this entry be decompressed: under its ceiling, and not a compression bomb."""
    return info.file_size <= ceiling and (not info.compress_size or info.file_size / info.compress_size <= ratio)


def _side(zf: zipfile.ZipFile, infos: dict[str, zipfile.ZipInfo], name: str, limits: Limits) -> bytes | None:
    """A side entry, strictly read -- or None when it is absent or failed its own limits (never decompressed)."""
    info = infos.get(name)
    if info is None or not _within(info, limits.side_bytes, limits.zip_ratio):
        return None
    return _read_entry(zf, name)


def _discover(number: int, gcode_head: bytes, slice_info: bytes | None, pick: bytes | None) -> dict[int, str]:
    """The shared best-effort parser over bytes already read strictly. Its catch-alls now see only a parse
    problem, never an I/O one; its behaviour for every other caller is untouched (consilium E3-R5)."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as view:
        view.writestr(f"Metadata/plate_{number}.gcode", gcode_head)
        if slice_info is not None:
            view.writestr("Metadata/slice_info.config", slice_info)
        if pick is not None:
            view.writestr(f"Metadata/pick_{number}.png", pick)
    with zipfile.ZipFile(io.BytesIO(buffer.getvalue())) as memory:
        return discover_plate_objects(memory, number)


def plan_3mf(zf: zipfile.ZipFile, plate_index: int, limits: Limits) -> tuple[PlatePlan, str]:
    infos = check_archive(zf, limits)
    found = [(int(m.group(1)), name) for name in infos if (m := _PLATE_GCODE.fullmatch(name))]
    plates = dict(found)
    if len(plates) != len(found):
        # plate_3 and plate_03 are one number: which G-code the printer runs is not ours to guess (final review M1)
        raise Outcome("unavailable", "invalid_archive")
    if plate_index == 0:
        if len(plates) != 1:
            raise Outcome("unavailable", "no_gcode")
        ((number, entry),) = plates.items()
    else:
        number, entry = plate_index, plates.get(plate_index)
        if entry is None:
            raise Outcome("unavailable", "no_gcode")
    # every side entry is read here, before Node starts: after it, a feed thread stuck in the ZIP may hold
    # its lock
    side = {
        key: _side(zf, infos, name, limits)
        for key, name in (
            ("slice", "Metadata/slice_info.config"),
            ("pick", f"Metadata/pick_{number}.png"),
            ("top", f"Metadata/top_{number}.png"),
            ("plate", f"Metadata/plate_{number}.json"),
        )
    }
    # an oversized pick is never decoded, here or by the shared parser (security review)
    pick = side["pick"] if fallback.fits(side["pick"]) else None
    objects = _discover(number, _read_entry(zf, entry, _HEAD_BYTES), side["slice"], pick)
    # the shared parser takes any digit run as an id; past u32 it is no slicer's object (final review C1)
    objects = {oid: name for oid, name in objects.items() if 0 <= oid <= _ID_MAX}
    if not objects:
        raise Outcome("unavailable", "no_objects")
    selected, skipped = select_instances(objects, limits.instance_cap)
    info = infos[entry]
    plan = PlatePlan(
        objects,
        selected,
        skipped,
        info.file_size,
        too_large=not _within(info, limits.gcode_bytes, limits.zip_ratio),
    )
    plan.pair = fallback.valid_pair(side["top"], side["pick"]) if side["top"] and side["pick"] else None
    if len(objects) == 1:
        plan.model_bbox = fallback.model_bbox(side["plate"], next(iter(objects)))
    return plan, entry


def plan_gcode(handle, task: dict, limits: Limits) -> PlatePlan:
    """A raw .gcode is plate 0, and its objects come from its own header (plan E3, R5)."""
    if task["plate_index"] != 0:
        raise Outcome("unavailable", "no_gcode")
    head = handle.read(_HEAD_BYTES)
    handle.seek(0)
    match = _MODEL_LABEL.search(head)
    tokens = match.group(1).split(b",") if match else []
    ids = sorted({int(token) for token in tokens if token.isdigit() and len(token) <= 10})  # u32, never int()'s limit
    if not ids:
        raise Outcome("unavailable", "no_objects")
    objects = {oid: f"Object_{oid}" for oid in ids}
    selected, skipped = select_instances(objects, limits.instance_cap)
    return PlatePlan(objects, selected, skipped, task["size"], too_large=task["size"] > limits.gcode_bytes)


def _zip_chunks(zf: zipfile.ZipFile, entry: str, cap: int, scan: MarkerScan) -> Iterator[bytes]:
    with zf.open(entry) as src:
        total = 0
        while block := src.read(_CHUNK):
            total += len(block)
            if total > cap:
                raise OSError("the plate's G-code is longer than its ZIP entry says")
            yield scan.feed(block)


def _file_chunks(handle, cap: int, scan: MarkerScan) -> Iterator[bytes]:
    """The whole file, exactly: ``cap`` is its size, checked against the hash before the first chunk."""
    total = 0
    while block := handle.read(_CHUNK):
        total += len(block)
        if total > cap:
            raise OSError("the G-code grew while it was read")
        yield scan.feed(block)
    if total != cap:
        raise OSError("the G-code ended before its size")  # a short read is half a plate (final review M3)


def run_node(boot: dict, root: Path, job: dict, chunks: Iterator[bytes], limits: Limits) -> NodeRenderResult:
    verify_bundle()  # spec §5.5: the child checks the bundle against its manifest before it starts Node
    deadline_s = (boot["deadline_ns"] - time.monotonic_ns()) / 1e9 - CHILD_MARGIN_SECONDS
    if deadline_s <= 0:
        raise NodeRenderError("timeout", "no time left before Node")
    launch(root, "node")  # before the spawn; on_spawn records it before the first byte of its stdin (R12)
    own = psutil.Process()

    def rss_of(pid: int) -> int:
        return own.memory_info().rss + psutil.Process(pid).memory_info().rss

    return run_frames(
        node_command(Path(boot["node"])),
        job,
        chunks,
        env=node_env(),
        deadline_s=deadline_s,
        output_bytes=limits.node_output_bytes,
        rss_of=rss_of,
        rss_limit=limits.rss_bytes,
        on_spawn=lambda pid: record(root / "node.pid", pid),
    )


def assemble(
    plan: PlatePlan,
    manifest: dict | None,
    pngs: dict[int, bytes],
    marked: set[int],
    *,
    any_marker: bool | None = None,
) -> list[Instance]:
    """``marked``: the selected ids whose marker passed; ``any_marker``: whether any marker passed at all
    (default: whether one of ``marked`` did)."""
    any_marker = bool(marked) if any_marker is None else any_marker
    entries = {entry["id"]: entry for entry in manifest["objects"]} if manifest else {}
    probe = entries.pop(MODEL_PROBE_ID, None) if MODEL_PROBE_ID not in plan.objects else None
    out = [Instance(oid, "skipped", "over_cap") for oid in plan.skipped]
    for oid in plan.selected:
        entry = entries.get(oid)
        if entry is not None and entry["method"] == "toolpath":
            out.append(Instance(oid, "toolpath", master=pngs[oid], tools=list(entry["tools"])))
            continue
        # spec §5.3: model only for a single object WITHOUT markers -- a marked id whose selection came out
        # empty is top_mask or empty_selection, however good the probe looks (consilium E3-R7)
        if probe is not None and not any_marker and probe["method"] == "model":
            out.append(Instance(oid, "model", master=pngs[MODEL_PROBE_ID], tools=list(probe["tools"])))
            continue
        if entry is None:
            reason = "no_valid_pair"  # Node did not run: only the pair can answer
        elif entry.get("reason") == "empty_render":
            reason = "empty_render"
        else:
            reason = "empty_selection" if oid in marked else "no_markers"
        master = fallback.top_mask(plan.pair, oid) if plan.pair is not None else None
        out.append(Instance(oid, "top_mask", master=master) if master else Instance(oid, "missing", reason))
    return sorted(out, key=lambda instance: instance.identify_id)


def finish(root: Path, instances: list[Instance], reason: str | None) -> dict:
    files: dict[str, bytes] = {}
    objects = []
    for inst in instances:
        entry: dict = {
            "identify_id": inst.identify_id,
            "method": inst.method,
            "reason": inst.reason,
            "width": None,
            "height": None,
            "tools": inst.tools,
            "files": {},
        }
        if inst.master is not None:
            if len(inst.master) > PNG_BYTES:
                raise Outcome("failed", "invalid_output")
            with Image.open(io.BytesIO(inst.master)) as img:
                entry["width"], entry["height"] = img.size
            for size_name, body in (("lg", inst.master), ("sm", fallback.small(inst.master))):
                files[f"{inst.identify_id}.{size_name}.png"] = body
                entry["files"][size_name] = {"bytes": len(body), "sha256": hashlib.sha256(body).hexdigest()}
        objects.append(entry)
    manifest = {"renderer": RENDERER_VERSION, "reason": reason, "objects": objects}
    files["manifest.json"] = json.dumps(manifest, separators=(",", ":")).encode()
    try:
        pack(files, root / "result.bin")
    except PackError as exc:
        raise Outcome("failed", "invalid_output") from exc
    return {
        "outcome": "ok",
        "reason": reason,
        "methods": {method: sum(1 for inst in instances if inst.method == method) for method in METHODS},
    }


class _Attempt:
    def __init__(self, boot: dict, limits: Limits) -> None:
        self.boot = boot
        self.limits = limits
        self.root = Path(boot["root"])
        self.handle = None
        self.zf: zipfile.ZipFile | None = None
        self.reader_alive = False

    def run(self) -> dict:
        task = self.boot["task"]
        self.handle, before = open_source(task)
        chunks: Callable[[MarkerScan], Iterator[bytes]]
        if task["kind"] == "3mf":
            self.zf = zipfile.ZipFile(self.handle)
            plan, entry = plan_3mf(self.zf, task["plate_index"], self.limits)
            chunks = lambda scan: _zip_chunks(self.zf, entry, plan.gcode_size, scan)  # noqa: E731
        else:
            plan = plan_gcode(self.handle, task, self.limits)
            chunks = lambda scan: _file_chunks(self.handle, plan.gcode_size, scan)  # noqa: E731
        scan = MarkerScan(plan.selected)
        manifest, pngs, reason = None, {}, None
        if plan.too_large:
            reason = "too_large"
        elif self.boot["node"] is not None:
            try:
                result = run_node(self.boot, self.root, job_for(plan, self.limits), chunks(scan), self.limits)
                manifest, pngs = result.manifest, result.pngs
            except NodeRenderError as exc:
                self.reader_alive = exc.reader_alive
                if exc.reason not in ("memory_limit", "parse_failed"):
                    raise
                reason = exc.reason  # deterministic: the fallback methods answer in this attempt (spec §5.3)
        if not _same_file(before, os.fstat(self.handle.fileno())):
            raise Outcome("failed", "source_changed")
        return finish(self.root, assemble(plan, manifest, pngs, scan.marked, any_marker=scan.any_marker), reason)

    def close(self) -> None:
        for resource in (self.zf, self.handle):
            if resource is not None:
                with contextlib.suppress(Exception):
                    resource.close()


def render_attempt(boot: dict, *, limits: Limits = Limits()) -> tuple[dict, bool]:
    """One plate end to end: (result.json, reader_alive). A property of the file is a result, never a raise."""
    attempt = _Attempt(boot, limits)
    try:
        result = attempt.run()
    except Outcome as end:
        result = {"outcome": end.outcome, "reason": end.reason}
    except NodeRenderError as exc:
        attempt.reader_alive = attempt.reader_alive or exc.reader_alive
        result = {"outcome": "failed", "reason": exc.reason}
    except (OSError, EOFError, zipfile.BadZipFile, zlib.error):
        result = {"outcome": "failed", "reason": "source_read_failed"}
    finally:
        if not attempt.reader_alive:
            attempt.close()  # never with a reader inside: closing would wait on the ZIP's lock
    return result, attempt.reader_alive


def _write_json(path: Path, value: dict) -> None:
    part = path.with_name(path.name + ".part")
    with part.open("x", encoding="utf-8") as out:
        json.dump(value, out, separators=(",", ":"))
        out.flush()
        os.fsync(out.fileno())
    os.replace(part, path)


def main() -> int:
    try:
        boot = json.loads(sys.stdin.buffer.readline(16385))
        if not isinstance(boot, dict) or set(boot) != _BOOT_KEYS:
            return 2
        root = Path(boot["root"])
        if root.is_symlink() or not root.is_dir():
            return 2
    except (ValueError, OSError, TypeError):
        return 2
    # child.launch / child.pid were written by this process's guardian before it sent the bootstrap (R12)
    result, reader_alive = render_attempt(boot)
    try:
        _write_json(root / "result.json", result)
    except OSError:
        return 3
    if reader_alive:
        os._exit(0)  # nothing in this process may wait for the reader stuck in the source (plan E3, R9)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
