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


@pytest.mark.parametrize("hold_phase", ["body", "decode"])
async def test_six_body_consumers_below_byte_budget_all_publish(monkeypatch, hold_phase):
    data = encoded_image()
    headers_ready, bodies_ready, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    reached = active = peak = reads = 0
    original_read = camera.read_snapshot_body
    original_decode = camera.decode_snapshot

    async def handler(request):
        nonlocal reached
        reached += 1
        if reached == 6:
            headers_ready.set()
        return web.Response(body=data, content_type="image/jpeg")

    async def hold_consumer():
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        if active == 6:
            bodies_ready.set()
        try:
            await release.wait()
        finally:
            active -= 1

    async def held_body(response, *, lease=None):
        nonlocal reads
        reads += 1
        if hold_phase == "body":
            await hold_consumer()
        return await original_read(response, lease=lease)

    async def held_decode(source, timeout, *, lease=None):
        if hold_phase == "decode":
            await hold_consumer()
        return await original_decode(source, timeout, lease=lease)

    monkeypatch.setattr(camera, "read_snapshot_body", held_body)
    monkeypatch.setattr(camera, "decode_snapshot", held_decode)
    async with endpoint(handler) as url:
        tasks = [asyncio.create_task(camera._capture_snapshot(f"{url}/{index}", 3)) for index in range(6)]
        try:
            await asyncio.wait_for(headers_ready.wait(), 2)
            await asyncio.wait_for(bodies_ready.wait(), 2)
            assert reached == 6 and reads == active == 6
            assert all(not task.done() for task in tasks)
            release.set()
            results = await asyncio.wait_for(asyncio.gather(*tasks), 3)
            assert results == [data] * 6
            assert reached == 6 and peak == 6
        finally:
            release.set()
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)


async def test_six_stalled_headers_do_not_starve_two_healthy_cameras_and_one_shot(monkeypatch):
    data = encoded_image()
    stalled_ready, release = asyncio.Event(), asyncio.Event()
    stalled = 0
    reads = []
    original_read = camera.read_snapshot_body

    async def handler(request):
        nonlocal stalled
        if request.path.startswith("/stalled"):
            stalled += 1
            if stalled == 6:
                stalled_ready.set()
            await release.wait()  # no response headers until cleanup
        return web.Response(body=data, content_type="image/jpeg")

    async def observed_body(response, *, lease=None):
        reads.append(response.url.path)
        return await original_read(response, lease=lease)

    monkeypatch.setattr(camera, "read_snapshot_body", observed_body)
    async with endpoint(handler) as url:
        tasks = [asyncio.create_task(camera._capture_snapshot(f"{url}/stalled/{i}", 5)) for i in range(6)]
        try:
            await asyncio.wait_for(stalled_ready.wait(), 2)
            assert all(not task.done() for task in tasks)
            healthy = [camera._capture_snapshot(f"{url}/healthy/{i}", 1) for i in range(2)]
            one_shot = camera.capture_frame(f"{url}/one-shot", "snapshot", timeout=1)
            assert await asyncio.wait_for(asyncio.gather(*healthy, one_shot), 1.5) == [data] * 3
            assert all(not task.done() for task in tasks)
            assert len(reads) == 3 and not any(path.startswith("/stalled") for path in reads)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            release.set()
        assert await camera._capture_snapshot(f"{url}/recovery", 1) == data


async def test_headers_and_body_and_decode_share_original_capture_budget(monkeypatch):
    ordinary, target = encoded_image(), encoded_image(progressive=True)
    holders_ready, release_holders = asyncio.Event(), asyncio.Event()
    target_request, release_headers, target_headers = asyncio.Event(), asyncio.Event(), asyncio.Event()
    target_body, release_body = asyncio.Event(), asyncio.Event()
    readers = 0
    original_read, original_decode = camera.read_snapshot_body, camera.decode_snapshot
    decode_budgets = []

    async def handler(request):
        if request.path == "/target":
            target_request.set()
            await release_headers.wait()
            target_headers.set()
            return web.Response(body=target, content_type="image/jpeg")
        return web.Response(body=ordinary, content_type="image/jpeg")

    async def held_body(response, *, lease=None):
        nonlocal readers
        if response.url.path == "/target":
            target_body.set()
            await release_body.wait()
        else:
            readers += 1
            if readers == 4:
                holders_ready.set()
            await release_holders.wait()
        return await original_read(response, lease=lease)

    async def observed_decode(source, timeout, *, lease=None):
        if source == target:
            decode_budgets.append((asyncio.get_running_loop().time(), timeout))
        return await original_decode(source, timeout, lease=lease)

    monkeypatch.setattr(camera, "read_snapshot_body", held_body)
    monkeypatch.setattr(camera, "decode_snapshot", observed_decode)
    async with endpoint(handler) as url:
        tasks = [asyncio.create_task(camera._capture_snapshot(f"{url}/hold/{i}", 5)) for i in range(4)]
        try:
            await asyncio.wait_for(holders_ready.wait(), 2)
            started = asyncio.get_running_loop().time()
            capture = asyncio.create_task(camera._capture_snapshot(f"{url}/target", 3))
            tasks.append(capture)
            await asyncio.wait_for(target_request.wait(), 1)
            await asyncio.sleep(0.08)
            release_headers.set()
            await asyncio.wait_for(target_headers.wait(), 1)
            await asyncio.sleep(0.08)
            assert target_body.is_set()  # unrelated bodies cannot gate this request
            release_holders.set()
            await asyncio.wait_for(target_body.wait(), 1)
            await asyncio.sleep(0.08)
            release_body.set()
            assert await asyncio.wait_for(capture, 1) == target
            assert len(decode_budgets) == 1
            decoded_at, remaining = decode_budgets[0]
            assert remaining == pytest.approx(3 - (decoded_at - started), abs=0.03)
            assert remaining < 2.85  # neither headers nor body wait resets the budget
        finally:
            release_headers.set()
            release_holders.set()
            release_body.set()
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
