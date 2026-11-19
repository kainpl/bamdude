"""Layer-based timelapse for external cameras (G10 F).

A frame is captured on each layer change and the frames are stitched into a
video when the print completes.

What this unit changed, and why:

* **A session is a UUID, and its directory says whose it is.** The directory
  used to be named by the second the print started, so two sessions of one
  printer could share it; and the only thing that ever cleaned the tree was an
  age rule. Every session directory now carries a manifest (:data:`MANIFEST`)
  written by the session itself: its id, its printer, its archive and its
  STATE — ``active`` while frames are captured, ``finalizing`` from before the
  stitch starts, ``retained`` when the stitch did not produce a published
  video.
* **Nothing is removed under a writer or a reader.** A frame being written and
  an ffmpeg reading the frames both hold the session (:attr:`TimelapseSession._busy`);
  ``cleanup`` asked meanwhile is carried out when the last of them has ended.
* **A disk write is owned until its THREAD ends** (:func:`_owned_io`), not until
  the coroutine that asked for it does: a capture that is cancelled, or whose
  session is detached or cancelled meanwhile, leaves its write counted in
  :data:`_io`, and a restore's drain is refused while one is still running.
* **A stitch whose ffmpeg could not be reaped pins the session**
  (:data:`_pinned_sessions`) for the life of the process: no cleanup, no
  retention bound, no startup-style sweep and no drain treats its frames or its
  output as free.
* **One encoder for the process**, shared with the editor (``encoder_slot``):
  two stitches, or a stitch and an edit, never encode together.
* **The stitch is an owned, bounded child** (``media_child``): two encoder
  threads and a short lookahead, a time limit that grows with the number of
  frames (:func:`stitch_limit`), an idle limit on ffmpeg's own progress output,
  and its stderr held only as a tail. The source resolution is kept (OD-6).
* **A failed stitch keeps its frames.** Failure, a cancelled stitch, a
  publication that was refused and a restart leave the frames ``retained`` for
  a manual recovery — bounded: :data:`RETAIN_SECONDS` and :data:`RETAIN_BYTES`
  over all retained sessions, oldest first. EVERY step into ``retained``
  applies the bounds at once (:meth:`TimelapseSession.retain`), and a running
  server applies them again every :data:`RETENTION_INTERVAL_SECONDS`
  (:func:`start_janitor`), so frames past their time go without a restart.
  Every such round is disk work counted until its thread ends, and none starts
  while a restore holds recording closed. The unfinished output is removed by
  the stitch that made it. Frames go only after the video was published (the
  caller says so) or the print itself failed.
* **Capture is bounded too.** When the frames of the sessions being recorded
  reach :data:`ACTIVE_BYTES`, or the volume is down to its reserve, capture
  stops for that session with one warning; the print is not affected.
* **The janitor reads manifests, never ages of unknown directories.** At
  startup a directory with our manifest that no live session owns becomes
  ``retained`` (an interrupted run) and then falls under the bounds above. A
  directory in the shape the old code wrote is adopted the same way. Anything
  else under the tree — a foreign file, a directory without a manifest, a link
  — is left alone and never entered.
* **A restore waits for it.** :func:`quiesce` closes capture, stops every
  stitch (its frames stay retained) and proves writers and encoders ended. A
  frame the camera answers after that is dropped, never written: closing is
  asked again in the same step that registers the write.

The camera side is untouched: frames still come from the live view's buffer or
the capture runtime (G09), and no recorder ever stops the shared producer.
"""

import asyncio
import contextlib
import functools
import json
import logging
import os
import re
import shutil
import stat
import time
from dataclasses import dataclass, field
from pathlib import Path
from uuid import uuid4

from backend.app.core.config import settings
from backend.app.services import media_child
from backend.app.services.camera import apply_camera_rotation
from backend.app.services.camera_runtime import CameraCaptureRequest, capture
from backend.app.services.encoder_slot import encoder_slot

logger = logging.getLogger(__name__)

FRAMES_DIRNAME = "timelapse_frames"
#: The owner manifest of a session directory.
MANIFEST = "session.json"
MANIFEST_KIND = "bamdude-layer-timelapse"

STATE_ACTIVE = "active"
STATE_FINALIZING = "finalizing"
STATE_RETAINED = "retained"

GIB = 1024**3
MIB = 1024**2
#: Starting bounds (spec r4 §12, GP3).
RETAIN_SECONDS = 24 * 3600
RETAIN_BYTES = 2 * GIB
ACTIVE_BYTES = 10 * GIB
RESERVE_FLOOR = 512 * MIB
RESERVE_SHARE = 0.05
STITCH_MIN_SECONDS = 300.0
STITCH_SECONDS_PER_FRAME = 0.5
STITCH_MAX_SECONDS = 3600.0
STITCH_IDLE_SECONDS = 60.0
#: How often a running server applies the retention bounds again.
RETENTION_INTERVAL_SECONDS = 3600.0
#: How long shutdown waits for a retention round already in its worker thread.
JANITOR_STOP_SECONDS = 30.0
ENCODER_THREADS = 2
ENCODER_LOOKAHEAD = 10

_FRAME_NAME = re.compile(r"layer_\d{5}\.jpg")
_LEGACY_SESSION = re.compile(r"\d{8}_\d{6}")
_OUTPUT_NAME = re.compile(r"timelapse_([0-9a-f]{32})\.mp4")

# Active timelapse sessions: {printer_id: TimelapseSession}
_active_sessions: dict[int, "TimelapseSession"] = {}
# Sessions detached from their printer and being stitched: {session_id: TimelapseSession}
_finalizing: dict[str, "TimelapseSession"] = {}
# Sessions whose encoder was not reaped: it may still read the frames and write the output.
_pinned_sessions: dict[str, "TimelapseSession"] = {}
# Disk writes of this module still running in a worker thread, whoever asked for them has gone or not.
_io: set = set()
_closed = False
_janitor: asyncio.Task | None = None
# The periodic round whose thread may still run after its loop was cancelled.
_janitor_round: asyncio.Future | None = None


def get_ffmpeg_path() -> str | None:
    """Get the path to ffmpeg executable."""
    # Try shutil.which first
    path = shutil.which("ffmpeg")
    if path:
        return path
    # Check common locations (systemd services may have limited PATH)
    for common_path in ["/usr/bin/ffmpeg", "/usr/local/bin/ffmpeg", "/opt/homebrew/bin/ffmpeg"]:
        if Path(common_path).exists():
            return common_path
    return None


def frames_root() -> Path:
    return settings.base_dir / FRAMES_DIRNAME  # SEC-PATH-OK: a module constant under DATA_DIR


def stitch_limit(frames: int) -> float:
    """The hard time limit of a stitch: it grows with the frames, between the two bounds."""
    return max(STITCH_MIN_SECONDS, min(STITCH_MAX_SECONDS, STITCH_MIN_SECONDS + STITCH_SECONDS_PER_FRAME * frames))


def _write_manifest(directory: Path, data: dict) -> None:
    """Replace the manifest in one step: a reader never sees half of it."""
    staged = directory / f".{MANIFEST}.{uuid4().hex[:8]}"
    staged.write_text(json.dumps(data, sort_keys=True), encoding="utf-8")
    os.replace(staged, directory / MANIFEST)


def _read_manifest(directory: Path) -> dict | None:
    """Our manifest of ``directory``, or ``None`` — also when it is not a plain file or names another session."""
    path = directory / MANIFEST
    try:
        if not stat.S_ISREG(path.lstat().st_mode):
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("kind") != MANIFEST_KIND or data.get("session_id") != directory.name:
        return None
    return data


def _is_real_directory(path: Path) -> bool:
    try:
        return stat.S_ISDIR(path.lstat().st_mode) and not path.is_symlink() and not path.is_junction()
    except OSError:
        return False


def _directory_bytes(directory: Path) -> int:
    total = 0
    try:
        for entry in os.scandir(directory):
            with contextlib.suppress(OSError):
                if entry.is_file(follow_symlinks=False):
                    total += entry.stat(follow_symlinks=False).st_size
    except OSError:
        return total
    return total


def _owned_thread(function, *args) -> asyncio.Future:
    """Run one piece of disk work of this module in a worker thread, counted in :data:`_io` until the THREAD ends.

    Counted by a callback on the future, not by the caller's ``finally``: a
    caller that is cancelled stops waiting, the work does not, and a drain
    still sees it.
    """
    future = asyncio.get_running_loop().run_in_executor(None, function, *args)
    _io.add(future)

    def ended(done: asyncio.Future) -> None:
        _io.discard(done)
        if not done.cancelled():
            done.exception()  # read here: a caller that was cancelled never reads it

    future.add_done_callback(ended)
    return future


def _owned_io(session: "TimelapseSession", function, *args) -> asyncio.Future:
    """Run one disk write of a session in a worker thread, owned until the THREAD ends.

    The session is held and the write is counted in :data:`_io` from here to the
    thread's own end. A caller that is cancelled stops waiting; the write does
    not stop being a write, and the directory is not removed under it.
    """
    session._hold()
    future = _owned_thread(function, *args)
    future.add_done_callback(lambda _done: session._release())
    return future


def _retention_round() -> asyncio.Future | None:
    """Start one application of the retention bounds — or none while recording is closed for a restore.

    The check and the registration in :data:`_io` are one synchronous step: a
    drain that has answered "drained" has closed recording, and no round starts
    after it (Codex F4-R).
    """
    if _closed:
        return None
    return _owned_thread(apply_retention)


@dataclass(eq=False)
class TimelapseSession:
    """One recording: its frames directory and everything that may touch it."""

    printer_id: int
    archive_id: int | None
    camera_url: str
    camera_type: str
    snapshot_url: str | None = None  # Optional single-frame override; upstream #1177
    rotation: int = 0  # The printer's configured camera_rotation, degrees clockwise
    last_layer: int = -1
    frame_count: int = 0
    # A UUID, never the clock: two sessions of one printer never share a directory.
    session_id: str = field(default_factory=lambda: uuid4().hex)
    frames_dir: Path = field(init=False)
    state: str = STATE_ACTIVE
    bytes_written: int = 0
    #: Why the last stitch did not give a video (a ``media_child`` status, or a code of ours).
    failure: str | None = None
    stop: asyncio.Event = field(default_factory=asyncio.Event, repr=False)
    _busy: int = field(default=0, repr=False)  # writers and the encoder holding the directory
    _discard: bool = field(default=False, repr=False)
    _pinned: bool = field(default=False, repr=False)  # an unreaped encoder may still be reading
    _capped: bool = field(default=False, repr=False)
    _created: float = field(default_factory=time.time, repr=False)

    def __post_init__(self):
        self.frames_dir = frames_root() / str(self.printer_id) / self.session_id
        self.frames_dir.mkdir(parents=True, exist_ok=False)
        self._save()
        logger.info("Created timelapse session %s for printer %s", self.session_id, self.printer_id)

    # -- ownership ---------------------------------------------------------------------

    def manifest(self, **extra) -> dict:
        return {
            "kind": MANIFEST_KIND,
            "session_id": self.session_id,
            "printer_id": self.printer_id,
            "archive_id": self.archive_id,
            "state": self.state,
            "created_at": self._created,
            "frames": self.frame_count,
            **extra,
        }

    def _save(self, **extra) -> None:
        try:
            _write_manifest(self.frames_dir, self.manifest(**extra))
        except OSError as exc:
            logger.warning("Timelapse session %s: its manifest could not be written (%s)", self.session_id, exc)

    @property
    def busy(self) -> bool:
        return self._busy > 0

    def _hold(self) -> None:
        self._busy += 1

    def _release(self) -> None:
        self._busy -= 1
        if self._busy == 0 and self._discard:
            self._remove()

    def _remove(self) -> None:
        if self._pinned:
            return
        try:
            if self.frames_dir.exists():
                shutil.rmtree(self.frames_dir, ignore_errors=True)
                logger.info("Cleaned up timelapse frames for session %s", self.session_id)
        except Exception as e:
            logger.warning("Failed to cleanup timelapse frames: %s", e)

    def cleanup(self):
        """Remove the frames directory — now, or as soon as nothing is writing to it or reading it."""
        self._discard = True
        if self._busy == 0:
            self._remove()

    def retain(self, reason: str) -> None:
        """Keep the frames for a manual recovery, within the retention bounds. Never undoes a ``cleanup``.

        The bounds are applied here, at every step into ``retained`` — also when
        the stitch succeeded and the archive then refused the video: what is
        kept is counted the moment it starts being kept. Blocking disk work; a
        caller on the event loop hands it to a thread.
        """
        if self._discard:
            return
        self.state = STATE_RETAINED
        self._save(reason=reason, retained_at=time.time())
        logger.info(
            "Timelapse session %s of printer %s: %s frames kept for recovery (%s)",
            self.session_id,
            self.printer_id,
            self.frame_count,
            reason,
        )
        if not _closed:  # a restore holds recording closed: nothing under the frames tree is removed now
            with contextlib.suppress(Exception):
                apply_retention()

    # -- capture -----------------------------------------------------------------------

    def _may_capture(self) -> bool:
        """The capture bounds: the frames of every session being recorded, and the volume's reserve."""
        if self._capped:
            return False
        recording = sum(session.bytes_written for session in (*_active_sessions.values(), *_finalizing.values()))
        over = recording >= ACTIVE_BYTES
        if not over:
            try:
                usage = shutil.disk_usage(self.frames_dir)
                over = usage.free < max(RESERVE_FLOOR, int(usage.total * RESERVE_SHARE))
            except OSError:
                over = False
        if over:
            self._capped = True
            logger.warning(
                "Layer timelapse of printer %s stopped capturing after %s frames: the frame storage limit "
                "or the disk reserve is reached. The print is not affected.",
                self.printer_id,
                self.frame_count,
            )
        return not over

    async def capture_layer(self, layer_num: int) -> bool:
        """Capture frame if layer changed.

        Args:
            layer_num: Current layer number from printer

        Returns:
            True if frame was captured, False otherwise
        """
        # Only capture if layer increased
        if layer_num <= self.last_layer:
            return False

        self.last_layer = layer_num
        if _closed or self.state != STATE_ACTIVE or self._discard or not self._may_capture():
            return False

        # Held from before the capture: the directory is not removed under the write below.
        self._hold()
        try:
            # Reuse the live view's frame rather than opening a second handle on
            # a single-reader device (#2707). Unguarded, a print watched from
            # start to finish recorded ZERO successful layer captures and the
            # stitched video came out empty — the failure this whole guard
            # exists for, and one nobody reports as a camera bug.
            from backend.app.api.routes.camera import live_frame_for_capture

            # A camera with its own snapshot URL records that still, watched or not: ``capture`` fetches it
            # beside the stream and falls back to the view's frame itself (owner, 2026-11-15).
            defer, buffered = (False, None) if self.snapshot_url else live_frame_for_capture(self.printer_id)
            if defer:
                if not buffered:
                    # Viewer attached, buffer momentarily empty: skip this layer
                    # rather than compete and kick them off (#1348).
                    logger.debug(
                        "Skipping layer %s for printer %s: viewer attached, no buffered frame yet",
                        layer_num,
                        self.printer_id,
                    )
                    return False
                frame_data = buffered
            else:
                result = await capture(
                    CameraCaptureRequest.external(
                        url=self.camera_url,
                        camera_type=self.camera_type,
                        snapshot_url=self.snapshot_url,
                        purpose="layer_timelapse",
                        printer_id=self.printer_id,
                    )
                )
                frame_data = result.frame
            if frame_data:
                # Rotate on the way in, not at stitch time: ffmpeg reads the
                # frames straight off disk, so a frame saved the wrong way up
                # stays that way in the finished video (#2708). Off the event
                # loop — PIL is synchronous and this runs once per layer.
                if self.rotation:
                    frame_data = await asyncio.to_thread(apply_camera_rotation, frame_data, self.rotation, logger)
                # Asked again after the last await, in the same synchronous step as the write's registration
                # in ``_io``: a frame the camera answered after a drain closed recording is dropped, never
                # written behind its "drained" (Codex F1-R).
                if _closed or self._discard or self.state != STATE_ACTIVE:
                    return False  # closed, cancelled or detached while the camera answered: nothing is written
                frame_path = self.frames_dir / f"layer_{layer_num:05d}.jpg"
                # Shielded: this caller may be cancelled, the write is owned to its thread's end.
                await asyncio.shield(_owned_io(self, frame_path.write_bytes, frame_data))
                self.frame_count += 1
                self.bytes_written += len(frame_data)
                logger.debug(
                    "Captured layer %s for printer %s (frame %s)", layer_num, self.printer_id, self.frame_count
                )
                return True
            else:
                logger.warning("Failed to capture frame for layer %s", layer_num)
                return False
        except Exception as e:
            logger.error("Error capturing timelapse frame: %s", e)
            return False
        finally:
            self._release()

    # -- stitch ------------------------------------------------------------------------

    def _write_concat(self, fps: int) -> int:
        """List the frames for ffmpeg's concat demuxer. Returns how many there are."""
        # Sorted names handle gaps in layer numbers (e.g., if some captures failed)
        frames = sorted(path.name for path in self.frames_dir.glob("layer_*.jpg") if _FRAME_NAME.fullmatch(path.name))
        if not frames:
            return 0
        with open(self.frames_dir / "frames.txt", "w", encoding="utf-8") as handle:
            for name in frames:
                # Each frame shown for 1/fps duration
                handle.write(f"file '{name}'\n")
                handle.write(f"duration {1.0 / fps}\n")
            # Add last frame again (required by concat demuxer)
            handle.write(f"file '{frames[-1]}'\n")
        return len(frames)

    async def stitch(self, output_path: Path, fps: int = 30) -> bool:
        """Create MP4 from captured frames using ffmpeg. ``self.failure`` says why it did not."""
        self.failure = None
        if self.frame_count == 0:
            logger.warning("No frames to stitch")
            self.failure = "no_frames"
            return False

        ffmpeg = get_ffmpeg_path()
        if not ffmpeg:
            logger.error("ffmpeg not found - required for timelapse stitching")
            self.failure = "ffmpeg_missing"
            return False

        self._hold()  # the encoder reads the frames: nothing removes them until it has ended
        try:
            try:
                frames = await asyncio.shield(_owned_io(self, self._write_concat, fps))
            except OSError as e:
                logger.error("Failed to create concat file: %s", e)
                self.failure = "concat_failed"
                return False
            if not frames:
                logger.warning("No frame files found in timelapse directory")
                self.failure = "no_frames"
                return False

            # The concat demuxer reads the frames off disk one by one; nothing here holds them in memory.
            cmd = [
                ffmpeg,
                "-y",
                "-nostdin",
                "-v",
                "error",
                "-progress",
                "pipe:1",
                "-nostats",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                "frames.txt",
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                "-preset",
                "medium",
                "-crf",
                "23",
                "-threads",
                str(ENCODER_THREADS),
                "-x264-params",
                f"rc-lookahead={ENCODER_LOOKAHEAD}",
                "-f",
                "mp4",
                str(output_path),
            ]
            # One encoder for the process, shared with the editor: taken in one step, first come first served.
            ticket = await encoder_slot.acquire(self.stop)
            if ticket is None:
                self.failure = media_child.CANCELLED
                return False
            result = None
            try:
                result = await media_child.run(
                    cmd,
                    name="timelapse-stitch",
                    hard_seconds=stitch_limit(frames),
                    idle_seconds=STITCH_IDLE_SECONDS,
                    stop=self.stop,
                    on_line=lambda _line: None,  # progress lines keep the idle limit away
                    cwd=self.frames_dir,  # the concat list names the frames relatively
                )
            finally:
                if result is not None and result.status == media_child.UNREAPED:
                    # The encoder may still run: no other one starts until it has really ended (Codex F3-R).
                    encoder_slot.keep_until_reaped(ticket, result.ended)
                else:
                    encoder_slot.release(ticket)
            if result.status == media_child.UNREAPED:
                # It may still be reading the frames and writing the output: neither is touched again,
                # by anybody — the pin is registered for every cleanup path and for the drain.
                self._pinned = True
                _pinned_sessions[self.session_id] = self
                self.failure = result.status
                return False
            if not result.ok:
                self.failure = result.status
                return False
            if not await _is_video(output_path):
                self.failure = "output_invalid"
                return False
            logger.info("Created timelapse video: %s (%s frames)", output_path.name, frames)
            return True
        finally:
            self._release()


async def _is_video(path: Path) -> bool:
    """Is the stitched file a playable video? One bounded probe."""
    from backend.app.services.timelapse_processor import ProbeFailed, TimelapseProcessor

    try:
        info = await TimelapseProcessor(path).get_info()
    except (ProbeFailed, RuntimeError, OSError):
        return False
    return bool(info.get("has_video")) and info["duration"] > 0


def start_session(
    printer_id: int,
    archive_id: int | None,
    url: str,
    cam_type: str,
    snapshot_url: str | None = None,
    rotation: int = 0,
) -> TimelapseSession | None:
    """Start new timelapse session for a printer.

    Args:
        printer_id: The printer ID
        archive_id: Associated print archive ID (optional)
        url: External camera URL
        cam_type: Camera type ("mjpeg", "rtsp", "snapshot")
        snapshot_url: Optional single-frame URL override; when set, layer captures
            fetch from it directly instead of opening the live stream. Upstream
            Bambuddy #1177.
        rotation: The printer's configured ``camera_rotation`` (degrees clockwise),
            applied to every frame before it is written. Without it the video
            comes out rotated while a snapshot of the same print does not (#2708).

    Returns:
        The new TimelapseSession, or ``None`` while a restore holds recording closed.
    """
    # Cancel any existing session
    cancel_session(printer_id)
    if _closed:
        logger.info("Layer timelapse for printer %s not started: recording is closed for a restore", printer_id)
        return None

    session = TimelapseSession(
        printer_id=printer_id,
        archive_id=archive_id,
        camera_url=url,
        camera_type=cam_type,
        snapshot_url=snapshot_url,
        rotation=rotation,
    )
    _active_sessions[printer_id] = session
    logger.info("Started timelapse session for printer %s", printer_id)
    return session


def get_session(printer_id: int) -> TimelapseSession | None:
    """Get active timelapse session for a printer."""
    return _active_sessions.get(printer_id)


async def on_layer_change(printer_id: int, layer_num: int):
    """Called on layer change - captures frame if session active.

    Args:
        printer_id: The printer ID
        layer_num: Current layer number
    """
    session = get_session(printer_id)
    if session:
        await session.capture_layer(layer_num)


async def on_print_complete(printer_id: int) -> Path | None:
    """Stitch timelapse and return path. Cleans up session.

    Args:
        printer_id: The printer ID

    Returns:
        Path to stitched video, or None if no session or stitching failed
    """
    session = _active_sessions.pop(printer_id, None)
    return await finish_session(session)


def take_session(printer_id: int, expected: TimelapseSession | None) -> TimelapseSession | None:
    """Detach only the session observed by this terminal, never its successor."""
    if expected is not None and _active_sessions.get(printer_id) is expected:
        return _active_sessions.pop(printer_id)
    return None


async def finish_session(session: TimelapseSession | None, *, keep_frames: bool = False) -> Path | None:
    """Stitch an already detached session; nothing here can affect a newer run.

    On success the video's path is returned. With ``keep_frames`` the frames
    stay until the caller has published the video and calls ``cleanup`` — or
    ``retain`` when the publication was refused; without it they go at once. On
    a failure the frames are RETAINED for a manual recovery and the unfinished
    output is removed.
    """
    if not session:
        return None

    if session.frame_count == 0:
        logger.info("No timelapse frames captured for session %s", session.session_id)
        session.cleanup()
        return None

    # Create output path in parent of frames dir
    output_path = session.frames_dir.parent / f"timelapse_{session.session_id}.mp4"
    # Durable before the encoder starts: a restart finds a session that was being finalised.
    session.state = STATE_FINALIZING
    session._save()
    _finalizing[session.session_id] = session
    ok = False
    try:
        ok = await session.stitch(output_path)
        if ok:
            if not keep_frames:
                session.cleanup()
            return output_path
        logger.error("Timelapse stitch for printer %s did not give a video (%s)", session.printer_id, session.failure)
        # Disk work (the manifest): off the loop and owned to its thread's end. The bounds are applied
        # below, once this session is no longer counted as being finalised.
        await retain_session(session, f"stitch_{session.failure}")
        return None
    except asyncio.CancelledError:
        session.retain("stitch_cancelled")
        raise
    except Exception as e:
        logger.error("Timelapse completion failed: %s", e)
        session.retain("stitch_error")
        return None
    finally:
        _finalizing.pop(session.session_id, None)
        # Both counted in ``_io`` from the step that registered them: the session has just left
        # ``_finalizing``, and a drain must still see the disk work it does on its way out.
        if not ok and not session._pinned:
            # The unfinished output is this stitch's own, and its encoder has ended.
            with contextlib.suppress(OSError):
                await asyncio.shield(_owned_thread(functools.partial(output_path.unlink, missing_ok=True)))
        if not ok:
            retention = _retention_round()
            if retention is not None:
                with contextlib.suppress(Exception):
                    await asyncio.shield(retention)


async def retain_session(session: TimelapseSession, reason: str) -> None:
    """:meth:`TimelapseSession.retain` off the event loop, owned until its thread ends (Codex F4-R).

    The manifest write and the retention bounds it applies are disk work a
    restore's drain must see; a caller that is cancelled stops waiting, the work
    is still counted.
    """
    await asyncio.shield(_owned_io(session, session.retain, reason))


def cancel_session(printer_id: int):
    """Cancel and cleanup timelapse session (on print fail/cancel).

    Args:
        printer_id: The printer ID
    """
    session = _active_sessions.pop(printer_id, None)
    if session:
        session.cleanup()
        logger.info("Cancelled timelapse session for printer %s", printer_id)


def get_active_sessions() -> dict[int, TimelapseSession]:
    """Get all active timelapse sessions."""
    return _active_sessions.copy()


# -- restore and shutdown ----------------------------------------------------------------


def admitting() -> bool:
    return not _closed


def reopen() -> None:
    """Only lifespan startup reopens recording."""
    global _closed
    _closed = False


async def quiesce(seconds: float = 30.0) -> bool:
    """Close capture, stop every stitch and prove that writers and encoders ended.

    A stopped stitch keeps its frames (``retained``). False = something is
    still writing or reading: a restore is refused.
    """
    global _closed
    _closed = True
    for session in list(_finalizing.values()):
        session.stop.set()
    deadline = time.monotonic() + seconds
    while True:
        # Physical writes are counted by their own threads (``_io``), whatever became of the request
        # or of the session's place in the registries; an unreaped encoder is never "ended".
        if not _finalizing and not _io and not _pinned_sessions:
            return True
        if time.monotonic() >= deadline:
            break
        await asyncio.sleep(min(0.2, max(deadline - time.monotonic(), 0.01)))
    logger.warning(
        "Layer timelapse not drained after %.2fs: %s stitch(es), %s disk write(s) still running, "
        "%s encoder(s) whose end was not seen",
        seconds,
        len(_finalizing),
        len(_io),
        len(_pinned_sessions),
    )
    return False


async def _retention_loop(interval: float) -> None:
    global _janitor_round
    while True:
        await asyncio.sleep(interval)
        # A round starts only while recording is admitted, and its thread is counted in ``_io``.
        current = _retention_round()
        if current is None:
            continue  # a restore holds recording closed
        _janitor_round = current
        try:
            removed = await asyncio.shield(current)
            if removed:
                logger.info("Removed %s retained timelapse frame director%s", removed, "y" if removed == 1 else "ies")
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — the next round tries again
            logger.debug("Timelapse retention round failed: %s", type(exc).__name__)


def start_janitor(interval: float | None = None) -> asyncio.Task:
    """Lifespan startup: apply the retention bounds again every ``interval`` while the server runs."""
    global _janitor
    if _janitor is None or _janitor.done():
        _janitor = asyncio.ensure_future(_retention_loop(RETENTION_INTERVAL_SECONDS if interval is None else interval))
    return _janitor


async def stop_janitor(seconds: float = JANITOR_STOP_SECONDS) -> bool:
    """Lifespan shutdown: the periodic loop is cancelled, and a round already in its thread is waited for.

    Cancelling the loop does not stop a round that is removing directories in
    a worker thread (Codex F4-R): shutdown waits for that thread, up to
    ``seconds``. False = it had not ended; it stays counted in ``_io``, so a
    restore's drain still refuses while it runs.
    """
    global _janitor, _janitor_round
    task, _janitor = _janitor, None
    if task is not None and not task.done():
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task
    current, _janitor_round = _janitor_round, None
    if current is None or current.done():
        return True
    done, _pending = await asyncio.wait({current}, timeout=seconds)
    if not done:
        logger.warning("Layer timelapse: a retention round was still running %.0fs after shutdown asked", seconds)
        return False
    return True


# -- the janitor -------------------------------------------------------------------------


def _live_ids() -> set[str]:
    """Sessions no janitor may touch: recorded, being stitched, or pinned by an encoder whose end was not seen."""
    return {
        session.session_id
        for session in (*_active_sessions.values(), *_finalizing.values(), *_pinned_sessions.values())
    }


def _adopt_legacy(directory: Path) -> dict | None:
    """A directory in the shape the old code wrote, taken as an interrupted session of ours.

    Only when its name is the old timestamp and it holds nothing but frames and
    the concat list: anything else is not provably ours and stays untouched.
    """
    if not _LEGACY_SESSION.fullmatch(directory.name):
        return None
    try:
        entries = list(os.scandir(directory))
    except OSError:
        return None
    for entry in entries:
        if not entry.is_file(follow_symlinks=False):
            return None
        if entry.name != "frames.txt" and not _FRAME_NAME.fullmatch(entry.name):
            return None
    try:
        retained_at = directory.stat().st_mtime
    except OSError:
        return None
    return {
        "kind": MANIFEST_KIND,
        "session_id": directory.name,
        "state": STATE_RETAINED,
        "reason": "legacy",
        "retained_at": retained_at,
    }


def _owned_sessions(root: Path) -> list[tuple[Path, dict]]:
    """Every session directory under ``root`` that is provably ours, with its manifest."""
    owned: list[tuple[Path, dict]] = []
    if not _is_real_directory(root):
        return owned
    try:
        printers = [Path(entry.path) for entry in os.scandir(root)]
    except OSError:
        return owned
    for printer_dir in printers:
        if not _is_real_directory(printer_dir):
            continue
        try:
            sessions = [Path(entry.path) for entry in os.scandir(printer_dir)]
        except OSError:
            continue
        for session_dir in sessions:
            if not _is_real_directory(session_dir):
                continue
            manifest = _read_manifest(session_dir)
            if manifest is None:
                manifest = _adopt_legacy(session_dir)
                if manifest is None:
                    continue  # foreign: never entered further, never removed
                with contextlib.suppress(OSError):
                    _write_manifest(session_dir, manifest)
            owned.append((session_dir, manifest))
    return owned


def apply_retention(now: float | None = None) -> int:
    """Enforce the retention bounds on retained sessions no live session owns. Returns how many went."""
    now = time.time() if now is None else now
    root = frames_root()
    live = _live_ids()
    kept: list[tuple[float, int, Path]] = []
    removed = 0
    for directory, manifest in _owned_sessions(root):
        if manifest.get("session_id") in live or manifest.get("state") != STATE_RETAINED:
            continue
        try:
            retained_at = float(manifest.get("retained_at") or 0.0)
        except (TypeError, ValueError):
            retained_at = 0.0
        if now - retained_at > RETAIN_SECONDS:
            removed += _remove_session(directory)
        else:
            kept.append((retained_at, _directory_bytes(directory), directory))
    total = sum(size for _at, size, _dir in kept)
    for _retained_at, size, directory in sorted(kept, key=lambda item: item[0]):
        if total <= RETAIN_BYTES:
            break
        if _remove_session(directory):
            removed += 1
            total -= size
    return removed


def _remove_session(directory: Path) -> int:
    try:
        shutil.rmtree(directory)
        return 1
    except OSError as exc:
        logger.debug("Could not remove retained timelapse frames %s: %s", directory.name, exc)
        return 0


def sweep_orphaned_frames(max_age_hours: float | None = None) -> int:
    """Startup: settle what earlier runs left under the frames tree. Returns how many directories went.

    ``_active_sessions`` lives in memory only, so a restart during a print
    leaves a directory nothing will come back for. Such a directory — one with
    our manifest, in a state only a live session can have — becomes
    ``retained``, and the retention bounds then decide. A stitched output file
    whose session no longer exists is removed. Nothing is decided by the age of
    a directory that is not provably ours (``max_age_hours`` is ignored and
    kept only for callers of the old signature).
    """
    root = frames_root()
    if not _is_real_directory(root):
        return 0
    live = _live_ids()
    now = time.time()
    for directory, manifest in _owned_sessions(root):
        if manifest.get("session_id") in live or manifest.get("state") == STATE_RETAINED:
            continue
        manifest = {**manifest, "state": STATE_RETAINED, "reason": "interrupted", "retained_at": now}
        with contextlib.suppress(OSError):
            _write_manifest(directory, manifest)
    removed = apply_retention(now)

    # Outputs a dead stitch left, and per-printer directories that are now empty.
    try:
        printers = [Path(entry.path) for entry in os.scandir(root)]
    except OSError:
        printers = []
    for printer_dir in printers:
        if not _is_real_directory(printer_dir):
            continue
        try:
            for entry in os.scandir(printer_dir):
                match = _OUTPUT_NAME.fullmatch(entry.name)
                if match and match.group(1) not in live and entry.is_file(follow_symlinks=False):
                    with contextlib.suppress(OSError):
                        os.unlink(entry.path)
            # Drop the per-printer directory once its last session is gone, so a
            # printer that never films again stops leaving an empty shell behind.
            if not any(os.scandir(printer_dir)):
                printer_dir.rmdir()
        except OSError:
            continue

    if removed:
        logger.info("Removed %s retained timelapse frame director%s", removed, "y" if removed == 1 else "ies")
    return removed
