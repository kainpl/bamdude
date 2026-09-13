import asyncio
from dataclasses import dataclass

import pytest

from backend.app.services import camera_runtime
from backend.app.services.camera_metrics import CameraCaptureResult


@dataclass
class FakeRuntime:
    result: CameraCaptureResult
    requests: list[camera_runtime.CameraCaptureRequest]

    async def capture(self, request):
        self.requests.append(request)
        return self.result


@pytest.mark.asyncio
async def test_inline_runtime_delegates_built_in_without_import_time_camera_stack(monkeypatch):
    from backend.app.services import camera

    expected = CameraCaptureResult(b"frame", "fresh", "attempt-1")
    call = {}

    async def capture_builtin(*args):
        call["args"] = args
        return expected

    monkeypatch.setattr(camera, "capture_camera_frame_with_provenance", capture_builtin)
    request = camera_runtime.CameraCaptureRequest.builtin(
        ip_address="192.0.2.10", access_code="secret", model="P1S", timeout=20, purpose="obico"
    )
    assert await camera_runtime.capture(request) is expected
    assert call["args"] == ("192.0.2.10", "secret", "P1S", 20)


@pytest.mark.asyncio
async def test_runtime_override_is_task_local_and_restores_inline():
    fake = FakeRuntime(CameraCaptureResult(b"frame", "fresh"), [])
    request = camera_runtime.CameraCaptureRequest.external(url="http://example.test/cam", camera_type="mjpeg")

    with camera_runtime.override_camera_runtime(fake):
        assert (await camera_runtime.capture(request)).frame == b"frame"
        assert fake.requests == [request]
        inherited = await asyncio.create_task(camera_runtime.capture(request))
        assert inherited.frame == b"frame"
        assert fake.requests == [request, request]

    assert camera_runtime.get_camera_runtime() is not fake


@pytest.mark.parametrize(
    "factory",
    [
        lambda: camera_runtime.CameraCaptureRequest.builtin(ip_address="", access_code="x", model=None),
        lambda: camera_runtime.CameraCaptureRequest.external(url="", camera_type="mjpeg"),
        lambda: camera_runtime.CameraCaptureRequest.external(url="http://x", camera_type="mjpeg", timeout=121),
    ],
)
def test_capture_request_rejects_ambiguous_or_unbounded_input(factory):
    with pytest.raises(ValueError):
        factory()
