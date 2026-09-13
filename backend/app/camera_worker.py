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


async def run(bootstrap: WorkerBootstrap) -> int:
    """Connect once to the parent and serve harness-only control operations."""

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

        while True:
            request = await read_control(reader)
            if request["generation"] != bootstrap.generation:
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
                try:
                    from backend.app.services.camera_worker_capture import WorkerCaptureCommand

                    result = await _capture(WorkerCaptureCommand.from_payload(request["payload"]), bootstrap)
                    reply = make_reply(
                        generation=bootstrap.generation,
                        request_id=request["request_id"],
                        ok=True,
                        result=result,
                    )
                except (CameraWorkerProtocolError, OSError, TimeoutError):
                    # Transport implementations write their own redacted evidence;
                    # no URL, credential, stack trace or raw exception crosses IPC.
                    reply = make_reply(
                        generation=bootstrap.generation,
                        request_id=request["request_id"],
                        ok=True,
                        result={
                            "frame_available": False,
                            "source": None,
                            "attempt_id": None,
                            "first_frame_ms": None,
                            "cleanup_ms": None,
                        },
                    )
            elif operation == "shutdown":
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
