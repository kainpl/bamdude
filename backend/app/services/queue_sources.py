"""Capture a queued job's source into the managed spool — spec §5 and §6.

Spec: ``60-specs/queue-source-spool-spec.md``. This module is the two halves of
§5's seven-step sequence that nothing else may own:

* **the worker** (steps 2–4) — a daemon thread that opens the *original* file,
  streams it in 1 MiB chunks into ``queue-spool/staging/<token>.part`` hashing as
  it goes, fsyncs it, checks the source did not change under it and validates the
  container. It has no DB session, no printer API and no way to publish
  anything: a late worker must not be able to create work (S6).
* **the supervisor** (steps 5–7) — the coroutine the caller awaits. It owns the
  deadlines, cancellation and the storage epoch, and at publication time it holds
  the one storage-mutation guard while it renames the staged file into
  ``objects/<aa>/<sha256>.<ext>``, writes the row and runs the caller's ``attach``
  in a single transaction. **The commit never precedes the file being in place.**

Between the two the caller holds a :class:`CaptureReceipt`: it owns the staged
bytes (so nothing else may unlink them — S6) and carries the right to publish
them exactly once. That gap is where §5 step 4 lives: build requirements and
policy from the *staged* bytes, and on a refusal call
:meth:`CaptureReceipt.discard` instead of :func:`publish`.

⚠️ **Why not ``services/source_io.py``.** That module is deliberately read-only
("never submit writes, DB work or printer commands here") and its four slots are
sized for 5-second metadata probes; a multi-MB copy through it would starve every
readiness check in the app. What is copied from it is the *shape*: a hard cap, a
daemon thread (an OS call cannot be cancelled, and a daemon is not joined at
shutdown — §6 forbids an interpreter that hangs waiting for a dead SMB read), and
a timed-out slot that stays held until the syscall really returns.

⚠️ **One process owns one spool** (§2's deployment contract). The guard here is an
``asyncio.Lock``, not an inter-process lock, and nothing in this module pretends
otherwise.
"""

from __future__ import annotations

import asyncio
import errno
import hashlib
import logging
import os
import secrets
import shutil
import stat as stat_module
import threading
import time
import zipfile
import zlib
from collections.abc import Awaitable, Callable
from concurrent.futures import Future
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.app.core import database
from backend.app.core.config import settings
from backend.app.core.db_portable import _file_work
from backend.app.models.queue_source import (
    FORMAT_3MF,
    QUEUE_SOURCE_FORMATS,
    STATE_DELETING,
    STATE_READY,
    QueueSource,
)
from backend.app.services.backup_files import _signature as _stat_signature, digest
from backend.app.services.queue_source_descriptor import QueueSourceDescriptor, source_snapshot
from backend.app.utils.safe_path import PathTraversalError, assert_under, safe_join_under

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Limits — spec §6. ONE block, and every value is patchable from a test.
# --------------------------------------------------------------------------- #

#: Concurrent capture workers per process. There is deliberately **no waiting
#: queue**: a third caller is refused (``503 source_copy_busy``) and a background
#: tick simply asks again, because an unbounded backlog of copies over a slow
#: share is how one dead mount becomes an unresponsive app.
CAPTURE_WORKERS = 2

#: How many of those slots background legacy hydration (§8) may hold at once.
#: Hydration must never be able to starve an operator who is adding a file now.
HYDRATION_WORKERS = 1

#: Read/write granularity. Also the unit at which the worker re-checks
#: cancellation and reports progress.
CHUNK_SIZE = 1024 * 1024

#: No bytes for this long ⇒ the supervisor gives up (``504``). It bounds a
#: *stalled* transfer, never a slow one: the window restarts with every chunk.
NO_PROGRESS_SECONDS = 60.0

#: Total wall-clock ceiling for one capture, however well it is progressing.
CAPTURE_CEILING_SECONDS = 30 * 60.0

#: Free space that must remain **after** the expected capture, counting what the
#: other live workers have reserved and not yet written (§6).
FREE_SPACE_RESERVE_BYTES = 256 * 1024 * 1024

#: How often the supervisor re-reads the clock while waiting for its worker.
#: Not a spec number — an implementation detail of watching the two deadlines
#: above without a callback from the thread, and small enough in tests that a
#: simulated 30 minutes costs milliseconds.
PROGRESS_POLL_SECONDS = 0.5

#: The garbage collector's two numbers (§6), kept in this block because the spec
#: asks for one home for all of them. Read by ``services/queue_source_gc.py``;
#: unused here by design — a second declaration there would be a second answer.
GC_MIN_INTERVAL_SECONDS = 15 * 60.0
ORPHAN_GRACE_SECONDS = 60 * 60.0

#: Every capture thread is named with this prefix so a leaked one is visible.
THREAD_NAME_PREFIX = "queue-source-capture"

SPOOL_DIRNAME = "queue-spool"
OBJECTS_DIRNAME = "objects"
STAGING_DIRNAME = "staging"
_HEX = frozenset("0123456789abcdef")


# --------------------------------------------------------------------------- #
# Errors — the taxonomy the API maps (§6). The HTTP status lives with the class
# so no route can invent a different one for the same failure.
# --------------------------------------------------------------------------- #


class QueueSourceError(Exception):
    """Base class. ``reason`` is the machine code, ``http_status`` the answer."""

    reason = "source_copy_failed"
    http_status = 500

    def __init__(self, detail: str | None = None) -> None:
        self.detail = detail
        # The reason always leads, so a caller (and a test) can match on it even
        # when a human-readable detail is attached.
        super().__init__(self.reason if detail is None else f"{self.reason}: {detail}")


class QueueSourceBusy(QueueSourceError):
    """No capacity, or another owner is mid-operation. Busy is never evidence
    that a file is broken — the caller retries later (§6)."""

    reason = "source_copy_busy"
    http_status = 503


class StorageReplaced(QueueSourceBusy):
    """A restore swapped the spool under an unpublished receipt (§11).

    A ``QueueSourceBusy`` on purpose: the answer is "ask again", and the API must
    not need a second mapping for it.
    """

    reason = "source_spool_replaced"
    http_status = 503


class SourceUnreadable(QueueSourceError):
    reason = "source_unreadable"
    http_status = 422


class SourceChanged(QueueSourceError):
    """The original changed while it was being read, or the read was short."""

    reason = "source_changed"
    http_status = 422


class SourceInvalid(QueueSourceError):
    """The bytes are not the container they claim to be (truncated 3MF, bad CRC)."""

    reason = "source_invalid"
    http_status = 422


class CaptureTimeout(QueueSourceError):
    reason = "source_copy_timeout"
    http_status = 504


class NoSpace(QueueSourceError):
    reason = "source_spool_no_space"
    http_status = 507


class WriteFailed(QueueSourceError):
    reason = "source_spool_write_failed"
    http_status = 507


class _Abandoned(Exception):
    """Internal: the supervisor stopped waiting, so this worker's bytes are void."""


# --------------------------------------------------------------------------- #
# The spool's layout. Derived from ``settings.data_dir`` at CALL time — a root
# captured at import time would ignore a restore and every test's isolation.
# --------------------------------------------------------------------------- #


def spool_root() -> Path:
    return Path(settings.data_dir) / SPOOL_DIRNAME


def objects_root() -> Path:
    return spool_root() / OBJECTS_DIRNAME


def staging_root() -> Path:
    return spool_root() / STAGING_DIRNAME


def object_relative_path(sha256: str, fmt: str) -> str:
    """The DB's ``relative_path``: POSIX, relative to ``settings.base_dir``.

    Written out rather than derived with ``relative_to`` so the stored string is
    identical on both platforms — this tree travels through portable backup and
    a Windows-flavoured path would not resolve after a restore onto Linux.
    """
    _check_hash(sha256)
    _check_format(fmt)
    return f"{SPOOL_DIRNAME}/{OBJECTS_DIRNAME}/{sha256[:2]}/{sha256}.{fmt}"


def object_path(sha256: str, fmt: str) -> Path:
    """Absolute path of the object for these bytes, containment-checked."""
    _check_hash(sha256)
    _check_format(fmt)
    return safe_join_under(objects_root(), sha256[:2], f"{sha256}.{fmt}", http=False)


def _check_hash(sha256: str) -> None:
    if len(sha256) != 64 or not set(sha256) <= _HEX:
        raise ValueError("a queue source is addressed by a lowercase hex SHA-256")


def _check_format(fmt: str) -> None:
    if fmt not in QUEUE_SOURCE_FORMATS:
        raise ValueError(f"unsupported queue source format {fmt!r}")


# --------------------------------------------------------------------------- #
# Process state: live workers, pins, the storage epoch.
# --------------------------------------------------------------------------- #

_state_lock = threading.Lock()
_inflight: dict[str, _Capture] = {}
_pins: dict[int, int] = {}
_backup_pins = 0
_staging_pins: set[Path] = set()
_epoch = 0

_storage_lock: asyncio.Lock | None = None
_storage_lock_loop: asyncio.AbstractEventLoop | None = None


def _now() -> float:
    """The monotonic clock, behind one name so a test can replace it."""
    return time.monotonic()


def _disk_free(path: Path) -> int:
    return shutil.disk_usage(path).free


def _open_source(path: Path):
    """Open the ORIGINAL. Called only from a worker thread — never from the loop."""
    return path.open("rb")


def _open_staging(path: Path):
    """Create this worker's private ``.part``. Exclusive: a token collision must fail."""
    return path.open("xb")


def _stat_source(path: Path):
    """``stat`` the original (follows links: an external share may legitimately
    hand us one, and we copy content, not the link)."""
    return path.stat()


def active_captures() -> int:
    """Slots currently held — including a worker the supervisor already gave up on."""
    with _state_lock:
        return len(_inflight)


def pinned_source_ids() -> frozenset[int]:
    with _state_lock:
        return frozenset(_pins)


def backup_pinned() -> bool:
    with _state_lock:
        return _backup_pins > 0


def pinned_staging_paths() -> frozenset[Path]:
    """``.part`` files a live worker or an unpublished receipt owns (§9, S6)."""
    with _state_lock:
        return frozenset(_staging_pins)


def current_epoch() -> int:
    with _state_lock:
        return _epoch


def invalidate_receipts() -> int:
    """Void every receipt that has not been published yet (§11, before a DB swap)."""
    global _epoch
    with _state_lock:
        _epoch += 1
        return _epoch


def _reset_state() -> None:
    """Tests only: drop the process state.

    Refuses while a worker is alive rather than warning about it in a docstring.
    Resetting under a live thread would hand its slot to the next test and leave
    it writing into a spool nobody is tracking — a test that leaks a worker has
    to fail, loudly, in its own teardown.
    """
    global _backup_pins, _epoch
    with _state_lock:
        if _inflight:
            raise RuntimeError(f"{len(_inflight)} capture worker(s) still alive; drain them before resetting")
        _pins.clear()
        _staging_pins.clear()
        _backup_pins = 0
        _epoch = 0


def _guard() -> asyncio.Lock:
    """The one storage-mutation guard: capture publication, clone, delete and GC.

    Created lazily and re-created when the running loop changes. A module-level
    ``asyncio.Lock`` caches the loop of its first *contended* acquire and then
    refuses every other one, which in a test suite that builds a fresh loop per
    test is a failure that appears only once two callers contend.
    """
    global _storage_lock, _storage_lock_loop
    loop = asyncio.get_running_loop()
    if _storage_lock is None or _storage_lock_loop is not loop:
        _storage_lock = asyncio.Lock()
        _storage_lock_loop = loop
    return _storage_lock


@asynccontextmanager
async def storage_mutation():
    """Serialise everything that changes the spool or its rows."""
    async with _guard():
        yield


@asynccontextmanager
async def pin(source_id: int):
    """Hold a blob against the GC for the body of the ``async with`` (§9).

    Registered **under the storage guard** on purpose: that is what makes the
    GC's "re-check the pins under the guard" authoritative — a pin taken while
    the GC holds the guard cannot slip in after its decision.

    ⚠️ Never call this from inside :func:`publish`'s ``attach``: that callback
    already runs under the guard and would deadlock.
    """
    global _pins
    async with storage_mutation():
        with _state_lock:
            _pins[source_id] = _pins.get(source_id, 0) + 1
    try:
        yield
    finally:
        with _state_lock:
            remaining = _pins.get(source_id, 0) - 1
            if remaining > 0:
                _pins[source_id] = remaining
            else:
                _pins.pop(source_id, None)


@asynccontextmanager
async def pin_backup():
    """Hold every blob against the GC while a backup stages its files (§11)."""
    global _backup_pins
    async with storage_mutation():
        with _state_lock:
            _backup_pins += 1
    try:
        yield
    finally:
        with _state_lock:
            _backup_pins = max(0, _backup_pins - 1)


def _blob_is_pinned(source_id: int) -> bool:
    with _state_lock:
        return source_id in _pins or _backup_pins > 0


# --------------------------------------------------------------------------- #
# The request, the live record and the receipt
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class CaptureRequest:
    """What to capture. ``path`` is touched by the worker thread and nowhere else."""

    path: Path
    format: str
    display_filename: str
    plate_fallback: int | None = None
    provenance: dict[str, Any] = field(default_factory=dict)
    #: Background legacy hydration (§8) — bounded by :data:`HYDRATION_WORKERS`.
    background: bool = False


class _Capture:
    """One live worker's slot: its reservation, its progress and its staging."""

    __slots__ = ("token", "expected", "written", "started", "last_progress", "stop", "abandoned", "background", "part")

    def __init__(self, token: str, *, background: bool) -> None:
        self.token = token
        self.expected = 0
        self.written = 0
        self.started = _now()
        self.last_progress = self.started
        self.stop = threading.Event()
        self.abandoned = False
        self.background = background
        self.part: Path | None = None

    def outstanding(self) -> int:
        return max(0, self.expected - self.written)


@dataclass(slots=True)
class CaptureReceipt:
    """Verified bytes in staging, plus the right to publish them once.

    The receipt is the only owner of its ``.part`` file: no other caller and no
    GC sweep may unlink it (S6). A caller that decides against the job must
    :meth:`discard` it rather than leave it for the grace window.
    """

    token: str
    staging_path: Path
    sha256: str
    size_bytes: int
    format: str
    display_filename: str
    plate_fallback: int | None
    provenance: dict[str, Any]
    epoch: int
    state: str = "staged"

    def descriptor(self, source: QueueSource) -> QueueSourceDescriptor:
        """The one shape readers get — path and format come from the ROW.

        The row is the disk truth: on a reuse the published object may predate
        this capture entirely.
        """
        return QueueSourceDescriptor(
            path=Path(settings.base_dir) / source.relative_path,
            format=source.format,
            sha256=source.sha256,
            size_bytes=source.size_bytes,
            display_filename=self.display_filename,
            plate_fallback=self.plate_fallback,
            provenance=dict(self.provenance),
        )

    async def discard(self) -> None:
        """Give up the staged bytes (§5 step 4's refusal, or any caller's own)."""
        if self.state == "published":
            raise RuntimeError("a published receipt has no staged bytes to discard")
        self.state = "discarded"
        _unpin_staging(self.staging_path)
        await _file_work(_drop, self.staging_path)

    def _claim(self) -> None:
        if self.state != "staged":
            raise RuntimeError(f"this capture receipt is already {self.state}")
        self.state = "publishing"

    def _release_claim(self) -> None:
        """Hand the receipt back as publishable — only when nothing moved."""
        if self.state == "publishing":
            self.state = "staged"

    def _spend(self) -> None:
        """The staged file is gone, so this receipt can never publish again.

        A publication that failed *after* the rename has no bytes left to retry
        with: handing the receipt back as ``staged`` would advertise a file that
        does not exist, and the retry would reach ``os.replace`` on nothing and
        escape the error taxonomy as a bare ``OSError``. The caller must capture
        again.
        """
        if self.state == "publishing":
            self.state = "discarded"


class _Publication:
    """One publication's single question: has the staged file left staging yet?

    A holder rather than a return value because both writing paths — a brand-new
    object and a repair — can move it, and the failure handler must know even
    when the exception came from inside the move itself.
    """

    __slots__ = ("staging_consumed",)

    def __init__(self) -> None:
        self.staging_consumed = False


def snapshot_for(receipt: CaptureReceipt, source: QueueSource) -> dict[str, Any]:
    """The ``source_snapshot`` payload for a job backed by this capture.

    One line, and it exists so that no queue writer hand-writes the dict:
    :func:`~backend.app.services.queue_source_descriptor.source_snapshot` is the
    single builder of that layout.
    """
    return source_snapshot(receipt.descriptor(source))


# --------------------------------------------------------------------------- #
# Filesystem primitives (always off the loop)
# --------------------------------------------------------------------------- #


def _drop(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:
        # Never pretend a completed rollback when the storage call failed (§6).
        logger.warning("Could not remove queue-spool file %s: %s", path, exc)


def _fsync_dir(path: Path) -> None:
    """Flush the directory entry where the platform supports it."""
    if os.name == "nt":  # Windows has no directory handle to fsync.
        return
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError as exc:
        logger.debug("Directory fsync unsupported for %s: %s", path, exc)
        return
    try:
        os.fsync(fd)
    except OSError as exc:
        logger.debug("Directory fsync failed for %s: %s", path, exc)
    finally:
        os.close(fd)


def _install_object(part: Path, target: Path) -> None:
    """Atomically move the staged file into its final content-addressed place.

    The containment check lives here rather than at the call site because
    ``target`` can come from a row's ``relative_path``: the check resolves paths,
    which is filesystem work, and §6 keeps that off the loop. A row that names a
    path outside the spool is a refusal inside the taxonomy (507), never a
    traversal that reaches ``os.replace`` and never a bare 500.
    """
    try:
        assert_under(objects_root(), target, http=False)
    except PathTraversalError as exc:
        raise WriteFailed(f"{target} is not inside the queue spool") from exc
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        os.replace(part, target)
    except OSError as exc:
        # Mapped, not left to escape as a bare OSError — and it keeps the rule
        # :func:`_install` depends on exact: a ``WriteFailed`` out of this
        # function always means the staged file did NOT move (``mkdir`` runs
        # before the rename, and a rename either happens or does not).
        raise _write_failure(exc) from exc
    _fsync_dir(target.parent)


def _verify_object(target: Path, sha256: str, size_bytes: int) -> bool:
    """Is the published object present and still the bytes it claims to be?

    §7 verifies the full hash at capture, reuse and restore — cheap here (a local
    read) and the only thing that makes reuse safe. A row whose ``relative_path``
    points outside the spool is not "a file we could not verify", it is a row
    that must never be read from: contained first, hashed second.
    """
    try:
        assert_under(objects_root(), target, http=False)
        if not target.is_file() or target.stat().st_size != size_bytes:
            return False
        return digest(target) == sha256
    except (OSError, PathTraversalError) as exc:
        logger.warning("Queue source object %s could not be verified: %s", target, exc)
        return False


def _validate_container(part: Path, fmt: str) -> None:
    """Refuse bytes that are not the container the caller claimed (§5 step 3)."""
    if fmt == FORMAT_3MF:
        try:
            with zipfile.ZipFile(part) as zf:
                if not zf.namelist():
                    raise SourceInvalid("the 3MF container is empty")
                broken = zf.testzip()
            if broken is not None:
                raise SourceInvalid(f"CRC mismatch in {broken}")
        except (zipfile.BadZipFile, EOFError, zlib.error) as exc:
            # ⚠️ ``zlib.error`` belongs here: a mangled deflate stream raises
            # BadZipFile most of the time and "invalid block type" the rest, so a
            # guard without it lets roughly one torn file in five through
            # (measured on the printer-file path, routes/printers.py).
            raise SourceInvalid(f"not a readable 3MF container: {exc}") from exc
        except OSError as exc:
            raise WriteFailed(f"the staged copy could not be re-read: {exc}") from exc
    else:
        try:
            with part.open("rb") as handle:
                head = handle.read(4)
        except OSError as exc:
            raise WriteFailed(f"the staged copy could not be re-read: {exc}") from exc
        if head[:4] == b"PK\x03\x04":
            # Raw gcode has its own per-printer rules downstream; a ZIP stored
            # under that format would take them with the wrong file (§4).
            raise SourceInvalid("a ZIP container was offered as raw gcode")


# --------------------------------------------------------------------------- #
# The worker (thread side). No DB session, no printer API, no publication.
# --------------------------------------------------------------------------- #


def _reserve_space(record: _Capture, expected: int, root: Path) -> None:
    with _state_lock:
        record.expected = expected
        others = sum(other.outstanding() for other in _inflight.values() if other is not record)
    try:
        free = _disk_free(root)
    except OSError as exc:
        raise WriteFailed(f"the spool could not be measured: {exc}") from exc
    if free < expected + others + FREE_SPACE_RESERVE_BYTES:
        raise NoSpace(
            f"{free} bytes free, need {expected} for this copy plus {others} reserved "
            f"by other workers and a {FREE_SPACE_RESERVE_BYTES} byte reserve"
        )


def _capture_bytes(record: _Capture, request: CaptureRequest) -> tuple[str, int]:
    """Copy the original into this worker's ``.part``, hashing as it goes."""
    staging = staging_root()
    try:
        staging.mkdir(parents=True, exist_ok=True)
        objects_root().mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise WriteFailed(f"the spool directories could not be created: {exc}") from exc

    try:
        before = _stat_source(request.path)
    except OSError as exc:
        raise SourceUnreadable(f"{request.path}: {exc}") from exc
    if not stat_module.S_ISREG(before.st_mode):
        raise SourceUnreadable(f"{request.path} is not a regular file")

    _reserve_space(record, before.st_size, staging)

    part = staging / f"{record.token}.part"
    record.part = part
    digest_state = hashlib.sha256()
    written = 0
    # Opened in two steps so the two failures keep their own codes: the original
    # is the source's fault (422), our own staging file is the spool's (507).
    try:
        source_handle = _open_source(request.path)
    except OSError as exc:
        raise SourceUnreadable(f"{request.path}: {exc}") from exc
    with source_handle as reader:
        try:
            staging_handle = _open_staging(part)
        except OSError as exc:
            raise _write_failure(exc) from exc
        with staging_handle as writer:
            while True:
                if record.stop.is_set():
                    raise _Abandoned()
                try:
                    chunk = reader.read(CHUNK_SIZE)
                except OSError as exc:
                    raise SourceUnreadable(f"{request.path}: {exc}") from exc
                if record.stop.is_set():
                    raise _Abandoned()
                if not chunk:
                    break
                try:
                    writer.write(chunk)
                except OSError as exc:
                    raise _write_failure(exc) from exc
                digest_state.update(chunk)
                written += len(chunk)
                progress_at = _now()  # read the clock before taking the lock
                with _state_lock:
                    record.written = written
                    record.last_progress = progress_at
                if _now() - record.started > CAPTURE_CEILING_SECONDS:
                    raise CaptureTimeout(f"capture exceeded {CAPTURE_CEILING_SECONDS} seconds")
            try:
                writer.flush()
                os.fsync(writer.fileno())
            except OSError as exc:
                raise _write_failure(exc) from exc

    try:
        after = _stat_source(request.path)
    except OSError as exc:
        raise SourceUnreadable(f"{request.path}: {exc}") from exc
    if _stat_signature(before) != _stat_signature(after):
        raise SourceChanged(f"{request.path} changed while it was being copied")
    if written != before.st_size:
        raise SourceChanged(f"read {written} of {before.st_size} bytes from {request.path}")
    if written == 0:
        raise SourceInvalid(f"{request.path} is empty")

    _validate_container(part, request.format)
    return digest_state.hexdigest(), written


def _write_failure(exc: OSError) -> QueueSourceError:
    """Both spool write failures answer 507; the reason code says which (§6)."""
    if exc.errno in (errno.ENOSPC, errno.EDQUOT):
        return NoSpace(str(exc))
    return WriteFailed(str(exc))


def _finish(record: _Capture, *, kept: bool) -> bool:
    """Release the slot; hand the ``.part`` to the receipt, or clean it up.

    Returns whether the bytes survived. The decision is made under the lock
    together with dropping out of ``_inflight``, so the supervisor can tell
    "this worker is still alive, abandon it" from "it already finished".
    """
    with _state_lock:
        handover = kept and not record.abandoned
        _inflight.pop(record.token, None)
        if handover and record.part is not None:
            _staging_pins.add(record.part)
    if not handover and record.part is not None:
        # Only the owning worker ever unlinks its own staging file (S6).
        _drop(record.part)
    return handover


def _work(record: _Capture, request: CaptureRequest, future: Future) -> None:
    try:
        sha256, size = _capture_bytes(record, request)
    except BaseException as exc:  # noqa: BLE001 - every failure travels to the supervisor
        _finish(record, kept=False)
        future.set_exception(exc)
        return
    if _finish(record, kept=True):
        future.set_result((sha256, size, record.part))
    else:
        # The supervisor stopped waiting: these bytes are gone and nobody is
        # owed a result. The future's exception is consumed by the callback the
        # supervisor left behind.
        future.set_exception(_Abandoned())


def _unpin_staging(path: Path) -> None:
    with _state_lock:
        _staging_pins.discard(path)


# --------------------------------------------------------------------------- #
# The supervisor (loop side)
# --------------------------------------------------------------------------- #


def _admit(request: CaptureRequest) -> _Capture:
    """Take a slot or refuse. Never waits — §6 rules out a backlog of copies."""
    with _state_lock:
        if len(_inflight) >= CAPTURE_WORKERS:
            raise QueueSourceBusy(f"all {CAPTURE_WORKERS} capture workers are busy")
        if request.background and sum(1 for r in _inflight.values() if r.background) >= HYDRATION_WORKERS:
            raise QueueSourceBusy("background hydration already holds its slot")
        record = _Capture(secrets.token_hex(16), background=request.background)
        _inflight[record.token] = record
        return record


def _remaining(record: _Capture) -> float:
    with _state_lock:
        last_progress, started = record.last_progress, record.started
    now = _now()
    return min(NO_PROGRESS_SECONDS - (now - last_progress), CAPTURE_CEILING_SECONDS - (now - started))


def _abandon(record: _Capture) -> bool:
    """Stop waiting for this worker. False ⇒ it already finished, so its answer stands."""
    with _state_lock:
        if record.token not in _inflight:
            return False
        record.abandoned = True
        record.stop.set()
        return True


async def capture(source: CaptureRequest) -> CaptureReceipt:
    """Copy ``source`` into staging and hash it — spec §5 steps 2–4.

    The caller must already have released any long DB transaction (§5 step 1):
    this awaits a filesystem copy that may take minutes.

    Raises :class:`QueueSourceBusy` (no slot), :class:`SourceUnreadable`,
    :class:`SourceChanged`, :class:`SourceInvalid`, :class:`CaptureTimeout`,
    :class:`NoSpace` or :class:`WriteFailed`. On every one of those the staged
    bytes are cleaned up by the worker that wrote them and the slot is released
    when the syscall really returns — which, for a hung mount, can be later than
    the refusal (§6).
    """
    _check_format(source.format)
    record = _admit(source)
    future: Future = Future()
    threading.Thread(
        target=_work,
        args=(record, source, future),
        name=f"{THREAD_NAME_PREFIX}-{record.token[:8]}",
        daemon=True,
    ).start()
    wrapped = asyncio.wrap_future(future)
    # Consume a late failure so an abandoned worker cannot log "exception was
    # never retrieved" minutes after the caller gave up (source_io's trick).
    wrapped.add_done_callback(lambda done: done.exception() if not done.cancelled() else None)

    try:
        while not future.done():
            remaining = _remaining(record)
            if remaining <= 0:
                if _abandon(record):
                    raise CaptureTimeout(f"no progress for {NO_PROGRESS_SECONDS} seconds, or over the ceiling")
                break
            await asyncio.wait({wrapped}, timeout=min(remaining, PROGRESS_POLL_SECONDS))
    except asyncio.CancelledError:
        # Do NOT join: a dead SMB read would hold shutdown forever (§6). The
        # worker keeps its slot and cleans up its own staging when it returns.
        _abandon(record)
        raise

    sha256, size, part = await wrapped
    return CaptureReceipt(
        token=record.token,
        staging_path=part,
        sha256=sha256,
        size_bytes=size,
        format=source.format,
        display_filename=source.display_filename,
        plate_fallback=source.plate_fallback,
        provenance=dict(source.provenance),
        epoch=current_epoch(),
    )


async def publish(
    receipt: CaptureReceipt,
    attach: Callable[[AsyncSession, QueueSource], Awaitable[None]],
    *,
    session_factory: async_sessionmaker | None = None,
) -> QueueSource:
    """Publish the staged bytes and attach the caller's rows — §5 steps 5–7.

    Under the one storage-mutation guard, in this order: re-check the epoch,
    look the hash up, put the file in its final place, write the row, run
    ``attach`` and commit. The commit is last, so a job row can never name bytes
    that are not on disk.

    ``attach`` receives the transaction's session and the ready
    :class:`~backend.app.models.queue_source.QueueSource`. It must not pin, take
    the guard or start another capture — it already runs under the guard. If it
    raises, nothing is committed and an object this publication created is
    removed again.

    ``session_factory`` exists because publication owns its own short
    transaction rather than borrowing the request's (§5 step 1). Tests pass the
    test engine's factory; production leaves it alone.
    """
    receipt._claim()
    publication = _Publication()
    try:
        async with storage_mutation():
            if receipt.epoch != current_epoch():
                # A restore replaced the tree: these bytes belong to the old one.
                receipt.state = "discarded"
                _unpin_staging(receipt.staging_path)
                await _file_work(_drop, receipt.staging_path)
                raise StorageReplaced("the queue spool was replaced while this capture was in flight")

            factory = session_factory or database.async_session
            async with factory() as session:
                # No SELECT ... FOR UPDATE: one process owns this spool (§2), the
                # guard above is the mutual exclusion, and SQLite has no such lock.
                existing = await session.scalar(select(QueueSource).where(QueueSource.sha256 == receipt.sha256))
                created = False
                relative = object_relative_path(receipt.sha256, receipt.format)
                if existing is None:
                    # Path arithmetic only on the loop; the containment check and
                    # every syscall happen inside the worker call below.
                    target = Path(settings.base_dir) / relative
                    await _install(receipt, target, publication)
                    created = True
                    row = QueueSource(
                        sha256=receipt.sha256,
                        size_bytes=receipt.size_bytes,
                        relative_path=relative,
                        format=receipt.format,
                        state=STATE_READY,
                    )
                    session.add(row)
                    await session.flush()
                else:
                    row = await _reuse_or_repair(existing, receipt, publication)

                try:
                    await attach(session, row)
                    await session.commit()
                    # Settled the instant the commit returns: after this point a
                    # failure must not hand the receipt back as publishable, or a
                    # retry would attach the caller's rows a second time (§5.7 —
                    # a lost response is not a rollback).
                    receipt.state = "published"
                except BaseException:
                    await session.rollback()
                    if created:
                        # Nothing could reference it yet, and the row is gone.
                        await _file_work(_drop, Path(settings.base_dir) / relative)
                    raise
    except BaseException:
        if publication.staging_consumed or receipt.state == "published":
            # Nobody owns that ``.part`` any more — it has either been renamed
            # away or (on the reuse path after a committed publication) become a
            # duplicate this receipt no longer speaks for. Either way the pin must
            # go: the GC reads that set, and a pin nothing will ever release would
            # hold a file, or the memory of one, forever.
            _unpin_staging(receipt.staging_path)
        if publication.staging_consumed:
            receipt._spend()
        else:
            # Nothing moved (busy, a traversal refusal, a stale epoch): the
            # caller still holds its bytes and may retry.
            receipt._release_claim()
        raise

    _unpin_staging(receipt.staging_path)
    # Reuse leaves the staged duplicate behind; an install already moved it.
    await _file_work(_drop, receipt.staging_path)
    return row


async def _install(receipt: CaptureReceipt, target: Path, publication: _Publication) -> None:
    """Move the staged file into place, recording the move PESSIMISTICALLY.

    ⚠️ ``_file_work`` joins its worker thread and only **then** re-raises
    ``CancelledError`` (``core/db_portable.py:79-96`` — deliberately, so that file
    work cannot be abandoned half-done). A cancellation from a client disconnect
    or from shutdown therefore arrives with ``os.replace`` already completed, and
    a flag set on the line *after* the await would still read False.

    So everything except our own :class:`WriteFailed` counts as consumed:
    ``_install_object`` raises that one only before the rename, which proves
    nothing moved. Spending a receipt we could have kept costs one re-capture;
    reviving one over a file that has moved costs a bare ``OSError`` out of the
    taxonomy on the retry and a pin in the GC's set that nothing will release.
    """
    try:
        await _file_work(_install_object, receipt.staging_path, target)
    except WriteFailed:
        raise
    except BaseException:
        publication.staging_consumed = True
        raise
    publication.staging_consumed = True


async def _reuse_or_repair(existing: QueueSource, receipt: CaptureReceipt, publication: _Publication) -> QueueSource:
    """Decide what an already-known hash means for these bytes (§5 step 5).

    A verified ``ready`` object is reused as it stands — one blob for the whole
    queue (A01). A ``deleting`` one answers busy rather than letting a file be
    swapped under an unfinished unlink. A ``broken`` one (or a ``ready`` row
    whose file went missing) is repaired in place by exactly these bytes, with
    the same id — but only with no live reader/writer/backup pin, and failed jobs
    stay failed until somebody retries them.

    A repair moves the staged file, so it records that on ``publication`` exactly
    as the new-object path does — see :func:`_install` for why the flag cannot
    live on the line after the await.
    """
    if existing.state == STATE_DELETING:
        # ⚠️ Before the verification, not after: the GC may have unlinked the
        # object already, and "missing" must not route these bytes into the
        # repair branch and put a file back under an unfinished unlink.
        raise QueueSourceBusy("the garbage collector is still unlinking these bytes")

    # The row owns where its bytes live; a repair must land there, not at a path
    # recomputed from the receipt's own format.
    target = Path(settings.base_dir) / existing.relative_path
    intact = await _file_work(_verify_object, target, existing.sha256, existing.size_bytes)
    if intact and existing.state == STATE_READY:
        return existing

    if _blob_is_pinned(existing.id):
        raise QueueSourceBusy("these bytes are in use and cannot be repaired right now")
    if not intact:
        await _install(receipt, target, publication)
        logger.info("Repaired queue source %s (%s) from a fresh capture", existing.id, existing.sha256[:12])
    existing.state = STATE_READY
    existing.size_bytes = receipt.size_bytes
    existing.unreferenced_at = None
    return existing
