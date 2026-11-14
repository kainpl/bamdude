"""The HTTP doors of a one-shot capture, beside a live view (G09 U5; upstream 7cea7acf0 legs 5 and 8).

A snapshot or a camera test of a camera somebody is watching answers from the live view and never dials a
second time: most firmware allows one camera connection, a USB camera one handle. The worker is a fake that
counts what reaches it; the rule itself (``camera_runtime.capture``) is real.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
import pytest_asyncio
from httpx import AsyncClient

from backend.app.api.routes import camera as printer_views, cameras as camera_views
from backend.app.services import camera_fanout, camera_runtime
from backend.app.services.camera_metrics import CameraCaptureResult
from backend.app.services.camera_runtime import override_camera_runtime
from backend.tests.auth_helpers import mint_test_authority

# A JPEG just long enough to carry a frame header: SOI, then SOF0 saying 480 rows by 640 columns.
LIVE = b"\xff\xd8\xff\xc0\x00\x11\x08\x01\xe0\x02\x80\x03LIVE"
WORKER = b"\xff\xd8\xff\xc0\x00\x11\x08\x00\xf0\x01\x40\x03WORKER"
STREAM_URL = "http://192.0.2.50/stream"
EXTERNAL = {"external_camera_enabled": True, "external_camera_url": STREAM_URL, "external_camera_type": "mjpeg"}
SHELF = {"name": "Shelf", "camera_type": "mjpeg", "url": STREAM_URL}

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


class _Worker:
    def __init__(self) -> None:
        self.requests: list = []

    async def capture(self, request):
        self.requests.append(request)
        return CameraCaptureResult(WORKER, "fresh")


@pytest.fixture
def worker():
    fake = _Worker()
    with override_camera_runtime(fake):
        yield fake


@pytest_asyncio.fixture
async def token():
    from backend.app.core.auth import create_camera_stream_token

    return {"token": await create_camera_stream_token(authority=await mint_test_authority())}


@pytest.fixture(autouse=True)
def _empty_registries(monkeypatch):
    monkeypatch.setattr(camera_runtime, "LIVE_VIEW_FIRST_FRAME_WAIT", 0.2)
    registries = (
        printer_views._active_worker_streams,
        printer_views._last_frames,
        printer_views._last_frame_times,
        printer_views._live_frame_metadata,
        printer_views._snapshot_frames,
        printer_views._snapshot_frame_times,
        camera_views._last_frames,
        camera_views._last_frame_times,
        camera_views._snapshot_frames,
        camera_views._snapshot_frame_times,
    )
    before = dict(camera_fanout._broadcasters)
    for registry in registries:
        registry.clear()
    yield
    for registry in registries:
        registry.clear()
    camera_fanout._broadcasters.clear()
    camera_fanout._broadcasters.update(before)


def _watch_printer(printer_id: int, source: str, frame: bytes | None = LIVE) -> None:
    """A live view of this printer's camera, as the relay registers it — with or without a first frame."""
    printer_views._active_worker_streams[printer_id] = ("token", source)
    if frame is not None:
        printer_views._last_frames[printer_id] = frame


def _watch_camera(camera_id: int, frame: bytes | None = LIVE) -> None:
    camera_fanout._broadcasters[f"camera-{camera_id}"] = SimpleNamespace(stopped=False)
    if frame is not None:
        camera_views._publish_frame(camera_id, frame)


PRINTER_KINDS = {
    "rtsp": ({"model": "X1C"}, "rtsp"),
    "chamber_image": ({"model": "A1"}, "chamber_image"),
    "external": (EXTERNAL, "external"),
}


@pytest.mark.parametrize("kind", PRINTER_KINDS)
class TestPrinterSnapshot:
    async def test_a_starting_view_is_a_503_with_retry_after_and_no_capture(
        self, async_client: AsyncClient, printer_factory, worker, token, kind
    ):
        columns, source = PRINTER_KINDS[kind]
        printer = await printer_factory(**columns)
        _watch_printer(printer.id, source, None)

        answer = await async_client.get(f"/api/v1/printers/{printer.id}/camera/snapshot", params=token)

        assert answer.status_code == 503
        assert answer.headers["Retry-After"] == "2"
        assert worker.requests == [], "the snapshot opened a second reader beside a starting live view"

    async def test_a_running_view_answers_with_its_frame(
        self, async_client: AsyncClient, printer_factory, worker, token, kind
    ):
        columns, source = PRINTER_KINDS[kind]
        printer = await printer_factory(**columns)
        _watch_printer(printer.id, source)

        answer = await async_client.get(f"/api/v1/printers/{printer.id}/camera/snapshot", params=token)

        assert answer.status_code == 200 and answer.content == LIVE
        assert worker.requests == []

    async def test_an_unwatched_camera_is_captured(
        self, async_client: AsyncClient, printer_factory, worker, token, kind
    ):
        columns, _source = PRINTER_KINDS[kind]
        printer = await printer_factory(**columns)

        answer = await async_client.get(f"/api/v1/printers/{printer.id}/camera/snapshot", params=token)

        assert answer.status_code == 200 and answer.content == WORKER
        assert len(worker.requests) == 1


class TestStandaloneSnapshot:
    async def test_a_starting_view_is_a_503_with_retry_after_and_no_capture(
        self, async_client: AsyncClient, worker, token
    ):
        camera_id = (await async_client.post("/api/v1/cameras/", json=SHELF)).json()["id"]
        _watch_camera(camera_id, None)

        answer = await async_client.get(f"/api/v1/cameras/{camera_id}/snapshot", params=token)

        assert answer.status_code == 503
        assert answer.headers["Retry-After"] == "2"
        assert worker.requests == []

    async def test_a_running_view_answers_with_its_frame(self, async_client: AsyncClient, worker, token):
        camera_id = (await async_client.post("/api/v1/cameras/", json=SHELF)).json()["id"]
        _watch_camera(camera_id)

        answer = await async_client.get(f"/api/v1/cameras/{camera_id}/snapshot", params=token)

        assert answer.status_code == 200 and answer.content == LIVE
        assert worker.requests == []

    async def test_an_unwatched_camera_is_captured(self, async_client: AsyncClient, worker, token):
        camera_id = (await async_client.post("/api/v1/cameras/", json=SHELF)).json()["id"]

        answer = await async_client.get(f"/api/v1/cameras/{camera_id}/snapshot", params=token)

        assert answer.status_code == 200 and answer.content == WORKER
        assert len(worker.requests) == 1


class TestBuiltInCameraTest:
    async def test_a_watched_camera_passes_on_the_live_view(self, async_client: AsyncClient, printer_factory, worker):
        printer = await printer_factory(model="A1")
        _watch_printer(printer.id, "chamber_image")

        answer = (await async_client.get(f"/api/v1/printers/{printer.id}/camera/test")).json()

        assert answer["success"] is True and answer["live_view"] is True
        assert worker.requests == []

    async def test_an_unwatched_camera_is_dialled(self, async_client: AsyncClient, printer_factory, worker):
        printer = await printer_factory(model="A1")

        answer = (await async_client.get(f"/api/v1/printers/{printer.id}/camera/test")).json()

        assert answer["success"] is True
        assert len(worker.requests) == 1


class TestExternalCameraTest:
    """`POST /printers/{id}/camera/external/test` — the Test button beside the external camera's URL."""

    async def test_the_saved_camera_being_watched_passes_on_the_live_view(
        self, async_client: AsyncClient, printer_factory, worker
    ):
        printer = await printer_factory(**EXTERNAL)
        _watch_printer(printer.id, "external")

        answer = (await async_client.post(f"/api/v1/printers/{printer.id}/camera/external/test", json={})).json()

        assert answer == {"success": True, "resolution": "640x480", "live_view": True}
        assert worker.requests == []

    async def test_a_starting_view_reads_as_busy_not_broken(self, async_client: AsyncClient, printer_factory, worker):
        printer = await printer_factory(**EXTERNAL)
        _watch_printer(printer.id, "external", None)

        answer = (await async_client.post(f"/api/v1/printers/{printer.id}/camera/external/test", json={})).json()

        assert answer["success"] is False and answer["live_view"] is True
        assert answer["error"] != "External camera connection failed"
        assert STREAM_URL not in str(answer)
        assert worker.requests == []

    async def test_a_draft_url_is_dialled_even_while_the_saved_camera_is_watched(
        self, async_client: AsyncClient, printer_factory, worker
    ):
        """Another address is another device: the view of the saved one proves nothing about it."""
        printer = await printer_factory(**EXTERNAL)
        _watch_printer(printer.id, "external")

        answer = (
            await async_client.post(
                f"/api/v1/printers/{printer.id}/camera/external/test",
                json={"url": "http://192.0.2.77/other", "camera_type": "mjpeg"},
            )
        ).json()

        assert answer == {"success": True, "resolution": "320x240"}
        assert [request.url for request in worker.requests] == ["http://192.0.2.77/other"]

    async def test_an_unwatched_camera_is_dialled(self, async_client: AsyncClient, printer_factory, worker):
        printer = await printer_factory(**EXTERNAL)

        answer = (await async_client.post(f"/api/v1/printers/{printer.id}/camera/external/test", json={})).json()

        assert answer == {"success": True, "resolution": "320x240"}
        assert len(worker.requests) == 1


class TestStandaloneCameraTest:
    async def test_the_saved_camera_being_watched_passes_on_the_live_view(self, async_client: AsyncClient, worker):
        camera_id = (await async_client.post("/api/v1/cameras/", json=SHELF)).json()["id"]
        _watch_camera(camera_id)

        answer = (await async_client.post(f"/api/v1/cameras/{camera_id}/test")).json()

        assert answer == {"success": True, "resolution": "640x480", "live_view": True}
        assert worker.requests == []

    async def test_a_starting_view_reads_as_busy_not_broken(self, async_client: AsyncClient, worker):
        camera_id = (await async_client.post("/api/v1/cameras/", json=SHELF)).json()["id"]
        _watch_camera(camera_id, None)

        answer = (await async_client.post(f"/api/v1/cameras/{camera_id}/test")).json()

        assert answer["success"] is False and answer["live_view"] is True
        assert answer["error"] != "Camera connection test failed"
        assert worker.requests == []

    async def test_a_draft_url_is_dialled_even_while_the_saved_camera_is_watched(
        self, async_client: AsyncClient, worker
    ):
        camera_id = (await async_client.post("/api/v1/cameras/", json=SHELF)).json()["id"]
        _watch_camera(camera_id)

        answer = (
            await async_client.post(f"/api/v1/cameras/{camera_id}/test", json={"url": "http://192.0.2.77/other"})
        ).json()

        assert answer == {"success": True, "resolution": "320x240"}
        assert len(worker.requests) == 1

    async def test_an_unwatched_camera_is_dialled(self, async_client: AsyncClient, worker):
        camera_id = (await async_client.post("/api/v1/cameras/", json=SHELF)).json()["id"]

        answer = (await async_client.post(f"/api/v1/cameras/{camera_id}/test")).json()

        assert answer == {"success": True, "resolution": "320x240"}
        assert len(worker.requests) == 1
