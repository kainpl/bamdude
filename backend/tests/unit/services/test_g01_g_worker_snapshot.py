"""Untrusted HTTP bodies cross the actual worker boundary only as images."""

import asyncio
from io import BytesIO

import pytest
from aiohttp import web
from PIL import Image

from backend.app.services.camera_runtime import CameraCaptureRequest, WorkerCameraRuntime
from backend.app.services.camera_worker_supervisor import CameraWorkerSupervisor
from backend.tests.unit.services.test_g01_g_capture_bounds import endpoint


@pytest.mark.asyncio
async def test_actual_worker_rejects_bad_snapshots_then_relays_transcoded_image():
    body, mime = b"<html>private error page</html>", "text/html"

    async def handler(request):
        return web.Response(body=body, content_type=mime)

    runtime = WorkerCameraRuntime(CameraWorkerSupervisor())
    async with endpoint(handler) as url:
        request = CameraCaptureRequest.external(url=url, camera_type="snapshot", timeout=3)
        try:
            await runtime.supervisor.start()
            for payload in [
                (b"<html>private error page</html>", "text/html"),
                (b"<svg/>", "image/svg+xml"),
                (b'{"error":"denied"}', "application/json"),
                (b"\xff\xd8\xffbroken\xff\xd9", "image/jpeg"),
            ]:
                body, mime = payload
                assert (await runtime.capture(request)).frame is None
                assert (await runtime.supervisor.request("heartbeat"))["result"] == {"state": "ready"}
            with Image.new("RGB", (24, 16), "red") as original, BytesIO() as output:
                original.save(output, format="PNG")
                body, mime = output.getvalue(), "image/png"
            result = await asyncio.wait_for(runtime.capture(request), 4)
            with Image.open(BytesIO(result.frame)) as image:
                image.load()
                assert image.format == "JPEG" and image.size == (24, 16)
            assert result.source == "fresh"
        finally:
            await runtime.stop()
