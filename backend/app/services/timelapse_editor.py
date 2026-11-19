"""Edit jobs: the timelapse editor's ffmpeg in the background, owned to its end (G10 E).

Until this unit *Save* in the editor was one HTTP request that waited for
ffmpeg. The request could time out behind a proxy while the encode went on; a
closed tab left an encoder nobody could stop; the music went to a predictable
name in the system temp directory; the result was moved over the file readers
were following — with no check that the archive still showed the video the edit
was made from, and a "save as new" file that no table knew about.

An edit is now a JOB:

* **Admitted in one step, fixed for good.** The caller, the archive's
  incarnation, the storage epoch, the video the archive shows
  (``archive_media.primary_token``) and the edit's numbers are captured when the
  job is made. One edit at a time per video version — a second one is refused
  (409), it does not become a second writer.
* **It encodes the file it was admitted for.** The video is pinned into the
  job's own directory when the place is taken — a hard link where the volume
  allows it, a copy otherwise — and checked against what the archive showed at
  that moment (:func:`_pin_source`). ffmpeg reads the pin, so another video put
  under the same name, size and time later cannot become this edit's result.
* **One encoder at a time** for the whole process, shared with the layer
  stitch (``encoder_slot``), first come first served; the rest wait, up to
  :data:`MAX_JOBS`. Each ffmpeg is a child ``media_child`` owns: bounded, killed
  and reaped on cancel, on the time limits, on lost authority and on a restore.
* **Watched** while it runs (``live_authority``): a revoked session or key, a
  lost permission or an archive that is no longer that archive stops the job.
* **Published through the seam** (``archive_media``): *replace* is a
  ``publish_primary`` with the token captured at admission — a video replaced,
  removed or re-attached meanwhile refuses the result ``source_changed``; *save
  as new* is a ``publish_variant``, a video with a row of its own beside the one
  the archive shows. The caller's rights are asked again inside that
  transaction.
* **100 % means published.** Progress follows ffmpeg's own ``-progress`` output
  and stops short of the end; the job reads ``done`` only after the process was
  reaped, its output probed and the publication committed.
* **A cancel is either honoured or refused, never both.** It is accepted up
  to the last moment the publication can still be stopped — the seam asks the
  job once more under its own locks, immediately before the bytes take their
  name (``may_run``) — and an accepted cancel leaves the archive as it was. From
  that moment (:attr:`EditJob.no_return`) a cancel is answered "too late".
* **Cancelled only when it has stopped**: the job reads ``cancelled`` after the
  child was reaped and its files removed. A child whose end was not seen leaves
  the job ``failed`` / ``process_unreaped`` with its output untouched, and a
  restore refused.
* **In memory, one process.** A restart forgets every job; what it left on disk
  — its scratch directory and the staged output it named there — is removed at
  startup by the marker, never by age or by a name that merely looks like ours.

The music an edit uploads is streamed into the job's own scratch directory
(``DATA_DIR/timelapse-edits/<job>/``), capped, and probed before the encode.
The encoder writes there too whenever that directory shares a volume with the
video — the scratch tree is outside every backup — and beside the video only
when the archive lives on another volume.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import math
import os
import shutil
import stat
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fastapi import HTTPException

from backend.app.services import media_child
from backend.app.services.encoder_slot import encoder_slot
from backend.app.services.live_authority import live_authority
from backend.app.services.media_downloads import JobScope, same_credential, same_user
from backend.app.services.timelapse_processor import (
    SPEED_MAX,
    SPEED_MIN,
    ProbeFailed,
    TimelapseProcessor,
    parse_progress_seconds,
)

logger = logging.getLogger(__name__)

SCRATCH_DIRNAME = "timelapse-edits"
#: Every job directory carries this file; startup removes only directories that have it.
MARKER = ".bamdude-edit"
#: Inside a job directory: the staged output's path relative to DATA_DIR, so startup can remove it.
OUTPUT_NOTE = "output"

MIB = 1024**2
#: Starting bounds (spec r4 §12, GP3).
AUDIO_LIMIT = 100 * MIB
EDIT_SECONDS = 3600.0
IDLE_SECONDS = 60.0
MAX_JOBS = 4  # waiting + encoding, all users
MAX_JOBS_PER_USER = 2
TERMINAL_TTL_SECONDS = 10 * 60
AUDIO_SUFFIXES = (".mp3", ".wav", ".m4a", ".aac", ".ogg")
_CHUNK = MIB
_POLL = 0.2
#: Progress shown while the encode runs never reaches 1.0: that is the publication's.
_ENCODE_CEILING = 0.97

PHASE_QUEUED = "queued"
PHASE_ENCODING = "encoding"
PHASE_PUBLISHING = "publishing"
PHASE_CANCELLING = "cancelling"
PHASE_DONE = "done"
PHASE_FAILED = "failed"
PHASE_CANCELLED = "cancelled"
TERMINAL_PHASES = frozenset({PHASE_DONE, PHASE_FAILED, PHASE_CANCELLED})

#: Why a job ended without a result. Codes, never sentences: the frontend words them.
ERROR_AUTHORITY = "authority_lost"
ERROR_INTERRUPTED = "interrupted"
ERROR_ENCODE = "encode_failed"
ERROR_TIMEOUT = "encode_timeout"
ERROR_STALLED = "encode_stalled"
ERROR_OUTPUT = "output_invalid"
ERROR_UNREAPED = "process_unreaped"
ERROR_PUBLISH = "publish_failed"
ERROR_CLEANUP = "cleanup_failed"

SAVE_REPLACE = "replace"
SAVE_NEW = "new"


class EditRefused(Exception):
    """An edit that is not admitted. ``code`` is machine-read; ``message`` is the English sentence."""

    def __init__(self, status: int, code: str, message: str, retry_after: int | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.retry_after = retry_after


@dataclass(frozen=True)
class EditRequest:
    trim_start: float
    trim_end: float | None
    speed: float
    audio_volume: float
    mute: bool
    save_mode: str


def validated_request(*, trim_start, trim_end, speed, audio_volume, mute, save_mode, duration: float) -> EditRequest:
    """The edit's numbers, finite and inside the video. Raises :class:`EditRefused` (400)."""

    def finite(value, name: str) -> float:
        try:
            number = float(value)
        except (TypeError, ValueError):
            raise EditRefused(400, "edit_invalid", f"{name} must be a number") from None
        if not math.isfinite(number):
            raise EditRefused(400, "edit_invalid", f"{name} must be a number")
        return number

    if save_mode not in (SAVE_REPLACE, SAVE_NEW):
        raise EditRefused(400, "edit_invalid", "save_mode must be 'replace' or 'new'")
    start = finite(trim_start, "trim_start")
    end = None if trim_end is None else finite(trim_end, "trim_end")
    rate = finite(speed, "speed")
    volume = finite(audio_volume, "audio_volume")
    if not SPEED_MIN <= rate <= SPEED_MAX:
        raise EditRefused(400, "edit_invalid", "Speed must be between 0.25 and 4.0")
    if not 0.0 <= volume <= 1.0:
        raise EditRefused(400, "edit_invalid", "The music volume must be between 0 and 1")
    if duration <= 0:
        raise EditRefused(422, "video_unreadable", "The video's length could not be read")
    # The dialog works in tenths of a second and reads the length from the browser's player.
    slack = 0.5
    if end is not None and end > duration:
        if end > duration + slack:
            raise EditRefused(400, "edit_invalid", "The trim must lie inside the video")
        end = duration
    if start < 0 or start >= (duration if end is None else end):
        raise EditRefused(400, "edit_invalid", "The trim must lie inside the video")
    return EditRequest(start, end, rate, volume, bool(mute), save_mode)


@dataclass(eq=False)
class EditJob:
    job_id: str
    scope: JobScope
    archive_id: int
    archive_identity: str
    epoch: int
    token: str
    source: Path
    source_has_audio: bool
    duration: float
    request: EditRequest
    output_name: str
    #: ``(st_dev, st_ino, st_size, st_mtime_ns)`` of the video when the archive was read at admission.
    source_identity: tuple | None = None
    #: The admitted video, pinned in the job's directory: what the encoder reads.
    pinned: Path | None = None
    #: The publication can no longer be stopped: a cancel from here is "too late".
    no_return: bool = False
    slot: Any = field(default=None, repr=False)
    directory: Path | None = None
    audio: Path | None = None
    phase: str = PHASE_QUEUED
    error: str | None = None
    progress: float = 0.0
    result: dict | None = None
    cancel_reason: str | None = None
    created: float = field(default_factory=time.monotonic)
    finished: float | None = None
    stop: asyncio.Event = field(default_factory=asyncio.Event, repr=False)
    task: asyncio.Task | None = field(default=None, repr=False)
    done: asyncio.Event = field(default_factory=asyncio.Event, repr=False)
    staged: Path | None = field(default=None, repr=False)
    keep_staged: bool = False  # an unreaped child may still be writing it
    handle: Any = field(default=None, repr=False)

    @property
    def capture(self):
        return self.scope.descriptor.capture

    @property
    def terminal(self) -> bool:
        return self.phase in TERMINAL_PHASES

    @property
    def expected_seconds(self) -> float:
        end = self.duration if self.request.trim_end is None else self.request.trim_end
        return max((end - self.request.trim_start) / self.request.speed, 0.001)

    def status(self) -> dict:
        return {
            "job_id": self.job_id,
            "phase": self.phase,
            "progress": round(1.0 if self.phase == PHASE_DONE else min(self.progress, _ENCODE_CEILING), 3),
            "error": self.error,
            "save_mode": self.request.save_mode,
            "result": self.result,
        }


class _Ended(Exception):
    """The job ends here without a result: ``code`` is ``None`` for a cancel."""

    def __init__(self, code: str | None) -> None:
        super().__init__(code or "cancelled")
        self.code = code


# -- scratch ---------------------------------------------------------------------------


def _make_directory(root: Path, job_id: str) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    if root.is_symlink() or root.is_junction():
        raise OSError("the edit scratch root is a link")
    directory = root / job_id
    directory.mkdir()  # never exist_ok: the directory is this call's own from here on
    try:
        (directory / MARKER).write_text(job_id, encoding="ascii")
    except BaseException:
        shutil.rmtree(directory, ignore_errors=True)
        raise
    return directory


def _is_owned_directory(path: Path) -> bool:
    try:
        info = path.lstat()
    except OSError:
        return False
    if not stat.S_ISDIR(info.st_mode) or path.is_symlink() or path.is_junction():
        return False
    try:
        return stat.S_ISREG((path / MARKER).lstat().st_mode)
    except OSError:
        return False


def staged_name(job_id: str) -> str:
    return f".{job_id}.editing.mp4"


def _remove_noted_output(directory: Path, base_dir: Path) -> None:
    """Remove the staged output a job directory names — only a file that carries that job's own name."""
    from backend.app.services.archive_media import contained

    try:
        relative = (directory / OUTPUT_NOTE).read_text(encoding="utf-8").strip()
    except OSError:
        return
    target = contained(base_dir, relative)
    if target is None or target.name != staged_name(directory.name):
        return
    with contextlib.suppress(OSError):
        target.unlink(missing_ok=True)


def clean_scratch(root: Path, base_dir: Path) -> tuple[int, int]:
    """Startup: remove what earlier runs left — ``(removed, kept)``.

    Only directories carrying our marker go, each with the staged output it
    named; anything else under the root is left alone and never entered.
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
            _remove_noted_output(path, base_dir)
            try:
                shutil.rmtree(path)
                removed += 1
                continue
            except OSError:
                logger.warning("[EDITS] a leftover edit directory could not be removed (%s)", entry.name)
        kept += 1
    return removed, kept


def _staging_place(job: EditJob) -> Path:
    """Where the encoder writes: the job's own directory when it shares a volume with the video.

    The publication gives the file its name with a rename, so the staged file
    must be on the video's volume. Normally that is the data volume, and then
    the half-encoded file stays OUT of the archive tree — a backup taken during
    an hour-long encode does not meet a file that changes under it. Only when
    the archive lives on another volume is the file staged beside the video.
    """
    try:
        same_volume = os.stat(job.directory).st_dev == os.stat(job.source.parent).st_dev
    except OSError:
        same_volume = False
    return job.directory / "output.mp4" if same_volume else job.source.with_name(staged_name(job.job_id))


def _identity(info: os.stat_result) -> tuple:
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)


def _pin_source(job: EditJob) -> Path:
    """Pin the admitted video into the job's directory and prove it is the admitted one. Worker thread.

    A hard link names the very file the archive showed: whatever is put under
    the video's name afterwards is another file, and the link still reads the
    admitted bytes. Where a link cannot be made (another volume, a file system
    without them) the video is copied, and the name is looked at again after
    the copy. Either way the pin's identity must be the one captured when the
    archive was read; anything else is a video that changed before the edit
    began — refused, nothing encoded.
    """
    pinned = job.directory / f"input{job.source.suffix.lower() or '.mp4'}"

    def refuse() -> None:
        raise EditRefused(409, "source_changed", "The archive's video changed while the edit was being started")

    try:
        os.link(job.source, pinned)
        linked = True
    except OSError:
        linked = False
    try:
        if linked:
            if job.source_identity is not None and _identity(os.stat(pinned)) != job.source_identity:
                refuse()
        else:
            if job.source_identity is not None and _identity(os.stat(job.source)) != job.source_identity:
                refuse()
            shutil.copyfile(job.source, pinned)
            # The name is read again: a file swapped in during the copy is not what was admitted.
            if job.source_identity is not None and _identity(os.stat(job.source)) != job.source_identity:
                refuse()
    except FileNotFoundError:
        refuse()
    return pinned


def _sha256(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        while chunk := handle.read(_CHUNK):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


class TimelapseEditRuntime:
    """Admission, the registry and the workers of edit jobs."""

    def __init__(self) -> None:
        self.closed = False
        self.jobs: dict[str, EditJob] = {}
        self.root: Path | None = None

    # -- admission ---------------------------------------------------------------------

    def admitting(self) -> bool:
        return not self.closed

    def reopen(self) -> None:
        """Only lifespan startup reopens; with a job or a child still running there is nothing safe to reopen onto."""
        if any(not job.done.is_set() for job in self.jobs.values()):
            raise RuntimeError("timelapse edit jobs are still running")
        self.jobs.clear()
        self.closed = False

    def scratch_root(self) -> Path:
        if self.root is None:
            from backend.app.core.config import settings

            self.root = Path(settings.base_dir) / SCRATCH_DIRNAME  # SEC-PATH-OK: a module constant under DATA_DIR
        return self.root

    def _sweep(self) -> None:
        now = time.monotonic()
        for job_id, job in list(self.jobs.items()):
            if job.done.is_set() and job.finished is not None and now - job.finished > TERMINAL_TTL_SECONDS:
                del self.jobs[job_id]

    def reserve(
        self,
        scope: JobScope,
        *,
        archive,
        token: str,
        epoch: int,
        source: Path,
        info: dict,
        request: EditRequest,
        output_name: str,
        source_identity: tuple | None = None,
    ) -> EditJob:
        """Take a place for one edit — synchronously, before the caller's first await after its checks.

        The place is the job itself, already in the registry: what was counted
        here is what is taken. The caller stages the music and then calls
        :meth:`start`, or gives the place back with :meth:`abandon`.
        """
        if self.closed:
            raise EditRefused(503, "restoring", "A restore is in progress; videos cannot be edited until it ends", 30)
        self._sweep()
        live = [job for job in self.jobs.values() if not job.done.is_set()]
        if any(job.archive_identity == archive.recovery_identity and job.token == token for job in live):
            raise EditRefused(409, "edit_in_progress", "This video is already being edited")
        if len(live) >= MAX_JOBS:
            raise EditRefused(429, "edits_busy", "Too many videos are being edited; try again later", 30)
        capture = scope.descriptor.capture
        if sum(1 for job in live if same_user(job.capture, capture)) >= MAX_JOBS_PER_USER:
            raise EditRefused(429, "edits_busy", "You already have videos being edited; wait for one to finish", 30)
        job = EditJob(
            job_id=uuid.uuid4().hex,
            scope=scope,
            archive_id=archive.id,
            archive_identity=archive.recovery_identity,
            epoch=epoch,
            token=token,
            source=source,
            source_has_audio=bool(info.get("has_audio")),
            duration=float(info.get("duration") or 0.0),
            request=request,
            output_name=output_name,
            source_identity=source_identity,
        )
        self.jobs[job.job_id] = job
        return job

    async def prepare_directory(self, job: EditJob) -> Path:
        """The job's own directory, with the admitted video pinned in it. Raises :class:`EditRefused`."""
        job.directory = await asyncio.to_thread(_make_directory, self.scratch_root(), job.job_id)
        job.pinned = await asyncio.to_thread(_pin_source, job)
        return job.directory

    async def abandon(self, job: EditJob) -> None:
        """Give back a place whose job never started: its directory goes, then the job."""
        if job.task is not None:
            return
        await self._remove_directory(job)
        job.phase = PHASE_FAILED
        job.finished = time.monotonic()
        job.done.set()
        self.jobs.pop(job.job_id, None)

    def start(self, job: EditJob) -> EditJob:
        if self.closed:
            job.cancel_reason = ERROR_INTERRUPTED
            job.stop.set()
        job.task = asyncio.ensure_future(self._run(job))
        return job

    # -- lookups -----------------------------------------------------------------------

    def get(self, job_id: str, capture, archive_id: int) -> EditJob | None:
        self._sweep()
        job = self.jobs.get(job_id)
        if job is None or job.archive_id != archive_id or job.task is None or not same_credential(job.capture, capture):
            return None
        return job

    def jobs_of(self, capture, archive_id: int) -> list[EditJob]:
        self._sweep()
        return [
            job
            for job in self.jobs.values()
            if job.archive_id == archive_id and job.task is not None and same_credential(job.capture, capture)
        ]

    def request_cancel(self, job: EditJob, reason: str | None = None) -> bool:
        """Ask a job to stop. False = too late: its publication can no longer be stopped.

        An accepted cancel is honoured: the job reads ``cancelled`` (or its
        failure) once it really has stopped, and nothing of it is published.
        """
        if job.done.is_set():
            return job.phase != PHASE_DONE
        if job.stop.is_set():
            return True
        if job.no_return:
            return False
        job.cancel_reason = reason
        if job.phase in (PHASE_QUEUED, PHASE_ENCODING, PHASE_PUBLISHING):
            job.phase = PHASE_CANCELLING
        job.stop.set()
        return True

    # -- the worker --------------------------------------------------------------------

    async def _run(self, job: EditJob) -> None:
        outcome: str | None = None
        try:
            try:
                await self._work(job)
            except _Ended as ended:
                outcome = ended.code or "cancelled"
            except asyncio.CancelledError:
                outcome = "cancelled"
            except Exception:  # noqa: BLE001 — reported as the job's failure
                logger.exception("[EDITS] job %s failed", job.job_id[:8])
                outcome = ERROR_PUBLISH if job.phase == PHASE_PUBLISHING else ERROR_ENCODE
        finally:
            # The encoder goes back first: the next edit, or a stitch, does not wait for our cleanup.
            encoder_slot.release(job.slot)
            job.slot = None
            cleaned = await self._settle(job)
            if outcome is None:
                job.phase = PHASE_DONE
                job.progress = 1.0
            elif outcome == "cancelled":
                job.phase = PHASE_CANCELLED if job.cancel_reason is None else PHASE_FAILED
                job.error = job.cancel_reason
            else:
                job.phase = PHASE_FAILED
                job.error = outcome
            if not cleaned and job.phase != PHASE_DONE and job.error != ERROR_UNREAPED:
                job.phase = PHASE_FAILED
                job.error = ERROR_CLEANUP
            job.finished = time.monotonic()
            logger.info("[EDITS] job %s ended: %s", job.job_id[:8], job.error or job.phase)
            job.done.set()

    def _retired(self, job: EditJob, code=None) -> None:
        # A restore closes live authority too; that is an interruption, not a revoked caller.
        interrupted = not live_authority.admitting or self.closed
        self.request_cancel(job, ERROR_INTERRUPTED if interrupted else ERROR_AUTHORITY)

    def _stopped(self, job: EditJob) -> None:
        if job.stop.is_set():
            raise _Ended(job.cancel_reason)

    async def _work(self, job: EditJob) -> None:
        # One encoder for the process, taken in one step (``encoder_slot``): two edits whose
        # preparation took different times can never both find it free.
        job.slot = await encoder_slot.acquire(job.stop)
        self._stopped(job)
        try:
            handle = live_authority.register(job.scope.descriptor)
        except HTTPException as exc:
            raise _Ended(ERROR_INTERRUPTED if self.closed or not live_authority.admitting else ERROR_AUTHORITY) from exc
        job.handle = handle
        handle.on_retire = lambda code=None: self._retired(job, code)
        try:
            await live_authority.admit(handle)
        except HTTPException as exc:
            raise _Ended(job.cancel_reason or ERROR_AUTHORITY) from exc
        self._stopped(job)
        await self._encode(job)
        if not handle.deliverable():
            raise _Ended(job.cancel_reason or ERROR_AUTHORITY)
        self._stopped(job)
        await self._publish(job)

    async def _encode(self, job: EditJob) -> None:
        from backend.app.core.config import settings

        job.phase = PHASE_ENCODING
        staged = await asyncio.to_thread(_staging_place, job)
        if staged.parent != job.directory:
            # Beside the video: named in the job's directory BEFORE the encoder may create it, so
            # startup removes what a crash leaves.
            relative = os.path.relpath(staged, settings.base_dir)
            await asyncio.to_thread((job.directory / OUTPUT_NOTE).write_text, relative, "utf-8")
        job.staged = staged
        # The pinned input: the bytes this edit was admitted for, whatever the video's name holds now.
        processor = TimelapseProcessor(job.pinned or job.source)
        command = processor.edit_command(
            staged,
            trim_start=job.request.trim_start,
            trim_end=job.request.trim_end,
            speed=job.request.speed,
            audio_path=job.audio,
            audio_volume=job.request.audio_volume,
            mute=job.request.mute,
            source_has_audio=job.source_has_audio,
        )
        expected = job.expected_seconds

        def on_line(line: str) -> None:
            seconds = parse_progress_seconds(line)
            if seconds is not None:
                # Monotonic: ffmpeg's clock can step back at a filter boundary.
                job.progress = max(job.progress, min(seconds / expected, 1.0) * _ENCODE_CEILING)

        result = await media_child.run(
            command,
            name="timelapse-edit",
            hard_seconds=EDIT_SECONDS,
            idle_seconds=IDLE_SECONDS,
            stop=job.stop,
            on_line=on_line,
        )
        if result.status == media_child.UNREAPED:
            job.keep_staged = True  # it may still be writing; nothing of it is touched
            raise _Ended(ERROR_UNREAPED)
        if result.status == media_child.CANCELLED:
            raise _Ended(job.cancel_reason)
        if result.status == media_child.TIMEOUT:
            raise _Ended(ERROR_TIMEOUT)
        if result.status == media_child.STALLED:
            raise _Ended(ERROR_STALLED)
        if not result.ok:
            raise _Ended(ERROR_ENCODE)
        # The process is reaped; now the file it left is looked at before anything is published.
        try:
            info = await TimelapseProcessor(staged).get_info()
        except (ProbeFailed, OSError) as exc:
            raise _Ended(ERROR_OUTPUT) from exc
        if not info.get("has_video") or info["duration"] <= 0:
            raise _Ended(ERROR_OUTPUT)

    async def _publish(self, job: EditJob) -> None:
        from backend.app.core import database
        from backend.app.core.config import settings
        from backend.app.core.permissions import Permission
        from backend.app.models.archive_media_asset import ORIGIN_EDITOR
        from backend.app.services import archive_media

        job.phase = PHASE_PUBLISHING
        digest, size = await asyncio.to_thread(_sha256, job.staged)
        # A cancel accepted while the file was being hashed is honoured here, before any lock is asked for.
        self._stopped(job)
        publish = (
            archive_media.publish_primary if job.request.save_mode == SAVE_REPLACE else archive_media.publish_variant
        )

        def still_wanted() -> bool:
            """Asked by the seam under its own locks, last immediately before the bytes take their name.

            Synchronous, so there is no moment between "not cancelled" and "no
            return": a cancel either arrived before this and stops the
            publication, or arrives after it and is told it is too late.
            """
            if job.stop.is_set():
                return False
            job.no_return = True
            return True

        try:
            async with database.async_session() as db:
                outcome = await publish(
                    db,
                    job.archive_id,
                    staged=job.staged,
                    directory=job.source.parent,
                    filename=job.output_name,
                    base_dir=settings.base_dir,
                    origin=ORIGIN_EDITOR,
                    expected_identity=job.archive_identity,
                    expected_epoch=job.epoch,
                    expected_token=job.token,
                    content_sha256=digest,
                    size_bytes=size,
                    authority=job.capture,
                    permissions=(Permission.ARCHIVES_UPDATE_ALL.value,),
                    may_run=still_wanted,
                )
        except HTTPException as exc:
            # The seam's own guard: the account, the session or the right is gone.
            raise _Ended(ERROR_AUTHORITY if exc.status_code in (401, 403) else ERROR_PUBLISH) from exc
        if not outcome.ok:
            if outcome.refusal == archive_media.REFUSED_RUN_CHANGED:
                # ``still_wanted`` said no: the cancel was accepted, and nothing was published.
                raise _Ended(job.cancel_reason)
            raise _Ended(ERROR_INTERRUPTED if outcome.refusal == archive_media.REFUSED_RESTORING else outcome.refusal)
        job.result = {"asset_uuid": outcome.asset_uuid, "save_mode": job.request.save_mode}
        try:
            from backend.app.core.websocket import ws_manager

            await ws_manager.send_archive_updated({"id": job.archive_id, "timelapse_attached": True})
        except Exception as exc:  # noqa: BLE001 — the video is published; a missed refresh is cosmetic
            logger.debug("archive_updated after an edit of archive %s was not sent: %s", job.archive_id, exc)

    # -- endings -----------------------------------------------------------------------

    async def _remove_directory(self, job: EditJob) -> bool:
        directory = job.directory
        if directory is None:
            return True

        def remove() -> bool:
            if not directory.exists():
                return True
            if not _is_owned_directory(directory):
                return False
            shutil.rmtree(directory)
            return True

        try:
            return await asyncio.to_thread(remove)
        except OSError:
            return False

    async def _settle(self, job: EditJob) -> bool:
        """Release the watch and remove the job's own files, through any number of cancels."""

        async def settle() -> bool:
            if job.handle is not None:
                job.handle.on_retire = None
                live_authority.release(job.handle)
                job.handle = None
            if job.keep_staged:
                # The directory keeps naming the output: the next startup removes both.
                return False
            ok = True
            if job.staged is not None:
                try:
                    await asyncio.to_thread(job.staged.unlink, missing_ok=True)
                except OSError:
                    ok = False
            return await self._remove_directory(job) and ok

        task = asyncio.ensure_future(settle())
        while not task.done():
            try:
                await asyncio.wait({task})
            except asyncio.CancelledError:
                continue
        return bool(task.result())

    async def quiesce(self, seconds: float = 30.0) -> bool:
        """Close admission, stop every job and prove the workers and their children ended.

        False = a job is still running or a child was not reaped: a restore is refused.
        """
        self.closed = True
        for job in list(self.jobs.values()):
            if job.task is None:
                continue
            self.request_cancel(job, ERROR_INTERRUPTED)
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if all(job.done.is_set() for job in self.jobs.values()) and media_child.live() == 0:
                break
            await asyncio.sleep(_POLL)
        running = [job.job_id[:8] for job in self.jobs.values() if not job.done.is_set()]
        # A child whose end was not seen is still counted by its owner: ``media_child.live()`` is the
        # proof, and it clears only when the process has really gone.
        if running or media_child.live():
            logger.warning(
                "[EDITS] not drained after %.0fs: %s job(s) running, %s child process(es) alive",
                seconds,
                len(running),
                media_child.live(),
            )
            return False
        return True


timelapse_edit_runtime = TimelapseEditRuntime()


async def stage_audio(job: EditJob, chunks, filename: str) -> Path:
    """Stream the uploaded music into the job's own directory, capped, and prove it is audio.

    ``chunks`` is the request body as it arrives (``request.stream()``): every
    byte is counted here, whatever ``Content-Length`` said, and nothing is held
    beyond one chunk. Raises :class:`EditRefused`.
    """
    suffix = Path(filename or "").suffix.lower()
    if suffix not in AUDIO_SUFFIXES:
        raise EditRefused(400, "audio_invalid", "Audio must be .mp3, .wav, .m4a, .aac, or .ogg")
    target = job.directory / f"audio{suffix}"
    written = 0
    handle = await asyncio.to_thread(target.open, "wb")
    try:
        async for chunk in chunks:
            if not chunk:
                continue
            written += len(chunk)
            if written > AUDIO_LIMIT:
                raise EditRefused(413, "audio_too_large", "The music file is too large (100 MB at most)")
            await asyncio.to_thread(handle.write, chunk)
    finally:
        await asyncio.to_thread(handle.close)
    if written == 0:
        raise EditRefused(400, "audio_invalid", "The music file is empty")
    try:
        info = await TimelapseProcessor(target).get_info()
    except ProbeFailed as exc:
        raise EditRefused(400, "audio_invalid", "The music file could not be read as audio") from exc
    if not info.get("has_audio"):
        raise EditRefused(400, "audio_invalid", "The music file could not be read as audio")
    job.audio = target
    return target
