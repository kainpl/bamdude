"""GA2: healthy snapshots wait within deadlines without unbounded native work."""

import asyncio
import threading
from contextlib import asynccontextmanager
from io import BytesIO
from unittest.mock import MagicMock

import pytest
from aiohttp import web
from PIL import Image

from backend.app.services import camera_snapshot as snapshot, external_camera as camera

pytestmark = pytest.mark.asyncio


def encoded_image(fmt="JPEG", *, progressive=False):
    with Image.new("RGB", (64, 48), (20, 120, 220)) as image, BytesIO() as output:
        options = {"progressive": progressive} if fmt == "JPEG" else {}
        image.save(output, format=fmt, **options)
        return output.getvalue()


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


async def test_six_distinct_valid_http_snapshots_wait_then_all_publish():
    data = encoded_image()
    ready, release = asyncio.Event(), asyncio.Event()
    reached = active = peak = 0

    async def handler(request):
        nonlocal reached, active, peak
        reached += 1
        active += 1
        peak = max(peak, active)
        if reached == 4:
            ready.set()
        try:
            await release.wait()
            return web.Response(body=data, content_type="image/jpeg")
        finally:
            active -= 1

    async with endpoint(handler) as url:
        tasks = [asyncio.create_task(camera._capture_snapshot(f"{url}/{index}", 3)) for index in range(6)]
        try:
            await asyncio.wait_for(ready.wait(), 2)
            assert reached == 4
            assert all(not task.done() for task in tasks)
            release.set()
            results = await asyncio.wait_for(asyncio.gather(*tasks), 3)
            assert results == [data] * 6
            assert reached == 6 and peak <= 4
        finally:
            release.set()
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)


async def test_http_admission_wait_timeout_and_cancel_do_no_outbound_then_recover():
    data = encoded_image()
    ready, release = asyncio.Event(), asyncio.Event()
    reached = active = peak = 0

    async def handler(request):
        nonlocal reached, active, peak
        reached += 1
        active += 1
        peak = max(peak, active)
        if reached == 4:
            ready.set()
        try:
            await release.wait()
            return web.Response(body=data, content_type="image/jpeg")
        finally:
            active -= 1

    async with endpoint(handler) as url:
        tasks = [asyncio.create_task(camera._capture_snapshot(f"{url}/{index}", 3)) for index in range(4)]
        try:
            await asyncio.wait_for(ready.wait(), 2)
            # All four operations are held; this deadline expires in admission.
            assert await asyncio.wait_for(camera._capture_snapshot(f"{url}/deadline", 0.05), 0.5) is None
            assert reached == 4 and all(not task.done() for task in tasks)
            entered = asyncio.Event()

            async def cancelled_waiter():
                entered.set()
                return await camera._capture_snapshot(f"{url}/cancel", 3)

            waiter = asyncio.create_task(cancelled_waiter())
            tasks.append(waiter)
            await asyncio.wait_for(entered.wait(), 1)
            waiter.cancel()
            with pytest.raises(asyncio.CancelledError):
                await waiter
            assert reached == 4
            recovery = asyncio.create_task(camera._capture_snapshot(f"{url}/recovery", 3))
            tasks.append(recovery)
            release.set()
            assert await asyncio.wait_for(asyncio.gather(*tasks[:4], recovery), 3) == [data] * 5
            assert reached == 5 and peak <= 4
        finally:
            release.set()
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.parametrize("progressive", [False, True])
async def test_six_checked_jpegs_publish_without_pillow_or_native_submission(monkeypatch, progressive):
    data = encoded_image(progressive=progressive)
    parser = MagicMock(side_effect=AssertionError("JPEG must not enter Pillow"))
    submit = MagicMock(side_effect=AssertionError("JPEG must not enter native executor"))
    monkeypatch.setattr(Image, "open", parser)
    monkeypatch.setattr(snapshot._DECODER_EXECUTOR, "submit", submit)
    results = await asyncio.gather(*(snapshot.decode_snapshot(data, 1) for _ in range(6)))
    assert results == [data] * 6
    parser.assert_not_called()
    submit.assert_not_called()


async def test_third_png_waits_without_native_submission_then_all_three_decode(monkeypatch):
    data = encoded_image("PNG")
    original = snapshot._decode_snapshot
    started = threading.Barrier(3)
    release = threading.Event()
    lock = threading.Lock()
    calls = active = peak = 0

    def blocked(source):
        nonlocal calls, active, peak
        with lock:
            calls += 1
            ordinal = calls
            active += 1
            peak = max(peak, active)
        try:
            if ordinal <= 2:
                started.wait(timeout=5)
                assert release.wait(timeout=5)
            return original(source)
        finally:
            with lock:
                active -= 1

    monkeypatch.setattr(snapshot, "_decode_snapshot", blocked)
    tasks = [asyncio.create_task(snapshot.decode_snapshot(data, 3)) for _ in range(3)]
    try:
        await asyncio.to_thread(started.wait, 5)
        assert calls == 2 and len(snapshot._DECODER_FUTURES) == 2
        assert all(not task.done() for task in tasks)
        release.set()
        results = await asyncio.wait_for(asyncio.gather(*tasks), 3)
        assert calls == 3 and peak <= 2
        for result in results:
            assert result
            with Image.open(BytesIO(result)) as image:
                image.load()
                assert image.format == "JPEG" and image.size == (64, 48)
    finally:
        release.set()
        await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 3)


async def test_native_wait_timeout_cancel_retains_two_futures_until_actual_completion(monkeypatch):
    data = encoded_image("PNG")
    original = snapshot._decode_snapshot
    started = threading.Barrier(3)
    release = threading.Event()
    lock = threading.Lock()
    calls = 0

    def blocked(source):
        nonlocal calls
        with lock:
            calls += 1
            ordinal = calls
        if ordinal <= 2:
            started.wait(timeout=5)
            assert release.wait(timeout=5)
        return original(source)

    monkeypatch.setattr(snapshot, "_decode_snapshot", blocked)
    tasks = [asyncio.create_task(snapshot.decode_snapshot(data, 3)) for _ in range(2)]
    try:
        await asyncio.to_thread(started.wait, 5)
        assert await asyncio.wait_for(snapshot.decode_snapshot(data, 0.05), 0.5) is None
        entered = asyncio.Event()

        async def cancelled_waiter():
            entered.set()
            return await snapshot.decode_snapshot(data, 3)

        waiter = asyncio.create_task(cancelled_waiter())
        tasks.append(waiter)
        await asyncio.wait_for(entered.wait(), 1)
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        assert calls == 2 and len(snapshot._DECODER_FUTURES) == 2
        release.set()
        assert all(await asyncio.wait_for(asyncio.gather(*tasks[:2]), 3))
        assert await snapshot.decode_snapshot(data, 1)
        assert calls == 3
    finally:
        release.set()
        await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 3)
