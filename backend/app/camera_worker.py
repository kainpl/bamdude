"""Minimal child process for the camera-worker IPC acceptance harness.

It intentionally imports only the IPC module.  No cameras, FFmpeg, FastAPI,
database, MQTT or application lifespan are started by this entry point.
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
