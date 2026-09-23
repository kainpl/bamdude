"""Supervisor for the contained, authenticated camera worker."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import secrets
import signal
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from backend.app.services.camera_metrics import CameraCaptureResult
from backend.app.services.camera_worker_capture import WorkerCaptureCommand
from backend.app.services.camera_worker_logging import MAX_LINE_BYTES, decode_worker_log
from backend.app.services.camera_worker_media import (
    WorkerMediaFrame,
    read_media_frame,
    read_media_hello,
    valid_media_hello,
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
from backend.app.services.worker_containment import WorkerContainment, WorkerContainmentError
from backend.app.services.worker_process import descendants, descendants_reaped, kill_owned_group

_STARTUP_TIMEOUT_SECONDS = 20.0
_REQUEST_TIMEOUT_SECONDS = 2.0
_SHUTDOWN_TIMEOUT_SECONDS = 5.0
_MAX_PENDING_CONTROL_REQUESTS = 256
# A live producer may legitimately run at 1 FPS.  Keep its relay open longer
# than the control/snapshot timeout while still detecting a lost producer.
_LIVE_MEDIA_IDLE_SECONDS = 20.0
_MAX_LIVE_MEDIA_QUEUES = 64
logger = logging.getLogger(__name__)

LiveMediaQueue = asyncio.Queue[WorkerMediaFrame | None]


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
    _control_write_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    _pending_requests: dict[str, asyncio.Future[dict]] = field(default_factory=dict)
    _control_reader_task: asyncio.Task[None] | None = None
    _stderr_task: asyncio.Task[None] | None = None
    _stderr_bytes: int = 0
    _containment: WorkerContainment | None = None
    _media_waiters: dict[str, asyncio.Future[WorkerMediaFrame]] = field(default_factory=dict)
    _live_media_queues: dict[str, LiveMediaQueue] = field(default_factory=dict)

    async def start(self) -> None:
        if self.process is not None:
            raise RuntimeError("camera worker harness is already started")
        self._prepare_start()
        self._control_server = await asyncio.start_server(self._accept_control, host="127.0.0.1", port=0)
        self._media_server = await asyncio.start_server(self._accept_media, host="127.0.0.1", port=0)
        control_port = self._listener_port(self._control_server)
        media_port = self._listener_port(self._media_server)
        self.bootstrap = WorkerBootstrap.create(control_port=control_port, media_port=media_port)

        creation_kwargs: dict = {}
        if os.name == "nt":
            creation_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
        else:
            creation_kwargs["start_new_session"] = True
        # ⚠️ ``cwd`` explicitly, not inherited. ``-m backend.app.camera_worker``
        # needs the repository root importable, and without this the child got
        # whatever directory the parent happened to be started from - which is
        # the root when uvicorn runs the app, and is NOT when pytest runs from
        # ``backend/``: the child then died at import and the supervisor could
        # only report "did not authenticate". Derived from this file rather than
        # from the process, so the answer does not depend on the caller. On the
        # Windows installer this resolves to the same directory the service
        # already uses as its working directory, so nothing there changes.
        self.process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "backend.app.worker_guardian",
            cwd=str(Path(__file__).resolve().parents[3]),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
            # stderr is a UTF-8 JSON-lines channel, independent of the host's
            # Windows console code page.
            env=self._worker_env(),
            **creation_kwargs,
        )
        try:
            # The child imports only its IPC harness before it receives stdin;
            # attaching here precedes any future FFmpeg/camera spawn.
            self._containment = WorkerContainment.attach(self.process.pid)
        except WorkerContainmentError as exc:
            await self._terminate_uncontained_process()
            await self._close_servers()
            raise CameraWorkerUnavailable("camera worker process containment is unavailable") from exc
        self._stderr_task = asyncio.create_task(self._drain_stderr(), name="camera-worker-harness-stderr")
        assert self.process.stdin is not None
        self.process.stdin.write(
            json.dumps(
                {
                    "module": "backend.app.camera_worker",
                    "camera_frame": base64.b64encode(self.bootstrap.to_bytes()).decode("ascii"),
                }
            ).encode("ascii")
            + b"\n"
        )
        await self.process.stdin.drain()
        # Keep this pipe open: its EOF is the guardian's owner-death signal.
        try:
            await asyncio.wait_for(self._ready.wait(), timeout=_STARTUP_TIMEOUT_SECONDS)
        except TimeoutError as exc:
            await self.stop()
            raise CameraWorkerUnavailable("camera worker did not authenticate") from exc
        logger.info("Camera worker ready: pid=%s generation=%s", self.process.pid, self.bootstrap.generation)

    async def request(
        self, operation: str, payload: dict | None = None, *, timeout: float = _REQUEST_TIMEOUT_SECONDS
    ) -> dict:
        """Send a request without making a slow capture block control replies."""

        if self.bootstrap is None or self._reader is None or self._writer is None:
            raise CameraWorkerUnavailable("camera worker is not connected")
        request_id = str(uuid.uuid4())
        future: asyncio.Future[dict] = asyncio.get_running_loop().create_future()
        async with self._control_write_lock:
            if len(self._pending_requests) >= _MAX_PENDING_CONTROL_REQUESTS:
                raise CameraWorkerUnavailable("camera worker control queue is full")
            self._pending_requests[request_id] = future
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
            except (CameraWorkerProtocolError, OSError, TimeoutError) as exc:
                self._pending_requests.pop(request_id, None)
                raise CameraWorkerUnavailable("camera worker control request failed") from exc
        try:
            return await asyncio.wait_for(asyncio.shield(future), timeout=timeout)
        except (CameraWorkerProtocolError, OSError, TimeoutError) as exc:
            raise CameraWorkerUnavailable("camera worker control request failed") from exc
        finally:
            pending = self._pending_requests.pop(request_id, None)
            if pending is not None and not pending.done():
                pending.cancel()

    async def capture(self, request) -> CameraCaptureResult:
        """Run one physical capture in the child and receive its bounded JPEG relay.

        This is the only production camera-capture path. The caller supplies
        the already-authorised in-memory request; its
        credentials travel only over the authenticated loopback control channel.
        """

        if self.process is None or self.process.returncode is not None:
            raise CameraWorkerUnavailable("camera worker is not ready")
        assert self.bootstrap is not None
        started = time.monotonic()
        session_id = str(uuid.uuid4())
        command = WorkerCaptureCommand.from_runtime_request(request, media_session_id=session_id)
        future: asyncio.Future[WorkerMediaFrame] = asyncio.get_running_loop().create_future()
        self._media_waiters[session_id] = future
        try:
            reply = await self.request("capture", command.to_payload(), timeout=request.timeout + 2.0)
            result = reply["result"]
            if not reply["ok"] or result.get("frame_available") is not True:
                return CameraCaptureResult(
                    None,
                    None,
                    _optional_string(result.get("attempt_id")),
                    _optional_timing(result.get("first_frame_ms")),
                    cleanup_ms=_optional_timing(result.get("cleanup_ms")),
                ).for_caller(started)
            remaining = request.timeout - (time.monotonic() - started)
            if remaining <= 0:
                raise CameraWorkerUnavailable("camera worker media relay timed out")
            media = await asyncio.wait_for(future, timeout=remaining)
            return CameraCaptureResult(
                media.frame,
                media.source,
                media.attempt_id,
                media.first_frame_ms,
                cleanup_ms=media.cleanup_ms,
            ).for_caller(started)
        except (CameraWorkerProtocolError, TimeoutError) as exc:
            raise CameraWorkerUnavailable("camera worker capture failed") from exc
        finally:
            waiter = self._media_waiters.pop(session_id, None)
            if waiter is not None and not waiter.done():
                waiter.cancel()

    async def subscribe_external(
        self, *, identity: str, url: str, camera_type: str, fps: int
    ) -> tuple[str, LiveMediaQueue]:
        """Start one worker-owned external producer and return its latest-frame relay."""

        if len(self._live_media_queues) >= _MAX_LIVE_MEDIA_QUEUES:
            raise CameraWorkerUnavailable("camera worker live relay limit reached")
        if self.process is None or self.process.returncode is not None:
            raise CameraWorkerUnavailable("camera worker is not ready")
        session_id = str(uuid.uuid4())
        queue: LiveMediaQueue = asyncio.Queue(maxsize=1)
        self._live_media_queues[session_id] = queue
        try:
            reply = await self.request(
                "subscribe",
                {
                    "identity": identity,
                    "media_session_id": session_id,
                    "url": url,
                    "camera_type": camera_type,
                    "fps": fps,
                },
            )
            lease_id = reply["result"].get("lease_id")
            if not reply["ok"] or not isinstance(lease_id, str):
                raise CameraWorkerUnavailable("camera worker rejected live subscription")
            return lease_id, queue
        except Exception:
            self._live_media_queues.pop(session_id, None)
            raise

    async def subscribe_builtin(
        self, *, identity: str, ip_address: str, access_code: str, model: str | None, fps: int
    ) -> tuple[str, LiveMediaQueue]:
        """Start one worker-owned Bambu chamber/RTSPS producer."""

        if self.process is None or self.process.returncode is not None:
            raise CameraWorkerUnavailable("camera worker is not ready")
        if len(self._live_media_queues) >= _MAX_LIVE_MEDIA_QUEUES:
            raise CameraWorkerUnavailable("camera worker live relay limit reached")
        session_id = str(uuid.uuid4())
        queue: LiveMediaQueue = asyncio.Queue(maxsize=1)
        self._live_media_queues[session_id] = queue
        try:
            reply = await self.request(
                "subscribe_builtin",
                {
                    "identity": identity,
                    "media_session_id": session_id,
                    "ip_address": ip_address,
                    "access_code": access_code,
                    "model": model,
                    "fps": fps,
                },
            )
            lease_id = reply["result"].get("lease_id")
            if not reply["ok"] or not isinstance(lease_id, str):
                raise CameraWorkerUnavailable("camera worker rejected built-in live subscription")
            return lease_id, queue
        except Exception:
            self._live_media_queues.pop(session_id, None)
            raise

    async def unsubscribe(self, lease_id: str, queue: LiveMediaQueue) -> None:
        try:
            await self.request("unsubscribe", {"lease_id": lease_id})
        finally:
            self._end_live_queue(queue)

    async def start_raw_proxy(
        self,
        *,
        identity: str,
        bind_address: str,
        listen_port: int,
        target_host: str,
        target_port: int,
    ) -> str:
        """Bind a worker-owned transparent VP camera listener and return its lease."""

        if self.process is None or self.process.returncode is not None:
            raise CameraWorkerUnavailable("camera worker is not ready")
        reply = await self.request(
            "start_raw_proxy",
            {
                "identity": identity,
                "bind_address": bind_address,
                "listen_port": listen_port,
                "target_host": target_host,
                "target_port": target_port,
            },
            timeout=_STARTUP_TIMEOUT_SECONDS + _REQUEST_TIMEOUT_SECONDS,
        )
        lease_id = reply["result"].get("lease_id")
        if not reply["ok"] or not isinstance(lease_id, str):
            raise CameraWorkerUnavailable("camera worker rejected raw camera lease")
        return lease_id

    async def stop_raw_proxy(self, lease_id: str) -> None:
        """Release a previously admitted worker-owned transparent listener."""

        await self.request("stop_raw_proxy", {"lease_id": lease_id})

    async def stop(self) -> None:
        """Bound normal shutdown, then terminate only this supervisor's child."""

        process = self.process
        children = descendants(process.pid) if process is not None and isinstance(process.pid, int) else []
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
                await asyncio.wait_for(self._writer.wait_closed(), timeout=_SHUTDOWN_TIMEOUT_SECONDS)
            except (OSError, TimeoutError):
                pass
        self._fail_pending_requests()
        if process is not None and process.stdin is not None:
            process.stdin.close()  # guardian EOF, including on clean shutdown
        if process is not None and process.returncode is None:
            try:
                await asyncio.wait_for(process.wait(), timeout=_SHUTDOWN_TIMEOUT_SECONDS)
            except TimeoutError:
                await self._terminate_process_tree(process)
        await self._close_servers()
        if self._containment is not None:
            self._containment.close()
            self._containment = None
        if process is not None:
            if os.name != "nt":
                kill_owned_group(process.pid)
            if not await asyncio.to_thread(descendants_reaped, children):
                raise CameraWorkerUnavailable("camera worker descendants could not be reaped")
        for task in (self._stderr_task, self._control_reader_task):
            if task is None:
                continue
            try:
                await asyncio.wait_for(task, timeout=_SHUTDOWN_TIMEOUT_SECONDS)
            except TimeoutError as exc:
                task.cancel()
                raise CameraWorkerUnavailable("camera worker I/O task did not finish") from exc
        if process is not None:
            logger.info("Camera worker stopped: pid=%s exit_code=%s", process.pid, process.returncode)
        self.process = None
        self._reader = None
        self._writer = None
        self._fail_media_waiters()
        self._end_all_live_queues()

    async def _terminate_uncontained_process(self) -> None:
        """Clean up a child when attaching its required containment failed."""

        process = self.process
        if process is not None and process.returncode is None:
            if os.name != "nt":
                kill_owned_group(process.pid)
            else:
                process.terminate()
            await asyncio.wait_for(process.wait(), timeout=_SHUTDOWN_TIMEOUT_SECONDS)
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
            kill_owned_group(process.pid)
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
            self._control_reader_task = asyncio.create_task(
                self._pump_control_replies(), name="camera-worker-control-replies"
            )
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

    async def _accept_media(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        """Accept exactly one authenticated JPEG for an admitted capture lease."""

        try:
            hello = await asyncio.wait_for(read_media_hello(reader), timeout=_STARTUP_TIMEOUT_SECONDS)
            if self.bootstrap is None or not valid_media_hello(
                hello, generation=self.bootstrap.generation, secret=self.bootstrap.secret
            ):
                return
            waiter = self._media_waiters.get(hello.session_id)
            live_queue = self._live_media_queues.get(hello.session_id)
            if (waiter is None or waiter.done()) and live_queue is None:
                return
            while True:
                timeout = _REQUEST_TIMEOUT_SECONDS if waiter is not None else _LIVE_MEDIA_IDLE_SECONDS
                frame = await asyncio.wait_for(read_media_frame(reader), timeout=timeout)
                if frame.generation != self.bootstrap.generation or frame.session_id != hello.session_id:
                    raise CameraWorkerProtocolError("media frame belongs to another capture")
                if waiter is not None:
                    waiter.set_result(frame)
                    return
                assert live_queue is not None
                if live_queue.full():
                    live_queue.get_nowait()
                live_queue.put_nowait(frame)
        except (CameraWorkerProtocolError, OSError, TimeoutError):
            if "hello" in locals():
                waiter = self._media_waiters.get(hello.session_id)
                if waiter is not None and not waiter.done():
                    waiter.set_exception(CameraWorkerUnavailable("camera worker media relay failed"))
                live_queue = self._live_media_queues.pop(hello.session_id, None)
                if live_queue is not None:
                    self._signal_live_queue_end(live_queue)
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass

    async def _pump_control_replies(self) -> None:
        """Route replies from the single control reader by their request UUID."""

        if self._reader is None or self.bootstrap is None:
            return
        try:
            while True:
                reply = await read_control(self._reader)
                request_id = reply["request_id"]
                future = self._pending_requests.get(request_id)
                if future is None:
                    raise CameraWorkerProtocolError("camera worker replied to an unknown request")
                result = validate_reply(reply, generation=self.bootstrap.generation, request_id=request_id)
                if not future.done():
                    future.set_result(result)
        except (CameraWorkerProtocolError, OSError, asyncio.IncompleteReadError):
            self._fail_pending_requests()

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
        pending = b""
        dropping_line = False
        unstructured = 0
        suppressed = 0
        emitted = 0
        window_started = time.monotonic()
        while True:
            chunk = await self.process.stderr.read(4096)
            if not chunk:
                break
            self._stderr_bytes = min(32 * 1024, self._stderr_bytes + len(chunk))
            for part in chunk.splitlines(keepends=True):
                pending += part
                if len(pending) > MAX_LINE_BYTES:
                    pending = b""
                    dropping_line = True
                if not part.endswith(b"\n"):
                    continue
                record = None if dropping_line else decode_worker_log(pending)
                pending = b""
                dropping_line = False
                if record is None:
                    unstructured += 1
                    if unstructured == 1:
                        logger.warning("Camera worker emitted unstructured stderr; raw content withheld")
                    continue
                now = time.monotonic()
                if now - window_started >= 10:
                    if suppressed:
                        logger.warning("Camera worker log rate limit: suppressed %d records", suppressed)
                    window_started, emitted, suppressed = now, 0, 0
                if emitted >= 200:
                    suppressed += 1
                    continue
                level, name, message = record
                logger.log(level, "Camera worker [%s]: %s", name, message)
                emitted += 1
        if pending or dropping_line:
            unstructured += 1
        if unstructured or suppressed:
            logger.warning(
                "Camera worker stderr summary: unstructured_lines=%d rate_limited_records=%d",
                unstructured,
                suppressed,
            )

    def _close_listeners(self) -> None:
        self._connection_closed.set()
        self._fail_media_waiters()
        self._end_all_live_queues()
        self._fail_pending_requests()
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
        self._media_waiters.clear()
        self._end_all_live_queues()
        self._pending_requests.clear()
        self._control_reader_task = None

    @staticmethod
    def _worker_env() -> dict[str, str]:
        """The guardian and child receive no database, API or broker secrets."""
        keys = {
            "PATH",
            "SYSTEMROOT",
            "WINDIR",
            "TEMP",
            "TMP",
            "TMPDIR",
            "LANG",
            "LC_ALL",
            "LD_LIBRARY_PATH",
            "DYLD_LIBRARY_PATH",
            "FFMPEG_PATH",
            "SSL_CERT_FILE",
        }
        env = {key: value for key, value in os.environ.items() if key.upper() in keys}
        env.update(BAMDUDE_IGNORE_DOTENV="1", PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8")
        return env

    def _fail_media_waiters(self) -> None:
        for waiter in self._media_waiters.values():
            if not waiter.done():
                waiter.set_exception(CameraWorkerUnavailable("camera worker stopped before media relay completed"))

    def _end_live_queue(self, queue: LiveMediaQueue) -> None:
        for session_id, registered in list(self._live_media_queues.items()):
            if registered is queue:
                self._live_media_queues.pop(session_id, None)
        self._signal_live_queue_end(queue)

    def _end_all_live_queues(self) -> None:
        queues = list(self._live_media_queues.values())
        self._live_media_queues.clear()
        for queue in queues:
            self._signal_live_queue_end(queue)

    @staticmethod
    def _signal_live_queue_end(queue: LiveMediaQueue) -> None:
        if queue.full():
            queue.get_nowait()
        queue.put_nowait(None)

    def _fail_pending_requests(self) -> None:
        for request in self._pending_requests.values():
            if not request.done():
                request.set_exception(CameraWorkerUnavailable("camera worker control connection closed"))


def _optional_string(value: object) -> str | None:
    return value if isinstance(value, str) and len(value) <= 128 else None


def _optional_timing(value: object) -> float | None:
    return float(value) if isinstance(value, (int, float)) and 0 <= value <= 120_000 else None
