"""Actual BamDude routes and middleware, with controlled server backpressure."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from backend.app.services import camera_fanout, live_media
from backend.tests.unit.live_media_harness import request

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def coordinator(monkeypatch, async_client):
    from backend.app.services import live_authority as authority_module

    runtime = authority_module.LiveAuthority()
    monkeypatch.setattr(authority_module, "live_authority", runtime)
    monkeypatch.setattr(live_media, "live_authority", runtime)
    yield runtime
    await camera_fanout.shutdown_all_broadcasters()
    assert await runtime.shutdown(timeout=5)


def _scope(path, client, extra_headers=()):
    req = request(path, headers=[(key.lower(), value) for key, value in [*client.headers.raw, *extra_headers]])
    req.scope["http_version"] = "1.1"
    req.scope["root_path"] = ""
    return req.scope


def _backpressure(app, scope):
    entered, resume, disconnect = asyncio.Event(), asyncio.Event(), asyncio.Event()
    starts = []
    sent_request = False

    async def receive():
        nonlocal sent_request
        if not sent_request:
            sent_request = True
            return {"type": "http.request", "body": b"", "more_body": False}
        await disconnect.wait()
        return {"type": "http.disconnect"}

    async def send(message):
        if message["type"] == "http.response.start":
            assert message["status"] in (200, 206), message
        if message["type"] == "http.response.body":
            starts.append(message)
            if len(starts) == 1:
                entered.set()
                await resume.wait()

    return asyncio.create_task(app(scope, receive, send)), starts, entered, resume, disconnect


async def test_actual_stack_and_route_inventory():
    from fastapi.routing import APIRoute
    from starlette.middleware.base import BaseHTTPMiddleware

    from backend.app.core.config import settings
    from backend.app.main import app

    installed_routes = [
        route for route in app.routes if isinstance(route, APIRoute) and getattr(route, "_live_media_installed", False)
    ]
    assert all(route.methods <= {"GET", "HEAD"} for route in installed_routes)
    installed = {
        (method, live_media._template(route.path, settings.api_prefix))
        for route in installed_routes
        for method in route.methods
    }
    assert {path for _, path in installed} == live_media.MEDIA_ROUTES

    def media_dependency(dependant):
        call = dependant.call
        qualified = getattr(call, "__qualname__", "")
        if getattr(call, "__module__", "") == "backend.app.core.auth" and qualified.startswith(
            (
                "require_media_",
                "require_camera_token_or_headers.",
                "require_camera_stream_token.",
            )
        ):
            return True
        return any(media_dependency(child) for child in dependant.dependencies)

    derived = {
        (method, live_media._template(route.path, settings.api_prefix))
        for route in app.routes
        if isinstance(route, APIRoute) and media_dependency(route.dependant)
        for method in route.methods & {"GET", "HEAD"}
    }
    assert installed == derived
    # Paths alone deduplicate a GET and its write sibling and hid accidental
    # media adapters on POST/PUT/DELETE. Those mutations keep their original
    # auth/transaction/response lifecycle.
    writes = {
        (method, live_media._template(route.path, settings.api_prefix))
        for route in app.routes
        if isinstance(route, APIRoute)
        and live_media._template(route.path, settings.api_prefix) in live_media.MEDIA_ROUTES
        for method in route.methods - {"GET", "HEAD"}
    }
    assert writes == {
        ("DELETE", "/archives/{archive_id}/timelapse"),
        ("POST", "/projects/{project_id}/cover-image"),
        ("DELETE", "/projects/{project_id}/cover-image"),
        ("PUT", "/products/{product_id}/cover-image"),
        ("DELETE", "/products/{product_id}/cover-image"),
        ("PUT", "/product-parts/{part_id}/image"),
    }
    assert not installed.intersection(writes)
    classes = [middleware.cls for middleware in app.user_middleware]
    assert classes.index(live_media.LiveMediaDeliveryMiddleware) < classes.index(BaseHTTPMiddleware)
    assert classes.count(BaseHTTPMiddleware) == 4


@pytest.mark.parametrize(
    "method,path",
    [
        ("DELETE", "/archives/1/timelapse"),
        ("POST", "/projects/1/cover-image"),
        ("DELETE", "/projects/1/cover-image"),
        ("PUT", "/products/1/cover-image"),
        ("DELETE", "/products/1/cover-image"),
        ("PUT", "/product-parts/1/image"),
    ],
)
async def test_non_media_write_paths_keep_strict_explicit_key_selection(async_client, method, path):
    # The client's valid JWT must not rescue an explicitly invalid key merely
    # because a write sharing a media URL bypasses the delivery-only adapter.
    response = await async_client.request(method, "/api/v1" + path, headers={"X-API-Key": "invalid-key"})
    assert response.status_code == 401


async def _entered(task, event):
    waiter = asyncio.create_task(event.wait())
    try:
        done, _ = await asyncio.wait((task, waiter), timeout=5, return_when=asyncio.FIRST_COMPLETED)
        if task in done:
            await task
            raise AssertionError("response ended before first server body")
        assert waiter in done, "first server body did not start"
    finally:
        waiter.cancel()
        await asyncio.gather(waiter, return_exceptions=True)


@pytest.mark.parametrize(
    "reason", ["retirement", "restore", "disconnect", "caller_cancel", "caller_then_retire", "retire_then_caller"]
)
async def test_actual_outer_send_does_not_wait_for_client_resume(
    async_client,
    coordinator,
    printer_factory,
    db_session,
    monkeypatch,
    reason,
):
    from backend.app.main import app
    from backend.app.services import camera_runtime

    printer = await printer_factory()
    await db_session.close()

    class Worker:
        async def stream_builtin(self, *, disconnect_event, **kwargs):
            while not disconnect_event.is_set():
                yield b"--frame\r\nContent-Type: image/jpeg\r\n\r\nframe\r\n"
                await asyncio.sleep(0)

    monkeypatch.setattr(camera_runtime, "get_camera_runtime", lambda: Worker())
    scope = _scope(f"/api/v1/printers/{printer.id}/camera/stream", async_client)
    task, starts, entered, resume, disconnect = _backpressure(app, scope)
    try:
        await _entered(task, entered)
        viewer = scope["state"]["live_media_viewer"]
        if reason == "retirement":
            viewer.handle.retire(4401)
        elif reason == "restore":
            assert await coordinator.drain(timeout=3)
        elif reason == "disconnect":
            disconnect.set()
        elif reason == "caller_then_retire":
            task.cancel("upstream caller cancellation")
            viewer.handle.retire(4401)
        elif reason == "retire_then_caller":
            viewer.handle.retire(4401)
            task.cancel("upstream caller cancellation")
        else:
            task.cancel("upstream caller cancellation")
        if "caller" in reason:
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 3)
        else:
            await asyncio.wait_for(task, 3)
        await asyncio.wait_for(viewer.cleanup, 3)
        assert not resume.is_set(), "no client/transport cooperation was supplied"
        assert len(starts) == 1
        assert viewer.cleanup_done and viewer.delivery_done
        assert viewer.handle not in coordinator.handles
        assert camera_fanout.get_subscriber_count(f"printer-{printer.id}") == 0
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_negative_control_without_outer_abort_stays_waiting_for_client(
    async_client,
    coordinator,
    printer_factory,
    db_session,
    monkeypatch,
):
    from backend.app.main import app
    from backend.app.services import camera_runtime

    printer = await printer_factory()
    await db_session.close()

    class Worker:
        async def stream_builtin(self, *, disconnect_event, **kwargs):
            yield b"--frame\r\nContent-Type: image/jpeg\r\n\r\nframe\r\n"
            await disconnect_event.wait()

    monkeypatch.setattr(camera_runtime, "get_camera_runtime", lambda: Worker())
    # Reproduce precisely the missing action in the former implementation.
    monkeypatch.setattr(live_media.MediaViewer, "abort_outer", lambda self: None)
    scope = _scope(f"/api/v1/printers/{printer.id}/camera/stream", async_client)
    task, _, entered, resume, _ = _backpressure(app, scope)
    try:
        await _entered(task, entered)
        viewer = scope["state"]["live_media_viewer"]
        viewer.handle.retire(4401)
        async with asyncio.timeout(3):
            while viewer.cleanup is None:
                await asyncio.sleep(0)
        await asyncio.wait_for(viewer.cleanup, 3)
        assert not task.done()
        assert not await coordinator.drain(timeout=0.02)
        assert viewer.handle in coordinator.handles and not viewer.delivery_done
        resume.set()
        await asyncio.wait_for(task, 3)
        assert viewer.delivery_done and viewer.handle not in coordinator.handles
    finally:
        resume.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("primed", [False, True])
@pytest.mark.parametrize("reason", ["retirement", "disconnect"])
async def test_actual_camera_buffered_pipeline_retirement_and_cleanup(
    async_client,
    coordinator,
    printer_factory,
    db_session,
    monkeypatch,
    primed,
    reason,
):
    from backend.app.main import app
    from backend.app.services import camera_light, camera_runtime

    printer = await printer_factory()
    await db_session.close()
    key = f"printer-{printer.id}"
    produced = asyncio.Event()
    monkeypatch.setattr(camera_light, "_lights", {})
    monkeypatch.setattr(camera_light, "allowed", AsyncMock(return_value=True))
    monkeypatch.setattr(
        camera_light,
        "_get_client",
        lambda _id: SimpleNamespace(
            state=SimpleNamespace(
                connected=True,
                has_chamber_light=True,
                chamber_light=True,
            )
        ),
    )

    async def source(stop):
        i = 0
        while not stop.is_set():
            yield b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + str(i).encode() + b"\r\n"
            i += 1
            await asyncio.sleep(0)

    class Worker:
        def stream_builtin(self, **kwargs):
            return source(kwargs["disconnect_event"])

    monkeypatch.setattr(camera_runtime, "get_camera_runtime", lambda: Worker())
    original_run = live_media._MjpegBody.run

    async def run(self, viewer, scope, receive, send):
        count = 0

        async def observed(message):
            nonlocal count
            if message["type"] == "http.response.body":
                count += 1
                if count >= 5:
                    produced.set()
            await send(message)

        await original_run(self, viewer, scope, receive, observed)

    monkeypatch.setattr(live_media._MjpegBody, "run", run)
    broadcaster = await camera_fanout.get_or_create_broadcaster(key, source)
    peer = await broadcaster.subscribe()
    if primed:
        await asyncio.wait_for(peer.get(), 3)
        assert broadcaster._last_chunk is not None
    scope = _scope(f"/api/v1/printers/{printer.id}/camera/stream", async_client)
    task, starts, entered, resume, disconnect = _backpressure(app, scope)
    try:
        await _entered(task, entered)
        await asyncio.wait_for(produced.wait(), 5)
        viewer = scope["state"]["live_media_viewer"]
        await broadcaster._lock.acquire()
        try:
            if reason == "retirement":
                viewer.handle.retire(4401)
            else:
                disconnect.set()
            resume.set()
            # The locked resource remains owned even though no buffered chunk
            # may start another server-facing send after retirement.
            async with asyncio.timeout(3):
                while viewer.cleanup is None:
                    await asyncio.sleep(0)
            assert viewer.handle in coordinator.handles
            assert not viewer.cleanup.done()
            if reason == "retirement":
                assert len(starts) == 1
        finally:
            broadcaster._lock.release()
        await asyncio.wait_for(task, 5)
        await asyncio.wait_for(viewer.cleanup, 3)
        if reason == "retirement":
            assert len(starts) == 1
        assert broadcaster.subscriber_count == 1
        assert camera_light._lights[printer.id].refcount == 0
        assert not broadcaster.stopped
        assert await asyncio.wait_for(peer.get(), 2)
    finally:
        resume.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("reason", ["retirement", "disconnect"])
async def test_actual_camera_waiting_frame_cleanup_does_not_need_gc(
    async_client,
    coordinator,
    printer_factory,
    db_session,
    monkeypatch,
    reason,
):
    from backend.app.main import app
    from backend.app.services import camera_runtime

    printer = await printer_factory()
    await db_session.close()
    frame = asyncio.Event()

    async def source(stop):
        await frame.wait()
        yield b"peer-frame"
        await stop.wait()

    class Worker:
        def stream_builtin(self, **kwargs):
            return source(kwargs["disconnect_event"])

    monkeypatch.setattr(camera_runtime, "get_camera_runtime", lambda: Worker())
    broadcaster = await camera_fanout.get_or_create_broadcaster(f"printer-{printer.id}", source)
    peer = await broadcaster.subscribe()
    scope = _scope(f"/api/v1/printers/{printer.id}/camera/stream", async_client)
    task, starts, _, resume, disconnect = _backpressure(app, scope)
    try:
        async with asyncio.timeout(5):
            while broadcaster.subscriber_count != 2:
                if task.done():
                    await task
                await asyncio.sleep(0)
        viewer = scope["state"]["live_media_viewer"]
        await broadcaster._lock.acquire()
        try:
            if reason == "retirement":
                viewer.handle.retire(4401)
            else:
                disconnect.set()
            async with asyncio.timeout(3):
                while viewer.cleanup is None:
                    await asyncio.sleep(0)
            task.cancel()
            task.cancel()
            assert viewer.handle in coordinator.handles
            assert not viewer.cleanup.done()
        finally:
            broadcaster._lock.release()
        await asyncio.wait_for(asyncio.shield(viewer.cleanup), 3)
        await asyncio.gather(task, return_exceptions=True)
        assert broadcaster.subscriber_count == 1
        assert not starts
        frame.set()
        assert await asyncio.wait_for(peer.get(), 2) == b"peer-frame"
    finally:
        resume.set()
        frame.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("range_header", [None, b"bytes=0-1048575", b"bytes=0-524287,600000-1048575"])
async def test_actual_video_buffered_range_pipeline_retirement(
    async_client,
    coordinator,
    printer_factory,
    db_session,
    archive_factory,
    monkeypatch,
    tmp_path,
    range_header,
):
    from backend.app.api.routes import archives
    from backend.app.main import app

    path = tmp_path / "video.mp4"
    path.write_bytes(b"private-video-data" * 100000)
    monkeypatch.setattr(archives.settings, "base_dir", tmp_path)
    printer = await printer_factory()
    await db_session.close()
    archive = await archive_factory(printer.id, timelapse_path="video.mp4")
    await db_session.close()
    produced = asyncio.Event()
    original_io = live_media._OwnedFileBody._io
    reads = 0

    async def observed(self, function, *args):
        nonlocal reads
        if getattr(function, "__name__", None) == "read":
            reads += 1
            if reads >= 5:
                produced.set()
        return await original_io(self, function, *args)

    monkeypatch.setattr(live_media._OwnedFileBody, "_io", observed)
    headers = [] if range_header is None else [(b"range", range_header)]
    scope = _scope(f"/api/v1/archives/{archive.id}/timelapse", async_client, headers)
    task, starts, entered, resume, _ = _backpressure(app, scope)
    try:
        await _entered(task, entered)
        # Multipart framing itself occupies pipeline slots, so body production
        # can stall before the fifth file read. The first send is already held.
        if range_header != b"bytes=0-524287,600000-1048575":
            await asyncio.wait_for(produced.wait(), 5)
        viewer = scope["state"]["live_media_viewer"]
        viewer.handle.retire(4401)
        resume.set()
        await asyncio.wait_for(task, 5)
        await asyncio.wait_for(viewer.cleanup, 3)
        assert len(starts) == 1
        assert viewer.cleanup_done
        assert viewer.handle not in coordinator.handles
    finally:
        resume.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
