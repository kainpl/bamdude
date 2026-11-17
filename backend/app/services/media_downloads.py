"""Download jobs: a printer's files (and an archive's videos) prepared on disk, then handed over natively (G10 D).

Until this unit a download was one HTTP request that read every file into
memory — ``read_bytes`` per file, a ``BytesIO`` ZIP, a ``Response(content=…)``
— and the browser then held the same bytes again as a Blob. A timelapse folder
is gigabytes; that path could not carry it, and nothing in it could be
cancelled, resumed after a closed dialog, or stopped when its caller lost the
right to read the printer.

A download is now a JOB:

* **Created once, immutable.** The caller, the scope (one printer, or one
  archive), the selection, the transport and its endpoint are fixed at
  creation. Polling never changes what is being prepared.
* **Prepared on the data volume**, in ``DATA_DIR/media-downloads/<job>/`` —
  never the system temp directory. Each file streams to disk through the
  printer's file lane (``download_to_file``), with the job's remaining byte
  budget and the volume's reserve plus what other jobs have been promised. A
  ZIP takes one file at a time and the raw copy goes as soon as its entry is
  closed.
* **Watched.** While it prepares, the job holds a live-authority handle
  (``live_authority``): the caller's account, credential, permissions and the
  resource are re-read on the coordinator's lease (seconds). A retired handle
  — a revoked session or key, a lost permission, a deleted printer, a database
  that cannot be read, a restore — stops the job; nothing prepared is handed
  out afterwards. This is bounded detection after a commit, not a fence before
  one.
* **Cancelled only when it has really stopped.** A cancel asks the transfer to
  stop and waits for its worker to end (``join_on_cancel``) and for the scratch
  directory to go; only then does the job read ``cancelled``.
* **In memory, one process.** A restart forgets every job; what it left on disk
  is recognised by its marker file and removed at startup. A ready result is
  kept for an hour of idleness and while anybody is reading it.

Fatal and per-file failures are different things. A byte budget, the disk
reserve, lost authority, the time limit and a restore end the whole job — a
partial result never works around them. A file the printer would not give is
recorded and the job goes on; at least one file must succeed, or the job fails
rather than hand out an empty archive.

The runtime follows the other G10 owners: ``quiesce`` closes admission first,
stops every job and proves the workers ended (or the restore is refused); only
lifespan startup reopens.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import secrets
import shutil
import stat
import threading
import time
import uuid
import zipfile
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fastapi import HTTPException

from backend.app.services.live_authority import LiveDescriptor, live_authority
from backend.app.services.preview_artifacts import disk
from backend.app.services.printer_files.base import (
    TRANSFER_BUSY,
    TRANSFER_CANCELLED,
    TRANSFER_INSUFFICIENT_STORAGE,
    TRANSFER_LIMIT_EXCEEDED,
    TRANSFER_UNAVAILABLE,
    TransferOutcome,
)

logger = logging.getLogger(__name__)

SCRATCH_DIRNAME = "media-downloads"
#: Every job directory carries this file; startup removes only directories that have it.
MARKER = ".bamdude-download"

GIB = 1024**3
MIB = 1024**2

#: Starting bounds (spec r4 §12, GP3) — measured starting points, not proven hardware limits.
MAX_PATHS = 1000
BYTE_BUDGET = 10 * GIB
JOB_LIFETIME_SECONDS = 45 * 60
MAX_ACTIVE = 2
MAX_WAITING = 8
MAX_WAITING_PER_USER = 2
READY_TTL_SECONDS = 60 * 60
MAX_READY_PER_USER = 4
TERMINAL_TTL_SECONDS = 10 * 60
RESERVE_FLOOR = 512 * MIB
RESERVE_SHARE = 0.05
#: After this many files in a row found the printer unavailable, the rest are not tried.
UNAVAILABLE_STREAK = 3
#: A reader that was promised the artifact but has not opened it yet keeps it this long.
PENDING_READER_SECONDS = 60.0
SWEEP_SECONDS = 30.0
COPY_CHUNK = MIB

PHASE_QUEUED = "queued"
PHASE_PREPARING = "preparing"
PHASE_CANCELLING = "cancelling"
PHASE_READY = "ready"
PHASE_FAILED = "failed"
PHASE_CANCELLED = "cancelled"
PHASE_EXPIRED = "expired"
TERMINAL_PHASES = frozenset({PHASE_READY, PHASE_FAILED, PHASE_CANCELLED, PHASE_EXPIRED})

#: Why a whole job ended without a result. Codes, never sentences: the frontend words them.
ERROR_AUTHORITY = "authority_lost"
ERROR_LIMIT = "limit_exceeded"
ERROR_STORAGE = "insufficient_storage"
ERROR_TIME = "time_limit"
ERROR_NOTHING = "nothing_downloaded"
ERROR_INTERRUPTED = "interrupted"
ERROR_CLEANUP = "cleanup_incomplete"
ERROR_INTERNAL = "internal"
JOB_ERRORS = (
    ERROR_AUTHORITY,
    ERROR_LIMIT,
    ERROR_STORAGE,
    ERROR_TIME,
    ERROR_NOTHING,
    ERROR_INTERRUPTED,
    ERROR_CLEANUP,
    ERROR_INTERNAL,
)

FILE_DONE = "completed"
FILE_PENDING = "pending"
FILE_NOT_ATTEMPTED = "not_attempted"

#: Already compressed: storing them costs no CPU and loses nothing.
_STORED_SUFFIXES = frozenset(
    {
        ".3mf",
        ".zip",
        ".mp4",
        ".avi",
        ".mov",
        ".mkv",
        ".jpg",
        ".jpeg",
        ".png",
        ".gif",
        ".webp",
        ".gz",
        ".bz2",
        ".xz",
        ".7z",
    }
)
_FORBIDDEN_NAME_CHARS = '<>:"/\\|?*'
_MAX_NAME = 150


class DownloadRefused(Exception):
    """A job was not created. ``status`` is the HTTP status, ``code`` a stable machine code."""

    def __init__(self, status: int, code: str, message: str, *, retry_after: int | None = None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.retry_after = retry_after


class _Fatal(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class _Stopped(Exception):
    """A disk step saw the job's stop flag."""


def safe_name(name: str | None, fallback: str = "download") -> str:
    """A display basename that is safe as a ZIP entry, a header value and a URL segment.

    Only the last path component survives; control characters and the
    characters Windows refuses in a file name are replaced; dots and spaces at
    the ends go (``..`` cannot survive that). Never empty.
    """
    text = (name or "").replace("\\", "/").rsplit("/", 1)[-1]
    cleaned = "".join("_" if (ch in _FORBIDDEN_NAME_CHARS or ord(ch) < 32 or ord(ch) == 127) else ch for ch in text)
    cleaned = cleaned.strip(" .")
    if len(cleaned) > _MAX_NAME:
        stem, dot, suffix = cleaned.rpartition(".")
        if dot and len(suffix) <= 10:
            cleaned = stem[: _MAX_NAME - len(suffix) - 1] + "." + suffix
        else:
            cleaned = cleaned[:_MAX_NAME]
    return cleaned or fallback


def unique_names(names: list[str]) -> list[str]:
    """Deterministic duplicate suffixes — ``a.txt``, ``a (2).txt`` — compared without case (Windows, macOS)."""
    taken: set[str] = set()
    out = []
    for name in names:
        candidate = name
        number = 1
        while candidate.casefold() in taken:
            number += 1
            stem, dot, suffix = name.rpartition(".")
            candidate = f"{stem} ({number}).{suffix}" if dot and stem else f"{name} ({number})"
        taken.add(candidate.casefold())
        out.append(candidate)
    return out


@dataclass(frozen=True)
class Source:
    """One file of a job. ``fetch`` writes it to the path it is given and says what that proved.

    ``fetch(destination, byte_budget=, reserve_bytes=, progress=, stop=)`` — the
    service knows nothing about transports; a remote source closes over one
    (and with it over the endpoint and credentials captured at creation).
    """

    name: str
    fetch: Callable[..., Awaitable[TransferOutcome]] = field(repr=False, compare=False)
    advisory_size: int | None = None
    origin: str = "printer"


@dataclass(frozen=True)
class JobScope:
    """What a job belongs to, and what its caller must still be allowed to do while it runs."""

    kind: str  # "printer" | "archive"
    resource_id: int
    descriptor: LiveDescriptor = field(repr=False)


class ReaderLease:
    """One promised read of a job's artifact.

    Taken when a download answer is built, started when its body opens the
    file, released when the body's cleanup has closed it. A lease that was
    never started lapses by itself, so an answer that never ran pins nothing
    for ever.
    """

    def __init__(self, runtime: MediaDownloadRuntime, job: DownloadJob, *, discard_after: bool = False):
        self._runtime = runtime
        self.job = job
        self.started = False
        self.released = False
        self.discard_after = discard_after
        self.pending_until = time.monotonic() + PENDING_READER_SECONDS

    def start(self) -> None:
        self.started = True

    def holds(self) -> bool:
        return not self.released and (self.started or time.monotonic() < self.pending_until)

    def release(self) -> None:
        if self.released:
            return
        self.released = True
        self.job.leases.discard(self)
        self.job.touched = time.monotonic()
        if self.discard_after or self.job.phase == PHASE_EXPIRED:
            with contextlib.suppress(RuntimeError):  # no running loop: the sweeper or startup cleans up
                self._runtime.discard_soon(self.job, forget=self.job.forget)


@dataclass(eq=False)
class DownloadJob:
    job_id: str
    scope: JobScope
    sources: tuple[Source, ...]
    names: tuple[str, ...]
    as_zip: bool
    filename: str
    directory: Path
    reserved: int
    hidden: bool = False  # a compatibility request's own job: not listed, discarded after its one read
    forget: bool = False  # leave the registry as soon as the directory is gone
    phase: str = PHASE_QUEUED
    error: str | None = None
    results: list[dict] = field(default_factory=list)
    received: int = 0
    current_index: int | None = None
    current_received: int = 0
    artifact: Path | None = None
    artifact_size: int | None = None
    created: float = field(default_factory=time.monotonic)
    started: float | None = None
    finished: float | None = None
    touched: float = field(default_factory=time.monotonic)
    cancel_reason: str | None = None
    stop: threading.Event = field(default_factory=threading.Event, repr=False)
    task: asyncio.Task | None = field(default=None, repr=False)
    done: asyncio.Event = field(default_factory=asyncio.Event, repr=False)
    leases: set = field(default_factory=set, repr=False)
    timer: Any = field(default=None, repr=False)

    @property
    def capture(self):
        return self.scope.descriptor.capture

    @property
    def terminal(self) -> bool:
        return self.phase in TERMINAL_PHASES

    @property
    def waiting(self) -> bool:
        return self.phase == PHASE_QUEUED

    @property
    def active(self) -> bool:
        return self.phase in (PHASE_QUEUED, PHASE_PREPARING, PHASE_CANCELLING)

    def pinned(self) -> bool:
        return any(lease.holds() for lease in tuple(self.leases))

    @property
    def succeeded(self) -> int:
        return sum(1 for r in self.results if r["status"] == FILE_DONE)

    @property
    def partial(self) -> bool:
        return self.phase == PHASE_READY and self.succeeded < len(self.sources)

    def unspent_reservation(self) -> int:
        return max(0, self.reserved - self.received - self.current_received) if self.active else 0

    def status(self) -> dict:
        """The public state: counts and safe codes — never a path on this server, never a credential."""
        failed = [
            {"name": r["name"], "status": r["status"], "reason": r.get("reason")}
            for r in self.results
            if r["status"] not in (FILE_DONE, FILE_PENDING)
        ]
        answer = {
            "job_id": self.job_id,
            "kind": self.scope.kind,
            "phase": self.phase,
            "as_zip": self.as_zip,
            "filename": self.filename,
            "requested": len(self.sources),
            "succeeded": self.succeeded,
            "failed": len(failed),
            "bytes_received": self.received + (self.current_received if self.phase == PHASE_PREPARING else 0),
            "bytes_expected": sum(s.advisory_size or 0 for s in self.sources) or None,
            "current": self.names[self.current_index]
            if self.current_index is not None and self.phase == PHASE_PREPARING
            else None,
            "partial": self.partial,
            "failures": failed,
            "error": self.error,
            "size": self.artifact_size if self.phase == PHASE_READY else None,
            "expires_in": max(0, int(self.touched + READY_TTL_SECONDS - time.monotonic()))
            if self.phase == PHASE_READY
            else None,
        }
        return answer


def _base_reserve(usage) -> int:
    return max(RESERVE_FLOOR, int(usage.total * RESERVE_SHARE))


def _make_job_directory(root: Path, job_id: str) -> Path:
    """The job's own directory, with its marker. Refuses a scratch root that is a link."""
    root.mkdir(parents=True, exist_ok=True)
    if root.is_symlink() or root.is_junction():
        raise OSError("the download scratch root is a link")
    directory = root / job_id
    directory.mkdir()
    (directory / MARKER).write_text(job_id, encoding="ascii")
    (directory / "raw").mkdir()
    return directory


def _is_owned_directory(path: Path) -> bool:
    try:
        info = path.lstat()
    except OSError:
        return False
    if not stat.S_ISDIR(info.st_mode) or path.is_symlink() or path.is_junction():
        return False
    marker = path / MARKER
    try:
        marker_info = marker.lstat()
    except OSError:
        return False
    return stat.S_ISREG(marker_info.st_mode)


def _remove_job_directory(directory: Path) -> None:
    """Remove one job directory of ours. A directory without our marker is not ours and stays."""
    if not directory.exists():
        return
    if not _is_owned_directory(directory):
        raise OSError("not a download job directory")
    shutil.rmtree(directory)


def clean_scratch(root: Path) -> tuple[int, int]:
    """Startup: remove what earlier runs left. ``(removed, kept)``.

    Only directories carrying our marker go; anything else under the root — a
    plain file, a foreign directory, a link — is left alone and never entered.
    """
    removed = kept = 0
    try:
        if not root.is_dir() or root.is_symlink() or root.is_junction():
            return 0, 0
        entries = list(os.scandir(root))
    except OSError:
        return 0, 0
    for entry in entries:
        path = Path(entry.path)
        if _is_owned_directory(path):
            try:
                shutil.rmtree(path)
                removed += 1
                continue
            except OSError:
                logger.warning("[DOWNLOADS] a leftover download directory could not be removed (%s)", entry.name)
        kept += 1
    return removed, kept


def _zip_add(archive: zipfile.ZipFile, raw: Path, arcname: str, stop: threading.Event, reserve: int) -> int:
    """Copy one raw file into the ZIP as ``arcname``; returns the bytes read. Runs in a worker thread.

    Chunked so a cancel is seen within one chunk; an entry cut short leaves the
    archive unusable, which is right — the job is ending without a result.
    """
    size = raw.stat().st_size
    if reserve and shutil.disk_usage(raw.parent).free - size < reserve:
        raise _Fatal(ERROR_STORAGE)
    info = zipfile.ZipInfo.from_file(raw, arcname)
    stored = Path(arcname).suffix.lower() in _STORED_SUFFIXES
    info.compress_type = zipfile.ZIP_STORED if stored else zipfile.ZIP_DEFLATED
    copied = 0
    with raw.open("rb") as source, archive.open(info, "w", force_zip64=True) as target:
        while True:
            if stop.is_set():
                raise _Stopped
            chunk = source.read(COPY_CHUNK)
            if not chunk:
                break
            target.write(chunk)
            copied += len(chunk)
    return copied


class MediaDownloadRuntime:
    """Admission, the registry and the workers of download jobs."""

    def __init__(self) -> None:
        self.closed = False
        self.generation = secrets.token_hex(8)
        self.jobs: dict[str, DownloadJob] = {}
        self.root: Path | None = None
        self._slots: asyncio.Semaphore | None = None
        self._slots_loop: asyncio.AbstractEventLoop | None = None
        self._sweeper: asyncio.Task | None = None
        self._discards: set[asyncio.Task] = set()

    # -- admission ---------------------------------------------------------------------

    def admitting(self) -> bool:
        return not self.closed

    def scratch_root(self) -> Path:
        if self.root is None:
            from backend.app.core.config import settings

            self.root = Path(settings.base_dir) / SCRATCH_DIRNAME
        return self.root

    def _semaphore(self) -> asyncio.Semaphore:
        loop = asyncio.get_running_loop()
        if self._slots is None or self._slots_loop is not loop:
            self._slots = asyncio.Semaphore(MAX_ACTIVE)
            self._slots_loop = loop
        return self._slots

    def reopen(self) -> None:
        """Only lifespan startup reopens; every job of an earlier life is forgotten with its grants."""
        if any(job.task is not None and not job.task.done() for job in self.jobs.values()):
            raise RuntimeError("download jobs are still running")
        self.jobs.clear()
        self.generation = secrets.token_hex(8)
        self.closed = False

    def in_flight(self) -> int:
        return sum(1 for job in self.jobs.values() if job.task is not None and not job.task.done())

    async def create(
        self,
        scope: JobScope,
        sources: list[Source],
        *,
        as_zip: bool,
        filename: str | None,
        hidden: bool = False,
    ) -> DownloadJob:
        """Admit one job, or refuse with a stable code. Nothing is read from the printer here."""
        if self.closed:
            raise DownloadRefused(503, "downloads_unavailable", "Downloads are stopped for a restore", retry_after=30)
        if not sources:
            raise DownloadRefused(400, "no_files", "No files specified")
        if len(sources) > MAX_PATHS:
            raise DownloadRefused(413, "too_many_files", f"A download takes at most {MAX_PATHS} files")
        if len(sources) > 1:
            as_zip = True
        capture = scope.descriptor.capture
        if not hidden:
            waiting = [job for job in self.jobs.values() if job.waiting and not job.hidden]
            if len(waiting) >= MAX_WAITING:
                raise DownloadRefused(429, "download_queue_full", "Too many downloads are waiting", retry_after=30)
            mine = [job for job in waiting if self._same_caller(job.capture, capture)]
            if len(mine) >= MAX_WAITING_PER_USER:
                raise DownloadRefused(429, "download_user_limit", "You already have downloads waiting", retry_after=30)
        known = [s.advisory_size for s in sources if s.advisory_size and s.advisory_size > 0]
        need = sum(known) + (max(known) if as_zip and known else 0)
        if sum(known) > BYTE_BUDGET:
            raise DownloadRefused(413, ERROR_LIMIT, "The selection is larger than one download may be")
        root = self.scratch_root()
        job_id = uuid.uuid4().hex
        try:
            directory = await disk(_make_job_directory, root, job_id)
            usage = await disk(shutil.disk_usage, directory)
        except OSError as exc:
            logger.warning("[DOWNLOADS] the scratch directory is not usable (%s)", type(exc).__name__)
            raise DownloadRefused(507, ERROR_STORAGE, "The server cannot prepare a download now") from exc
        promised = sum(job.unspent_reservation() for job in self.jobs.values())
        if usage.free - promised - need < _base_reserve(usage):
            await self._remove(directory)
            raise DownloadRefused(507, ERROR_STORAGE, "Not enough free space on the server to prepare this download")
        if self.closed:  # a restore began while the directory was made
            await self._remove(directory)
            raise DownloadRefused(503, "downloads_unavailable", "Downloads are stopped for a restore", retry_after=30)
        names = unique_names([safe_name(s.name, "file") for s in sources])
        if as_zip:
            display = safe_name(filename, "download.zip")
            if not display.lower().endswith(".zip"):
                display += ".zip"
        else:
            display = safe_name(filename or names[0], names[0])
        job = DownloadJob(
            job_id=job_id,
            scope=scope,
            sources=tuple(sources),
            names=tuple(names),
            as_zip=as_zip,
            filename=display,
            directory=directory,
            reserved=need,
            hidden=hidden,
            results=[{"name": n, "status": FILE_PENDING} for n in names],
        )
        self.jobs[job_id] = job
        self._evict_ready(capture, keep=job)
        job.task = asyncio.get_running_loop().create_task(self._run(job), name=f"media-download-{job_id[:8]}")
        self._ensure_sweeper()
        logger.info(
            "[DOWNLOADS] job %s created: %s %s, %d file(s), zip=%s",
            job_id[:8],
            scope.kind,
            scope.resource_id,
            len(sources),
            as_zip,
        )
        return job

    @staticmethod
    def _same_caller(a, b) -> bool:
        return a.account_identity == b.account_identity and a.key_identity == b.key_identity

    def get(self, job_id: str, capture, kind: str, resource_id: int) -> DownloadJob | None:
        """The caller's own job in this scope. Somebody else's and a missing one are both ``None``."""
        job = self.jobs.get(job_id)
        if (
            job is None
            or job.hidden
            or job.scope.kind != kind
            or job.scope.resource_id != resource_id
            or not self._same_caller(job.capture, capture)
        ):
            return None
        return job

    def find(self, job_id: str) -> DownloadJob | None:
        """By id alone — only for a door that proves the caller some other way (a redeemed grant)."""
        job = self.jobs.get(job_id)
        return None if job is None or job.hidden else job

    def jobs_of(self, capture, kind: str, resource_id: int) -> list[DownloadJob]:
        return sorted(
            (
                job
                for job in self.jobs.values()
                if not job.hidden
                and job.scope.kind == kind
                and job.scope.resource_id == resource_id
                and self._same_caller(job.capture, capture)
            ),
            key=lambda job: job.created,
        )

    # -- cancel, discard ---------------------------------------------------------------

    def request_cancel(self, job: DownloadJob, reason: str = "cancelled") -> None:
        """Ask a job to stop. It reads ``cancelled`` (or ``failed``) only once its worker and scratch are gone."""
        if job.terminal:
            return
        if job.cancel_reason is None:
            job.cancel_reason = reason
        job.stop.set()
        if job.phase == PHASE_PREPARING:
            job.phase = PHASE_CANCELLING
        if job.task is not None and not job.task.done():
            job.task.cancel()

    def discard_soon(self, job: DownloadJob, *, forget: bool = False) -> None:
        """Drop a finished job's result; an active one is cancelled. Idempotent.

        ``forget`` — the caller's own delete: the job leaves the registry once
        its directory is gone. Otherwise an expired result stays readable as
        ``expired`` for a while, so a poll says what happened to it.
        """
        if job.active:
            self.request_cancel(job)
            return
        task = asyncio.get_running_loop().create_task(self._discard(job, forget=forget), name="media-download-discard")
        self._discards.add(task)
        task.add_done_callback(self._discards.discard)

    async def _discard(self, job: DownloadJob, *, forget: bool = False) -> None:
        if job.phase == PHASE_READY:
            # No new reader from here on; those already reading keep the file until they are done.
            job.phase = PHASE_EXPIRED
            job.finished = time.monotonic()
        job.forget = job.forget or forget or job.hidden
        if job.pinned():
            return  # the sweeper removes it when the last reader is done
        job.artifact = None
        if await self._remove(job.directory) and job.forget:
            self.jobs.pop(job.job_id, None)

    async def _remove(self, directory: Path) -> bool:
        try:
            await disk(_remove_job_directory, directory)
            return True
        except OSError as exc:
            logger.warning("[DOWNLOADS] a download directory could not be removed (%s)", type(exc).__name__)
            return False

    def _evict_ready(self, capture, *, keep: DownloadJob) -> None:
        ready = sorted(
            (
                job
                for job in self.jobs.values()
                if job.phase == PHASE_READY
                and not job.hidden
                and job is not keep
                and self._same_caller(job.capture, capture)
            ),
            key=lambda job: job.touched,
        )
        for job in ready[: max(0, len(ready) - MAX_READY_PER_USER + 1)]:
            if not job.pinned():
                self.discard_soon(job)

    # -- readers -----------------------------------------------------------------------

    def lease(self, job: DownloadJob, *, discard_after: bool = False) -> ReaderLease | None:
        """Promise one read of a ready artifact, or ``None`` when there is nothing to read."""
        if job.phase != PHASE_READY or job.artifact is None or self.closed:
            return None
        lease = ReaderLease(self, job, discard_after=discard_after)
        job.leases.add(lease)
        job.touched = time.monotonic()
        return lease

    # -- the worker --------------------------------------------------------------------

    async def _run(self, job: DownloadJob) -> None:
        """One job from the queue to its terminal phase. Never raises: the outcome is the job's state."""
        outcome: str | None = None
        try:
            try:
                async with self._semaphore():
                    if job.stop.is_set() or self.closed:
                        raise asyncio.CancelledError
                    await self._prepare(job)
            except asyncio.CancelledError:
                outcome = job.cancel_reason or ERROR_INTERRUPTED
            except _Fatal as fatal:
                outcome = fatal.code
            except Exception:
                logger.exception("[DOWNLOADS] job %s failed", job.job_id[:8])
                outcome = ERROR_INTERNAL
        finally:
            if job.timer is not None:
                job.timer.cancel()
                job.timer = None
            await self._finish(job, outcome)

    async def _finish(self, job: DownloadJob, outcome: str | None) -> None:
        job.current_index = None
        job.current_received = 0
        job.finished = job.touched = time.monotonic()
        if outcome is None and job.stop.is_set():
            # Asked to stop while the last step was finishing: the request is honoured, not overtaken.
            outcome = job.cancel_reason or ERROR_INTERRUPTED
        if outcome is None:
            job.phase = PHASE_READY
            logger.info(
                "[DOWNLOADS] job %s ready: %d of %d file(s), %d bytes, %.1fs",
                job.job_id[:8],
                job.succeeded,
                len(job.sources),
                job.artifact_size or 0,
                job.finished - (job.started or job.created),
            )
        else:
            # The worker has ended (its transfer was joined); the scratch goes before the phase says so.
            job.artifact = None
            job.artifact_size = None
            for result in job.results:
                if result["status"] == FILE_PENDING:
                    result["status"] = FILE_NOT_ATTEMPTED
            removed = await self._settled_remove(job.directory)
            if outcome == "cancelled":
                job.phase = PHASE_CANCELLED
            else:
                job.phase = PHASE_FAILED
                job.error = outcome
            if not removed:
                job.phase = PHASE_FAILED
                job.error = ERROR_CLEANUP
            logger.info("[DOWNLOADS] job %s ended: %s", job.job_id[:8], job.error or job.phase)
        job.done.set()

    async def _settled_remove(self, directory: Path) -> bool:
        """Remove a job's scratch through any number of cancels."""
        task = asyncio.ensure_future(self._remove(directory))
        while not task.done():
            try:
                await asyncio.wait({task})
            except asyncio.CancelledError:
                continue
        return bool(task.result())

    def _retired(self, job: DownloadJob) -> None:
        # A restore closes live authority too; that is an interruption, not a revoked caller.
        self.request_cancel(job, ERROR_INTERRUPTED if not live_authority.admitting or self.closed else ERROR_AUTHORITY)

    async def _prepare(self, job: DownloadJob) -> None:
        loop = asyncio.get_running_loop()
        job.phase = PHASE_PREPARING
        job.started = time.monotonic()
        job.timer = loop.call_later(JOB_LIFETIME_SECONDS, self.request_cancel, job, ERROR_TIME)
        try:
            handle = live_authority.register(job.scope.descriptor)
        except HTTPException as exc:
            raise _Fatal(ERROR_INTERRUPTED if self.closed or not live_authority.admitting else ERROR_AUTHORITY) from exc
        handle.on_retire = lambda _code=None: self._retired(job)
        try:
            try:
                await live_authority.admit(handle)
            except HTTPException as exc:
                raise _Fatal(job.cancel_reason or ERROR_AUTHORITY) from exc
            await self._fetch_all(job, handle)
        finally:
            handle.on_retire = None
            live_authority.release(handle)

    def _reserve_for(self, job: DownloadJob, usage) -> int:
        others = sum(other.unspent_reservation() for other in self.jobs.values() if other is not job)
        return _base_reserve(usage) + others

    async def _fetch_all(self, job: DownloadJob, handle) -> None:
        raw_dir = job.directory / "raw"
        artifact = job.directory / "artifact"
        archive: zipfile.ZipFile | None = None
        if job.as_zip:
            archive = await disk(zipfile.ZipFile, artifact, "w", zipfile.ZIP_DEFLATED, True)
        budget_left = BYTE_BUDGET
        streak = 0
        try:
            for index, source in enumerate(job.sources):
                if job.stop.is_set():
                    raise asyncio.CancelledError
                if not handle.deliverable():
                    raise _Fatal(ERROR_AUTHORITY)
                result = job.results[index]
                if streak >= UNAVAILABLE_STREAK:
                    result.update(status=FILE_NOT_ATTEMPTED, reason=TRANSFER_UNAVAILABLE)
                    continue
                destination = raw_dir / f"{index:04d}.part"
                job.current_index = index
                job.current_received = 0

                def progress(count: int, _job=job) -> None:
                    _job.current_received = count

                usage = await disk(shutil.disk_usage, job.directory)
                reserve = self._reserve_for(job, usage)
                outcome = await source.fetch(
                    destination,
                    byte_budget=budget_left,
                    reserve_bytes=reserve,
                    progress=progress,
                    stop=job.stop,
                )
                job.current_received = 0
                if not outcome.completed:
                    if outcome.status == TRANSFER_LIMIT_EXCEEDED:
                        raise _Fatal(ERROR_LIMIT)
                    if outcome.status == TRANSFER_INSUFFICIENT_STORAGE:
                        raise _Fatal(ERROR_STORAGE)
                    if outcome.status == TRANSFER_CANCELLED or (
                        outcome.status == TRANSFER_BUSY and outcome.reason == "restoring"
                    ):
                        raise _Fatal(job.cancel_reason or ERROR_INTERRUPTED)
                    result.update(status=outcome.status, reason=outcome.reason)
                    streak = streak + 1 if outcome.status == TRANSFER_UNAVAILABLE else 0
                    continue
                streak = 0
                # Authority is asked again before the bytes become part of a result.
                if not handle.deliverable():
                    raise _Fatal(ERROR_AUTHORITY)
                budget_left -= outcome.received
                if archive is not None:
                    try:
                        await disk(_zip_add, archive, destination, job.names[index], job.stop, reserve)
                    except _Stopped:
                        raise asyncio.CancelledError from None
                    except OSError as exc:
                        logger.warning(
                            "[DOWNLOADS] job %s: the archive could not be written (%s)",
                            job.job_id[:8],
                            type(exc).__name__,
                        )
                        raise _Fatal(ERROR_STORAGE) from exc
                    await disk(destination.unlink)
                else:
                    await disk(os.replace, destination, artifact)
                job.received += outcome.received
                result.update(status=FILE_DONE, bytes=outcome.received, check=outcome.check)
            if archive is not None:
                # Closing writes the central directory: part of the result, never swallowed.
                closing, archive = archive, None
                try:
                    await disk(closing.close)
                except OSError as exc:
                    raise _Fatal(ERROR_STORAGE) from exc
        finally:
            job.current_index = None
            if archive is not None:  # ending without a result: only the descriptor matters
                with contextlib.suppress(Exception):
                    await disk(archive.close)
        if job.succeeded == 0:
            raise _Fatal(ERROR_NOTHING)
        if not handle.deliverable():
            raise _Fatal(ERROR_AUTHORITY)
        job.artifact_size = (await disk(artifact.stat)).st_size
        job.artifact = artifact

    async def wait(self, job: DownloadJob) -> None:
        """Wait for a job's terminal phase. A cancelled waiter cancels the job and still waits it out."""
        try:
            await job.done.wait()
        except asyncio.CancelledError:
            self.request_cancel(job)
            while not job.done.is_set():
                try:
                    await job.done.wait()
                except asyncio.CancelledError:
                    continue
            raise

    # -- janitor -----------------------------------------------------------------------

    def _ensure_sweeper(self) -> None:
        if self._sweeper is None or self._sweeper.done():
            self._sweeper = asyncio.get_running_loop().create_task(self._sweep_loop(), name="media-download-sweep")

    async def _sweep_loop(self) -> None:
        while self.jobs and not self.closed:
            await asyncio.sleep(SWEEP_SECONDS)
            await self.sweep()

    async def sweep(self) -> None:
        """Expire idle results and forget old terminal jobs. A pinned result is never removed."""
        now = time.monotonic()
        for job in list(self.jobs.values()):
            if job.active or job.pinned():
                continue
            if job.phase == PHASE_READY:
                if now - job.touched >= READY_TTL_SECONDS:
                    await self._discard(job)
            elif job.phase == PHASE_EXPIRED and job.artifact is not None:
                await self._discard(job, forget=job.forget)  # its last reader has gone
            elif now - (job.finished or job.created) >= TERMINAL_TTL_SECONDS:
                # Its directory may be a leftover of a failed removal: try once more, then forget it.
                await self._remove(job.directory)
                self.jobs.pop(job.job_id, None)

    # -- restore / shutdown ------------------------------------------------------------

    def close_admission(self) -> None:
        self.closed = True

    async def quiesce(self, seconds: float = 30.0) -> bool:
        """Close admission, stop every job and prove the workers ended. False = one is still running.

        Results already prepared are dropped with their directories: they
        belong to the storage that is being replaced, and their grants die with
        the generation.
        """
        self.close_admission()
        running = []
        for job in list(self.jobs.values()):
            if job.task is not None and not job.task.done():
                self.request_cancel(job, ERROR_INTERRUPTED)
                running.append(job.task)
        if running:
            await asyncio.wait(running, timeout=seconds)
        if self._sweeper is not None and not self._sweeper.done():
            self._sweeper.cancel()
        if self._discards:
            await asyncio.wait(tuple(self._discards), timeout=seconds)
        still = [job.job_id[:8] for job in self.jobs.values() if job.task is not None and not job.task.done()]
        if still:
            logger.warning("[DOWNLOADS] %d download job(s) still running after %.0fs", len(still), seconds)
            return False
        self.generation = secrets.token_hex(8)
        for job in self.jobs.values():
            if job.phase == PHASE_READY:
                job.phase = PHASE_EXPIRED  # no new reader
        deadline = time.monotonic() + seconds
        while any(lease.started and not lease.released for job in self.jobs.values() for lease in tuple(job.leases)):
            if time.monotonic() >= deadline:
                logger.warning("[DOWNLOADS] a prepared download is still being read after %.0fs", seconds)
                return False
            await asyncio.sleep(0.05)
        for job in list(self.jobs.values()):
            job.artifact = None
            await self._remove(job.directory)
        self.jobs.clear()
        return True


media_download_runtime = MediaDownloadRuntime()


def remote_source(transport, path: str, *, name: str | None = None, advisory_size: int | None = None) -> Source:
    """A file on a printer, read through ``transport`` — which was built from the printer row as it was
    when the job was created, so a later change of address or access code never moves the job."""

    async def fetch(destination: Path, *, byte_budget, reserve_bytes, progress, stop) -> TransferOutcome:
        return await transport.download_to_file(
            path,
            destination,
            byte_budget=byte_budget,
            reserve_bytes=reserve_bytes,
            progress=progress,
            join_on_cancel=True,
        )

    return Source(name or path.rsplit("/", 1)[-1], fetch, advisory_size, "printer")


def _copy_local(source: Path, destination: Path, byte_budget: int, reserve: int, progress, stop: threading.Event):
    from backend.app.services.printer_files.base import TRANSFER_COMPLETED, TRANSFER_NOT_READABLE
    from backend.app.services.printer_files.staging import ReserveSchedule

    copied = 0
    schedule = ReserveSchedule(reserve)
    try:
        with source.open("rb") as reader, destination.open("wb") as writer:
            while True:
                if stop.is_set():
                    raise _Stopped
                chunk = reader.read(COPY_CHUNK)
                if not chunk:
                    break
                copied += len(chunk)
                if copied > byte_budget:
                    raise _Fatal(ERROR_LIMIT)
                if schedule.due_before(copied) and not schedule.fits(
                    shutil.disk_usage(destination.parent).free, len(chunk)
                ):
                    raise _Fatal(ERROR_STORAGE)
                writer.write(chunk)
                if progress is not None:
                    progress(copied)
    except FileNotFoundError:
        destination.unlink(missing_ok=True)
        return TransferOutcome(TRANSFER_NOT_READABLE, reason="missing")
    except BaseException:
        destination.unlink(missing_ok=True)
        raise
    return TransferOutcome(TRANSFER_COMPLETED, copied, "length_verified")


def local_source(path: Path, *, name: str, advisory_size: int | None = None) -> Source:
    """A file already on this server (an archive's attached video), copied into the job's scratch."""

    async def fetch(destination: Path, *, byte_budget, reserve_bytes, progress, stop) -> TransferOutcome:
        try:
            return await disk(_copy_local, path, destination, byte_budget, reserve_bytes, progress, stop)
        except _Stopped:
            return TransferOutcome(TRANSFER_CANCELLED, reason="cancelled")
        except _Fatal as fatal:
            status = TRANSFER_LIMIT_EXCEEDED if fatal.code == ERROR_LIMIT else TRANSFER_INSUFFICIENT_STORAGE
            return TransferOutcome(status, reason=fatal.code)
        except OSError as exc:
            return TransferOutcome(TRANSFER_UNAVAILABLE, reason=type(exc).__name__)

    return Source(name, fetch, advisory_size, "local")
