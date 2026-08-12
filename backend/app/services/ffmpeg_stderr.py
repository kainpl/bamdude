"""Keep a long-lived ffmpeg's stderr drained, and keep what it said.

ffmpeg writes to stderr for the whole life of a stream, not only when it fails.
Nobody reading that pipe is the classic precondition for a deadlock: once the
kernel pipe buffer fills — 64 KiB on Linux — ffmpeg blocks inside ``write()``
and stops producing frames on stdout, while the process is still alive, still
in ``_active_streams``, and still looks perfectly healthy from the outside.

Three long-lived spawns had exactly that shape: the RTSP fan-out stream in
``routes/camera.py`` and the RTSP and USB streams in ``services/external_camera``.
All three piped stderr, read it once in the immediate-failure branch, and then
never again for the life of the stream. One drain here rather than three copies
there — the same reasoning that produced ``library_helpers.folder_activity_at``:
a fix applied to two of three sites is the shape this codebase keeps paying for.

Whether the buffer *actually* fills depends on how chatty the ffmpeg build is
when stderr is not a TTY, and a reconnect resets it — so this is not a report of
a confirmed hang. It removes the class either way, and it costs one task per
stream.

**Both ends are kept, not just one.** ffmpeg prints the banner and its stream
analysis once at startup — the most diagnostic output there is for "connected
but never produced a frame", which is the usual P2S failure — and prints
whatever just went wrong at the end. A plain ring buffer would discard the
first; a plain head-capture would discard the second.
"""

from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger(__name__)

# Per side. 32 KiB each is far more than either end is worth reading and still
# well under the pipe buffer we are protecting.
_SIDE_CAP = 32 * 1024


class FfmpegStderrDrain:
    """Continuously read a process's stderr so the writer can never block.

    Start one per spawned ffmpeg; ``aclose()`` it when the process goes away.
    ``text()`` is safe to call at any time and does not consume anything — the
    point is that the reading has already happened.
    """

    def __init__(self, process: asyncio.subprocess.Process, *, name: str = "ffmpeg") -> None:
        self._process = process
        self._name = name
        self._task: asyncio.Task | None = None
        self._head = bytearray()
        self._tail = bytearray()
        self._dropped = 0

    def start(self) -> FfmpegStderrDrain:
        """Begin draining. No-op when the process has no stderr pipe."""
        if self._task is not None or not self._process or not self._process.stderr:
            return self
        self._task = asyncio.create_task(self._run(), name=f"ffmpeg-stderr-{self._name}")
        return self

    async def _run(self) -> None:
        stderr = self._process.stderr
        try:
            while True:
                chunk = await stderr.read(8192)
                if not chunk:
                    return  # EOF — ffmpeg has exited
                self._absorb(chunk)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — draining must never kill the stream
            logger.debug("ffmpeg stderr drain (%s) ended: %s", self._name, exc)

    def _absorb(self, chunk: bytes) -> None:
        room = _SIDE_CAP - len(self._head)
        if room > 0:
            self._head += chunk[:room]
            chunk = chunk[room:]
            if not chunk:
                return
        self._tail += chunk
        if len(self._tail) > _SIDE_CAP:
            overflow = len(self._tail) - _SIDE_CAP
            del self._tail[:overflow]
            self._dropped += overflow

    def text(self) -> str:
        """Everything captured, with a marker where output was dropped."""
        head = self._head.decode(errors="replace")
        tail = self._tail.decode(errors="replace")
        if not tail:
            return head
        if self._dropped:
            return f"{head}\n… {self._dropped} bytes omitted …\n{tail}"
        return head + tail

    async def aclose(self) -> None:
        """Stop draining. Whatever was captured stays readable afterwards."""
        task, self._task = self._task, None
        if task is None:
            return
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):  # noqa: B014 — best-effort teardown
            pass
