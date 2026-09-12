"""Attempt boundaries: real proxy sockets with controlled ffmpeg processes."""

import asyncio
import sys
from dataclasses import replace
from unittest.mock import AsyncMock, Mock
from urllib.parse import urlparse

import pytest

from backend.app.api.routes import camera as route
from backend.app.services import camera, camera_cleanup, camera_tls, external_camera
from backend.app.services.camera_cleanup import CameraAttempt, CameraCleanupError
from backend.tests.unit.services.test_camera_tls_lifecycle import TLSPeer, eventually, run, tls_context

FRAME = b"\xff\xd8" + b"f" * 120 + b"\xff\xd9"


class FakeProcess:
    def __init__(self, pid, *, immediate=False, mode="eof"):
        self.pid = pid
        self.returncode = 1 if immediate else None
        self.stderr = asyncio.StreamReader()
        self.stdout = Mock(read=AsyncMock(side_effect=[FRAME, TimeoutError() if mode == "timeout" else b""]))
        self.communicate = AsyncMock(return_value=(FRAME, b""))
        self.client = None
        self.terminate = Mock()
        self.kill = Mock()
        self.reaped = False
        if immediate:
            self.stderr.feed_data(b"ffmpeg failed to start\n")
            self.stderr.feed_eof()

    async def wait(self):
        self.returncode = -15
        self.reaped = True
        if self.client is not None:
            self.client.close()
            await self.client.wait_closed()
        self.stderr.feed_eof()
        return self.returncode


@pytest.fixture
def processes(monkeypatch):
    created, servers, events = [], [], []
    target_port = None
    modes = []

    async def proxy(host, port):
        assert target_port is not None
        local_port, server = await camera_tls.create_tls_proxy("127.0.0.1", target_port)
        servers.append(server)
        return local_port, server

    async def spawn(*cmd, **kwargs):
        # This executes at the very next subprocess acquisition, before any
        # fixture cleanup. The old transports and tasks must already be gone.
        for old in servers[:-1]:
            state = camera_tls._proxy_states[old]
            assert state.closed and not state.connections
        for old in created:
            assert old.returncode is not None
        parsed = urlparse(cmd[cmd.index("-i") + 1])
        process = FakeProcess(10000 + len(created), **(modes.pop(0) if modes else {}))
        reader, process.client = await asyncio.open_connection(parsed.hostname, parsed.port)
        assert await asyncio.wait_for(reader.readline(), 1) == b"ready\n"
        created.append(process)
        events.append("spawn")
        return process

    monkeypatch.setattr(camera, "create_tls_proxy", proxy)
    monkeypatch.setattr(route, "create_tls_proxy", proxy)
    monkeypatch.setattr(camera, "get_ffmpeg_path", lambda: "ffmpeg-test")
    monkeypatch.setattr(route, "get_ffmpeg_path", lambda: "ffmpeg-test")
    monkeypatch.setattr(external_camera, "get_ffmpeg_path", lambda: "ffmpeg-test")
    monkeypatch.setattr(route, "rtsp_socket_timeout_flag", lambda: "timeout")
    monkeypatch.setattr(camera, "rtsp_socket_timeout_flag", lambda: "timeout")
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    profile = replace(route.get_camera_profile("X1C"), rtsp_reconnect_delay=0.01, rtsp_reconnect_max=3)
    monkeypatch.setattr(route, "get_camera_profile", lambda model: profile)

    class Farm:
        async def __aenter__(self):
            nonlocal target_port
            self.peers = []

            def factory():
                peer = TLSPeer(self.context)
                self.peers.append(peer)
                return peer

            self.target = await asyncio.get_running_loop().create_server(factory, "127.0.0.1", 0)
            target_port = self.target.sockets[0].getsockname()[1]
            return self

        async def __aexit__(self, *args):
            for process in created:
                if process.client:
                    process.client.transport.abort()
                camera_cleanup._unreaped_processes.pop(process.pid, None)
            for peer in self.peers:
                peer.transport.abort()
            for server in servers:
                try:
                    await camera.close_tls_proxy(server)
                except TimeoutError:
                    pass
            self.target.close()
            await self.target.wait_closed()
            route._active_streams.clear()
            route._spawned_ffmpeg_pids.clear()
            route._disconnect_events.clear()

        def __call__(self, context):
            self.context = context
            return self

    return Farm(), created, servers, modes


@pytest.mark.parametrize("mode", ["eof", "timeout"])
def test_reconnect_closes_old_proxy_before_spawn_including_immediate_failure(run, tls_context, processes, mode):
    farm, created, servers, modes = processes
    modes.extend([{"mode": mode}, {"immediate": True}, {}])

    async def scenario():
        async with farm(tls_context):
            stream = route.generate_rtsp_mjpeg_stream("127.0.0.1", "secret", "X1C", stream_id="test", printer_id=1)
            assert FRAME in await anext(stream)
            assert FRAME in await anext(stream)  # after EOF/timeout and one immediate-exit retry
            assert len(created) == 3
            assert route._last_frames[1] == FRAME
            await stream.aclose()
            assert all(camera_tls._proxy_states[server].closed for server in servers)
            assert all(process.returncode is not None for process in created)
            await eventually(lambda: not camera_cleanup._owned_tasks)
            assert not route._active_streams and not route._spawned_ffmpeg_pids

    run(scenario())


@pytest.mark.parametrize("action", ["disconnect", "cancel"])
def test_stop_during_backoff_cannot_spawn_again(run, tls_context, processes, monkeypatch, action):
    farm, created, servers, _ = processes
    original_sleep = asyncio.sleep
    backoff = asyncio.Event()

    async def sleep(delay):
        if delay == 0.01:
            backoff.set()
            await original_sleep(0.05)
        else:
            await original_sleep(delay)

    monkeypatch.setattr(asyncio, "sleep", sleep)

    async def scenario():
        async with farm(tls_context):
            stop = asyncio.Event()
            stream = route.generate_rtsp_mjpeg_stream(
                "127.0.0.1", "secret", "X1C", stream_id="stop", disconnect_event=stop
            )
            await anext(stream)
            next_frame = asyncio.create_task(anext(stream))
            await asyncio.wait_for(backoff.wait(), 2)
            if action == "cancel":
                next_frame.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await next_frame
            else:
                stop.set()
                with pytest.raises(StopAsyncIteration):
                    await next_frame
            await stream.aclose()
            assert len(created) == 1 and camera_tls._proxy_states[servers[0]].closed

    run(scenario())


@pytest.mark.parametrize("owner", ["capture", "external_capture", "stream", "external_stream"])
def test_spawn_failure_closes_each_owner_proxy(run, tls_context, processes, monkeypatch, owner, caplog):
    farm, created, servers, _ = processes
    monkeypatch.setattr(
        asyncio, "create_subprocess_exec", AsyncMock(side_effect=FileNotFoundError("rtsp://bblp:secret@localhost/live"))
    )

    async def scenario():
        async with farm(tls_context):
            if owner == "capture":
                assert await camera._capture_camera_frame_bytes_uncoalesced("127.0.0.1", "secret", "X1C") is None
            elif owner == "external_capture":
                assert await external_camera._capture_rtsp_frame("rtsps://bblp:secret@127.0.0.1/live", 1) is None
            else:
                stream = (
                    route.generate_rtsp_mjpeg_stream("127.0.0.1", "secret", "X1C")
                    if owner == "stream"
                    else external_camera._stream_rtsp("rtsps://bblp:secret@127.0.0.1/live", 5)
                )
                async for _ in stream:
                    pass
            assert len(servers) == 1 and camera_tls._proxy_states[servers[0]].closed
            assert not created and "secret" not in caplog.text

    run(scenario())


@pytest.mark.parametrize("owner", ["capture", "external_capture", "stream", "external_stream"])
def test_cancellation_reaps_process_then_closes_proxy(run, tls_context, processes, owner, monkeypatch):
    farm, created, servers, _ = processes

    async def scenario():
        async with farm(tls_context):
            blocked = asyncio.Event()

            async def stall(*args):
                blocked.set()
                await asyncio.Event().wait()

            original_spawn = asyncio.create_subprocess_exec

            async def spawn(*args, **kwargs):
                process = await original_spawn(*args, **kwargs)
                process.communicate = stall
                process.stdout.read = stall
                return process

            monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
            stream = None
            if owner == "capture":
                task = asyncio.create_task(camera._capture_camera_frame_bytes_uncoalesced("127.0.0.1", "secret", "X1C"))
            elif owner == "external_capture":
                task = asyncio.create_task(
                    external_camera._capture_rtsp_frame("rtsps://bblp:secret@127.0.0.1/live", 10)
                )
            else:
                stream = (
                    route.generate_rtsp_mjpeg_stream("127.0.0.1", "secret", "X1C", stream_id="cancel")
                    if owner == "stream"
                    else external_camera._stream_rtsp("rtsps://bblp:secret@127.0.0.1/live", 5)
                )
                task = asyncio.create_task(anext(stream))
            await asyncio.wait_for(blocked.wait(), 2)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            if stream:
                await stream.aclose()
            assert created[0].reaped and camera_tls._proxy_states[servers[0]].closed
            assert not camera._active_capture_pids
            await eventually(lambda: not camera_cleanup._owned_tasks)

    run(scenario())


@pytest.mark.parametrize("fault", ["unreaped", "tls_timeout"])
def test_cleanup_failure_stops_stream_retries(run, tls_context, processes, monkeypatch, fault):
    farm, created, servers, _ = processes

    async def scenario():
        async with farm(tls_context):
            stream = route.generate_rtsp_mjpeg_stream("127.0.0.1", "secret", "X1C", stream_id="failed")
            await anext(stream)
            if fault == "unreaped":

                async def unconfirmed(*args):
                    raise TimeoutError()

                created[0].wait = unconfirmed
            else:
                original_close = camera_tls.close_tls_proxy

                async def failed_close(server):
                    await original_close(server)
                    raise TimeoutError("fault injection")

                monkeypatch.setattr(camera_tls, "close_tls_proxy", failed_close)
            with pytest.raises(StopAsyncIteration):
                await anext(stream)
            assert len(created) == 1
            assert camera_tls._proxy_states[servers[0]].closed
            assert not route._active_streams and not route._spawned_ffmpeg_pids
            if fault == "unreaped":
                assert camera_cleanup._unreaped_processes[created[0].pid] is created[0]

    run(scenario())


@pytest.mark.parametrize("process_error", [OSError, asyncio.CancelledError])
def test_process_and_drain_errors_cannot_skip_proxy_or_replace_original(run, monkeypatch, process_error):
    async def scenario():
        proxy = object()
        closed = AsyncMock()
        monkeypatch.setattr(camera_tls, "close_tls_proxy", closed)
        original = ValueError("original operation failure")
        with pytest.raises(ValueError) as result:
            async with CameraAttempt(
                "test", stop_process=AsyncMock(side_effect=process_error("process cleanup"))
            ) as attempt:
                attempt.proxy = proxy
                attempt.process = Mock(returncode=0)
                attempt.stderr = Mock(aclose=AsyncMock(side_effect=RuntimeError("drain cleanup")))
                raise original
        assert result.value is original
        closed.assert_awaited_once_with(proxy)

    run(scenario())


@pytest.mark.asyncio
@pytest.mark.skipif(
    sys.platform == "win32" and not hasattr(asyncio, "ProactorEventLoop"), reason="needs subprocess support"
)
async def test_real_subprocess_is_reaped_on_cancellation():
    started = asyncio.Event()
    process = None

    async def capture():
        nonlocal process
        async with CameraAttempt("local-python-test") as attempt:
            process = attempt.process = await asyncio.create_subprocess_exec(
                sys.executable, "-c", "import time; time.sleep(60)"
            )
            started.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(capture())
    await asyncio.wait_for(started.wait(), 3)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert process.returncode is not None


@pytest.mark.asyncio
@pytest.mark.skipif(
    sys.platform == "win32" and not hasattr(asyncio, "ProactorEventLoop"), reason="needs subprocess support"
)
async def test_cleanup_drains_full_stdout_before_waiting_for_exit():
    """A noisy ffmpeg must not force the two-second terminate timeout.

    The child is a stand-in for ffmpeg writing MJPEG frames faster than the
    client consumes them.  There is deliberately no stdout reader until the
    attempt starts teardown.
    """
    import time

    process = None
    started = asyncio.Event()
    started_at = time.monotonic()
    async with CameraAttempt("stdout-drain-test") as attempt:
        process = attempt.process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-u",
            "-c",
            "import sys; chunk = b'x' * 65536\nwhile True:\n sys.stdout.buffer.write(chunk); sys.stdout.buffer.flush()",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        started.set()
        await asyncio.sleep(0.05)

    assert started.is_set()
    assert process.returncode is not None
    assert time.monotonic() - started_at < 1.5


@pytest.mark.parametrize("owner", ["capture", "external_capture"])
@pytest.mark.parametrize("failure", ["timeout", "tls_cleanup"])
def test_capture_failure_reaps_and_returns_none(run, tls_context, processes, monkeypatch, owner, failure):
    farm, created, servers, _ = processes

    async def scenario():
        async with farm(tls_context):
            original_spawn = asyncio.create_subprocess_exec

            async def spawn(*args, **kwargs):
                process = await original_spawn(*args, **kwargs)
                if failure == "timeout":
                    process.communicate.side_effect = TimeoutError()
                else:
                    process.returncode = 0
                return process

            monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
            if failure == "tls_cleanup":
                original_close = camera_tls.close_tls_proxy

                async def close(server):
                    await original_close(server)
                    raise TimeoutError("injected failure")

                monkeypatch.setattr(camera_tls, "close_tls_proxy", close)
            result = (
                await camera._capture_camera_frame_bytes_uncoalesced("127.0.0.1", "secret", "X1C")
                if owner == "capture"
                else await external_camera._capture_rtsp_frame("rtsps://bblp:secret@127.0.0.1/live", 1)
            )
            assert result is None
            assert created[0].returncode is not None
            assert camera_tls._proxy_states[servers[0]].closed
            assert not camera._active_capture_pids

    run(scenario())


def test_external_direct_fallback_is_preserved(run, monkeypatch, caplog):
    async def scenario():
        url = "rtsps://user:secret@127.0.0.1:322/live?channel=1"
        monkeypatch.setattr(external_camera, "get_ffmpeg_path", lambda: "ffmpeg-test")
        monkeypatch.setattr(camera, "create_tls_proxy", AsyncMock(side_effect=OSError(url)))
        process = FakeProcess(123)
        process.returncode = 0
        spawn = AsyncMock(return_value=process)
        monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
        assert await external_camera._capture_rtsp_frame(url, 1) == FRAME
        args = spawn.call_args.args
        assert args[args.index("-i") + 1] == url
        assert "secret" not in caplog.text

    run(scenario())


def test_existing_janitor_reaps_explicit_handoff_without_proc_scan(run, monkeypatch):
    async def scenario():
        process = FakeProcess(98765)
        camera_cleanup._unreaped_processes[process.pid] = process
        monkeypatch.setattr(route, "_scan_bambu_ffmpeg_pids", lambda: [])
        try:
            await route.cleanup_orphaned_streams()
            assert process.reaped and process.pid not in camera_cleanup._unreaped_processes
        finally:
            camera_cleanup._unreaped_processes.pop(process.pid, None)

    run(scenario())
