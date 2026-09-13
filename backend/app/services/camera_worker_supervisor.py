"""Harness-only supervisor for the future out-of-process camera runtime.

This is not wired into application settings yet.  It exists to prove the child
boundary, bootstrap authentication and bounded shutdown before moving any
physical camera owner into another process.
"""

from __future__ import annotations

import asyncio
import os
import secrets
import signal
import subprocess
import sys
import uuid
from dataclasses import dataclass, field

from backend.app.services.camera_worker_containment import (
    CameraWorkerContainmentError,
    WorkerContainment,
)
from backend.app.services.camera_worker_protocol import (
    CameraWorkerProtocolError,
    WorkerBootstrap,
    make_reply,
    make_request,
    read_control,
    validate_reply,
    write_control,
)

_STARTUP_TIMEOUT_SECONDS = 5.0
_REQUEST_TIMEOUT_SECONDS = 2.0
_SHUTDOWN_TIMEOUT_SECONDS = 5.0


class CameraWorkerUnavailable(RuntimeError):
    """The harness child did not authenticate or stopped responding."""


@dataclass
class CameraWorkerSupervisor:
    """Own exactly one harness child and its loopback listeners."""

    process: asyncio.subprocess.Process | None = None
    bootstrap: WorkerBootstrap | None = None
    _control_server: asyncio.AbstractServer | None = None
    _media_server: asyncio.AbstractServer | None = None
    _reader: asyncio.StreamReader | None = None
    _writer: asyncio.StreamWriter | None = None
    _ready: asyncio.Event = field(default_factory=asyncio.Event)
    _connection_closed: asyncio.Event = field(default_factory=asyncio.Event)
    _request_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    _stderr_task: asyncio.Task[None] | None = None
    _stderr_bytes: int = 0
    _containment: WorkerContainment | None = None

    async def start(self) -> None:
        if self.process is not None:
            raise RuntimeError("camera worker harness is already started")
        self._prepare_start()
        self._control_server = await asyncio.start_server(self._accept_control, host="127.0.0.1", port=0)
        self._media_server = await asyncio.start_server(self._reject_media, host="127.0.0.1", port=0)
        control_port = self._listener_port(self._control_server)
        media_port = self._listener_port(self._media_server)
        self.bootstrap = WorkerBootstrap.create(control_port=control_port, media_port=media_port)

        creation_kwargs: dict = {}
        if os.name == "nt":
            creation_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            creation_kwargs["start_new_session"] = True
        self.process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "backend.app.camera_worker",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
            **creation_kwargs,
        )
        try:
            # The child imports only its IPC harness before it receives stdin;
            # attaching here precedes any future FFmpeg/camera spawn.
            self._containment = WorkerContainment.attach(self.process.pid)
        except CameraWorkerContainmentError as exc:
            await self._terminate_uncontained_process()
            await self._close_servers()
            raise CameraWorkerUnavailable("camera worker process containment is unavailable") from exc
        self._stderr_task = asyncio.create_task(self._drain_stderr(), name="camera-worker-harness-stderr")
        assert self.process.stdin is not None
        self.process.stdin.write(self.bootstrap.to_bytes())
        await self.process.stdin.drain()
        self.process.stdin.close()
        # Proactor's anonymous-pipe ``wait_closed`` can hang after a successful
        # drain on Windows.  The child already has the whole bounded bootstrap;
        # close is enough to prevent a later write from extending its input.
        try:
            await asyncio.wait_for(self._ready.wait(), timeout=_STARTUP_TIMEOUT_SECONDS)
        except TimeoutError as exc:
            await self.stop()
            raise CameraWorkerUnavailable("camera worker did not authenticate") from exc

    async def request(self, operation: str, payload: dict | None = None) -> dict:
        """Send one serialised request; the harness has one control reader."""

        if self.bootstrap is None or self._reader is None or self._writer is None:
            raise CameraWorkerUnavailable("camera worker is not connected")
        request_id = str(uuid.uuid4())
        async with self._request_lock:
            try:
                await write_control(
                    self._writer,
                    make_request(
                        generation=self.bootstrap.generation,
                        request_id=request_id,
                        operation=operation,
                        payload=payload or {},
                    ),
                )
                reply = await asyncio.wait_for(read_control(self._reader), timeout=_REQUEST_TIMEOUT_SECONDS)
                return validate_reply(reply, generation=self.bootstrap.generation, request_id=request_id)
            except (CameraWorkerProtocolError, OSError, TimeoutError) as exc:
                raise CameraWorkerUnavailable("camera worker control request failed") from exc

    async def stop(self) -> None:
        """Bound normal shutdown, then terminate only this supervisor's child."""

        process = self.process
        if process is not None and process.returncode is None:
            try:
                await self.request("shutdown")
            except CameraWorkerUnavailable:
                pass
        # Release the accepted handler and close the parent's half before
        # waiting for the child.  ``Server.wait_closed`` otherwise waits for a
        # handler that is deliberately holding this connection open for
        # ``request()`` ownership.
        self._close_listeners()
        if self._writer is not None:
            self._writer.close()
            try:
                await self._writer.wait_closed()
            except OSError:
                pass
        if process is not None and process.returncode is None:
            try:
                await asyncio.wait_for(process.wait(), timeout=_SHUTDOWN_TIMEOUT_SECONDS)
            except TimeoutError:
                await self._terminate_process_tree(process)
        await self._close_servers()
        if self._stderr_task is not None:
            await self._stderr_task
        if self._containment is not None:
            self._containment.close()
            self._containment = None
        self.process = None
        self._reader = None
        self._writer = None

    async def _terminate_uncontained_process(self) -> None:
        """Clean up a child when attaching its required containment failed."""

        process = self.process
        if process is not None and process.returncode is None:
            process.terminate()
            await process.wait()
        self.process = None

    async def _terminate_process_tree(self, process: asyncio.subprocess.Process) -> None:
        """Escalate a stuck worker without leaving its future producers behind."""

        if os.name == "nt" and self._containment is not None:
            # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE ends every descendant, unlike
            # Process.terminate(), which would only end the worker parent.
            self._containment.close()
            self._containment = None
        elif os.name != "nt":
            os.killpg(process.pid, signal.SIGTERM)
        else:  # pragma: no cover - Windows attaches a Job Object before bootstrap
            process.terminate()
        try:
            await asyncio.wait_for(process.wait(), timeout=_SHUTDOWN_TIMEOUT_SECONDS)
            return
        except TimeoutError:
            pass

        if os.name != "nt":
            os.killpg(process.pid, signal.SIGKILL)
        else:  # pragma: no cover - defensive fallback after Job Object close
            process.kill()
        await process.wait()

    async def _accept_control(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            if self._writer is not None:
                return
            request = await asyncio.wait_for(read_control(reader), timeout=_STARTUP_TIMEOUT_SECONDS)
            if not self._is_valid_hello(request):
                await write_control(
                    writer,
                    make_reply(
                        generation=self.bootstrap.generation if self.bootstrap else str(uuid.uuid4()),
                        request_id=request.get("request_id", str(uuid.uuid4())),
                        ok=False,
                        error="authentication_failed",
                    ),
                )
                return
            assert self.bootstrap is not None
            self._reader, self._writer = reader, writer
            await write_control(
                writer,
                make_reply(
                    generation=self.bootstrap.generation,
                    request_id=request["request_id"],
                    ok=True,
                    result={"state": "ready"},
                ),
            )
            self._ready.set()
            await self._connection_closed.wait()
        except (CameraWorkerProtocolError, OSError, TimeoutError):
            return
        finally:
            if writer is not self._writer:
                writer.close()
                try:
                    await writer.wait_closed()
                except OSError:
                    pass

    async def _reject_media(self, _reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        # A separate listener exists so media can never head-of-line-block control.
        # No media contract is enabled until CW-03 owns physical producers.
        writer.close()
        try:
            await writer.wait_closed()
        except OSError:
            pass

    def _is_valid_hello(self, request: dict) -> bool:
        if self.bootstrap is None:
            return False
        if request.get("generation") != self.bootstrap.generation or request.get("operation") != "hello":
            return False
        secret = request.get("payload", {}).get("secret")
        return isinstance(secret, str) and secrets.compare_digest(secret, self.bootstrap.secret.hex())

    @staticmethod
    def _listener_port(server: asyncio.AbstractServer) -> int:
        socket = next(iter(server.sockets or []), None)
        if socket is None:
            raise CameraWorkerUnavailable("camera worker loopback listener has no socket")
        return int(socket.getsockname()[1])

    async def _drain_stderr(self) -> None:
        if self.process is None or self.process.stderr is None:
            return
        while True:
            chunk = await self.process.stderr.read(4096)
            if not chunk:
                return
            # The harness retains no child stderr.  A later camera worker must
            # use a bounded, redacted diagnostic buffer before it gains URLs.
            self._stderr_bytes = min(32 * 1024, self._stderr_bytes + len(chunk))

    def _close_listeners(self) -> None:
        self._connection_closed.set()
        for server in (self._control_server, self._media_server):
            if server is not None:
                server.close()

    async def _close_servers(self) -> None:
        self._close_listeners()
        await asyncio.gather(
            *(server.wait_closed() for server in (self._control_server, self._media_server) if server is not None)
        )

    def _prepare_start(self) -> None:
        """Allow a stopped harness object to start a fresh authenticated child."""

        self.bootstrap = None
        self._reader = None
        self._writer = None
        self._ready.clear()
        self._connection_closed.clear()
        self._stderr_task = None
        self._stderr_bytes = 0
