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
async def test_unstarted_worker_never_falls_back_to_builtin_capture(monkeypatch):
    from backend.app.services import camera

    async def capture_builtin(*args):
        raise AssertionError("main must not open a physical camera")

    monkeypatch.setattr(camera, "capture_camera_frame_with_provenance", capture_builtin)
    request = camera_runtime.CameraCaptureRequest.builtin(
        ip_address="192.0.2.10", access_code="secret", model="P1S", timeout=20, purpose="obico"
    )
    with pytest.raises(camera_runtime.CameraWorkerUnavailable):
        await camera_runtime.capture(request)


@pytest.mark.asyncio
async def test_worker_owner_starts_and_stops_without_opening_a_camera():
    owner = camera_runtime.CameraRuntimeOwner(
        camera_runtime.WorkerCameraRuntime(camera_runtime.CameraWorkerSupervisor())
    )
    try:
        await owner.start()
        assert owner.snapshot()["state"] == "ready"
        assert owner.runtime.supervisor.process is not None
        assert owner.runtime.supervisor.process.stdin is not None
        assert not owner.runtime.supervisor.process.stdin.is_closing()
    finally:
        await owner.stop()
    assert owner.snapshot()["state"] == "stopped"
    assert owner.runtime.supervisor.process is None


@pytest.mark.asyncio
async def test_worker_owner_recovers_from_child_crash_with_new_generation():
    owner = camera_runtime.CameraRuntimeOwner(
        camera_runtime.WorkerCameraRuntime(camera_runtime.CameraWorkerSupervisor())
    )
    try:
        await owner.start()
        first = owner.runtime.supervisor
        first_generation = first.bootstrap.generation
        first.process.kill()
        await asyncio.wait_for(first.process.wait(), timeout=5)

        async def recovered():
            while owner.snapshot()["state"] != "ready" or owner.runtime.supervisor is first:
                await asyncio.sleep(0.1)

        await asyncio.wait_for(recovered(), timeout=12)
        assert owner.runtime.supervisor.bootstrap.generation != first_generation
        assert owner.snapshot()["restart_count"] >= 1
    finally:
        await owner.stop()


def test_worker_restart_policy_uses_bounded_backoff_and_half_open_circuit():
    owner = camera_runtime.CameraRuntimeOwner(
        camera_runtime.WorkerCameraRuntime(camera_runtime.CameraWorkerSupervisor())
    )
    assert [owner._restart_delay(now, half_open=False)[0] for now in (0, 1, 2)] == [1, 2, 4]
    assert owner._restart_delay(3, half_open=False) == (60, True)
    assert owner.snapshot()["state"] == "unavailable"
    assert owner._restart_delay(64, half_open=True) == (60, True)
    assert owner._restart_delay(125, half_open=False) == (1, False)


@pytest.mark.asyncio
async def test_runtime_override_is_task_local_and_restores_worker():
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
        lambda: camera_runtime.CameraCaptureRequest.external(url="http://x", camera_type="mjpeg", hold=-1.0),
    ],
)
def test_capture_request_rejects_ambiguous_or_unbounded_input(factory):
    with pytest.raises(ValueError):
        factory()


def test_capture_request_repr_hides_endpoints_and_credentials():
    request = camera_runtime.CameraCaptureRequest.builtin(
        ip_address="192.0.2.10", access_code="access-secret", model="P1S"
    )
    assert "192.0.2.10" not in repr(request)
    assert "access-secret" not in repr(request)


# ---------------------------------------------------------------- the chamber light (services/camera_light)


@pytest.mark.asyncio
async def test_the_facade_holds_the_light_waits_for_it_captures_and_lets_go_in_that_order(monkeypatch):
    """Whichever runtime captures, the light is taken in the main process around it."""
    from backend.app.services import camera_light

    events: list = []

    class FakeLease:
        async def settle(self):
            events.append("settle")

    async def acquire(printer_id, purpose, *, hold=None):
        events.append(("acquire", printer_id, purpose, hold))
        return FakeLease()

    def release(lease):
        events.append("release")

    class Runtime:
        async def capture(self, request):
            events.append("capture")
            return CameraCaptureResult(b"frame", "fresh")

    monkeypatch.setattr(camera_light, "acquire", acquire)
    monkeypatch.setattr(camera_light, "release", release)
    request = camera_runtime.CameraCaptureRequest.builtin(
        ip_address="192.0.2.10", access_code="secret", model="P1S", purpose="telegram", printer_id=7, hold=25.0
    )
    with camera_runtime.override_camera_runtime(Runtime()):
        assert (await camera_runtime.capture(request)).frame == b"frame"
    assert events == [("acquire", 7, "telegram", 25.0), "settle", "capture", "release"]


@pytest.mark.asyncio
async def test_the_light_is_let_go_when_the_capture_raises(monkeypatch):
    from backend.app.services import camera_light

    events: list = []

    class FakeLease:
        async def settle(self):
            pass

    async def acquire(printer_id, purpose, *, hold=None):
        return FakeLease()

    monkeypatch.setattr(camera_light, "acquire", acquire)
    monkeypatch.setattr(camera_light, "release", lambda lease: events.append("release"))

    class Runtime:
        async def capture(self, request):
            raise RuntimeError("camera gone")

    request = camera_runtime.CameraCaptureRequest.builtin(
        ip_address="192.0.2.10", access_code="x", model=None, printer_id=7
    )
    with camera_runtime.override_camera_runtime(Runtime()), pytest.raises(RuntimeError):
        await camera_runtime.capture(request)
    assert events == ["release"]


@pytest.mark.asyncio
async def test_a_request_that_names_no_printer_takes_no_light(monkeypatch):
    """The real lease: no printer id means no settings read, no client lookup, no command."""
    from backend.app.services import camera_light

    def never(printer_id):
        raise AssertionError("no lookup without a printer")

    monkeypatch.setattr(camera_light, "_get_client", never)
    fake = FakeRuntime(CameraCaptureResult(b"frame", "fresh"), [])
    request = camera_runtime.CameraCaptureRequest.external(url="http://example.test/cam", camera_type="mjpeg")
    with camera_runtime.override_camera_runtime(fake):
        assert (await camera_runtime.capture(request)).frame == b"frame"
