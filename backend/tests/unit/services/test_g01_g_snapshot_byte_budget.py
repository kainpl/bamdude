"""GA4: slow valid bodies share actual encoded bytes, never a request count."""

import asyncio
import random
import threading
from concurrent.futures import Future
from contextlib import asynccontextmanager
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from aiohttp import web
from PIL import Image

from backend.app.services import camera_snapshot as snapshot, external_camera as camera

pytestmark = pytest.mark.asyncio


def image_bytes(fmt="JPEG"):
    pixels = random.Random(19).randbytes(128 * 128 * 3)
    with Image.frombytes("RGB", (128, 128), pixels) as image, BytesIO() as output:
        image.save(output, format=fmt)
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


async def test_six_slow_valid_chunked_frames_do_not_delay_healthy_capture(monkeypatch):
    data = image_bytes()
    budget = snapshot.SnapshotByteBudget(40 * 1024 * 1024)
    monkeypatch.setattr(camera, "_SNAPSHOT_BYTE_BUDGET", budget)
    all_started = asyncio.Event()
    started = completed = 0
    progress = [0] * 6

    async def handler(request):
        nonlocal started, completed
        if request.path == "/healthy":
            return web.Response(body=data, content_type="image/jpeg")
        index = int(request.match_info["tail"])
        response = web.StreamResponse(headers={"Content-Type": "image/jpeg"})
        await response.prepare(request)  # chunked, deliberately no Content-Length
        for offset in range(0, len(data), 128):
            await response.write(data[offset : offset + 128])
            progress[index] += 1
            if offset == 0:
                started += 1
                if started == 6:
                    all_started.set()
            await asyncio.sleep(0.01)  # finite, genuinely progressing slow body
        completed += 1
        await response.write_eof()
        return response

    async with endpoint(handler) as url:
        tasks = [asyncio.create_task(camera._capture_snapshot(f"{url}/{i}", 5)) for i in range(6)]
        try:
            await asyncio.wait_for(all_started.wait(), 1)
            before = list(progress)
            assert await asyncio.wait_for(camera._capture_snapshot(f"{url}/healthy", 0.5), 0.5) == data
            assert completed == 0 and all(not task.done() for task in tasks)
            assert await asyncio.wait_for(asyncio.gather(*tasks), 5) == [data] * 6
            assert all(after > prior for after, prior in zip(progress, before, strict=True))
            assert completed == 6
            assert 0 < budget.peak_bytes <= budget.capacity
            assert budget.used_bytes == 0
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)


async def test_six_prefix_stalled_bodies_charge_only_received_bytes_and_recover(monkeypatch):
    data = image_bytes()
    budget = snapshot.SnapshotByteBudget(32 * 1024)
    monkeypatch.setattr(camera, "_SNAPSHOT_BYTE_BUDGET", budget)
    release, charged = asyncio.Event(), asyncio.Event()

    async def handler(request):
        if request.path == "/healthy":
            return web.Response(body=data, content_type="image/jpeg")
        response = web.StreamResponse(headers={"Content-Type": "image/jpeg", "Content-Length": str(len(data))})
        await response.prepare(request)
        await response.write(data[:128])
        await release.wait()
        return response

    original_lease = budget.lease

    def observed_lease():
        lease = original_lease()
        original_grow = lease.try_grow

        def grow(size):
            admitted = original_grow(size)
            if budget.used_bytes == 6 * 128:
                charged.set()
            return admitted

        lease.try_grow = grow
        return lease

    monkeypatch.setattr(budget, "lease", observed_lease)
    async with endpoint(handler) as url:
        tasks = [asyncio.create_task(camera._capture_snapshot(f"{url}/{i}", 5)) for i in range(6)]
        try:
            await asyncio.wait_for(charged.wait(), 1)
            assert budget.used_bytes == 6 * 128  # no full Content-Length reservation
            assert await asyncio.wait_for(camera._capture_snapshot(f"{url}/healthy", 0.5), 0.5) == data
            assert budget.used_bytes == 6 * 128
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            release.set()
        assert budget.used_bytes == 0
        assert await camera._capture_snapshot(f"{url}/healthy", 1) == data
        assert budget.used_bytes == 0 and budget.peak_bytes <= budget.capacity


async def test_full_partial_byte_budget_aborts_without_waiting_or_reading_more_then_recovers():
    data = image_bytes()
    prefix = len(data) * 3 // 4
    budget = snapshot.SnapshotByteBudget(2 * prefix)
    both_charged, release = asyncio.Event(), asyncio.Event()
    entered = 0
    after_failure = []

    def response(index):
        async def chunks(size):
            nonlocal entered
            assert size == 64 * 1024
            yield data[:prefix]
            entered += 1
            if entered == 2:
                both_charged.set()
            await release.wait()
            yield data[prefix:]
            # A rejected growth must terminate iteration, never pull this chunk.
            after_failure.append(index)

        return SimpleNamespace(headers={"Content-Type": "image/jpeg"}, content=SimpleNamespace(iter_chunked=chunks))

    async def consume(index):
        lease = budget.lease()
        try:
            return await snapshot.read_snapshot_body(response(index), lease=lease)
        except snapshot.SnapshotCapacityError:
            return None
        finally:
            lease.release()

    tasks = [asyncio.create_task(consume(i)) for i in range(2)]
    try:
        await asyncio.wait_for(both_charged.wait(), 1)
        assert budget.used_bytes == budget.capacity
        release.set()
        results = await asyncio.wait_for(asyncio.gather(*tasks), 0.5)
        assert results.count(None) == 1 and results.count(data) == 1
        assert len(after_failure) == 1
        assert budget.peak_bytes == budget.capacity and budget.used_bytes == 0
        lease = budget.lease()
        try:
            assert lease.try_grow(len(data))
            assert await snapshot.decode_snapshot(data, 1, lease=lease) == data
        finally:
            lease.release()
        assert budget.used_bytes == 0
    finally:
        release.set()
        await asyncio.gather(*tasks, return_exceptions=True)


async def test_native_timeout_and_cancel_keep_input_charged_until_actual_future_completion(monkeypatch):
    data = image_bytes("PNG")
    budget = snapshot.SnapshotByteBudget(2 * len(data))
    started = threading.Barrier(3)
    release = threading.Event()
    finished = asyncio.Event()
    loop = asyncio.get_running_loop()
    original_decode, original_finished = snapshot._decode_snapshot, snapshot._decoder_finished
    completions = 0

    def blocked(source):
        started.wait(timeout=5)
        assert release.wait(timeout=5)
        return original_decode(source)

    def observed_finished(future):
        nonlocal completions
        original_finished(future)
        completions += 1
        if completions == 2:
            loop.call_soon_threadsafe(finished.set)

    async def caller(timeout):
        lease = budget.lease()
        assert lease.try_grow(len(data))
        try:
            return await snapshot.decode_snapshot(data, timeout, lease=lease)
        finally:
            lease.release()

    monkeypatch.setattr(snapshot, "_decode_snapshot", blocked)
    monkeypatch.setattr(snapshot, "_decoder_finished", observed_finished)
    tasks = [asyncio.create_task(caller(timeout)) for timeout in (0.15, 3)]
    try:
        await asyncio.to_thread(started.wait, 5)
        tasks[1].cancel()
        with pytest.raises(asyncio.CancelledError):
            await tasks[1]
        assert await asyncio.wait_for(tasks[0], 1) is None
        assert budget.used_bytes == 2 * len(data)
        assert len(snapshot._DECODER_FUTURES) == 2
        rejected = budget.lease()
        try:
            assert not rejected.try_grow(1)
        finally:
            rejected.release()
        release.set()
        await asyncio.wait_for(finished.wait(), 2)
        assert budget.used_bytes == 0 and not snapshot._DECODER_FUTURES
        monkeypatch.setattr(snapshot, "_decode_snapshot", original_decode)
        assert await caller(1)
        assert budget.used_bytes == 0
    finally:
        release.set()
        await asyncio.gather(*tasks, return_exceptions=True)
        await asyncio.wait_for(finished.wait(), 2)


async def test_native_submission_failure_releases_retained_owner_and_recovers(monkeypatch):
    data = image_bytes("PNG")
    budget = snapshot.SnapshotByteBudget(len(data))
    lease = budget.lease()
    assert lease.try_grow(len(data))
    original_submit = snapshot._DECODER_EXECUTOR.submit
    monkeypatch.setattr(snapshot._DECODER_EXECUTOR, "submit", Mock(side_effect=RuntimeError("submission failed")))
    try:
        assert await snapshot.decode_snapshot(data, 1, lease=lease) is None
        assert budget.used_bytes == len(data)  # only the caller still owns input
        assert not snapshot._DECODER_FUTURES
    finally:
        lease.release()
    assert budget.used_bytes == 0
    monkeypatch.setattr(snapshot._DECODER_EXECUTOR, "submit", original_submit)
    lease = budget.lease()
    assert lease.try_grow(len(data))
    try:
        assert await snapshot.decode_snapshot(data, 1, lease=lease)
    finally:
        lease.release()
    assert budget.used_bytes == 0


class CodecAbort(BaseException):
    pass


@pytest.mark.parametrize("failure", [RuntimeError("untrusted codec metadata"), CodecAbort("codec abort")])
async def test_native_failure_retains_no_exception_or_input_after_completion(monkeypatch, failure):
    data = image_bytes("PNG")
    budget = snapshot.SnapshotByteBudget(len(data))
    original_submit = snapshot._DECODER_EXECUTOR.submit
    jobs, futures = [], []

    def record_submit(run):
        jobs.append(run.__self__)
        future = original_submit(run)
        futures.append(future)
        return future

    def fail(_data):
        raise failure

    monkeypatch.setattr(snapshot, "_decode_snapshot", fail)
    monkeypatch.setattr(snapshot._DECODER_EXECUTOR, "submit", record_submit)
    lease = budget.lease()
    assert lease.try_grow(len(data))
    try:
        assert await snapshot.decode_snapshot(data, 1, lease=lease) is None
        assert len(jobs) == len(futures) == 1
        assert jobs[0].data is None
        assert futures[0].exception() is None and futures[0].result() is None
        assert not snapshot._DECODER_FUTURES
        assert budget.used_bytes == len(data)
    finally:
        lease.release()
    assert budget.used_bytes == 0


async def test_control_exception_during_submission_rolls_back_slot_and_lease(monkeypatch):
    data = image_bytes("PNG")
    budget = snapshot.SnapshotByteBudget(len(data))
    original_submit = snapshot._DECODER_EXECUTOR.submit
    jobs = []

    def abort(run):
        jobs.append(run.__self__)
        raise CodecAbort()

    monkeypatch.setattr(snapshot._DECODER_EXECUTOR, "submit", abort)
    lease = budget.lease()
    assert lease.try_grow(len(data))
    try:
        with pytest.raises(CodecAbort):
            await snapshot.decode_snapshot(data, 1, lease=lease)
        assert jobs[0].data is None and not snapshot._DECODER_FUTURES
        assert budget.used_bytes == len(data)
    finally:
        lease.release()
    assert budget.used_bytes == 0
    monkeypatch.setattr(snapshot._DECODER_EXECUTOR, "submit", original_submit)
    assert await snapshot.decode_snapshot(data, 1)


async def test_immediately_completed_native_future_releases_exactly_one_retained_owner(monkeypatch):
    data = image_bytes("PNG")
    budget = snapshot.SnapshotByteBudget(len(data))
    jobs = []

    def immediate(run):
        jobs.append(run.__self__)
        future = Future()
        future.set_result(run())
        return future

    monkeypatch.setattr(snapshot._DECODER_EXECUTOR, "submit", immediate)
    lease = budget.lease()
    assert lease.try_grow(len(data))
    try:
        result = await snapshot.decode_snapshot(data, 1, lease=lease)
        assert result and result.endswith(b"\xff\xd9")
        assert jobs[0].data is None and not snapshot._DECODER_FUTURES
        assert budget.used_bytes == len(data)
    finally:
        lease.release()
    assert budget.used_bytes == 0
