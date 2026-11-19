"""One ffmpeg / ffprobe child process, owned from its spawn to its reap (G10 E, spec r4 §3.9).

Until this unit the editor started ffmpeg with ``asyncio.create_subprocess_exec``
and ``await process.communicate()`` inside an HTTP request. Nothing bounded it: a
probe of a broken file could hang for ever, a cancelled request left the child
running with nobody to reap it, its whole stderr was held in memory, and a
restore could replace the archive tree under an encoder that was still writing
into it.

A child started here has ONE owner — a watcher thread that spawns it with
``Popen``, drains both pipes and reaps it:

* **A thread, not an asyncio subprocess.** ``uvicorn --reload`` on Windows runs
  a ``SelectorEventLoop``, which has no subprocess support; ``Popen`` in a
  thread works on every loop (the precedent is ``render_runtime_child``).
* **Registered before the spawn.** The watcher is in :data:`_live` before its
  thread starts and leaves only when the child has been reaped (or was never
  started). A cancel that arrives while ``Popen`` is still being created is
  remembered and acted on the moment the child exists — the spawn/cancel race
  ends in a killed, reaped child, never in an orphan.
* **Bounded.** A hard limit, an optional idle limit (no line on stdout for that
  long), a stop event and the caller's own cancellation all end the child the
  same way: terminate, a short grace, kill, and then WAIT for the reap. stdout is
  either handed line by line to ``on_line`` (ffmpeg's ``-progress``) or kept up
  to ``stdout_limit`` bytes; stderr is read to the end and only its tail kept.
* **An unreaped child is said to be unreaped.** When the reap is not seen within
  :data:`REAP_WAIT`, the outcome is ``unreaped`` and the watcher stays in
  :data:`_live`: :func:`live` is what a restore asks before it replaces files.
* **The parent log carries the work** (``inv-local-microservice-parent-log``):
  a start line and one terminal line — outcome, elapsed time, a correlation id
  and a sanitised reason. Never the command line, a path or the child's raw
  output.

A subprocess helper, not a service and not a queue: who may run how many
encoders at once is the caller's business (``timelapse_editor``).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import subprocess  # nosec B404 — the argv is built by our own callers from validated values, never a shell
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field

from backend.app.utils.ffmpeg_output import summarize_ffmpeg_stderr

logger = logging.getLogger(__name__)

COMPLETED = "completed"
FAILED = "failed"  # the child ended with a non-zero code
TIMEOUT = "timeout"  # the hard limit
STALLED = "stalled"  # the idle limit: no progress line for that long
CANCELLED = "cancelled"  # the stop event, or the caller's own cancellation
SPAWN_FAILED = "spawn_failed"
UNREAPED = "unreaped"  # killed, and its end was not seen — it may still be running

#: stderr bytes kept; the rest is read and dropped.
STDERR_TAIL = 8192
#: The longest reason a log line or a result carries.
REASON_CHARS = 240
#: Seconds between ``terminate`` and ``kill``.
TERMINATE_GRACE = 3.0
#: Seconds a killed child gets to be reaped before the outcome is ``unreaped``.
REAP_WAIT = 10.0
_POLL = 0.2
_READ = 65536

_live: set[_Watcher] = set()
_live_lock = threading.Lock()


def live() -> int:
    """Children this process started and has not seen end. A restore refuses while this is not zero."""
    with _live_lock:
        return len(_live)


@dataclass(frozen=True)
class ChildResult:
    status: str
    returncode: int | None = None
    stdout: bytes = b""
    #: A sanitised, bounded description of a failure — safe for a log line.
    reason: str | None = None
    elapsed: float = 0.0
    #: Set when the watcher's thread has ended — for ``unreaped``, the moment the child really went.
    ended: threading.Event | None = field(default=None, compare=False, repr=False)

    @property
    def ok(self) -> bool:
        return self.status == COMPLETED


class _Watcher:
    """Spawns one child in its own thread, drains its pipes and reaps it."""

    def __init__(self, argv: list[str], *, stdout_limit: int, on_line: Callable[[str], None] | None) -> None:
        self.argv = argv
        self.stdout_limit = stdout_limit
        self.on_line = on_line
        self.process: subprocess.Popen | None = None
        self.spawn_error: BaseException | None = None
        self.returncode: int | None = None
        self.stdout = bytearray()
        self.stderr = bytearray()
        self.last_activity = time.monotonic()
        self.ended = threading.Event()
        self._lock = threading.Lock()
        self._stop_asked = False
        self.thread = threading.Thread(target=self._run, name="media-child", daemon=True)

    # -- the thread ----------------------------------------------------------------------

    def _run(self) -> None:
        try:
            try:
                process = subprocess.Popen(  # nosec B603 — argv list, no shell
                    self.argv,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
            except BaseException as exc:  # noqa: BLE001 — reported as the spawn's failure
                self.spawn_error = exc
                return
            with self._lock:
                self.process = process
                stop_now = self._stop_asked
            if stop_now:
                # The cancel came while the child was being created: it exists now, so it ends now.
                self._signal(process, kill=False)
            errors = threading.Thread(target=self._drain_stderr, args=(process,), name="media-child-err", daemon=True)
            errors.start()
            self._read_stdout(process)
            self.returncode = process.wait()
            errors.join()
        finally:
            with _live_lock:
                _live.discard(self)
            self.ended.set()

    def _read_stdout(self, process: subprocess.Popen) -> None:
        stream = process.stdout
        if self.on_line is not None:
            for raw in iter(stream.readline, b""):
                self.last_activity = time.monotonic()
                with contextlib.suppress(Exception):  # a progress sink never ends the child
                    self.on_line(raw.decode("utf-8", "replace").strip())
            return
        while chunk := stream.read(_READ):
            self.last_activity = time.monotonic()
            room = self.stdout_limit - len(self.stdout)
            if room > 0:
                self.stdout += chunk[:room]

    def _drain_stderr(self, process: subprocess.Popen) -> None:
        while chunk := process.stderr.read(_READ):
            self.stderr += chunk
            if len(self.stderr) > STDERR_TAIL:
                del self.stderr[: len(self.stderr) - STDERR_TAIL]

    @staticmethod
    def _signal(process: subprocess.Popen, *, kill: bool) -> None:
        with contextlib.suppress(OSError):
            if kill:
                process.kill()
            else:
                process.terminate()

    # -- asked from the event loop -------------------------------------------------------

    def stop(self, *, kill: bool = False) -> None:
        with self._lock:
            self._stop_asked = True
            process = self.process
        if process is not None and process.poll() is None:
            self._signal(process, kill=kill)

    def reason(self) -> str | None:
        text = summarize_ffmpeg_stderr(bytes(self.stderr))
        return text[:REASON_CHARS] if text else None


async def _wait_end(watcher: _Watcher, seconds: float) -> bool:
    """Wait for the watcher's thread to end, through any number of cancellations of the caller."""
    deadline = time.monotonic() + seconds
    while not watcher.ended.is_set():
        if time.monotonic() >= deadline:
            return False
        try:
            await asyncio.sleep(_POLL)
        except asyncio.CancelledError:
            continue
    return True


async def _end(watcher: _Watcher) -> bool:
    """Terminate, then kill, then wait for the reap. False = its end was not seen."""
    watcher.stop()
    if await _wait_end(watcher, TERMINATE_GRACE):
        return True
    watcher.stop(kill=True)
    return await _wait_end(watcher, REAP_WAIT)


async def run(
    argv: list[str],
    *,
    name: str,
    hard_seconds: float,
    idle_seconds: float | None = None,
    stop: asyncio.Event | None = None,
    stdout_limit: int = 0,
    on_line: Callable[[str], None] | None = None,
    on_unreaped: Callable[[threading.Event], None] | None = None,
) -> ChildResult:
    """Run one child to its end and say how it ended. Never raises for the child's own failure.

    ``on_line`` is called on the watcher's thread with each stdout line (and
    marks progress for ``idle_seconds``); without it up to ``stdout_limit``
    bytes of stdout are returned. The caller's cancellation ends the child and
    is re-raised only after the child has been reaped (or declared unreaped).

    ``on_unreaped`` is called on the event loop, synchronously, the moment a
    child is declared unreaped, with the event its watcher sets when it really
    ends — ALSO when the caller was cancelled and is about to receive the
    cancel instead of a result (Codex E2-R2 / F3-R2). A caller that holds a
    resource for the child (the encoder slot, the files it writes) learns here
    that it must keep it.
    """
    run_id = uuid.uuid4().hex[:8]
    started = time.monotonic()
    watcher = _Watcher(argv, stdout_limit=stdout_limit, on_line=on_line)
    with _live_lock:
        _live.add(watcher)
    try:
        watcher.thread.start()
    except BaseException:
        with _live_lock:
            _live.discard(watcher)
        raise
    logger.info("[MEDIA-CHILD] %s %s started", name, run_id)

    def finish(status: str, reason: str | None = None) -> ChildResult:
        elapsed = time.monotonic() - started
        result = ChildResult(status, watcher.returncode, bytes(watcher.stdout), reason, elapsed, watcher.ended)
        log = logger.info if status in (COMPLETED, CANCELLED) else logger.warning
        log("[MEDIA-CHILD] %s %s %s after %.1fs%s", name, run_id, status, elapsed, f": {reason}" if reason else "")
        return result

    cause: str | None = None
    cancelled = False
    while not watcher.ended.is_set():
        now = time.monotonic()
        if stop is not None and stop.is_set():
            cause = CANCELLED
        elif now - started >= hard_seconds:
            cause = TIMEOUT
        elif idle_seconds is not None and now - watcher.last_activity >= idle_seconds:
            cause = STALLED
        if cause is not None:
            break
        try:
            await asyncio.sleep(_POLL)
        except asyncio.CancelledError:
            cancelled = True
            cause = CANCELLED
            break
    if cause is not None:
        reaped = await _end(watcher)
        result = finish(cause if reaped else UNREAPED, None if reaped else f"not reaped after {cause}")
        if not reaped and on_unreaped is not None:
            try:
                on_unreaped(watcher.ended)
            except Exception:  # noqa: BLE001 — the caller's bookkeeping never hides the outcome
                logger.exception("[MEDIA-CHILD] %s %s: on_unreaped failed", name, run_id)
        if cancelled:
            raise asyncio.CancelledError
        return result
    if watcher.spawn_error is not None:
        return finish(SPAWN_FAILED, type(watcher.spawn_error).__name__)
    if watcher.returncode != 0:
        return finish(FAILED, watcher.reason() or f"exit code {watcher.returncode}")
    return finish(COMPLETED)
