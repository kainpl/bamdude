"""Timelapse video probing, thumbnails and the editor's ffmpeg command (G10 E).

Every ffmpeg / ffprobe here is a child ``media_child`` owns: bounded in time,
reaped on every way out, its output never held whole. Probes and thumbnail runs
of the same file are shared — an editor opened three times starts one probe —
and at most :data:`PROBE_SLOTS` of them run at once, so a page that is reloaded
in a loop cannot fill the host with ffmpeg processes.

The editor's encode itself is started by ``timelapse_editor``; this module only
builds its command and reads its progress.
"""

import asyncio
import json
import logging
import math
import os
import time
from pathlib import Path

from backend.app.services import media_child
from backend.app.services.camera import get_ffmpeg_path

logger = logging.getLogger(__name__)

#: Starting bounds (spec r4 §12, GP3).
PROBE_SECONDS = 10.0
THUMBNAILS_SECONDS = 30.0
PROBE_SLOTS = 2
ENCODER_THREADS = 2
ENCODER_LOOKAHEAD = 10
_PROBE_BYTES = 256 * 1024
_THUMBNAIL_BYTES = 512 * 1024

SPEED_MIN = 0.25
SPEED_MAX = 4.0

_shared: dict[tuple, asyncio.Task] = {}
_slots: asyncio.Semaphore | None = None
_slots_loop: asyncio.AbstractEventLoop | None = None


class ProbeFailed(RuntimeError):
    """ffprobe / ffmpeg did not give an answer. ``code`` is safe to show; nothing of the child's output is."""

    def __init__(self, code: str, detail: str | None = None) -> None:
        # ``detail`` is the child's stderr already summarised and bounded (``media_child``): fit for a log.
        super().__init__(f"ffprobe failed: {detail}" if detail else code)
        self.code = code


def _probe_slots() -> asyncio.Semaphore:
    global _slots, _slots_loop
    loop = asyncio.get_running_loop()
    if _slots is None or _slots_loop is not loop:
        _slots, _slots_loop = asyncio.Semaphore(PROBE_SLOTS), loop
    return _slots


def _file_key(path: Path) -> tuple:
    info = os.stat(path)
    return (str(path), info.st_size, info.st_mtime_ns)


async def _share(key: tuple, work):
    """Run ``work()`` once for everybody who asks for ``key`` while it runs.

    The shared task belongs to nobody in particular: a caller that is cancelled
    leaves it running for the others, and it ends by its own limits.
    """
    task = _shared.get(key)
    if task is None or task.done():
        task = asyncio.ensure_future(work())
        _shared[key] = task
        task.add_done_callback(lambda done, key=key: _shared.pop(key, None) if _shared.get(key) is done else None)
    return await asyncio.shield(task)


def parse_progress_seconds(line: str) -> float | None:
    """Seconds of output written so far, from one line of ffmpeg's ``-progress`` stream."""
    name, _, value = line.partition("=")
    if name not in ("out_time_us", "out_time_ms"):  # both are microseconds, despite the second one's name
        return None
    try:
        seconds = int(value) / 1_000_000
    except ValueError:
        return None
    return seconds if seconds >= 0 else None


def ffprobe_beside(ffmpeg: str) -> str:
    """ffprobe next to this ffmpeg: the FILE's name is changed, never a directory's.

    A plain replace over the whole path also rewrote a folder such as
    ``ffmpeg-8.1-full_build/bin/`` and pointed at a program that does not exist.
    """
    head, sep, name = ffmpeg.replace(chr(92), "/").rpartition("/")
    probe = name.replace("ffmpeg", "ffprobe")
    return f"{ffmpeg[: len(head)]}{ffmpeg[len(head)] if sep else ''}{probe}" if sep else probe


class TimelapseProcessor:
    """Probes one video file and builds the editor's command for it."""

    def __init__(self, input_path: Path):
        self.input_path = input_path
        self.ffmpeg = get_ffmpeg_path()
        if not self.ffmpeg:
            raise RuntimeError("FFmpeg not found")
        self.ffprobe = ffprobe_beside(self.ffmpeg)

    async def get_info(self) -> dict:
        """Video metadata. Bounded, and shared with whoever asks about the same file meanwhile."""
        key = ("info", *await asyncio.to_thread(_file_key, self.input_path))
        return await _share(key, self._probe)

    async def _probe(self) -> dict:
        cmd = [
            self.ffprobe,
            "-v",
            "quiet",
            "-print_format",
            "json",
            "-show_format",
            "-show_streams",
            str(self.input_path),
        ]
        async with _probe_slots():
            result = await media_child.run(
                cmd, name="timelapse-probe", hard_seconds=PROBE_SECONDS, stdout_limit=_PROBE_BYTES
            )
        if not result.ok:
            raise ProbeFailed(
                "probe_timeout" if result.status == media_child.TIMEOUT else "probe_failed", result.reason
            )
        try:
            data = json.loads(result.stdout.decode("utf-8", "replace"))
        except ValueError as exc:
            raise ProbeFailed("probe_failed") from exc
        if not isinstance(data, dict):
            raise ProbeFailed("probe_failed")
        streams = [s for s in data.get("streams") or [] if isinstance(s, dict)]
        video_stream = next((s for s in streams if s.get("codec_type") == "video"), {})
        audio_stream = next((s for s in streams if s.get("codec_type") == "audio"), None)

        # Parse frame rate (can be "30/1" or "29.97")
        fps = 30.0
        r_frame_rate = str(video_stream.get("r_frame_rate", "30/1"))
        try:
            if "/" in r_frame_rate:
                num, den = r_frame_rate.split("/")
                fps = float(num) / float(den)
            else:
                fps = float(r_frame_rate)
        except (ValueError, ZeroDivisionError):
            pass  # Keep default fps if frame rate string is unparseable
        container = data.get("format") if isinstance(data.get("format"), dict) else {}

        def number(value, default=0.0) -> float:
            try:
                number = float(value)
            except (TypeError, ValueError):
                return default
            return number if math.isfinite(number) and number >= 0 else default

        return {
            "duration": number(container.get("duration")),
            "width": int(number(video_stream.get("width"))),
            "height": int(number(video_stream.get("height"))),
            "fps": fps if math.isfinite(fps) and fps > 0 else 30.0,
            "codec": str(video_stream.get("codec_name", "unknown")),
            "file_size": int(number(container.get("size"))),
            "has_audio": audio_stream is not None,
            "has_video": bool(video_stream),
        }

    async def generate_thumbnails(self, count: int = 10, width: int = 160) -> list[tuple[float, bytes]]:
        """Evenly spaced frames, within one time budget for the whole strip. Shared like the probe."""
        key = ("thumbnails", count, width, *await asyncio.to_thread(_file_key, self.input_path))
        return await _share(key, lambda: self._thumbnails(count, width))

    async def _thumbnails(self, count: int, width: int) -> list[tuple[float, bytes]]:
        info = await self.get_info()
        duration = info["duration"]
        if duration <= 0:
            return []
        interval = duration / max(count, 1)
        thumbnails: list[tuple[float, bytes]] = []
        deadline = time.monotonic() + THUMBNAILS_SECONDS
        async with _probe_slots():
            for i in range(count):
                left = deadline - time.monotonic()
                if left <= 0:
                    break  # the strip is shorter; the editor works without the rest
                timestamp = i * interval
                cmd = [
                    self.ffmpeg,
                    "-v",
                    "error",
                    "-ss",
                    str(timestamp),
                    "-i",
                    str(self.input_path),
                    "-vframes",
                    "1",
                    "-vf",
                    f"scale={int(width)}:-1",
                    "-q:v",
                    "5",
                    "-f",
                    "image2pipe",
                    "-vcodec",
                    "mjpeg",
                    "pipe:1",
                ]
                result = await media_child.run(
                    cmd, name="timelapse-thumbnail", hard_seconds=left, stdout_limit=_THUMBNAIL_BYTES
                )
                if result.status in (media_child.TIMEOUT, media_child.UNREAPED):
                    break
                if result.ok and result.stdout:
                    thumbnails.append((timestamp, result.stdout))
        return thumbnails

    def edit_command(
        self,
        output_path: Path,
        *,
        trim_start: float = 0,
        trim_end: float | None = None,
        speed: float = 1.0,
        audio_path: Path | None = None,
        audio_volume: float = 1.0,
        mute: bool = False,
        source_has_audio: bool = True,
    ) -> list[str]:
        """The ffmpeg command for one edit. Progress goes to stdout (``-progress``), never the output file.

        ``mute`` gives an output with no audio track at all — neither the
        music nor the source's own sound. The music is looped and the output
        ends with the video, so a short track neither cuts the video nor leaves
        it silent half way (the dialog's preview loops it the same way). The
        source's resolution is kept (owner decision OD-6); the encoder is held to
        :data:`ENCODER_THREADS` threads and a short lookahead instead.
        """
        cmd = [self.ffmpeg, "-y", "-nostdin", "-v", "error", "-progress", "pipe:1", "-nostats"]

        # Seek and cut on the INPUT side: as an output option ``-t`` would also cut a slowed-down
        # video back to the length it had before the speed change.
        if trim_start > 0:
            cmd.extend(["-ss", str(trim_start)])
        if trim_end is not None and trim_end > trim_start:
            cmd.extend(["-t", str(trim_end - trim_start)])
        cmd.extend(["-i", str(self.input_path)])

        music = audio_path is not None and not mute
        if music:
            cmd.extend(["-stream_loop", "-1", "-i", str(audio_path)])

        # setpts changes video speed: PTS/speed = faster
        video_filter = f"setpts={1.0 / speed}*PTS" if speed != 1.0 else ""

        if music:
            # filter_complex for the overlay (cannot be mixed with -vf/-af)
            filter_parts = []
            if video_filter:
                filter_parts.append(f"[0:v]{video_filter}[v]")
                video_out = "[v]"
            else:
                video_out = "0:v"
            filter_parts.append(f"[1:a]volume={audio_volume}[a]")
            cmd.extend(["-filter_complex", ";".join(filter_parts)])
            cmd.extend(["-map", video_out, "-map", "[a]"])
            cmd.extend(["-shortest"])
        else:
            if video_filter:
                cmd.extend(["-vf", video_filter])
            if mute or not source_has_audio:
                cmd.extend(["-an"])
            elif speed != 1.0:
                atempo_chain = self._build_atempo_chain(speed)
                if atempo_chain:
                    cmd.extend(["-af", atempo_chain])

        cmd.extend(
            [
                "-c:v",
                "libx264",
                "-preset",
                "fast",
                "-crf",
                "23",
                "-threads",
                str(ENCODER_THREADS),
                "-x264-params",
                f"rc-lookahead={ENCODER_LOOKAHEAD}",
            ]
        )
        if music or not (mute or not source_has_audio):
            cmd.extend(["-c:a", "aac", "-b:a", "128k"])
        cmd.extend(["-movflags", "+faststart", "-f", "mp4", str(output_path)])
        return cmd

    def _build_atempo_chain(self, speed: float) -> str:
        """Build atempo filter chain.

        atempo filter only supports values between 0.5 and 2.0,
        so we chain multiple filters for extreme speeds.
        """
        if speed == 1.0:
            return ""

        filters = []
        remaining_speed = speed

        # Handle speeds > 2.0 by chaining atempo=2.0
        while remaining_speed > 2.0:
            filters.append("atempo=2.0")
            remaining_speed /= 2.0

        # Handle speeds < 0.5 by chaining atempo=0.5
        while remaining_speed < 0.5:
            filters.append("atempo=0.5")
            remaining_speed *= 2.0

        # Add final atempo for remaining adjustment
        # After the while loops above, remaining_speed is guaranteed to be in [0.5, 2.0]
        if remaining_speed != 1.0:
            filters.append(f"atempo={remaining_speed:.4f}")

        return ",".join(filters)
