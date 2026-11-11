"""G01 G: real HTTP response ownership and bounded worker capture paths."""

import asyncio
import gzip
from contextlib import asynccontextmanager
from io import BytesIO
from unittest.mock import AsyncMock, Mock

import pytest
from aiohttp import web
from PIL import Image

from backend.app.services import external_camera as ec


@asynccontextmanager
async def endpoint(handler):
    app = web.Application()
    app.router.add_get("/{tail:.*}", handler)
    runner = web.AppRunner(app, shutdown_timeout=0.1)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    try:
        yield f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"
    finally:
        await runner.cleanup()


def image_bytes(fmt):
    with Image.new("RGB", (24, 16), "red") as image, BytesIO() as output:
        image.save(output, format=fmt)
        return output.getvalue()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fmt,mime", [("JPEG", "image/jpeg"), ("PNG", "image/png"), ("WEBP", "image/webp"), ("BMP", "image/bmp")]
)
async def test_wire_snapshot_is_a_decodable_jpeg(fmt, mime):
    original = image_bytes(fmt)

    async def handler(request):
        assert request.headers["Accept-Encoding"] == "identity"
        return web.Response(body=original, content_type=mime)

    async with endpoint(handler) as url:
        result = await ec._capture_snapshot(url, 1)
    with Image.open(BytesIO(result)) as image:
        image.load()
        assert image.format == "JPEG" and image.size == (24, 16)
    if fmt == "JPEG":
        assert result == original


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mime,body",
    [
        ("text/html", b"<html>denied</html>"),
        ("image/svg+xml", b"<svg/>"),
        ("application/json", b'{"error":"denied"}'),
        ("image/jpeg", b"\xff\xd8\xffgarbage\xff\xd9"),
    ],
)
async def test_wire_non_image_never_publishes(mime, body):
    async def handler(request):
        return web.Response(body=body, content_type=mime)

    async with endpoint(handler) as url:
        assert await ec._capture_snapshot(url, 1) is None


@pytest.mark.asyncio
async def test_compressed_image_is_rejected_without_decoder(monkeypatch):
    decode = AsyncMock()
    monkeypatch.setattr(ec, "decode_snapshot", decode)

    async def handler(request):
        return web.Response(
            body=gzip.compress(image_bytes("PNG")), headers={"Content-Encoding": "gzip", "Content-Type": "image/png"}
        )

    async with endpoint(handler) as url:
        assert await ec._capture_snapshot(url, 1) is None
    decode.assert_not_awaited()


@pytest.mark.asyncio
async def test_body_stall_obeys_capture_deadline_and_closes_response():
    sent, disconnected = asyncio.Event(), asyncio.Event()

    async def handler(request):
        response = web.StreamResponse(headers={"Content-Type": "image/jpeg"})
        await response.prepare(request)
        await response.write(b"\xff\xd8\xff")
        sent.set()
        for _ in range(100):
            if request.transport is None or request.transport.is_closing():
                disconnected.set()
                return response
            await asyncio.sleep(0.01)
        return response

    async with endpoint(handler) as url:
        assert await asyncio.wait_for(ec._capture_snapshot(url, 0.08), 0.5) is None
        assert sent.is_set()
        await asyncio.wait_for(disconnected.wait(), 0.5)


@pytest.mark.asyncio
async def test_four_body_consumers_wait_deadline_and_cancel_restores_capacity(monkeypatch):
    # Headers may arrive; the fifth call must wait before consuming its body.
    monkeypatch.setattr(ec, "_SNAPSHOT_HTTP_SLOTS", ec.SnapshotAdmission(4))
    reached = readers = 0
    ready, release = asyncio.Event(), asyncio.Event()
    replacement_reader = asyncio.Event()
    original_read = ec.read_snapshot_body
    data = image_bytes("JPEG")

    async def handler(request):
        nonlocal reached
        reached += 1
        return web.Response(body=data, content_type="image/jpeg")

    async def held_body(response):
        nonlocal readers
        readers += 1
        if readers == 4:
            ready.set()
        if readers == 5:
            replacement_reader.set()
        await release.wait()
        return await original_read(response)

    monkeypatch.setattr(ec, "read_snapshot_body", held_body)
    async with endpoint(handler) as url:
        tasks = [asyncio.create_task(ec._capture_snapshot(f"{url}/{index}", 2)) for index in range(4)]
        try:
            await asyncio.wait_for(ready.wait(), 1)
            assert await ec._capture_snapshot(f"{url}/fifth", 0.03) is None
            assert reached == 5 and readers == 4
            tasks[0].cancel()
            with pytest.raises(asyncio.CancelledError):
                await tasks[0]
            sixth = asyncio.create_task(ec._capture_snapshot(f"{url}/sixth", 1))
            tasks.append(sixth)
            await asyncio.wait_for(replacement_reader.wait(), 1)
            assert reached == 6 and readers == 5
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            release.set()


class Process:
    def __init__(self, chunks, *, stall=False):
        self.pid, self.returncode = 4242, None
        self.stdout = Mock(read=AsyncMock(side_effect=[*chunks, b""]))
        self.stderr = asyncio.StreamReader()
        self.terminate, self.kill = Mock(), Mock()
        if stall:

            async def blocked(_size):
                await asyncio.Event().wait()

            self.stdout.read = blocked

    async def wait(self):
        self.returncode = -15 if self.terminate.called else 0
        self.stderr.feed_eof()
        return self.returncode


@pytest.mark.asyncio
@pytest.mark.parametrize("suffix", [b"", b"\xff\xd9"])
async def test_rtsp_one_shot_caps_stdout_and_reaps(monkeypatch, suffix):
    monkeypatch.setattr(ec, "MAX_SNAPSHOT_BYTES", 128)
    process = Process([b"\xff\xd8" + b"x" * 127 + suffix])
    monkeypatch.setattr(ec, "get_ffmpeg_path", lambda: "ffmpeg")
    monkeypatch.setattr(ec.asyncio, "create_subprocess_exec", AsyncMock(return_value=process))
    assert await ec._capture_rtsp_frame("rtsp://127.0.0.1/live", 1) is None
    assert process.returncode is not None
    process.terminate.assert_called_once()


@pytest.mark.asyncio
async def test_rtsp_one_shot_cancel_reaps_process(monkeypatch):
    entered = asyncio.Event()
    process = Process([], stall=True)

    async def blocked(size):
        entered.set()
        await asyncio.Event().wait()

    process.stdout.read = blocked
    monkeypatch.setattr(ec, "get_ffmpeg_path", lambda: "ffmpeg")
    monkeypatch.setattr(ec.asyncio, "create_subprocess_exec", AsyncMock(return_value=process))
    task = asyncio.create_task(ec._capture_rtsp_frame("rtsp://127.0.0.1/live", 1))
    await asyncio.wait_for(entered.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert process.returncode is not None
    process.terminate.assert_called_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("suffix", [b"", b"\xff\xd9"])
async def test_live_mjpeg_caps_incomplete_and_complete_frames(monkeypatch, suffix):
    monkeypatch.setattr(ec, "MAX_SNAPSHOT_BYTES", 128)

    async def handler(request):
        return web.Response(body=b"\xff\xd8" + b"x" * 127 + suffix, content_type="multipart/x-mixed-replace")

    async with endpoint(handler) as url:
        assert [frame async for frame in ec._stream_mjpeg(url)] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("suffix", [b"", b"\xff\xd9"])
async def test_live_rtsp_caps_incomplete_and_complete_frames(monkeypatch, suffix):
    monkeypatch.setattr(ec, "MAX_SNAPSHOT_BYTES", 128)
    process = Process([b"\xff\xd8" + b"x" * 127 + suffix])
    monkeypatch.setattr(ec, "get_ffmpeg_path", lambda: "ffmpeg")
    monkeypatch.setattr(ec.asyncio, "create_subprocess_exec", AsyncMock(return_value=process))
    assert [frame async for frame in ec._stream_rtsp("rtsp://127.0.0.1/live", 5)] == []
    assert process.returncode is not None
