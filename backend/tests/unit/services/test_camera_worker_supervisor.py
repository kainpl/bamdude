import asyncio
import logging
import os
import socket
import subprocess
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace

import psutil
import pytest

from backend.app.services.camera_runtime import CameraCaptureRequest, WorkerCameraRuntime
from backend.app.services.camera_worker_supervisor import CameraWorkerSupervisor, CameraWorkerUnavailable

_JPEG = b"\xff\xd8camera-worker-test\xff\xd9"


@pytest.mark.asyncio
async def test_shutdown_keeps_guardian_stdin_open_until_worker_exits(monkeypatch):
    entered = asyncio.Event()
    exited = asyncio.Event()
    closed = []

    class Process:
        pid = 12345
        returncode = None
        stdin = SimpleNamespace(close=lambda: closed.append("stdin"))

        async def wait(self):
            entered.set()
            await exited.wait()
            self.returncode = 0
            return 0

    async def reply(*_args, **_kwargs):
        return {"ok": True, "result": {"state": "stopping"}}

    async def close_servers():
        pass

    async def wait_closed():
        pass

    import backend.app.services.camera_worker_supervisor as module

    monkeypatch.setattr(module, "descendants", lambda _pid: [])
    monkeypatch.setattr(module, "kill_owned_group", lambda _pid: False)
    monkeypatch.setattr(module, "descendants_reaped", lambda _children, timeout: True)
    supervisor = CameraWorkerSupervisor(process=Process(), bootstrap=SimpleNamespace(generation="test"))
    supervisor._writer = SimpleNamespace(close=lambda: None, wait_closed=wait_closed)
    monkeypatch.setattr(supervisor, "request", reply)
    monkeypatch.setattr(supervisor, "_close_servers", close_servers)
    task = asyncio.create_task(supervisor.stop())
    await asyncio.wait_for(entered.wait(), 2)
    assert closed == []
    another = asyncio.create_task(supervisor.stop())
    task.cancel()
    exited.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 2)
    await asyncio.wait_for(another, 2)
    assert closed == ["stdin"]
    assert supervisor.last_stop_outcome == "graceful"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("initial", "reply_kind", "expected"),
    [
        (None, "none", "not_started"),
        (2, "none", "already_exited"),
        ("live", "nonzero", "unexpected_exit"),
        ("live", "zero_no_ack", "unexpected_exit"),
        ("live", "reject", "forced"),
        ("live", "force_zero", "forced"),
    ],
)
async def test_stop_outcome_uses_path_not_exit_code(monkeypatch, initial, reply_kind, expected):
    import backend.app.services.camera_worker_supervisor as module

    class Process:
        pid = 12345
        returncode = None if initial == "live" else initial
        stdin = SimpleNamespace(close=lambda: None)

    process = Process() if initial is not None else None
    supervisor = CameraWorkerSupervisor(process=process, bootstrap=SimpleNamespace(generation="test"))

    async def close_servers():
        pass

    async def request(*_args, **_kwargs):
        if reply_kind != "reject":
            process.returncode = 0 if reply_kind == "zero_no_ack" else 2
        return {"ok": False}

    async def force(_process, _deadline):
        process.returncode = 0
        return True

    async def wait_closed():
        pass

    if reply_kind in {"nonzero", "zero_no_ack", "reject"}:
        supervisor._writer = SimpleNamespace(close=lambda: None, wait_closed=wait_closed)
        monkeypatch.setattr(supervisor, "request", request)
    monkeypatch.setattr(supervisor, "_close_servers", close_servers)
    monkeypatch.setattr(supervisor, "_terminate_process_tree", force)
    monkeypatch.setattr(module, "descendants", lambda _pid: [])
    monkeypatch.setattr(module, "descendants_reaped", lambda _children, timeout: True)
    monkeypatch.setattr(module, "kill_owned_group", lambda _pid: False)
    await supervisor.stop()
    assert supervisor.last_stop_outcome == expected
    if reply_kind == "force_zero":
        assert process.returncode == 0


@pytest.mark.asyncio
async def test_unproven_reap_keeps_handle_and_reports_cleanup_failed(monkeypatch):
    import backend.app.services.camera_worker_supervisor as module

    process = SimpleNamespace(pid=12345, returncode=2, stdin=SimpleNamespace(close=lambda: None))
    supervisor = CameraWorkerSupervisor(process=process, bootstrap=SimpleNamespace(generation="test"))

    async def close_servers():
        pass

    monkeypatch.setattr(supervisor, "_close_servers", close_servers)
    monkeypatch.setattr(module, "descendants", lambda _pid: [])
    monkeypatch.setattr(module, "descendants_reaped", lambda _children, timeout: False)
    monkeypatch.setattr(module, "kill_owned_group", lambda _pid: False)
    with pytest.raises(CameraWorkerUnavailable, match="could not be reaped"):
        await supervisor.stop()
    assert supervisor.last_stop_outcome == "cleanup_failed"
    assert supervisor.process is process


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["ack_timeout", "exit_hung"])
async def test_graceful_budget_expires_into_bounded_force(monkeypatch, mode):
    import backend.app.services.camera_worker_supervisor as module

    released = asyncio.Event()

    class Process:
        pid = 12345
        returncode = None
        stdin = SimpleNamespace(close=lambda: None)

        async def wait(self):
            await released.wait()
            return self.returncode

    process = Process()
    supervisor = CameraWorkerSupervisor(process=process, bootstrap=SimpleNamespace(generation="test"))

    async def request(*_args, **_kwargs):
        if mode == "ack_timeout":
            await asyncio.Event().wait()
        return {"ok": True, "result": {"state": "stopping"}}

    async def force(_process, _deadline):
        process.returncode = 0
        released.set()
        return True

    async def close_servers():
        pass

    async def wait_closed():
        pass

    supervisor._writer = SimpleNamespace(close=lambda: None, wait_closed=wait_closed)
    monkeypatch.setattr(supervisor, "request", request)
    monkeypatch.setattr(supervisor, "_terminate_process_tree", force)
    monkeypatch.setattr(supervisor, "_close_servers", close_servers)
    monkeypatch.setattr(module, "_STOP_GRACE_SECONDS", 0.05)
    monkeypatch.setattr(module, "_STOP_HARD_SECONDS", 1.0)
    monkeypatch.setattr(module, "descendants", lambda _pid: [])
    monkeypatch.setattr(module, "descendants_reaped", lambda _children, timeout: True)
    monkeypatch.setattr(module, "kill_owned_group", lambda _pid: False)
    await asyncio.wait_for(supervisor.stop(), timeout=0.5)
    assert supervisor.last_stop_outcome == "forced" and process.returncode == 0


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
    process = None
    try:
        await supervisor.start()
        process = supervisor.process
        assert supervisor._containment is not None
        if os.name == "nt":
            assert supervisor._containment._job_handle is not None
        assert (await supervisor.request("heartbeat"))["result"] == {"state": "ready"}
        assert (await supervisor.request("status"))["result"]["camera_runtime"] == "worker"
        assert (await supervisor.request("not_real"))["error"] == "unknown_operation"
        assert supervisor.bootstrap is not None
        assert supervisor.bootstrap.secret.hex() not in repr(supervisor.bootstrap)
    finally:
        await supervisor.stop()
    assert supervisor._containment is None
    assert process is not None and process.returncode == 0
    assert supervisor.last_stop_outcome == "graceful"


@pytest.mark.asyncio
async def test_worker_tcp_probe_does_not_open_camera_socket_in_main():
    async def accept(_reader, writer):
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_server(accept, host="127.0.0.1", port=0)
    supervisor = CameraWorkerSupervisor()
    try:
        await supervisor.start()
        runtime = WorkerCameraRuntime(supervisor)
        assert (
            await runtime.probe_tcp("127.0.0.1", server.sockets[0].getsockname()[1], 2, identity=str(uuid.uuid4()))
            == "ok"
        )
        assert (await supervisor.request("heartbeat"))["result"]["state"] == "ready"
    finally:
        await supervisor.stop()
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_owner_hard_death_reaps_camera_guardian_and_child():
    script = (
        "import asyncio\n"
        "import psutil\n"
        "from backend.app.services.camera_worker_supervisor import CameraWorkerSupervisor\n"
        "async def run():\n"
        "    owner=CameraWorkerSupervisor()\n"
        "    await owner.start()\n"
        "    children=psutil.Process(owner.process.pid).children(recursive=True)\n"
        "    print(owner.process.pid, children[0].pid, flush=True)\n"
        "    await asyncio.Event().wait()\n"
        "asyncio.run(run())\n"
    )
    parent = subprocess.Popen(
        [sys.executable, "-c", script],
        cwd=Path(__file__).resolve().parents[4],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    guardian_pid = None
    worker_pid = None
    try:
        line = await asyncio.wait_for(asyncio.to_thread(parent.stdout.readline), timeout=15)
        guardian_pid, worker_pid = map(int, line.split())
        parent.kill()
        await asyncio.to_thread(parent.wait, 5)

        async def gone():
            while psutil.pid_exists(guardian_pid) or psutil.pid_exists(worker_pid):
                await asyncio.sleep(0.1)

        await asyncio.wait_for(gone(), timeout=8)
    finally:
        if parent.poll() is None:
            parent.kill()
            await asyncio.to_thread(parent.wait, 5)
        parent.stdout.close()


@pytest.mark.asyncio
async def test_owner_death_during_acknowledged_wait_still_reaps_guardian():
    script = (
        "import asyncio, json, psutil\n"
        "from backend.app.services.camera_worker_supervisor import CameraWorkerSupervisor\n"
        "async def run():\n"
        "    owner=CameraWorkerSupervisor()\n"
        "    await owner.start()\n"
        "    async def ack(*args, **kwargs): return {'ok':True,'result':{'state':'stopping'}}\n"
        "    owner.request=ack  # ACK without telling the real child to exit\n"
        "    stop=asyncio.create_task(owner.stop())\n"
        "    await asyncio.sleep(0.2)\n"
        "    tree=psutil.Process(owner.process.pid).children(recursive=True)\n"
        "    pids=[owner.process.pid]+[p.pid for p in tree if any(m in p.cmdline() for m in "
        "('backend.app.worker_guardian','backend.app.camera_worker'))]\n"
        "    print(json.dumps(pids), flush=True)\n"
        "    await stop\n"
        "asyncio.run(run())\n"
    )
    parent = subprocess.Popen(
        [sys.executable, "-c", script],
        cwd=Path(__file__).resolve().parents[4],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    try:
        import json

        line = await asyncio.wait_for(asyncio.to_thread(parent.stdout.readline), timeout=15)
        pids = json.loads(line)
        assert len(pids) >= 2
        parent.kill()
        await asyncio.to_thread(parent.wait, 5)

        async def gone():
            while any(psutil.pid_exists(pid) for pid in pids):
                await asyncio.sleep(0.1)

        await asyncio.wait_for(gone(), timeout=8)
    finally:
        if parent.poll() is None:
            parent.kill()
            await asyncio.to_thread(parent.wait, 5)
        parent.stdout.close()


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
async def test_unresponsive_worker_is_forced_without_graceful_wait():
    supervisor = CameraWorkerSupervisor()
    await supervisor.start()
    process = supervisor.process
    started = asyncio.get_running_loop().time()
    await supervisor.stop(cause="unresponsive")
    assert asyncio.get_running_loop().time() - started < 5
    assert supervisor.last_stop_outcome == "forced"
    assert process.returncode is not None


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
async def test_worker_runtime_relays_an_external_snapshot_without_starting_main_camera_io(caplog):
    caplog.set_level(logging.INFO, logger="backend.app.services.camera_worker_supervisor")
    server = await asyncio.start_server(_serve_snapshot, host="127.0.0.1", port=0)
    port = server.sockets[0].getsockname()[1]
    runtime = WorkerCameraRuntime(CameraWorkerSupervisor())
    try:
        await runtime.supervisor.start()
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
    assert "Camera worker ready:" in caplog.text
    assert "Camera worker stopped:" in caplog.text
    assert "Camera worker [backend.app.services.camera_metrics]: identity=" in caplog.text
    assert "Camera session completed:" in caplog.text
    assert f"http://127.0.0.1:{port}/snapshot.jpg" not in caplog.text


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


@pytest.mark.asyncio
async def test_worker_relays_latest_frames_for_an_external_live_lease():
    server = await asyncio.start_server(_serve_snapshot, host="127.0.0.1", port=0)
    port = server.sockets[0].getsockname()[1]
    supervisor = CameraWorkerSupervisor()
    queue = None
    try:
        await supervisor.start()
        lease_id, queue = await supervisor.subscribe_external(
            identity=str(uuid.uuid4()),
            url=f"http://127.0.0.1:{port}/snapshot.jpg",
            camera_type="snapshot",
            fps=5,
        )
        assert (await asyncio.wait_for(queue.get(), timeout=3)).frame == _JPEG
        await supervisor.unsubscribe(lease_id, queue)
        assert not supervisor._live_media_queues
    finally:
        await supervisor.stop()
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_normal_stop_joins_multiple_live_leases_and_shared_producer():
    server = await asyncio.start_server(_serve_snapshot, host="127.0.0.1", port=0)
    port = server.sockets[0].getsockname()[1]
    supervisor = CameraWorkerSupervisor()
    process = None
    try:
        await supervisor.start()
        process = supervisor.process
        shared_identity = str(uuid.uuid4())
        queues = []
        for identity in (shared_identity, shared_identity, str(uuid.uuid4())):
            _lease, queue = await supervisor.subscribe_external(
                identity=identity,
                url=f"http://127.0.0.1:{port}/snapshot.jpg",
                camera_type="snapshot",
                fps=5,
            )
            queues.append(queue)
        for queue in queues:
            assert (await asyncio.wait_for(queue.get(), timeout=3)).frame == _JPEG
    finally:
        await supervisor.stop()
        server.close()
        await server.wait_closed()
    assert supervisor.last_stop_outcome == "graceful"
    assert process is not None and process.returncode == 0
    assert not supervisor._live_media_queues


@pytest.mark.asyncio
async def test_worker_refuses_the_sixty_fifth_live_relay_before_opening_a_camera():
    supervisor = CameraWorkerSupervisor(process=object())
    supervisor._live_media_queues = {str(index): asyncio.Queue(maxsize=1) for index in range(64)}

    with pytest.raises(CameraWorkerUnavailable, match="live relay limit"):
        await supervisor.subscribe_external(
            identity=str(uuid.uuid4()),
            url="http://127.0.0.1/snapshot.jpg",
            camera_type="snapshot",
            fps=1,
        )


@pytest.mark.asyncio
async def test_worker_runtime_adapts_external_live_lease_to_mjpeg_and_releases_it():
    server = await asyncio.start_server(_serve_snapshot, host="127.0.0.1", port=0)
    port = server.sockets[0].getsockname()[1]
    supervisor = CameraWorkerSupervisor()
    runtime = WorkerCameraRuntime(supervisor)
    disconnect = asyncio.Event()
    await supervisor.start()
    stream = runtime.stream_external(
        identity=str(uuid.uuid4()),
        url=f"http://127.0.0.1:{port}/snapshot.jpg",
        camera_type="snapshot",
        fps=5,
        disconnect_event=disconnect,
    )
    try:
        chunk = await asyncio.wait_for(anext(stream), timeout=3)
        assert b"Content-Type: image/jpeg" in chunk
        assert _JPEG in chunk
    finally:
        disconnect.set()
        await stream.aclose()
        assert not supervisor._live_media_queues
        await runtime.stop()
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_worker_owns_and_releases_transparent_raw_camera_proxy():
    async def echo(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while data := await reader.read(1024):
                writer.write(data)
                await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    target = await asyncio.start_server(echo, host="127.0.0.1", port=0)
    target_port = target.sockets[0].getsockname()[1]
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        listen_port = reservation.getsockname()[1]

    supervisor = CameraWorkerSupervisor()
    try:
        await supervisor.start()
        lease_id = await supervisor.start_raw_proxy(
            identity=str(uuid.uuid4()),
            bind_address="127.0.0.1",
            listen_port=listen_port,
            target_host="127.0.0.1",
            target_port=target_port,
        )
        reader, writer = await asyncio.open_connection("127.0.0.1", listen_port)
        writer.write(b"raw-camera")
        await writer.drain()
        assert await reader.readexactly(10) == b"raw-camera"
        writer.close()
        await writer.wait_closed()

        await supervisor.stop_raw_proxy(lease_id)
        with pytest.raises(OSError):
            await asyncio.open_connection("127.0.0.1", listen_port)
        await supervisor.start_raw_proxy(
            identity=str(uuid.uuid4()),
            bind_address="127.0.0.1",
            listen_port=listen_port,
            target_host="127.0.0.1",
            target_port=target_port,
        )
    finally:
        await supervisor.stop()
        target.close()
        await target.wait_closed()
    assert supervisor.last_stop_outcome == "graceful"
    with pytest.raises(OSError):
        await asyncio.open_connection("127.0.0.1", listen_port)
