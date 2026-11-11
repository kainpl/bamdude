"""Bounded HTTP snapshot bodies and image decoding inside the camera worker.

Timeout/cancellation stops waiting, not a native codec thread. Admission stays
occupied until that thread actually finishes. Hard stopping the existing worker
is the recovery boundary; a healthy heartbeat does not automatically restart a
wedged codec. This module never opens a camera connection.
"""

import asyncio
import math
import threading
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
_DECODER_SLOTS = threading.BoundedSemaphore(2)
_DECODER_FUTURES: set[Future] = set()
_DECODER_LOCK = threading.Lock()


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


async def read_snapshot_body(response: aiohttp.ClientResponse) -> bytes | None:
    """Read at most 10 MiB, accepting only supported image MIME/magic.

    Missing/octet-stream MIME is compatible with simple camera firmwares, but
    still requires supported magic and subsequent real image decoding.
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
    try:
        with Image.open(BytesIO(data), formats=_FORMATS) as image:
            width, height = image.size
            if not _dimensions_allowed(width, height) or getattr(image, "n_frames", 1) != 1:
                return None
            image.verify()
        # verify() invalidates the parser; reopen to force the actual decode.
        with Image.open(BytesIO(data), formats=_FORMATS) as image:
            image.load()
            if image.format == "JPEG":
                return data
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
    """Return a verified/decoded JPEG, or None; never queue a third decoder.

    The concurrent future and slot survive caller cancellation/timeout. Only
    actual completion releases capacity; no thread-kill guarantee is implied.
    """
    if (
        not _input_allowed(data)
        or not math.isfinite(timeout)
        or timeout <= 0
        or not _DECODER_SLOTS.acquire(blocking=False)
    ):
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
        return await asyncio.wait_for(asyncio.shield(asyncio.wrap_future(future)), timeout=timeout)
    except TimeoutError:
        return None
