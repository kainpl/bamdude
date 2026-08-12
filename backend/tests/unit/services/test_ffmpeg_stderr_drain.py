"""ffmpeg's stderr is read for the whole life of a stream (upstream #2707 neighbours).

Nothing read the pipe after the immediate-failure check, in three long-lived
spawns. Once a 64 KiB pipe buffer fills, ffmpeg blocks inside ``write()`` and
stops producing frames on stdout — while the process is alive, registered in
``_active_streams``, and healthy by every check we make.

The drain also has to keep what it read, because the two ends carry different
evidence: the banner and stream analysis are printed once at startup and are
what diagnose "connected but never produced a frame", and the last lines are
whatever just went wrong.
"""

from __future__ import annotations

import asyncio

import pytest

from backend.app.services.ffmpeg_stderr import _SIDE_CAP, FfmpegStderrDrain


class _FakeStderr:
    """A pipe that hands out chunks, then blocks — like a live ffmpeg."""

    def __init__(self, chunks: list[bytes], *, eof: bool = True) -> None:
        self._chunks = list(chunks)
        self._eof = eof
        self.reads = 0

    async def read(self, _n: int) -> bytes:
        self.reads += 1
        if self._chunks:
            return self._chunks.pop(0)
        if self._eof:
            return b""
        await asyncio.sleep(3600)  # alive and quiet — never closes stderr
        return b""


class _FakeProcess:
    def __init__(self, stderr) -> None:
        self.stderr = stderr


async def _drain_to_eof(chunks: list[bytes]) -> FfmpegStderrDrain:
    d = FfmpegStderrDrain(_FakeProcess(_FakeStderr(chunks))).start()
    await asyncio.sleep(0)
    for _ in range(len(chunks) + 3):
        await asyncio.sleep(0)
    return d


@pytest.mark.asyncio
async def test_it_actually_reads_the_pipe():
    """The whole point: the reading happens without anyone asking for it."""
    stderr = _FakeStderr([b"line one\n", b"line two\n"])
    d = FfmpegStderrDrain(_FakeProcess(stderr)).start()
    await asyncio.sleep(0.01)
    await d.aclose()
    assert stderr.reads >= 2
    assert "line one" in d.text()


@pytest.mark.asyncio
async def test_a_process_that_never_closes_stderr_is_still_drained():
    # The stalled-but-alive case. A read-to-EOF here would hang forever; the
    # drain keeps consuming and simply waits for more.
    stderr = _FakeStderr([b"analysis\n"], eof=False)
    d = FfmpegStderrDrain(_FakeProcess(stderr)).start()
    await asyncio.sleep(0.01)
    assert "analysis" in d.text()
    await d.aclose()  # must not hang


@pytest.mark.asyncio
async def test_both_ends_survive_more_output_than_the_cap():
    """A ring buffer would lose the startup analysis; a head buffer would lose
    the failure. Overflowing by a wide margin must keep both."""
    d = await _drain_to_eof([b"HEAD-MARKER\n", b"x" * (_SIDE_CAP * 3), b"TAIL-MARKER\n"])
    await d.aclose()
    text = d.text()
    assert "HEAD-MARKER" in text
    assert "TAIL-MARKER" in text
    assert "omitted" in text  # says so rather than silently dropping


@pytest.mark.asyncio
async def test_output_that_fits_is_returned_whole_with_no_marker():
    d = await _drain_to_eof([b"short and complete\n"])
    await d.aclose()
    assert d.text() == "short and complete\n"


@pytest.mark.asyncio
async def test_a_process_with_no_stderr_pipe_is_a_no_op():
    d = FfmpegStderrDrain(_FakeProcess(None)).start()
    assert d.text() == ""
    await d.aclose()  # must not raise


@pytest.mark.asyncio
async def test_closing_twice_is_safe():
    # Both camera paths close in a `finally` that can run after an earlier
    # close on the reconnect path.
    d = await _drain_to_eof([b"x\n"])
    await d.aclose()
    await d.aclose()


@pytest.mark.asyncio
async def test_a_reader_that_raises_does_not_kill_the_stream():
    """Draining is a safety net; it must never be the thing that fails."""

    class _Exploding:
        async def read(self, _n: int) -> bytes:
            raise OSError("pipe went away")

    d = FfmpegStderrDrain(_FakeProcess(_Exploding())).start()
    await asyncio.sleep(0.01)
    await d.aclose()  # the task swallowed it
    assert d.text() == ""
