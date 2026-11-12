"""Viewer resource ownership, file semantics and final delivery boundary."""

import asyncio
import importlib.util
import logging
import socket
import threading

import pytest
from fastapi import FastAPI, HTTPException
from starlette.responses import FileResponse, Response

from backend.app.services import live_media
from backend.app.services.camera_fanout import MjpegBroadcaster, iter_subscriber
from backend.tests.unit.live_media_harness import Handle, media_runtime, request, serve  # noqa: F401

pytestmark = pytest.mark.asyncio


@pytest.mark.parametrize("retire", [True, False])
async def test_cleanup_stays_owned_at_contended_unsubscribe_and_repeated_cancel(media_runtime, retire):
    async def source(stop):
        yield b"frame"
        await stop.wait()

    broadcaster = MjpegBroadcaster("test", source)
    peer = await broadcaster.subscribe()
    baseline = broadcaster.subscriber_count
    light = [0]

    async def prepare(viewer):
        queue = await broadcaster.subscribe()
        light[0] += 1
        viewer.releases.append(lambda: light.__setitem__(0, light[0] - 1))
        viewer.releases.append(lambda: broadcaster.unsubscribe(queue))
        return iter_subscriber(broadcaster, queue, request_cleanup=viewer.request_cleanup)

    req = request()
    task, chunks, disconnect = serve(live_media.mjpeg_response(req, prepare), req)
    assert (await asyncio.wait_for(chunks.get(), 2))["body"] == b"frame"
    await broadcaster._lock.acquire()
    viewer = req.state.live_media_viewer
    if retire:
        viewer.handle.retire()
    else:
        disconnect.set()
    try:
        async with asyncio.timeout(2):
            while viewer.cleanup is None:
                await asyncio.sleep(0)
        assert not viewer.cleanup.done()
        assert viewer.handle in media_runtime.handles
        task.cancel()
        task.cancel()
        assert not viewer.cleanup.cancelled()
    finally:
        broadcaster._lock.release()
    await asyncio.wait_for(asyncio.shield(viewer.cleanup), 2)
    await asyncio.gather(task, return_exceptions=True)
    assert broadcaster.subscriber_count == baseline
    assert light == [0]
    assert peer in broadcaster._subscribers
    assert not broadcaster.stopped
    await broadcaster.force_shutdown()


async def test_cancel_before_body_start_does_not_acquire(media_runtime):
    acquired = []
    entered = asyncio.Event()

    async def admit(handle):
        entered.set()
        await asyncio.Event().wait()

    media_runtime.admit = admit

    async def prepare(viewer):
        acquired.append(viewer)
        raise AssertionError("must not acquire")

    req = request()
    task, _, _ = serve(live_media.mjpeg_response(req, prepare), req)
    await asyncio.wait_for(entered.wait(), 2)
    req.state.live_media_viewer.handle.retire()
    with pytest.raises(HTTPException) as caught:
        await asyncio.wait_for(task, 2)
    assert caught.value.status_code == 503
    assert not acquired
    assert not media_runtime.handles


async def test_failed_unsubscribe_releases_light_but_keeps_uncertain_slot(media_runtime):
    light = [1]

    async def unsubscribe():
        raise RuntimeError("synthetic unsubscribe failure")

    async def prepare(viewer):
        viewer.releases.extend([lambda: light.__setitem__(0, 0), unsubscribe])

        async def frames():
            yield b"frame"

        return frames()

    req = request()
    task, _, _ = serve(live_media.mjpeg_response(req, prepare), req)
    with pytest.raises(RuntimeError, match="synthetic unsubscribe failure"):
        await task
    viewer = req.state.live_media_viewer
    assert light == [0]
    assert not viewer.cleanup_done
    assert viewer.handle in media_runtime.handles


async def test_fastapi_attached_background_runs_once_on_normal_body(media_runtime):
    from starlette.background import BackgroundTask

    calls = []

    async def prepare(viewer):
        async def frames():
            yield b"frame"

        return frames()

    req = request()
    response = live_media.mjpeg_response(req, prepare)
    response.background = BackgroundTask(lambda: calls.append("done"))
    task, _, _ = serve(response, req)
    await asyncio.wait_for(task, 2)
    assert calls == ["done"]


@pytest.mark.parametrize(
    "range_header,status", [(None, 200), (b"bytes=2-7", 206), (b"bytes=0-2,8-10", 206), (b"bytes=1000000-2000000", 416)]
)
@pytest.mark.parametrize("method", ["GET", "HEAD"])
async def test_owned_file_preserves_ranges_and_head(tmp_path, media_runtime, range_header, status, method):
    path = tmp_path / "video.mp4"
    data = b"0123456789abcdef" * 9000
    path.write_bytes(data)
    headers = [] if range_header is None else [(b"range", range_header)]
    req = request("/api/v1/archives/1/timelapse", headers=headers, method=method)
    messages = []

    async def receive():
        await asyncio.Event().wait()

    async def send(message):
        messages.append(message)

    response = live_media.OwnedMediaResponse(
        FileResponse(path, media_type="video/mp4", headers={"ETag": '"saved"'}), req
    )
    await response(req.scope, receive, send)
    assert messages[0]["status"] == status
    body = b"".join(m.get("body", b"") for m in messages)
    if method == "HEAD" or status == 416:
        assert not body
    elif range_header == b"bytes=2-7":
        assert body == data[2:8]
    elif range_header is None:
        assert body == data
    else:
        assert b"Content-Range: bytes 0-2/" in body or b"content-range: bytes 0-2/" in body
        assert b"012" in body and b"89a" in body
    assert max((len(m.get("body", b"")) for m in messages), default=0) <= 65536
    assert response.payload.file is None
    assert not response.payload.pending
    assert not media_runtime.handles


async def test_if_range_mismatch_returns_full_body(tmp_path, media_runtime):
    path = tmp_path / "v.mp4"
    path.write_bytes(b"whole-video")
    req = request(headers=[(b"range", b"bytes=0-2"), (b"if-range", b'"old"')])
    task, chunks, _ = serve(live_media.OwnedMediaResponse(FileResponse(path, headers={"ETag": '"current"'}), req), req)
    await asyncio.wait_for(task, 2)
    assert (await chunks.get())["body"] == b"whole-video"


async def test_covered_success_without_handle_fails_closed_but_errors_survive(caplog):
    async def app(scope, receive, send):
        await Response(b"private")(scope, receive, send)

    req = request()
    bodies = []
    caplog.set_level(logging.INFO, logger=live_media.__name__)
    await live_media.LiveMediaDeliveryMiddleware(app, prefix="/api/v1")(req.scope, None, lambda_send(bodies))
    assert not any(message["type"] == "http.response.body" for message in bodies)
    assert "category=authority_retired" in caplog.text

    async def refused(scope, receive, send):
        await Response(b"denied", status_code=401)(scope, receive, send)

    await live_media.LiveMediaDeliveryMiddleware(refused, prefix="/api/v1")(request().scope, None, lambda_send(bodies))
    assert bodies[-1]["body"] == b"denied"


def lambda_send(messages):
    async def send(message):
        messages.append(message)

    return send


async def test_outer_gate_blocks_buffered_chunks_when_client_resumes(media_runtime):
    app = FastAPI()
    produced = asyncio.Event()

    @app.get("/api/v1/printers/{printer_id}/camera/stream")
    async def stream():
        async def response(scope, receive, send):
            handle = Handle(None)
            handle.admitted = True
            viewer = live_media.MediaViewer(handle)
            scope["state"]["live_media_viewer"] = viewer
            await send({"type": "http.response.start", "status": 200, "headers": []})
            for i in range(20):
                if i == 4:
                    produced.set()
                await send({"type": "http.response.body", "body": str(i).encode(), "more_body": True})

        return AsgiResponse(response)

    for _ in range(4):

        @app.middleware("http")
        async def layer(req, call_next):
            return await call_next(req)

    wrapped = live_media.LiveMediaDeliveryMiddleware(app, prefix="/api/v1")
    req = request()
    resumed = asyncio.Event()
    entered = asyncio.Event()
    starts = []

    async def send(message):
        if message["type"] == "http.response.body":
            starts.append(message["body"])
            if len(starts) == 1:
                entered.set()
                await resumed.wait()

    async def receive():
        await asyncio.Event().wait()

    task = asyncio.create_task(wrapped(req.scope, receive, send))
    await asyncio.wait_for(entered.wait(), 2)
    await asyncio.wait_for(produced.wait(), 2)
    req.scope["state"]["live_media_viewer"].handle.retire()
    resumed.set()
    await asyncio.wait_for(task, 2)
    assert starts == [b"0"]  # entered before retirement; every buffered successor refused


class AsgiResponse(Response):
    def __init__(self, app):
        super().__init__()
        self.app = app

    async def __call__(self, scope, receive, send):
        await self.app(scope, receive, send)


async def test_actual_file_close_keeps_slot_until_thread_finishes(tmp_path, media_runtime):
    path = tmp_path / "video.mp4"
    path.write_bytes(b"data")
    response = live_media.OwnedMediaResponse(FileResponse(path), request())
    closing, release = threading.Event(), threading.Event()

    class File:
        def read(self, size):
            return b"data"

        def close(self):
            closing.set()
            assert release.wait(5)

    response.payload.file = File()
    task, chunks, _ = serve(response, response.request)
    await asyncio.wait_for(chunks.get(), 2)
    async with asyncio.timeout(2):
        while not closing.is_set():
            await asyncio.sleep(0)
    viewer = response.request.state.live_media_viewer
    try:
        task.cancel()
        task.cancel()
        assert viewer.handle in media_runtime.handles
        assert not viewer.cleanup.done()
    finally:
        release.set()
    await asyncio.wait_for(asyncio.shield(viewer.cleanup), 2)
    await asyncio.gather(task, return_exceptions=True)
    assert not media_runtime.handles
    assert response.payload.file is None


@pytest.mark.parametrize("protocol", ["h11", "httptools"])
async def test_uvicorn_expected_retirement_abort_is_info_plus_transport_close(media_runtime, caplog, protocol):
    if importlib.util.find_spec(protocol) is None:
        pytest.skip(f"optional {protocol} protocol is not installed")
    import uvicorn

    caplog.set_level(logging.INFO)
    served = asyncio.Event()

    async def app(scope, receive, send):
        handle = Handle(None)
        handle.admitted = True
        scope.setdefault("state", {})["live_media_viewer"] = live_media.MediaViewer(handle)
        await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"video/mp4")]})
        await send({"type": "http.response.body", "body": b"before", "more_body": True})
        handle.retire()
        try:
            await send({"type": "http.response.body", "body": b"PRIVATE-AFTER", "more_body": True})
        finally:
            served.set()

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    config = uvicorn.Config(
        live_media.LiveMediaDeliveryMiddleware(app, prefix="/api/v1"),
        host="127.0.0.1",
        port=port,
        http=protocol,
        loop="asyncio",
        lifespan="off",
        log_config=None,
    )
    server = uvicorn.Server(config)
    serving = asyncio.create_task(server.serve(sockets=[listener]))
    writer = None
    try:
        async with asyncio.timeout(3):
            while not server.started:
                await asyncio.sleep(0)
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(b"GET /api/v1/archives/1/timelapse HTTP/1.1\r\nHost: localhost\r\n\r\n")
        await writer.drain()
        body = await asyncio.wait_for(reader.read(), 3)  # EOF proves uvicorn closed it
        await asyncio.wait_for(served.wait(), 3)
        assert b"before" in body
        assert b"PRIVATE-AFTER" not in body
        assert "Live media delivery stopped: category=authority_retired" in caplog.text
        assert "ASGI callable returned without completing response." in caplog.text
    finally:
        if writer is not None:
            writer.close()
            await writer.wait_closed()
        server.should_exit = True
        await asyncio.wait_for(serving, 3)
        listener.close()
