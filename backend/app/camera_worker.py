"""Child process for opt-in one-shot camera capture through local IPC.

The entry point starts no FastAPI, database or MQTT runtime.  Camera transports
are imported only after an authenticated capture command reaches this process.
"""

from __future__ import annotations

import asyncio
import sys
import uuid

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

        result = await _capture(WorkerCaptureCommand.from_payload(request["payload"]), bootstrap)
    except (CameraWorkerProtocolError, OSError, TimeoutError):
        # Transport implementations write their own redacted evidence; no URL,
        # credential, stack trace or raw exception crosses IPC.
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
                    result={"state": "ready", "camera_runtime": "not_started"},
                )
            elif operation == "capture":
                task = asyncio.create_task(
                    _serve_capture(request, bootstrap, writer, write_lock),
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
                        from backend.app.services.external_camera import generate_mjpeg_stream

                        async for _chunk in generate_mjpeg_stream(
                            f"rtsps://bblp:{subscription.access_code}@{subscription.ip_address}:322/streaming/live/1"
                            if isinstance(subscription, LiveBuiltinSubscription)
                            else subscription.url,
                            "rtsp" if isinstance(subscription, LiveBuiltinSubscription) else subscription.camera_type,
                            subscription.fps,
                            on_frame=lambda frame: live_registry.publish(subscription.identity, frame),
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
        return 3
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except OSError:
            pass


def main() -> int:
    try:
        bootstrap = read_bootstrap(sys.stdin.buffer)
    except CameraWorkerProtocolError:
        return 2
    return asyncio.run(run(bootstrap))


if __name__ == "__main__":
    raise SystemExit(main())
