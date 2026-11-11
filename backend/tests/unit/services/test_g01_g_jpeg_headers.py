"""GA2 JPEG fast path: bounded first scan headers, no native image decoder."""

import asyncio
import logging
from io import BytesIO
from unittest.mock import MagicMock

import pytest
from PIL import Image

from backend.app.services import camera_snapshot as snapshot, external_camera as camera


def segment(marker, payload):
    return bytes([255, marker]) + (len(payload) + 2).to_bytes(2, "big") + payload


SOI, EOI = b"\xff\xd8", b"\xff\xd9"
FRAME = b"\x08\x00\x10\x00\x18\x03\x01\x11\x00\x02\x11\x00\x03\x11\x00"
SCAN = b"\x03\x01\x00\x02\x11\x03\x11\x00\x3f\x00"


def framed(frame=FRAME, scan=SCAN, *, entropy=b"x"):
    return SOI + segment(0xC0, frame) + segment(0xDA, scan) + entropy + EOI


@pytest.mark.parametrize("mode,progressive", [("L", False), ("RGB", False), ("RGB", True), ("CMYK", False)])
@pytest.mark.asyncio
async def test_supported_real_jpeg_headers_preserve_pixels_without_native_admission(monkeypatch, mode, progressive):
    with Image.new(mode, (192, 108)) as image, BytesIO() as output:
        image.save(output, format="JPEG", progressive=progressive)
        data = output.getvalue()
    parser = MagicMock(side_effect=AssertionError("must not open Pillow"))
    submit = MagicMock(side_effect=AssertionError("must not submit native work"))
    monkeypatch.setattr(Image, "open", parser)
    monkeypatch.setattr(snapshot._DECODER_EXECUTOR, "submit", submit)
    assert snapshot._decode_snapshot(data) == data
    assert await snapshot.decode_snapshot(data, 1) == data
    parser.assert_not_called()
    submit.assert_not_called()


@pytest.mark.parametrize(
    "data",
    [
        SOI + b"\xff\xe1\x00\x00" + EOI,
        SOI + b"\xff\xe1\x00\x01" + EOI,
        SOI + b"\xff\xe1\xff\xff" + EOI,
        SOI + SOI + segment(0xC0, FRAME) + segment(0xDA, SCAN) + b"x" + EOI,
        SOI + EOI,
        SOI + segment(0xDA, SCAN) + b"x" + EOI,
        SOI + segment(0xC0, FRAME) + segment(0xC0, FRAME) + segment(0xDA, SCAN) + b"x" + EOI,
        framed(frame=FRAME[:7]),
        framed(frame=FRAME[:6] + b"\x01\x00\x00" + FRAME[9:]),
        framed(frame=FRAME[:9] + FRAME[6:9] + FRAME[12:]),
        framed(frame=FRAME[:8] + b"\x04" + FRAME[9:]),
        framed(frame=b"\x08\x20\x01" + FRAME[3:]),
        framed(frame=b"\x08\x00\x00" + FRAME[3:]),
        framed(frame=b"\x0c" + FRAME[1:]),
        framed(scan=SCAN[:3]),
        framed(scan=b"\x03\x04" + SCAN[2:]),
        framed(scan=SCAN[:3] + SCAN[1:3] + SCAN[5:]),
        framed(scan=SCAN[:2] + b"\x40" + SCAN[3:]),
        framed(scan=SCAN[:-3] + b"\x00\x40\x00"),
        framed(scan=SCAN[:-3] + b"\x01\x3f\x00"),
        framed(scan=SCAN[:-1] + b"\xe0"),
        framed(entropy=b""),
        framed() + b"\r\n",
        framed()[:-2],
        SOI + b"not a marker" + EOI,
        SOI + segment(0xC2, FRAME) + segment(0xDA, SCAN) + b"x" + EOI,
        SOI + segment(0xC2, FRAME) + segment(0xDA, SCAN[:-3] + b"\x01\x3f\x00") + b"x" + EOI,
    ],
)
@pytest.mark.asyncio
async def test_malformed_header_or_missing_terminal_structure_is_rejected(monkeypatch, data):
    parser = MagicMock(side_effect=AssertionError("must not open Pillow"))
    monkeypatch.setattr(Image, "open", parser)
    assert snapshot._decode_snapshot(data) is None
    assert await snapshot.decode_snapshot(data, 1) is None
    parser.assert_not_called()


def test_marker_budget_skips_large_metadata_without_copy_or_native_work():
    # Two structural markers leave capacity for 254 length-delimited headers.
    metadata = segment(0xE1, b"x" * 4096)
    accepted = SOI + metadata * 254 + framed()[2:]
    assert snapshot._checked_jpeg(accepted) == accepted
    assert snapshot._checked_jpeg(SOI + metadata * 255 + framed()[2:]) is None
    assert snapshot._checked_jpeg(SOI + b"\xff" * 1_000_000 + b"\xd9") is None


def test_checked_jpeg_does_not_claim_full_entropy_validation():
    # Framed first scan is the explicit §6c exception, not a real pixel decode.
    assert snapshot._checked_jpeg(framed()) == framed()


@pytest.mark.asyncio
async def test_fifo_admission_preserves_waiter_order_after_timeout_and_cancel():
    gate = snapshot.SnapshotAdmission(1)
    loop = asyncio.get_running_loop()
    assert await gate.acquire(loop.time() + 1)
    order = []

    async def waiter(index, budget=1):
        if not await gate.acquire(loop.time() + budget):
            return False
        try:
            order.append(index)
            await asyncio.sleep(0)
            return True
        finally:
            gate.release()

    first = asyncio.create_task(waiter(1))
    cancelled = asyncio.create_task(waiter(2))
    expired = asyncio.create_task(waiter(3, 0.02))
    last = asyncio.create_task(waiter(4))
    await asyncio.sleep(0)
    cancelled.cancel()
    with pytest.raises(asyncio.CancelledError):
        await cancelled
    assert await expired is False
    gate.release()
    assert await asyncio.gather(first, last) == [True, True]
    assert order == [1, 4]
    # Deadline wins over an available permit, and no permit is lost on refusal.
    assert await gate.acquire(loop.time() - 1) is False
    assert await gate.acquire(loop.time() + 1)
    gate.release()


def test_failure_warnings_are_aggregated_without_camera_secrets(monkeypatch, caplog):
    monkeypatch.setattr(camera, "_snapshot_warnings", {})
    clock = [10.0]
    monkeypatch.setattr(camera.time, "monotonic", lambda: clock[0])
    with caplog.at_level(logging.WARNING, logger=camera.__name__):
        for _ in range(100):
            camera._snapshot_warning("Camera snapshot decode unavailable or rejected")
        assert len(caplog.records) == 1
        clock[0] += 60
        camera._snapshot_warning("Camera snapshot decode unavailable or rejected")
    assert len(caplog.records) == 2
    assert "suppressed since prior report: 99" in caplog.records[-1].getMessage()
