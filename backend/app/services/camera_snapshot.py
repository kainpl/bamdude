"""Bounded HTTP snapshot bodies and image decoding inside the camera worker.

Timeout/cancellation stops waiting, not a native codec thread. Admission stays
occupied until that thread actually finishes. Hard stopping the existing worker
is the recovery boundary; a healthy heartbeat does not automatically restart a
wedged codec. This module never opens a camera connection.
"""

import asyncio
import math
import re
import threading
from collections import deque
from concurrent.futures import Future, ThreadPoolExecutor
from io import BytesIO

import aiohttp
from PIL import Image

MAX_SNAPSHOT_BYTES = 10 * 1024 * 1024
MAX_SNAPSHOT_EDGE = 8192
MAX_SNAPSHOT_PIXELS = 16_000_000
_CHUNK_SIZE = 64 * 1024
_FORMATS = ("JPEG", "PNG", "WEBP", "BMP")
_MIME_TYPES = frozenset({"image/jpeg", "image/jpg", "image/png", "image/webp", "image/bmp", "image/x-ms-bmp"})
_DECODER_EXECUTOR = ThreadPoolExecutor(max_workers=2, thread_name_prefix="camera-snapshot")
_DECODER_FUTURES: set[Future] = set()
_DECODER_LOCK = threading.Lock()
_JPEG_MARKER = re.compile(b"\xff+([^\xff])")
_JPEG_SOF = frozenset({0xC0, 0xC1, 0xC2, 0xC9, 0xCA})


class SnapshotAdmission:
    """FIFO deadline admission on the worker loop; release is thread-safe.

    Tickets hold no body/executor work before admission. Polling avoids a
    cross-thread event/closed-loop handoff; native completion only releases
    the semaphore. No native job is queued while its two slots are occupied.
    """

    def __init__(self, capacity: int):
        self._semaphore = threading.BoundedSemaphore(capacity)
        self._waiters = deque()

    async def acquire(self, deadline: float) -> bool:
        loop = asyncio.get_running_loop()
        ticket = object()
        self._waiters.append(ticket)
        try:
            while (remaining := deadline - loop.time()) > 0:
                if self._waiters[0] is ticket and self._semaphore.acquire(blocking=False):
                    return True
                await asyncio.sleep(min(0.005, remaining))
            return False
        finally:
            self._waiters.remove(ticket)

    def release(self) -> None:
        self._semaphore.release()


_DECODER_SLOTS = SnapshotAdmission(2)


def _image_format(data: bytes | bytearray) -> str | None:
    if data.startswith(b"\xff\xd8\xff"):
        return "JPEG"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "PNG"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "WEBP"
    if data.startswith(b"BM"):
        return "BMP"
    return None


def _dimensions_allowed(width: int, height: int) -> bool:
    return 0 < width <= MAX_SNAPSHOT_EDGE and 0 < height <= MAX_SNAPSHOT_EDGE and width * height <= MAX_SNAPSHOT_PIXELS


def _webp_dimensions(data: bytes) -> tuple[int, int] | None:
    """Read fixed WebP headers before Pillow's native canvas allocation."""
    chunk = data[12:16]
    if chunk == b"VP8X" and len(data) >= 30:
        if data[20] & 2:  # animated extended WebP is not a static snapshot
            return None
        return 1 + int.from_bytes(data[24:27], "little"), 1 + int.from_bytes(data[27:30], "little")
    if chunk == b"VP8L" and len(data) >= 25 and data[20] == 0x2F:
        bits = int.from_bytes(data[21:25], "little")
        return 1 + (bits & 0x3FFF), 1 + ((bits >> 14) & 0x3FFF)
    if chunk == b"VP8 " and len(data) >= 30 and data[23:26] == b"\x9d\x01\x2a":
        return int.from_bytes(data[26:28], "little") & 0x3FFF, int.from_bytes(data[28:30], "little") & 0x3FFF
    return None


def _input_allowed(data: bytes) -> bool:
    if not data or len(data) > MAX_SNAPSHOT_BYTES:
        return False
    fmt = _image_format(data)
    if fmt == "JPEG":
        return data.endswith(b"\xff\xd9")
    if fmt == "WEBP":
        dimensions = _webp_dimensions(data)
        return dimensions is not None and _dimensions_allowed(*dimensions)
    return fmt is not None


def _checked_jpeg(data: bytes) -> bytes | None:
    """Bounded first-scan header check, never native decode or entropy proof.

    Skip metadata by segment length, with at most 256 header markers. Check
    the frame dimensions/components and first scan before preserving bytes.
    Later progressive scans and entropy/MCU completeness are not validated.
    """
    if not _input_allowed(data) or _image_format(data) != "JPEG":
        return None
    offset, limit = 2, len(data) - 2  # terminal EOI was checked above
    components, frame_marker = None, None
    for _ in range(256):
        match = _JPEG_MARKER.match(data, offset)
        if match is None:
            return None
        marker, offset = match[1][0], match.end()
        if marker in {0, 0xD8, 0xD9} or 0xD0 <= marker <= 0xD7:
            return None
        if marker == 1:  # TEM is a standalone marker
            continue
        if offset + 2 > limit:
            return None
        length = int.from_bytes(data[offset : offset + 2], "big")
        end, payload = offset + length, offset + 2
        if length < 2 or end > limit:
            return None
        if marker in _JPEG_SOF:
            if components is not None or length < 8:
                return None
            height = int.from_bytes(data[payload + 1 : payload + 3], "big")
            width = int.from_bytes(data[payload + 3 : payload + 5], "big")
            count = data[payload + 5]
            if (
                data[payload] not in {8, 12}
                or (marker == 0xC0 and data[payload] != 8)
                or not _dimensions_allowed(width, height)
                or not 1 <= count <= 4
                or length != 8 + 3 * count
            ):
                return None
            components = set()
            frame_marker = marker
            for index in range(count):
                ident, sampling, table = data[payload + 6 + 3 * index : payload + 9 + 3 * index]
                if ident in components or not (1 <= sampling >> 4 <= 4 and 1 <= sampling & 15 <= 4) or table > 3:
                    return None
                components.add(ident)
        elif marker == 0xDA:
            if not components or length < 6:
                return None
            count, scanned = data[payload], set()
            if not 1 <= count <= len(components) or length != 6 + 2 * count or end >= limit:
                return None
            for index in range(count):
                ident, table = data[payload + 1 + 2 * index : payload + 3 + 2 * index]
                if ident not in components or ident in scanned or table >> 4 > 3 or table & 15 > 3:
                    return None
                scanned.add(ident)
            start, stop, successive = data[end - 3 : end]
            if not 0 <= start <= stop <= 63 or successive >> 4 > 13 or successive & 15 > 13:
                return None
            if frame_marker in {0xC0, 0xC1, 0xC9} and (start, stop, successive) != (0, 63, 0):
                return None
            if frame_marker in {0xC2, 0xCA} and ((start == 0 and stop != 0) or (start > 0 and count != 1)):
                return None
            return data
        offset = end
    return None


async def read_snapshot_body(response: aiohttp.ClientResponse) -> bytes | None:
    """Read at most 10 MiB, accepting only supported image MIME/magic.

    Missing/octet-stream MIME is compatible with simple camera firmwares, but
    still requires supported magic and subsequent bounded image checks.
    The caller owns HTTP status, redirects, credentials and the response scope.
    """
    encoding = response.headers.get("Content-Encoding", "").strip().lower()
    if encoding and encoding != "identity":
        return None
    mime = response.headers.get("Content-Type", "").partition(";")[0].strip().lower()
    if mime and mime not in _MIME_TYPES and mime != "application/octet-stream":
        return None
    raw_length = response.headers.get("Content-Length")
    if raw_length is not None:
        try:
            length = int(raw_length)
        except (TypeError, ValueError):
            return None
        if length < 0 or length > MAX_SNAPSHOT_BYTES:
            return None
    body = bytearray()
    async for chunk in response.content.iter_chunked(_CHUNK_SIZE):
        if len(body) + len(chunk) > MAX_SNAPSHOT_BYTES:
            return None
        body.extend(chunk)
        if len(body) >= 12 and not _image_format(body):
            return None
    return bytes(body) if body and _image_format(body) else None


class _BoundedOutput(BytesIO):
    def write(self, data: bytes) -> int:
        if self.tell() + len(data) > MAX_SNAPSHOT_BYTES:
            raise ValueError("snapshot output exceeds byte limit")
        return super().write(data)


def _decode_snapshot(data: bytes) -> bytes | None:
    """Synchronous codec implementation; production calls use decode_snapshot."""
    if not _input_allowed(data):
        return None
    if _image_format(data) == "JPEG":
        return _checked_jpeg(data)
    try:
        with Image.open(BytesIO(data), formats=_FORMATS) as image:
            width, height = image.size
            if not _dimensions_allowed(width, height) or getattr(image, "n_frames", 1) != 1:
                return None
            image.verify()
        # verify() invalidates the parser; reopen to force the actual decode.
        with Image.open(BytesIO(data), formats=_FORMATS) as image:
            image.load()
            with image.convert("RGB") as rgb, _BoundedOutput() as output:
                rgb.save(output, format="JPEG", quality=85)
                return output.getvalue()
    except Exception:
        # Driver/codec messages may contain untrusted image metadata.
        return None


def _decoder_finished(future: Future) -> None:
    with _DECODER_LOCK:
        _DECODER_FUTURES.discard(future)
    _DECODER_SLOTS.release()


async def decode_snapshot(data: bytes, timeout: float) -> bytes | None:
    """Check JPEG headers, or wait within budget for an owned native decoder.

    The concurrent future and slot survive caller cancellation/timeout. Only
    actual completion releases capacity; no thread-kill guarantee is implied.
    """
    if not _input_allowed(data) or not math.isfinite(timeout) or timeout <= 0:
        return None
    if _image_format(data) == "JPEG":
        return _checked_jpeg(data)
    deadline = asyncio.get_running_loop().time() + timeout
    if not await _DECODER_SLOTS.acquire(deadline):
        return None
    if asyncio.get_running_loop().time() >= deadline:
        _DECODER_SLOTS.release()
        return None
    try:
        future = _DECODER_EXECUTOR.submit(_decode_snapshot, data)
    except Exception:
        _DECODER_SLOTS.release()
        return None
    with _DECODER_LOCK:
        _DECODER_FUTURES.add(future)
    future.add_done_callback(_decoder_finished)
    try:
        remaining = max(0, deadline - asyncio.get_running_loop().time())
        return await asyncio.wait_for(asyncio.shield(asyncio.wrap_future(future)), timeout=remaining)
    except TimeoutError:
        return None
