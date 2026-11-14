"""A view learns whether ITS OWN stream is attached, not only whether the camera's upstream is alive.

Every viewer of a camera shares one upstream. When the server ends one viewer cleanly (its authority was
retired, its token expired) while another still watches, the upstream's status stays healthy and the ended
viewer's ``<img>`` keeps its last frame for good — a frozen picture on a broadcast. So each stream carries
a random correlation id, and the status answers for that id.
"""

import asyncio
import uuid
from types import SimpleNamespace

import pytest

from backend.app.api.routes import camera as camera_routes
from backend.app.services import camera_viewers
from backend.app.services.external_camera import format_mjpeg_frame
from backend.tests.unit.live_media_harness import media_runtime, request as make_request, serve  # noqa: F401

_JPEG = b"\xff\xd8viewer-liveness\xff\xd9"
WALL = "wallTile_0123456"
OVERLAY = "obsOverlay-98765"


@pytest.fixture(autouse=True)
def _clean_registry():
    camera_viewers.forget_all()
    yield
    camera_viewers.forget_all()


class _Worker:
    async def stream_external(self, **kwargs):
        kwargs["on_frame"](_JPEG)
        yield format_mjpeg_frame(_JPEG)
        await kwargs["disconnect_event"].wait()


async def _open(printer_id: int, viewer_id: str | None):
    printer = SimpleNamespace(
        id=printer_id,
        connector_identity=str(uuid.uuid4()),
        external_camera_url="rtsp://camera.example/live",
        external_camera_type="rtsp",
    )
    request = make_request()
    response = await camera_routes._worker_stream_response(
        printer=printer,
        printer_id=printer_id,
        request=request,
        fps=5,
        runtime=_Worker(),
        viewer_id=viewer_id,
    )
    task, chunks, disconnect = serve(response, request)
    assert _JPEG in (await asyncio.wait_for(chunks.get(), timeout=1))["body"]
    return task, disconnect


async def _close(printer_id: int, *viewers):
    for task, disconnect in viewers:
        disconnect.set()
        await asyncio.wait_for(task, 2)
    await camera_routes.shutdown_broadcaster(f"printer-{printer_id}")
    camera_routes._active_worker_streams.pop(printer_id, None)
    camera_routes._release_printer_frame_state(printer_id)


async def _status(printer_id: int, viewer: str | None) -> dict:
    return await camera_routes.camera_status(printer_id, None, viewer)


@pytest.mark.asyncio
async def test_an_attached_viewer_is_told_so_and_an_unknown_id_is_not(media_runtime):
    printer_id = 987_701
    wall = await _open(printer_id, WALL)
    try:
        assert (await _status(printer_id, WALL))["viewer_attached"] is True
        assert (await _status(printer_id, "neverOpened_0000"))["viewer_attached"] is False
        # The same id says nothing about another camera.
        assert (await _status(printer_id + 1, WALL))["viewer_attached"] is False
    finally:
        await _close(printer_id, wall)


@pytest.mark.asyncio
async def test_a_viewer_the_server_retired_is_detached_while_the_other_keeps_the_upstream_alive(media_runtime):
    printer_id = 987_702
    wall = await _open(printer_id, WALL)
    overlay = await _open(printer_id, OVERLAY)
    try:
        handles = list(media_runtime.handles)
        assert len(handles) == 2
        # The server ends ONE of them — whichever; the registry says which id that was.
        handles[0].retire()
        await asyncio.wait_for(asyncio.gather(*media_runtime.cleanups, return_exceptions=True), 2)
        gone, kept = (
            (WALL, OVERLAY) if camera_viewers.attached(f"printer-{printer_id}", WALL) is False else (OVERLAY, WALL)
        )

        ended = await _status(printer_id, gone)
        other = await _status(printer_id, kept)

        # The upstream is as healthy as before: only the per-viewer answer tells the two apart.
        assert ended["active"] is True and ended["stalled"] is False
        assert ended["viewer_attached"] is False
        assert other["viewer_attached"] is True
    finally:
        await _close(printer_id, wall, overlay)


@pytest.mark.asyncio
async def test_a_viewer_that_closes_is_removed(media_runtime):
    printer_id = 987_703
    wall = await _open(printer_id, WALL)
    await _close(printer_id, wall)

    assert camera_viewers.attached(f"printer-{printer_id}", WALL) is False
    assert camera_viewers._attached == {}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "raw", ["", "short", "x" * 33, "has space 12345", "semi;colon12345", "../../etc/passwd", "ключ-кирилицею"]
)
async def test_a_malformed_id_is_ignored_on_both_sides(media_runtime, raw):
    printer_id = 987_704
    wall = await _open(printer_id, raw)
    try:
        assert camera_viewers._attached == {}
        # No field: the view reads that as "no answer", never as a dead stream.
        assert "viewer_attached" not in await _status(printer_id, raw)
    finally:
        await _close(printer_id, wall)


@pytest.mark.asyncio
async def test_a_status_read_without_an_id_answers_as_it_always_did(media_runtime):
    printer_id = 987_705
    wall = await _open(printer_id, WALL)
    try:
        assert "viewer_attached" not in await _status(printer_id, None)
        assert camera_routes.stream_liveness(printer_id) == {"active": True, "stalled": False}
    finally:
        await _close(printer_id, wall)


def test_one_id_opened_twice_stays_attached_until_both_are_gone():
    first = camera_viewers.attach("printer-1", WALL)
    second = camera_viewers.attach("printer-1", WALL)

    first()
    first()  # a release is spent once
    assert camera_viewers.attached("printer-1", WALL) is True
    second()
    assert camera_viewers.attached("printer-1", WALL) is False


def test_the_registry_is_bounded_and_a_turned_away_id_is_unknown_not_dead():
    releases = [
        camera_viewers.attach("camera-9", f"viewer-{index:08d}")
        for index in range(camera_viewers.MAX_VIEWERS_PER_CAMERA)
    ]
    camera_viewers.attach("camera-9", "oneTooMany_00000")

    assert len(camera_viewers._attached["camera-9"]) == camera_viewers.MAX_VIEWERS_PER_CAMERA
    assert camera_viewers.attached("camera-9", "oneTooMany_00000") is None
    assert camera_viewers.attached("camera-9", "viewer-00000000") is True

    for release in releases:
        release()
    assert camera_viewers._attached == {}
    assert camera_viewers.attached("camera-9", "oneTooMany_00000") is False
