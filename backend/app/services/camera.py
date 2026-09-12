"""Camera capture service for Bambu Lab printers.

Supports two camera protocols:
- RTSP: Used by X1, X1C, X1E, X2D, H2C, H2D, H2DPRO, H2S, P2S (port 322)
- Chamber Image: Used by A1, A1MINI, P1P, P1S (port 6000, custom binary protocol)
"""

import asyncio
import functools
import logging
import shutil
import ssl
import struct
import subprocess
import time
import traceback
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal

from backend.app.services.camera_cleanup import CameraAttempt
from backend.app.services.camera_tls import (
    close_tls_proxy as close_tls_proxy,
    create_tls_proxy as create_tls_proxy,
    rewrite_rtsp_request_url as rewrite_rtsp_request_url,
)
from backend.app.utils.ffmpeg_output import NO_FFMPEG_OUTPUT, summarize_ffmpeg_stderr

logger = logging.getLogger(__name__)

# In-flight one-shot captures, keyed by printer IP (#2705).
#
# Keyed by IP rather than printer_id because IP is what the firmware's
# one-connection limit applies to: two printer rows pointing at the same address
# still share one camera. (This function never sees a printer_id anyway.) The key
# deliberately excludes the timeout — callers disagree about it, from 10 s to
# 30 s, and including it would mean they never coalesce, which is exactly the
# Obico-vs-snapshot pair from the report.
_inflight_captures: dict[str, "asyncio.Task[CameraCaptureResult]"] = {}


@dataclass(frozen=True)
class CameraCaptureResult:
    """One-shot frame plus how this caller obtained it.

    ``fresh`` means this task opened the capture path. ``coalesced`` means the
    caller deliberately joined a capture already running for the same printer,
    which preserves the printer's single-camera-connection limit. A missing
    source means no usable frame arrived.
    """

    frame: bytes | None
    source: Literal["fresh", "coalesced"] | None


def capture_in_flight(ip_address: str) -> bool:
    """True iff a one-shot capture for this IP is running right now.

    Most callers should ignore this and use
    :func:`capture_camera_frame_with_provenance` when the distinction matters,
    or :func:`capture_camera_frame_bytes` when it does not. A pre-call check is
    only a momentary observation: the leader can finish before the caller joins.
    """
    task = _inflight_captures.get(ip_address)
    return task is not None and not task.done()


def _discard_inflight_capture(ip_address: str, task: "asyncio.Task") -> None:
    """Done-callback: drop the finished task from the in-flight registry.

    Guarded on identity so a slow task that finishes after a newer capture has
    registered cannot evict its successor.

    Also retrieves the exception, if any. The leader normally awaits the task and
    would surface it, but a leader whose own caller was cancelled leaves nobody
    to collect it — and an unretrieved task exception surfaces later as an
    asyncio warning with a traceback from nowhere.
    """
    if _inflight_captures.get(ip_address) is task:
        del _inflight_captures[ip_address]
    if not task.cancelled() and task.exception() is not None:
        logger.debug(
            "In-flight camera capture for %s ended in an exception [capture_id=%s]", ip_address, task.get_name()
        )


# JPEG markers
JPEG_START = b"\xff\xd8"
JPEG_END = b"\xff\xd9"

# Cache the ffmpeg path after first lookup
_ffmpeg_path: str | None = None

# Cached result of rtsp_socket_timeout_flag(); see that function for context.
_rtsp_socket_timeout_flag: str | None = None

# PIDs of ffmpeg processes spawned for one-shot frame capture (finish photos,
# timelapse seed frames, future Obico detection). The cleanup task in
# routes/camera.py consults this set and skips these PIDs so a short-lived
# snapshot running in parallel with the cleanup tick can't get SIGKILL'd by
# the /proc-scan orphan sweep (#979 upstream 62950e37).
_active_capture_pids: set[int] = set()


def get_ffmpeg_path() -> str | None:
    """Find the ffmpeg executable path.

    Uses shutil.which first, then checks common installation locations
    for systems where PATH may be limited (e.g., systemd services).
    """
    global _ffmpeg_path

    if _ffmpeg_path is not None:
        return _ffmpeg_path

    # Explicit override wins — lets an operator point straight at the binary when
    # it isn't on the service's PATH (e.g. a fresh Windows winget install whose
    # PATH change hasn't propagated to the running process).
    from backend.app.core.config import settings

    configured = (settings.ffmpeg_path or "").strip()
    if configured and Path(configured).is_file():
        _ffmpeg_path = configured
        logger.info("Using configured ffmpeg path (FFMPEG_PATH): %s", configured)
        return configured
    if configured:
        logger.warning("FFMPEG_PATH is set to %r but no file exists there; falling back to PATH search", configured)

    # Try PATH first
    ffmpeg_path = shutil.which("ffmpeg")

    # If not found via PATH, check common installation locations
    if ffmpeg_path is None:
        common_paths = [
            "/usr/bin/ffmpeg",
            "/usr/local/bin/ffmpeg",
            "/opt/homebrew/bin/ffmpeg",  # macOS Homebrew
            "/snap/bin/ffmpeg",  # Ubuntu Snap
            "C:\\ffmpeg\\bin\\ffmpeg.exe",  # Windows common
        ]
        for path in common_paths:
            if Path(path).exists():
                ffmpeg_path = path
                break

    _ffmpeg_path = ffmpeg_path
    if ffmpeg_path:
        logger.info("Found ffmpeg at: %s", ffmpeg_path)
    else:
        logger.warning("ffmpeg not found in PATH or common locations")

    return ffmpeg_path


def rtsp_socket_timeout_flag() -> str:
    """Return the ffmpeg argv flag (without the leading dash) that sets the
    RTSP demuxer's client-side TCP socket I/O timeout, in microseconds.

    ffmpeg has shipped three different option arrangements for this over
    time, and BamDude supports the full range:

    - **Modern ffmpeg (5.x / 6.x / 7.x)** — Debian 13, Ubuntu 24.04, current
      Homebrew, etc. ``-timeout`` is the socket I/O timeout (microseconds);
      ``-stimeout`` was REMOVED.
    - **Transitional ffmpeg (~late-4.x, some 5.x builds)** — Ubuntu 22.04's
      shipped version is one of these. ``-timeout`` was deprecated and
      *repurposed* to mean the RTSP listen-mode incoming-connection
      timeout — and any non-zero value implies ``-listen``, which makes
      ffmpeg bind the localhost proxy port and fail with EADDRINUSE
      (#1504). ``-stimeout`` was the replacement socket I/O timeout in
      that window.
    - **Old ffmpeg (early 4.x and earlier)** — ``-timeout`` is socket I/O
      timeout (the original meaning, before the deprecation churn).

    We probe ``-h demuxer=rtsp`` once and cache: if ``-stimeout`` is
    advertised, prefer it (covers the transitional window and stays
    correct on the older builds that still accept it as an alias); else
    fall back to ``-timeout`` (correct on modern and pre-deprecation
    ffmpeg). The result is cached for the process lifetime — ffmpeg
    isn't going to swap mid-run.

    Returns the option name without the leading dash, e.g. ``"timeout"``
    or ``"stimeout"``. Callers must prepend ``-`` themselves so a string
    formatting bug can't pass an empty flag.
    """
    global _rtsp_socket_timeout_flag

    if _rtsp_socket_timeout_flag is not None:
        return _rtsp_socket_timeout_flag

    ffmpeg = get_ffmpeg_path()
    chosen = "timeout"  # safe default for modern ffmpeg
    if ffmpeg:
        try:
            result = subprocess.run(
                [ffmpeg, "-hide_banner", "-h", "demuxer=rtsp"],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            help_text = (result.stdout or "") + (result.stderr or "")
            # Help lines list each option as `-<name> ` (trailing space) — match
            # that exact form so we don't accidentally hit a substring elsewhere.
            if "-stimeout " in help_text:
                chosen = "stimeout"
        except (OSError, subprocess.SubprocessError) as exc:
            # If probing fails, keep the modern-ffmpeg default. Worst case
            # is the EADDRINUSE regression returns for transitional-ffmpeg
            # users — same as before this function existed.
            logger.warning("Could not probe ffmpeg RTSP timeout flag, defaulting to -timeout: %s", exc)

    _rtsp_socket_timeout_flag = chosen
    logger.info("RTSP socket I/O timeout flag: -%s", chosen)
    return chosen


def supports_rtsp(model: str | None) -> bool:
    """Check if printer model supports RTSP camera streaming.

    RTSP supported: X1, X1C, X1E, X2D, H2C, H2D, H2DPRO, H2S, P2S
    Chamber image only: A1, A1MINI, P1P, P1S

    Note: Model can be either display name (e.g., "P2S") or internal code (e.g., "N7").
    Internal codes from MQTT/SSDP:
      - BL-P001: X1/X1C
      - C13: X1E
      - N6: X2D
      - O1D: H2D
      - O1C, O1C2: H2C
      - O1S: H2S
      - O1E, O2D: H2D Pro
      - N7: P2S
    """
    if model:
        model_upper = model.upper()
        # Display names: X1, X1C, X1E, X2D, H2C, H2D, H2DPRO, H2S, P2S
        if model_upper.startswith(("X1", "X2", "H2", "P2")):
            return True
        # Internal codes for RTSP models
        if model_upper in ("BL-P001", "C13", "N6", "O1D", "O1C", "O1C2", "O1S", "O1E", "O2D", "N7"):
            return True
    # A1/P1 and unknown models use chamber image protocol
    return False


def get_camera_port(model: str | None) -> int:
    """Get the camera port based on printer model.

    X1/X2/H2/P2 series use RTSP on port 322.
    A1/P1 series use chamber image protocol on port 6000.
    """
    if supports_rtsp(model):
        return 322
    return 6000


def is_chamber_image_model(model: str | None) -> bool:
    """Check if printer uses chamber image protocol instead of RTSP.

    A1, A1MINI, P1P, P1S use the chamber image protocol on port 6000.
    """
    return not supports_rtsp(model)


def _create_chamber_auth_payload(access_code: str) -> bytes:
    """Create the 80-byte authentication payload for chamber image protocol.

    Format:
    - Bytes 0-3: 0x40 0x00 0x00 0x00 (magic)
    - Bytes 4-7: 0x00 0x30 0x00 0x00 (command)
    - Bytes 8-15: zeros (padding)
    - Bytes 16-47: username "bblp" (32 bytes, null-padded)
    - Bytes 48-79: access code (32 bytes, null-padded)
    """
    username = b"bblp"
    access_code_bytes = access_code.encode("utf-8")

    # Build the 80-byte payload
    payload = struct.pack(
        "<II8s32s32s",
        0x40,  # Magic header
        0x3000,  # Command
        b"\x00" * 8,  # Padding
        username.ljust(32, b"\x00"),  # Username padded to 32 bytes
        access_code_bytes.ljust(32, b"\x00"),  # Access code padded to 32 bytes
    )
    return payload


def _create_ssl_context() -> ssl.SSLContext:
    """Create an SSL context for chamber image connection.

    Bambu printers use self-signed certificates, so we disable verification.
    """
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


async def read_chamber_image_frame(
    ip_address: str,
    access_code: str,
    timeout: float = 10.0,
) -> bytes | None:
    """Read a single JPEG frame from the chamber image protocol.

    This is used by A1/P1 printers which don't support RTSP.

    Args:
        ip_address: Printer IP address
        access_code: Printer access code
        timeout: Connection timeout in seconds

    Returns:
        JPEG image data or None if failed
    """
    port = 6000
    ssl_context = _create_ssl_context()

    try:
        # Connect with SSL
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(ip_address, port, ssl=ssl_context),
            timeout=timeout,
        )

        try:
            # Send authentication payload
            auth_payload = _create_chamber_auth_payload(access_code)
            writer.write(auth_payload)
            await writer.drain()

            # Read the 16-byte header
            header = await asyncio.wait_for(reader.readexactly(16), timeout=timeout)
            if len(header) < 16:
                logger.error("Chamber image: incomplete header received")
                return None

            # Parse payload size from header (little-endian uint32 at offset 0)
            payload_size = struct.unpack("<I", header[0:4])[0]

            if payload_size == 0 or payload_size > 10_000_000:  # Sanity check: max 10MB
                logger.error("Chamber image: invalid payload size %s", payload_size)
                return None

            # Read the JPEG data
            jpeg_data = await asyncio.wait_for(
                reader.readexactly(payload_size),
                timeout=timeout,
            )

            # Validate JPEG markers
            if not jpeg_data.startswith(JPEG_START):
                logger.error("Chamber image: data is not a valid JPEG (missing start marker)")
                return None

            if not jpeg_data.endswith(JPEG_END):
                logger.warning("Chamber image: JPEG missing end marker, may be truncated")

            logger.debug("Chamber image: received %s bytes", len(jpeg_data))
            return jpeg_data

        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass  # Socket already closed; cleanup is best-effort

    except TimeoutError:
        logger.error("Chamber image: connection timeout to %s:%s", ip_address, port)
        return None
    except ConnectionRefusedError:
        logger.error("Chamber image: connection refused by %s:%s", ip_address, port)
        return None
    except Exception as e:
        logger.exception("Chamber image: error connecting to %s:%s: %s", ip_address, port, e)
        return None


async def generate_chamber_image_stream(
    ip_address: str,
    access_code: str,
    fps: int = 5,
) -> asyncio.StreamReader | None:
    """Create a persistent connection for streaming chamber images.

    Returns a connected reader or None if connection failed.
    """
    port = 6000
    ssl_context = _create_ssl_context()

    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(ip_address, port, ssl=ssl_context),
            timeout=10.0,
        )

        # Send authentication payload
        auth_payload = _create_chamber_auth_payload(access_code)
        writer.write(auth_payload)
        await writer.drain()

        logger.info("Chamber image: connected to %s:%s", ip_address, port)
        return reader, writer

    except Exception as e:
        logger.error("Chamber image: failed to connect to %s:%s: %s", ip_address, port, e)
        return None


async def read_next_chamber_frame(reader: asyncio.StreamReader, timeout: float = 10.0) -> bytes | None:
    """Read the next JPEG frame from an established chamber image connection."""
    try:
        # Read the 16-byte header
        header = await asyncio.wait_for(reader.readexactly(16), timeout=timeout)

        # Parse payload size from header (little-endian uint32 at offset 0)
        payload_size = struct.unpack("<I", header[0:4])[0]

        if payload_size == 0 or payload_size > 10_000_000:
            logger.error("Chamber image: invalid payload size %s", payload_size)
            return None

        # Read the JPEG data
        jpeg_data = await asyncio.wait_for(
            reader.readexactly(payload_size),
            timeout=timeout,
        )

        return jpeg_data

    except asyncio.IncompleteReadError:
        logger.warning("Chamber image: connection closed by printer")
        return None
    except TimeoutError:
        logger.warning("Chamber image: read timeout")
        return None
    except Exception as e:
        logger.error("Chamber image: error reading frame: %s", e)
        return None


async def capture_camera_frame(
    ip_address: str,
    access_code: str,
    model: str | None,
    output_path: Path,
    timeout: int = 30,
) -> bool:
    """Capture a single frame from the printer's camera stream and save to disk.

    Uses capture_camera_frame_bytes() internally for protocol selection,
    then writes the result to the specified output path.

    Args:
        ip_address: Printer IP address
        access_code: Printer access code
        model: Printer model (X1, H2D, P1, A1, etc.)
        output_path: Path where to save the captured image
        timeout: Timeout in seconds for the capture operation

    Returns:
        True if capture was successful, False otherwise
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)

    jpeg_data = await capture_camera_frame_bytes(ip_address, access_code, model, timeout)
    if jpeg_data:
        try:
            with open(output_path, "wb") as f:
                f.write(jpeg_data)
            logger.info("Saved camera frame to: %s", output_path)
            return True
        except OSError as e:
            logger.error("Failed to write camera frame: %s", e)
            return False
    return False


async def capture_camera_frame_bytes(
    ip_address: str,
    access_code: str,
    model: str | None,
    timeout: int = 15,
) -> bytes | None:
    """Capture a single frame and return as JPEG bytes (no disk write).

    This compatibility wrapper deliberately hides whether the caller opened a
    socket or joined an already-running capture. Use
    :func:`capture_camera_frame_with_provenance` only where that distinction is
    part of the result, such as the operator-facing diagnostic.

    """
    return (await capture_camera_frame_with_provenance(ip_address, access_code, model, timeout)).frame


async def capture_camera_frame_with_provenance(
    ip_address: str,
    access_code: str,
    model: str | None,
    timeout: int = 15,
) -> CameraCaptureResult:
    """Capture a frame and report whether this caller opened the camera socket.

    Concurrent callers for the same printer **share one capture** (#2705): the
    first opens the connection, everyone arriving while it is in flight awaits
    the same result. Every consumer here wants "a recent frame" rather than "a
    frame captured at exactly my timestamp", so handing identical bytes to
    simultaneous callers is correct — and it is the only way to honour the
    firmware's one-connection limit without serialising captures behind a lock,
    which would merely turn a collision into a queue.

    The existing guards (``live_frame_for_capture`` and its ancestors, #1271 +
    #1348 + #2707) only stop a capturer competing with the fan-out
    **broadcaster**. With no viewer attached, every consumer correctly concludes
    it is not competing with a viewer — and then collides with the others. On
    the reporter's P2S an Obico poll and a snapshot opened two RTSP sockets
    207 ms apart and knocked over the camera wall's stream, which was then
    reaped for having received no frames.

    This **coalesces; it does not cache.** A call arriving after the previous
    capture finished always captures fresh. Two consumers of these frames —
    plate detection and the finish photo — decide things about a running print,
    and a stale frame there is worse than a slow one: the whole of #1397 was a
    finish photo taken seconds late showing the bed already lowered.

    Args:
        ip_address: Printer IP address
        access_code: Printer access code
        model: Printer model (X1, H2D, P1, A1, etc.)
        timeout: Timeout in seconds for this caller's own wait, including when
            it joins another caller's capture. The call sites disagree about the
            value — 10 s for plate detection, 20 s for Obico — and a follower
            must not silently inherit the leader's deadline in either direction.

    Returns:
        A frame and its source. ``frame`` is ``None`` when no usable image was
        received within the caller's deadline.
    """
    # A follower whose leader failed takes a turn of its own rather than
    # inheriting a failure it never had a chance to avoid — by then the leader
    # has finished, so there is no socket left to compete with. Bounded at two
    # rounds: if the capture we joined AND its replacement both failed, a third
    # connection will not help, and this caller has spent its patience.
    for _ in range(2):
        leader = _inflight_captures.get(ip_address)
        if leader is None or leader.done():
            break
        wait_started = time.monotonic()
        logger.debug("Waiting on in-flight camera capture for %s [capture_id=%s]", ip_address, leader.get_name())
        try:
            result = await asyncio.wait_for(asyncio.shield(leader), timeout=timeout)
        except TimeoutError:
            # shield() keeps the capture running for whoever else is still
            # waiting on it — giving up is this caller's decision alone.
            logger.warning(
                "Gave up waiting %ss on the in-flight camera capture for %s [capture_id=%s elapsed=%.3fs]",
                timeout,
                ip_address,
                leader.get_name(),
                time.monotonic() - wait_started,
            )
            return CameraCaptureResult(frame=None, source=None)
        except asyncio.CancelledError:
            # Distinguish "the capture I joined was cancelled" from "I was
            # cancelled". Only the former is ours to recover from.
            if not leader.cancelled():
                raise
            logger.info(
                "In-flight camera capture for %s was cancelled; capturing our own [capture_id=%s]",
                ip_address,
                leader.get_name(),
            )
            continue
        if result.frame is not None:
            logger.info(
                "Reusing in-flight camera capture for %s: %s bytes "
                "(no second connection opened) [capture_id=%s elapsed=%.3fs]",
                ip_address,
                len(result.frame),
                leader.get_name(),
                time.monotonic() - wait_started,
            )
            return CameraCaptureResult(frame=result.frame, source="coalesced")
        logger.info(
            "In-flight camera capture for %s failed; capturing our own [capture_id=%s]", ip_address, leader.get_name()
        )
    else:
        return CameraCaptureResult(frame=None, source=None)

    # The name is the attempt ID: followers already hold the task, so they can
    # log the same ID without a second registry or changing the capture API.
    task = asyncio.create_task(
        _capture_camera_frame_with_provenance_uncoalesced(ip_address, access_code, model, timeout),
        name=f"camera-capture-{uuid.uuid4().hex[:12]}",
    )
    _inflight_captures[ip_address] = task
    task.add_done_callback(functools.partial(_discard_inflight_capture, ip_address))
    # No wait_for here: this caller IS the capture, and the implementation
    # already enforces ``timeout`` internally where it can also kill the ffmpeg
    # process. A second deadline on top would abandon the subprocess instead.
    # shield() so a cancelled leader — a client navigating away mid-snapshot is
    # routine — does not take the capture down with it; the followers already
    # waiting on it still get their frame.
    return await asyncio.shield(task)


async def _capture_camera_frame_with_provenance_uncoalesced(
    ip_address: str,
    access_code: str,
    model: str | None,
    timeout: int,
) -> CameraCaptureResult:
    """Run the socket-owning capture and annotate a successful fresh frame."""
    frame = await _capture_camera_frame_bytes_uncoalesced(ip_address, access_code, model, timeout)
    return CameraCaptureResult(frame=frame, source="fresh" if frame is not None else None)


async def _capture_camera_frame_bytes_uncoalesced(
    ip_address: str,
    access_code: str,
    model: str | None,
    timeout: int = 15,
) -> bytes | None:
    """Open a connection and capture one frame. See :func:`capture_camera_frame_bytes`.

    Callers want that wrapper, not this: this opens a socket unconditionally,
    which is the collision #2705 is about.
    """
    started = time.monotonic()
    task = asyncio.current_task()
    capture_id = task.get_name() if task else "unknown"
    port = get_camera_port(model)
    protocol = "chamber" if is_chamber_image_model(model) else "rtsp"
    context = f"capture_id={capture_id} target={ip_address}:{port} model={model} protocol={protocol}"
    logger.info("Capturing camera frame bytes [%s timeout=%ss]", context, timeout)

    # Chamber image models: A1/P1 - returns bytes directly
    if is_chamber_image_model(model):
        frame = await read_chamber_image_frame(ip_address, access_code, timeout=float(timeout))
        logger.log(
            logging.INFO if frame else logging.WARNING,
            "Chamber camera frame capture %s [%s elapsed=%.3fs bytes=%s]",
            "succeeded" if frame else "failed",
            context,
            time.monotonic() - started,
            len(frame) if frame else 0,
        )
        return frame

    process = None
    try:
        async with CameraAttempt(context) as attempt:
            # RTSP models: X1/H2/P2 - use ffmpeg piping to stdout
            # TLS proxy avoids GnuTLS compatibility issues with some printer firmwares
            proxy_port, attempt.proxy = await create_tls_proxy(ip_address, port)
            context += f" proxy_port={proxy_port}"
            attempt.context = context
            camera_url = f"rtsp://bblp:{access_code}@127.0.0.1:{proxy_port}/streaming/live/1"

            ffmpeg = get_ffmpeg_path()
            if not ffmpeg:
                logger.error(
                    "ffmpeg not found for camera frame capture [%s elapsed=%.3fs]", context, time.monotonic() - started
                )
                return None

            cmd = [
                ffmpeg,
                "-y",
                "-rtsp_transport",
                "tcp",
                "-rtsp_flags",
                "prefer_tcp",
                "-i",
                camera_url,
                "-frames:v",
                "1",
                "-f",
                "image2pipe",
                "-vcodec",
                "mjpeg",
                "-q:v",
                "2",
                "-",
            ]

            process = attempt.process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            # Protect this short-lived capture from the orphan-ffmpeg cleanup sweep —
            # the /proc scan can otherwise SIGKILL us mid-snapshot (#979).
            _active_capture_pids.add(process.pid)

            try:
                stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
            except TimeoutError:
                logger.error(
                    "Camera frame bytes capture timed out after %ss [%s pid=%s elapsed=%.3fs]",
                    timeout,
                    context,
                    process.pid,
                    time.monotonic() - started,
                )
                return None

            if process.returncode == 0 and stdout and len(stdout) >= 100:
                logger.info(
                    "Successfully captured camera frame bytes: %s bytes [%s pid=%s elapsed=%.3fs]",
                    len(stdout),
                    context,
                    process.pid,
                    time.monotonic() - started,
                )
                return stdout
            else:
                stderr_text = summarize_ffmpeg_stderr(stderr) or NO_FFMPEG_OUTPUT
                logger.error(
                    "ffmpeg frame bytes capture failed (code %s) [%s pid=%s elapsed=%.3fs bytes=%s]: %s",
                    process.returncode,
                    context,
                    process.pid,
                    time.monotonic() - started,
                    len(stdout) if stdout else 0,
                    stderr_text,
                )
                return None

    except FileNotFoundError:
        logger.error(
            "ffmpeg not found for camera frame capture [%s elapsed=%.3fs]", context, time.monotonic() - started
        )
        return None
    except Exception as e:
        # logger.exception would append the original, unredacted exception even
        # if its message was masked. Subprocess errors can quote the whole argv.
        logger.error(
            "Camera frame bytes capture failed [%s elapsed=%.3fs exception=%s]: %s",
            context,
            time.monotonic() - started,
            type(e).__name__,
            summarize_ffmpeg_stderr(traceback.format_exc()) or NO_FFMPEG_OUTPUT,
        )
        return None
    finally:
        if process is not None:
            _active_capture_pids.discard(process.pid)


async def capture_finish_photo(
    printer_id: int,
    ip_address: str,
    access_code: str,
    model: str | None,
    archive_dir: Path,
    rotation: int = 0,
) -> str | None:
    """Capture a finish photo and save it to the archive's photos folder.

    Args:
        printer_id: ID of the printer
        ip_address: Printer IP address
        access_code: Printer access code
        model: Printer model
        archive_dir: Directory of the archive (where the 3MF is stored)
        rotation: The printer's ``camera_rotation``, applied to the saved file
            so this source agrees with every other finish-photo source (#2708).

    Returns:
        Filename of the captured photo, or None if capture failed
    """
    # Create photos subdirectory
    photos_dir = archive_dir / "photos"
    photos_dir.mkdir(parents=True, exist_ok=True)

    # Generate filename with timestamp
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"finish_{timestamp}_{uuid.uuid4().hex[:8]}.jpg"
    output_path = photos_dir / filename  # SEC-PATH-OK: filename is the server-generated finish_<timestamp>_<uuid8>.jpg

    success = await capture_camera_frame(
        ip_address=ip_address,
        access_code=access_code,
        model=model,
        output_path=output_path,
        timeout=30,
    )

    if success:
        await apply_camera_rotation_to_file(output_path, rotation, logger)
        logger.info("Finish photo saved: %s", filename)
        return filename
    else:
        logger.warning("Failed to capture finish photo for printer %s", printer_id)
        return None


async def test_camera_connection(
    ip_address: str,
    access_code: str,
    model: str | None,
) -> dict:
    """Test if the camera stream is accessible.

    Returns dict with success status and any error message.
    """
    import os
    import tempfile

    fd, tmp_name = tempfile.mkstemp(suffix=".jpg")
    os.close(fd)
    test_path = Path(tmp_name)
    test_path.chmod(0o600)

    try:
        success = await capture_camera_frame(
            ip_address=ip_address,
            access_code=access_code,
            model=model,
            output_path=test_path,
            timeout=15,
        )

        if success:
            return {"success": True, "message": "Camera connection successful"}
        else:
            return {
                "success": False,
                "error": (
                    "Failed to capture frame from camera. "
                    "Ensure the printer is powered on, camera is enabled, and Developer Mode is active. "
                    "If running in Docker, try 'network_mode: host' in docker-compose.yml."
                ),
            }
    finally:
        # Clean up test file
        if test_path.exists():
            test_path.unlink()


async def extract_video_last_frame(video_path: Path, output_path: Path) -> bool:
    """Extract the last frame of `video_path` as JPEG at `output_path`.

    Used to source finish photos from a Bambu timelapse. The Bambu firmware
    stops timelapse recording AFTER the toolhead parks but BEFORE the bed-drop
    end-gcode runs, so the last frame frames the finished print correctly.
    A live camera grab at ``gcode_state=FINISH`` captures the bed already
    lowered (#1397).

    Implementation: ``-update 1`` writes each decoded frame to the same
    output file (overwriting), so the file left on disk after ffmpeg
    finishes is the LAST frame. This works regardless of how short the
    video is — a small print's timelapse can be sub-second / sub-30 frames
    (one frame per layer-change capture), and the earlier ``-sseof -1.0``
    approach failed there because the seek went before the start of the
    file and ffmpeg silently returned frame 0 (empty bed at print start).
    Decoding every frame is fine: Bambu timelapses are short by
    construction (<1 minute even on hours-long prints).

    Returns False on missing ffmpeg, missing video, subprocess failure or
    timeout. Never raises.
    """
    ffmpeg = get_ffmpeg_path()
    if not ffmpeg:
        logger.warning("Cannot extract video last frame: ffmpeg not available")
        return False

    if not video_path.exists() or video_path.stat().st_size == 0:
        logger.warning("Cannot extract last frame: %s missing or empty", video_path)
        return False

    output_path.parent.mkdir(parents=True, exist_ok=True)

    cmd = [
        ffmpeg,
        "-y",
        "-i",
        str(video_path),
        "-q:v",
        "2",
        "-update",
        "1",
        str(output_path),
    ]

    process = None
    try:
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await asyncio.wait_for(process.communicate(), timeout=15.0)
        if process.returncode != 0:
            logger.warning(
                "ffmpeg failed extracting last frame from %s: %s",
                video_path,
                stderr.decode(errors="replace")[:500],
            )
            return False
        if not output_path.exists() or output_path.stat().st_size == 0:
            logger.warning("ffmpeg produced no output for %s", video_path)
            return False
        return True
    except TimeoutError:
        logger.warning("ffmpeg timed out extracting last frame from %s", video_path)
        if process is not None:
            try:
                process.kill()
                await process.wait()
            except ProcessLookupError:
                pass  # Already exited
        return False
    except OSError as e:
        logger.warning("ffmpeg subprocess error for %s: %s", video_path, e)
        return False


def apply_camera_rotation(image_data: bytes, rotation: int, logger: logging.Logger) -> bytes:
    """Apply a ``camera_rotation`` value (degrees clockwise) to a captured JPEG.

    Shared by every capture path that saves a still: notification snapshots,
    finish photos and layer-timelapse frames. It used to live inline in the
    snapshot path alone, which left finish photos and timelapse videos
    upside-down for anyone whose camera is mounted rotated — and made the
    timelapse look like the bug, since the snapshot of the same print was the
    right way up (upstream Bambuddy #2708).

    **Rotate exactly once.** Some sources arrive already rotated (the in-print
    frame bank is filled from the snapshot path), so the caller has to know
    which it is holding; the producer of a stored frame is the right place to
    decide, not each of its consumers.

    Never raises: a still that could not be rotated is better than no still.
    """
    if not rotation:
        return image_data

    try:
        from io import BytesIO

        from PIL import Image

        img = Image.open(BytesIO(image_data))
        # PIL rotates counter-clockwise, so negate for clockwise rotation.
        img = img.rotate(-rotation, expand=True)
        buf = BytesIO()
        img.save(buf, format="JPEG", quality=90)
        rotated = buf.getvalue()
        logger.info("Applied %d° camera rotation: %s → %s bytes", rotation, len(image_data), len(rotated))
        return rotated
    except Exception as e:
        logger.warning("Failed to apply camera rotation: %s", e)
        return image_data


async def apply_camera_rotation_to_file(path: Path, rotation: int, logger: logging.Logger) -> None:
    """Rotate a JPEG already written to disk, in place.

    For the finish photo recovered from the printer's own timelapse video: the
    still is extracted by ffmpeg straight to a file, so there are no bytes to
    rotate on the way past. The **video** is left alone deliberately — it is the
    printer's file, and rotating it would mean re-encoding it.
    """
    if not rotation:
        return
    try:
        data = await asyncio.to_thread(path.read_bytes)
        rotated = await asyncio.to_thread(apply_camera_rotation, data, rotation, logger)
        if rotated is not data:
            await asyncio.to_thread(path.write_bytes, rotated)
    except OSError as e:
        logger.warning("Failed to rotate %s: %s", path, e)
