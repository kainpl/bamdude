"""Real image parsing and bounded decoder ownership without camera/network I/O."""

import asyncio
import random
import struct
import threading
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from PIL import Image, ImageFile

from backend.app.services import camera_snapshot as snapshot


def image_bytes(fmt="JPEG", size=(32, 16)):
    with Image.new("RGB", size, (20, 120, 220)) as image, BytesIO() as output:
        image.save(output, format=fmt)
        return output.getvalue()


class Body:
    def __init__(self, chunks):
        self.chunks = chunks
        self.reads = 0
        self.requested_size = None

    async def iter_chunked(self, size):
        self.requested_size = size
        for chunk in self.chunks:
            self.reads += 1
            yield chunk


def response(chunks, mime="image/jpeg", length=None):
    headers = {} if mime is None else {"Content-Type": mime}
    if length is not None:
        headers["Content-Length"] = length
    return SimpleNamespace(headers=headers, content=Body(chunks))


@pytest.mark.asyncio
@pytest.mark.parametrize("fmt", ["JPEG", "PNG", "WEBP", "BMP"])
async def test_real_images_decode_to_jpeg_and_existing_jpeg_is_preserved(fmt):
    data = image_bytes(fmt)
    result = await snapshot.decode_snapshot(data, timeout=5)
    assert result and result.startswith(b"\xff\xd8\xff")
    with Image.open(BytesIO(result)) as decoded:
        decoded.verify()
    with Image.open(BytesIO(result)) as decoded:
        decoded.load()
        assert decoded.size == (32, 16)
    if fmt == "JPEG":
        assert result == data


@pytest.mark.asyncio
@pytest.mark.parametrize("data", [b"", b"<html>login</html>", b"<svg></svg>", b'{"error":"no"}', b"GIF89a"])
async def test_nonimage_rejected_before_decoder_admission(monkeypatch, data):
    submit = MagicMock(side_effect=AssertionError("must not submit"))
    monkeypatch.setattr(snapshot._DECODER_EXECUTOR, "submit", submit)
    assert await snapshot.decode_snapshot(data, timeout=1) is None
    submit.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("fmt", ["JPEG", "PNG", "WEBP", "BMP"])
async def test_truncated_real_images_are_not_returned_as_raw_fallback(fmt):
    data = image_bytes(fmt)
    assert await snapshot.decode_snapshot(data[: len(data) // 2], timeout=5) is None


@pytest.mark.asyncio
async def test_truncated_jpeg_with_valid_soi_is_really_decoded():
    data = image_bytes()
    assert await snapshot.decode_snapshot(data[:-2], timeout=5) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("suffix", [None, b"trailing bytes"])
async def test_jpeg_requires_terminal_eoi_before_parser_or_executor(monkeypatch, suffix):
    data = image_bytes()
    rejected = data[:-2] if suffix is None else data + suffix
    parser = MagicMock(side_effect=AssertionError("must reject before parser"))
    submit = MagicMock(side_effect=AssertionError("must reject before submission"))
    monkeypatch.setattr(Image, "open", parser)
    monkeypatch.setattr(snapshot._DECODER_EXECUTOR, "submit", submit)
    assert snapshot._decode_snapshot(rejected) is None
    assert await snapshot.decode_snapshot(rejected, timeout=1) is None
    parser.assert_not_called()
    submit.assert_not_called()


@pytest.mark.asyncio
async def test_jpeg_with_valid_headers_but_truncated_entropy_is_rejected():
    pixels = random.Random(7).randbytes(128 * 128 * 3)
    with Image.frombytes("RGB", (128, 128), pixels) as image, BytesIO() as output:
        image.save(output, format="JPEG")
        data = output.getvalue()
    sos = data.index(b"\xff\xda")
    entropy_start = sos + 2 + int.from_bytes(data[sos + 2 : sos + 4], "big")
    assert len(data) - entropy_start > 1000
    assert await snapshot.decode_snapshot(data, timeout=5) == data
    truncated = data[: entropy_start + (len(data) - entropy_start) // 2]
    assert await snapshot.decode_snapshot(truncated, timeout=5) is None


@pytest.mark.asyncio
async def test_oversize_input_and_invalid_timeout_do_not_start_decoder(monkeypatch):
    submit = MagicMock(side_effect=AssertionError("must not submit"))
    monkeypatch.setattr(snapshot._DECODER_EXECUTOR, "submit", submit)
    data = b"\xff\xd8\xff" + b"x" * snapshot.MAX_SNAPSHOT_BYTES
    assert await snapshot.decode_snapshot(data, timeout=1) is None
    for timeout in (0, -1, float("nan"), float("inf")):
        assert await snapshot.decode_snapshot(image_bytes(), timeout=timeout) is None
    submit.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("size", [(8193, 1), (4001, 4000)])
async def test_dimensions_rejected_before_verify_or_load(monkeypatch, size):
    data = bytearray(image_bytes("BMP"))
    data[18:26] = struct.pack("<ii", *size)
    ordinary = image_bytes("BMP", size=(2, 3))
    original_load = Image.Image.load
    original_verify = ImageFile.ImageFile.verify
    original_dimensions_allowed = snapshot._dimensions_allowed
    forbidden_calls = []
    checked_dimensions = []

    def guarded_load(image):
        if image.size == size:
            forbidden_calls.append("load")
            raise AssertionError("must reject candidate before load")
        return original_load(image)

    def guarded_verify(image):
        if image.size == size:
            forbidden_calls.append("verify")
            raise AssertionError("must reject candidate before verify")
        return original_verify(image)

    def checked_dimensions_allowed(width, height):
        checked_dimensions.append((width, height))
        return original_dimensions_allowed(width, height)

    # Cold Image.open may initialize Palm's 1x256 prototype with load().
    # Trap the candidate pixels, while allowing unrelated bootstrap/images.
    monkeypatch.setattr(Image.Image, "load", guarded_load)
    monkeypatch.setattr(ImageFile.ImageFile, "verify", guarded_verify)
    monkeypatch.setattr(snapshot, "_dimensions_allowed", checked_dimensions_allowed)
    assert await snapshot.decode_snapshot(bytes(data), timeout=5) is None
    assert size in checked_dimensions  # refusal reached the real size gate
    assert await snapshot.decode_snapshot(ordinary, timeout=5) is not None
    assert forbidden_calls == []


@pytest.mark.asyncio
async def test_animated_webp_is_not_a_static_snapshot():
    with Image.new("RGB", (8, 8), "red") as first, Image.new("RGB", (8, 8), "blue") as second, BytesIO() as out:
        first.save(out, format="WEBP", save_all=True, append_images=[second], duration=100, loop=0)
        assert await snapshot.decode_snapshot(out.getvalue(), timeout=5) is None


def webp_bytes(kind):
    options = {"lossy": {}, "lossless": {"lossless": True}, "extended": {"exif": b"snapshot-test"}}[kind]
    with Image.new("RGB", (32, 16), "red") as image, BytesIO() as output:
        image.save(output, format="WEBP", **options)
        return output.getvalue()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind,chunk", [("lossy", b"VP8 "), ("lossless", b"VP8L"), ("extended", b"VP8X")])
async def test_webp_fixed_dimension_headers_match_real_decode(kind, chunk):
    data = webp_bytes(kind)
    assert data[12:16] == chunk
    assert snapshot._webp_dimensions(data) == (32, 16)
    decoded = await snapshot.decode_snapshot(data, timeout=5)
    assert decoded
    with Image.open(BytesIO(decoded)) as image:
        image.load()
        assert image.size == (32, 16)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["lossy", "lossless", "extended"])
@pytest.mark.parametrize("limit,value", [("MAX_SNAPSHOT_PIXELS", 1), ("MAX_SNAPSHOT_EDGE", 1)])
async def test_webp_dimension_preflight_precedes_native_canvas_and_executor(monkeypatch, kind, limit, value):
    from PIL import _webp

    data = webp_bytes(kind)
    monkeypatch.setattr(snapshot, limit, value)
    native = MagicMock(side_effect=AssertionError("native canvas must not be allocated"))
    submit = MagicMock(side_effect=AssertionError("must not submit"))
    monkeypatch.setattr(_webp, "WebPAnimDecoder", native)
    monkeypatch.setattr(snapshot._DECODER_EXECUTOR, "submit", submit)
    assert snapshot._decode_snapshot(data) is None
    assert await snapshot.decode_snapshot(data, timeout=1) is None
    native.assert_not_called()
    submit.assert_not_called()


@pytest.mark.asyncio
async def test_encoded_output_rejected_while_writing_without_unbounded_buffer(monkeypatch):
    data = image_bytes("PNG")
    monkeypatch.setattr(snapshot, "MAX_SNAPSHOT_BYTES", len(data) + 10)
    assert await snapshot.decode_snapshot(data, timeout=5) is None
    with snapshot._BoundedOutput() as output:
        output.write(b"a" * snapshot.MAX_SNAPSHOT_BYTES)
        with pytest.raises(ValueError):
            output.write(b"b")
        assert len(output.getvalue()) == snapshot.MAX_SNAPSHOT_BYTES


@pytest.mark.asyncio
@pytest.mark.parametrize("mime", [None, "application/octet-stream", "image/jpeg; charset=binary", "IMAGE/JPEG"])
async def test_body_streams_valid_magic_with_explicit_generic_mime_compatibility(mime):
    data = image_bytes()
    reply = response([data[:2], data[2:9], data[9:]], mime)
    assert await snapshot.read_snapshot_body(reply) == data
    assert reply.content.reads == 3
    assert reply.content.requested_size == 64 * 1024


@pytest.mark.asyncio
@pytest.mark.parametrize("mime", ["text/html", "image/svg+xml", "application/json", "image/gif"])
async def test_unsupported_mime_is_rejected_without_reading_body(mime):
    reply = response([image_bytes()], mime)
    assert await snapshot.read_snapshot_body(reply) is None
    assert reply.content.reads == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("encoding", ["gzip", "deflate", "br", "gzip, identity"])
async def test_encoded_response_is_rejected_before_reading_or_inflation(encoding):
    reply = response([image_bytes()])
    reply.headers["Content-Encoding"] = encoding
    assert await snapshot.read_snapshot_body(reply) is None
    assert reply.content.reads == 0


@pytest.mark.asyncio
async def test_explicit_identity_encoding_is_supported():
    data = image_bytes()
    reply = response([data])
    reply.headers["Content-Encoding"] = " Identity "
    assert await snapshot.read_snapshot_body(reply) == data


@pytest.mark.asyncio
@pytest.mark.parametrize("length", [str(10 * 1024 * 1024 + 1), "-1", "malformed"])
async def test_bad_content_length_rejected_without_body_allocation(length):
    reply = response([image_bytes()], length=length)
    assert await snapshot.read_snapshot_body(reply) is None
    assert reply.content.reads == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("length", [None, "1"])
async def test_actual_stream_limit_applies_with_missing_or_lying_length(monkeypatch, length):
    monkeypatch.setattr(snapshot, "MAX_SNAPSHOT_BYTES", 32)
    reply = response([b"\xff\xd8\xff" + b"x" * 17, b"x" * 13, b"never read"], length=length)
    assert await snapshot.read_snapshot_body(reply) is None
    assert reply.content.reads == 2


@pytest.mark.asyncio
async def test_invalid_body_magic_and_empty_body_are_rejected_early():
    reply = response([b"<html>login!!", b"never read"])
    assert await snapshot.read_snapshot_body(reply) is None
    assert reply.content.reads == 1
    assert await snapshot.read_snapshot_body(response([])) is None


@pytest.mark.asyncio
async def test_timeout_and_cancellation_keep_both_slots_until_actual_completion(monkeypatch):
    started = threading.Barrier(3)
    release = threading.Event()
    finished = threading.Event()
    state_lock = threading.Lock()
    calls = 0
    completions = 0
    original_finished = snapshot._decoder_finished

    def blocked(data):
        nonlocal calls
        with state_lock:
            calls += 1
        started.wait(timeout=5)
        assert release.wait(timeout=5)
        return data

    def completed(future):
        nonlocal completions
        original_finished(future)
        with state_lock:
            completions += 1
            if completions == 2:
                finished.set()

    monkeypatch.setattr(snapshot, "_decode_snapshot", blocked)
    monkeypatch.setattr(snapshot, "_decoder_finished", completed)
    data = image_bytes()
    cancelled = asyncio.create_task(snapshot.decode_snapshot(data, timeout=5))
    timed_out = asyncio.create_task(snapshot.decode_snapshot(data, timeout=0.05))
    try:
        await asyncio.to_thread(started.wait, 5)
        cancelled.cancel()
        with pytest.raises(asyncio.CancelledError):
            await cancelled
        assert await timed_out is None
        assert len(snapshot._DECODER_FUTURES) == 2
        for _ in range(5):
            assert await snapshot.decode_snapshot(data, timeout=1) is None
        assert calls == 2  # no queued/submitted third job after either waiter ended
    finally:
        release.set()
        assert await asyncio.to_thread(finished.wait, 5)
        await asyncio.gather(cancelled, timed_out, return_exceptions=True)
    assert not snapshot._DECODER_FUTURES
    monkeypatch.setattr(snapshot, "_decode_snapshot", lambda data: data)
    assert await snapshot.decode_snapshot(data, timeout=1) == data
