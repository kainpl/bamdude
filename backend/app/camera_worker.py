"""Child process for worker-only camera capture through local IPC.

The entry point starts no FastAPI, database or MQTT runtime.  Camera transports
are imported only after an authenticated capture command reaches this process.
"""

from __future__ import annotations

import asyncio
import logging
import sys
import uuid

from backend.app.services.camera_worker_logging import configure_worker_logging, set_camera_log_secrets
from backend.app.services.camera_worker_protocol import (
    CameraWorkerProtocolError,
    WorkerBootstrap,
    make_reply,
    make_request,
    read_bootstrap,
    read_control,
    validate_reply,
    write_control,
)

logger = logging.getLogger(__name__)


async def _capture(command, bootstrap: WorkerBootstrap) -> dict:
    """Capture one JPEG in the child and relay it on its separate media socket."""

    if command.kind == "builtin":
        from backend.app.services.camera import capture_camera_frame_with_provenance

        result = await capture_camera_frame_with_provenance(
            command.ip_address or "",
            command.access_code or "",
            command.model,
            command.timeout_ms // 1000,
        )
    else:
        from backend.app.services.external_camera import capture_frame_with_provenance

        result = await capture_frame_with_provenance(
            command.url or "",
            command.camera_type or "",
            command.timeout_ms // 1000,
            command.snapshot_url,
        )
    if result.frame is not None:
        from backend.app.services.camera_worker_media import (
            MediaHello,
            WorkerMediaFrame,
            write_media_frame,
            write_media_hello,
        )

        _reader, media_writer = await asyncio.wait_for(
            asyncio.open_connection("127.0.0.1", bootstrap.media_port), timeout=5.0
        )
        try:
            await write_media_hello(
                media_writer,
                MediaHello(
                    generation=bootstrap.generation, session_id=command.media_session_id, secret=bootstrap.secret
                ),
            )
            await asyncio.wait_for(
                write_media_frame(
                    media_writer,
                    WorkerMediaFrame(
                        generation=bootstrap.generation,
                        session_id=command.media_session_id,
                        frame=result.frame,
                        source=result.source,
                        attempt_id=result.attempt_id,
                        first_frame_ms=result.first_frame_ms,
                        cleanup_ms=result.cleanup_ms,
                    ),
                ),
                timeout=5.0,
            )
        finally:
            media_writer.close()
            try:
                await media_writer.wait_closed()
            except OSError:
                pass
    return {
        "frame_available": result.frame is not None,
        "source": result.source,
        "attempt_id": result.attempt_id,
        "first_frame_ms": result.first_frame_ms,
        "cleanup_ms": result.cleanup_ms,
    }


async def _serve_capture(request: dict, bootstrap: WorkerBootstrap, writer, write_lock: asyncio.Lock) -> None:
    """Keep control readable while one physical capture awaits its transport."""

    try:
        from backend.app.services.camera_worker_capture import WorkerCaptureCommand

        command = WorkerCaptureCommand.from_payload(request["payload"])
        set_camera_log_secrets(
            command.access_code, command.url, command.snapshot_url, identity=command.media_session_id
        )
        result = await _capture(command, bootstrap)
        if not result["frame_available"]:
            logger.warning(
                "Camera worker capture produced no frame: purpose=%s session=%s",
                command.purpose,
                command.media_session_id,
            )
    except (CameraWorkerProtocolError, OSError, TimeoutError):
        logger.exception("Camera worker capture failed")
        result = {
            "frame_available": False,
            "source": None,
            "attempt_id": None,
            "first_frame_ms": None,
            "cleanup_ms": None,
        }
    async with write_lock:
        await write_control(
            writer,
            make_reply(
                generation=bootstrap.generation,
                request_id=request["request_id"],
                ok=True,
                result=result,
            ),
        )


async def run(bootstrap: WorkerBootstrap) -> int:
    """Connect once and keep control responsive while captures run."""

    reader, writer = await asyncio.open_connection("127.0.0.1", bootstrap.control_port)
    try:
        hello_id = str(uuid.uuid4())
        await write_control(
            writer,
            make_request(
                generation=bootstrap.generation,
                request_id=hello_id,
                operation="hello",
                payload={"secret": bootstrap.secret.hex()},
            ),
        )
        hello = validate_reply(await read_control(reader), generation=bootstrap.generation, request_id=hello_id)
        if not hello["ok"]:
            return 2

        write_lock = asyncio.Lock()
        capture_tasks: set[asyncio.Task[None]] = set()
        from backend.app.services.camera_worker_live import (
            LIVE_STREAM_ENDED,
            LiveBuiltinSubscription,
            LiveExternalSubscription,
            LiveProducerRegistry,
            RawProxyCommand,
        )

        live_registry = LiveProducerRegistry()
        transport_locks: dict[str, asyncio.Lock] = {}
        live_leases = {}
        live_forwarders: dict[str, asyncio.Task[None]] = {}
        raw_proxies: dict[str, tuple[object, object, asyncio.Task[None]]] = {}

        async def release_live_lease(lease_id: str, *, cancel_forwarder: bool) -> bool:
            """Release both halves of a live lease exactly once.

            A media socket can disappear without an HTTP client issuing an
            unsubscribe command.  The forwarder completion path uses this same
            helper, so an abandoned relay cannot keep a physical producer or
            its ffmpeg child alive in this worker.
            """

            lease = live_leases.pop(lease_id, None)
            if lease is None:
                return False
            await live_registry.unsubscribe(lease)
            forwarder = live_forwarders.pop(lease_id, None)
            if cancel_forwarder and forwarder is not None and forwarder is not asyncio.current_task():
                forwarder.cancel()
                await asyncio.gather(forwarder, return_exceptions=True)
            return True

        def release_finished_forwarder(completed: asyncio.Task[None], lease_id: str) -> None:
            # Retrieve the exception so a broken loopback writer does not turn
            # into an unobserved-task warning.  The parent will receive the
            # closed relay and reconnect through its normal fan-out lifecycle.
            if not completed.cancelled():
                completed.exception()
            if lease_id in live_leases:
                asyncio.create_task(
                    release_live_lease(lease_id, cancel_forwarder=False),
                    name=f"camera-worker-live-release-{lease_id}",
                )

        async def release_raw_proxy(lease_id: str) -> bool:
            """Stop transparent VP transport before releasing its exclusive lease."""

            entry = raw_proxies.pop(lease_id, None)
            if entry is None:
                return False
            lease, proxy, task = entry
            await proxy.stop()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await live_registry.release_raw(lease)
            return True

        def release_finished_raw_proxy(completed: asyncio.Task[None], lease_id: str) -> None:
            if not completed.cancelled():
                completed.exception()
            if lease_id in raw_proxies:
                asyncio.create_task(
                    release_raw_proxy(lease_id),
                    name=f"camera-worker-raw-release-{lease_id}",
                )

        while True:
            request = await read_control(reader)
            if request["generation"] != bootstrap.generation:
                async with write_lock:
                    await write_control(
                        writer,
                        make_reply(
                            generation=bootstrap.generation,
                            request_id=request["request_id"],
                            ok=False,
                            error="protocol_error",
                        ),
                    )
                return 3
            operation = request["operation"]
            if operation == "heartbeat":
                reply = make_reply(
                    generation=bootstrap.generation,
                    request_id=request["request_id"],
                    ok=True,
                    result={"state": "ready"},
                )
            elif operation == "status":
                reply = make_reply(
                    generation=bootstrap.generation,
                    request_id=request["request_id"],
                    ok=True,
                    result={"state": "ready", "camera_runtime": "worker"},
                )
            elif operation == "probe_tcp":
                payload = request["payload"]
                host = payload.get("host")
                port = payload.get("port")
                timeout = payload.get("timeout")
                identity = payload.get("identity")
                try:
                    uuid.UUID(identity)
                    valid_identity = True
                except (TypeError, ValueError, AttributeError):
                    valid_identity = False
                if (
                    set(payload) != {"host", "port", "timeout", "identity"}
                    or not isinstance(host, str)
                    or not host
                    or len(host) > 255
                    or any(char.isspace() for char in host)
                    or not isinstance(port, int)
                    or not 1 <= port <= 65535
                    or not isinstance(timeout, (int, float))
                    or not 0.1 <= timeout <= 10
                    or not valid_identity
                ):
                    reply = make_reply(
                        generation=bootstrap.generation,
                        request_id=request["request_id"],
                        ok=False,
                        error="protocol_error",
                    )
                else:

                    async def probe(
                        host=host, port=port, timeout=timeout, identity=identity, request_id=request["request_id"]
                    ):
                        lock = transport_locks.setdefault(host, asyncio.Lock())
                        if lock.locked() or not await live_registry.acquire_probe(identity):
                            code = "camera_busy"
                        else:
                            try:
                                await asyncio.wait_for(lock.acquire(), timeout=0.01)
                            except TimeoutError:
                                code = "camera_busy"
                            else:
                                try:
                                    try:
                                        _probe_reader, probe_writer = await asyncio.wait_for(
                                            asyncio.open_connection(host, port), timeout=float(timeout)
                                        )
                                    except TimeoutError:
                                        code = "tcp_timeout"
                                    except ConnectionRefusedError:
                                        code = "tcp_refused"
                                    except OSError:
                                        code = "tcp_unreachable"
                                    else:
                                        probe_writer.close()
                                        try:
                                            await probe_writer.wait_closed()
                                        except OSError:
                                            pass
                                        code = "ok"
                                finally:
                                    lock.release()
                            finally:
                                await live_registry.release_probe(identity)
                        async with write_lock:
                            await write_control(
                                writer,
                                make_reply(
                                    generation=bootstrap.generation,
                                    request_id=request_id,
                                    ok=True,
                                    result={"code": code},
                                ),
                            )

                    task = asyncio.create_task(probe(), name=f"camera-worker-probe-{request['request_id']}")
                    capture_tasks.add(task)
                    task.add_done_callback(capture_tasks.discard)
                    continue
            elif operation == "capture":

                async def guarded_capture(request=request):
                    payload = request["payload"]
                    host = payload.get("ip_address") if payload.get("kind") == "builtin" else None
                    if isinstance(host, str) and host:
                        async with transport_locks.setdefault(host, asyncio.Lock()):
                            await _serve_capture(request, bootstrap, writer, write_lock)
                    else:
                        await _serve_capture(request, bootstrap, writer, write_lock)

                task = asyncio.create_task(
                    guarded_capture(),
                    name=f"camera-worker-{request['request_id']}",
                )
                capture_tasks.add(task)
                task.add_done_callback(capture_tasks.discard)
                continue
            elif operation in {"subscribe", "subscribe_builtin"}:
                try:
                    subscription = (
                        LiveExternalSubscription.from_payload(request["payload"])
                        if operation == "subscribe"
                        else LiveBuiltinSubscription.from_payload(request["payload"])
                    )

                    async def producer(subscription=subscription) -> None:
                        set_camera_log_secrets(
                            subscription.access_code
                            if isinstance(subscription, LiveBuiltinSubscription)
                            else subscription.url,
                            identity=subscription.identity,
                        )
                        if isinstance(subscription, LiveBuiltinSubscription):
                            from backend.app.services.camera import (
                                generate_chamber_image_stream,
                                is_chamber_image_model,
                                read_next_chamber_frame,
                            )

                            if is_chamber_image_model(subscription.model):
                                connection = await generate_chamber_image_stream(
                                    subscription.ip_address, subscription.access_code, subscription.fps
                                )
                                if connection is None:
                                    return
                                reader, chamber_writer = connection
                                try:
                                    while frame := await read_next_chamber_frame(reader, timeout=30.0):
                                        live_registry.publish(subscription.identity, frame)
                                finally:
                                    chamber_writer.close()
                                    try:
                                        await chamber_writer.wait_closed()
                                    except OSError:
                                        pass
                                return
                        from backend.app.services.camera_profiles import get_camera_profile
                        from backend.app.services.external_camera import generate_mjpeg_stream

                        async for _chunk in generate_mjpeg_stream(
                            f"rtsps://bblp:{subscription.access_code}@{subscription.ip_address}:322/streaming/live/1"
                            if isinstance(subscription, LiveBuiltinSubscription)
                            else subscription.url,
                            "rtsp" if isinstance(subscription, LiveBuiltinSubscription) else subscription.camera_type,
                            subscription.fps,
                            on_frame=lambda frame: live_registry.publish(subscription.identity, frame),
                            rtsp_profile=(
                                get_camera_profile(subscription.model)
                                if isinstance(subscription, LiveBuiltinSubscription)
                                else None
                            ),
                        ):
                            pass

                    lease, _queue = await live_registry.subscribe(subscription.identity, producer)
                    live_leases[lease.lease_id] = lease

                    async def forward(queue=_queue, session_id=subscription.media_session_id) -> None:
                        from backend.app.services.camera_worker_media import (
                            MediaHello,
                            WorkerMediaFrame,
                            write_media_frame,
                            write_media_hello,
                        )

                        _reader, media_writer = await asyncio.open_connection("127.0.0.1", bootstrap.media_port)
                        try:
                            await write_media_hello(
                                media_writer,
                                MediaHello(
                                    generation=bootstrap.generation, session_id=session_id, secret=bootstrap.secret
                                ),
                            )
                            while True:
                                frame = await queue.get()
                                if frame == LIVE_STREAM_ENDED:
                                    return
                                await write_media_frame(
                                    media_writer,
                                    WorkerMediaFrame(
                                        generation=bootstrap.generation,
                                        session_id=session_id,
                                        frame=frame,
                                        source="fresh",
                                        attempt_id=None,
                                        first_frame_ms=None,
                                        cleanup_ms=None,
                                    ),
                                )
                        finally:
                            media_writer.close()
                            try:
                                await media_writer.wait_closed()
                            except (OSError, RuntimeError):
                                pass

                    forwarder = asyncio.create_task(forward(), name=f"camera-worker-live-forward-{lease.lease_id}")
                    live_forwarders[lease.lease_id] = forwarder
                    forwarder.add_done_callback(
                        lambda completed, lease_id=lease.lease_id: release_finished_forwarder(completed, lease_id)
                    )
                    reply = make_reply(
                        generation=bootstrap.generation,
                        request_id=request["request_id"],
                        ok=True,
                        result={"lease_id": lease.lease_id},
                    )
                except CameraWorkerProtocolError:
                    reply = make_reply(
                        generation=bootstrap.generation,
                        request_id=request["request_id"],
                        ok=False,
                        error="protocol_error",
                    )
            elif operation == "unsubscribe":
                lease_id = request["payload"].get("lease_id")
                released = (
                    await release_live_lease(lease_id, cancel_forwarder=True) if isinstance(lease_id, str) else False
                )
                reply = make_reply(
                    generation=bootstrap.generation,
                    request_id=request["request_id"],
                    ok=True,
                    result={"released": released},
                )
            elif operation == "start_raw_proxy":
                try:
                    command = RawProxyCommand.from_payload(request["payload"])
                    lease = await live_registry.acquire_raw(command.identity)
                    from backend.app.services.virtual_printer.tcp_proxy import TCPProxy

                    proxy = TCPProxy(
                        name=f"WorkerCamera-{command.listen_port}",
                        listen_port=command.listen_port,
                        target_host=command.target_host,
                        target_port=command.target_port,
                        bind_address=command.bind_address,
                    )
                    task = asyncio.create_task(proxy.start(), name=f"camera-worker-raw-{lease.lease_id}")
                    try:
                        await asyncio.wait_for(proxy.ready.wait(), timeout=5.0)
                    except TimeoutError:
                        task.cancel()
                        await asyncio.gather(task, return_exceptions=True)
                        await live_registry.release_raw(lease)
                        raise CameraWorkerProtocolError("raw proxy did not become ready")
                    raw_proxies[lease.lease_id] = (lease, proxy, task)
                    task.add_done_callback(
                        lambda completed, lease_id=lease.lease_id: release_finished_raw_proxy(completed, lease_id)
                    )
                    reply = make_reply(
                        generation=bootstrap.generation,
                        request_id=request["request_id"],
                        ok=True,
                        result={"lease_id": lease.lease_id},
                    )
                except (CameraWorkerProtocolError, RuntimeError):
                    reply = make_reply(
                        generation=bootstrap.generation,
                        request_id=request["request_id"],
                        ok=False,
                        error="protocol_error",
                    )
            elif operation == "stop_raw_proxy":
                lease_id = request["payload"].get("lease_id")
                released = await release_raw_proxy(lease_id) if isinstance(lease_id, str) else False
                reply = make_reply(
                    generation=bootstrap.generation,
                    request_id=request["request_id"],
                    ok=True,
                    result={"released": released},
                )
            elif operation == "shutdown":
                for task in capture_tasks:
                    task.cancel()
                await asyncio.gather(*capture_tasks, return_exceptions=True)
                for lease_id in tuple(live_leases):
                    await release_live_lease(lease_id, cancel_forwarder=True)
                for lease_id in tuple(raw_proxies):
                    await release_raw_proxy(lease_id)
                async with write_lock:
                    await write_control(
                        writer,
                        make_reply(
                            generation=bootstrap.generation,
                            request_id=request["request_id"],
                            ok=True,
                            result={"state": "stopping"},
                        ),
                    )
                return 0
            else:
                reply = make_reply(
                    generation=bootstrap.generation,
                    request_id=request["request_id"],
                    ok=False,
                    error="unknown_operation",
                )
            async with write_lock:
                await write_control(writer, reply)
    except (CameraWorkerProtocolError, OSError, asyncio.IncompleteReadError):
        logger.exception("Camera worker control connection failed")
        return 3
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except OSError:
            pass


def main() -> int:
    configure_worker_logging()
    try:
        bootstrap = read_bootstrap(sys.stdin.buffer)
    except CameraWorkerProtocolError:
        logger.error("Camera worker bootstrap rejected")
        return 2
    try:
        return asyncio.run(run(bootstrap))
    except Exception:
        logger.exception("Camera worker terminated unexpectedly")
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
