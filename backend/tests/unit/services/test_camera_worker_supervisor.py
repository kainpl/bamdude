import asyncio
import os

import pytest

from backend.app.services.camera_runtime import CameraCaptureRequest, WorkerCameraRuntime
from backend.app.services.camera_worker_supervisor import CameraWorkerSupervisor

_JPEG = b"\xff\xd8camera-worker-test\xff\xd9"


async def _serve_snapshot(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        await reader.readuntil(b"\r\n\r\n")
        writer.write(
            b"HTTP/1.1 200 OK\r\nContent-Type: image/jpeg\r\nContent-Length: "
            + str(len(_JPEG)).encode()
            + b"\r\nConnection: close\r\n\r\n"
            + _JPEG
        )
        await writer.drain()
    finally:
        writer.close()
        await writer.wait_closed()


@pytest.mark.asyncio
async def test_harness_worker_authenticates_answers_and_exits_cleanly():
    supervisor = CameraWorkerSupervisor()
    try:
        await supervisor.start()
        assert supervisor._containment is not None
        if os.name == "nt":
            assert supervisor._containment._job_handle is not None
        assert (await supervisor.request("heartbeat"))["result"] == {"state": "ready"}
        assert (await supervisor.request("status"))["result"]["camera_runtime"] == "not_started"
        assert (await supervisor.request("not_real"))["error"] == "unknown_operation"
        assert supervisor.bootstrap is not None
        assert supervisor.bootstrap.secret.hex() not in repr(supervisor.bootstrap)
    finally:
        await supervisor.stop()
    assert supervisor._containment is None


@pytest.mark.asyncio
async def test_harness_worker_can_restart_with_a_new_generation():
    first = CameraWorkerSupervisor()
    try:
        await first.start()
        first_generation = first.bootstrap.generation
    finally:
        await first.stop()

    second = CameraWorkerSupervisor()
    try:
        await second.start()
        assert second.bootstrap.generation != first_generation
        assert (await second.request("status"))["ok"] is True
    finally:
        await second.stop()


@pytest.mark.asyncio
async def test_harness_object_can_restart_after_a_clean_stop():
    supervisor = CameraWorkerSupervisor()
    try:
        await supervisor.start()
        first_generation = supervisor.bootstrap.generation
    finally:
        await supervisor.stop()

    try:
        await supervisor.start()
        assert supervisor.bootstrap is not None
        assert supervisor.bootstrap.generation != first_generation
        assert (await supervisor.request("status"))["ok"] is True
    finally:
        await supervisor.stop()


@pytest.mark.asyncio
async def test_harness_timeout_fallback_stops_the_contained_process_tree():
    supervisor = CameraWorkerSupervisor()
    try:
        await supervisor.start()
        assert supervisor.process is not None
        await supervisor._terminate_process_tree(supervisor.process)
        assert supervisor.process.returncode is not None
        if os.name == "nt":
            assert supervisor._containment is None
    finally:
        await supervisor.stop()


@pytest.mark.asyncio
async def test_worker_runtime_relays_an_external_snapshot_without_starting_main_camera_io():
    server = await asyncio.start_server(_serve_snapshot, host="127.0.0.1", port=0)
    port = server.sockets[0].getsockname()[1]
    runtime = WorkerCameraRuntime(CameraWorkerSupervisor())
    try:
        result = await runtime.capture(
            CameraCaptureRequest.external(
                url=f"http://127.0.0.1:{port}/snapshot.jpg", camera_type="snapshot", timeout=5
            )
        )
        assert result.frame == _JPEG
        assert result.source == "fresh"
        assert result.attempt_id is not None
        assert result.caller_wait_ms is not None
    finally:
        await runtime.stop()
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_worker_keeps_heartbeat_responsive_and_coalesces_concurrent_capture():
    requests = 0

    async def delayed_snapshot(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        nonlocal requests
        try:
            await reader.readuntil(b"\r\n\r\n")
            requests += 1
            await asyncio.sleep(0.1)
            writer.write(
                b"HTTP/1.1 200 OK\r\nContent-Type: image/jpeg\r\nContent-Length: "
                + str(len(_JPEG)).encode()
                + b"\r\nConnection: close\r\n\r\n"
                + _JPEG
            )
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(delayed_snapshot, host="127.0.0.1", port=0)
    port = server.sockets[0].getsockname()[1]
    supervisor = CameraWorkerSupervisor()
    runtime = WorkerCameraRuntime(supervisor)
    request = CameraCaptureRequest.external(
        url=f"http://127.0.0.1:{port}/snapshot.jpg", camera_type="snapshot", timeout=5
    )
    try:
        await supervisor.start()
        first = asyncio.create_task(runtime.capture(request))
        second = asyncio.create_task(runtime.capture(request))
        await asyncio.sleep(0.02)
        assert (await supervisor.request("heartbeat"))["result"] == {"state": "ready"}
        first_result, second_result = await asyncio.gather(first, second)
        assert first_result.frame == second_result.frame == _JPEG
        assert requests == 1
    finally:
        await runtime.stop()
        server.close()
        await server.wait_closed()
