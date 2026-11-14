"""A changed (or deleted) printer camera does not keep serving its old upstream (upstream 31888777a leg 2).

The broadcaster key is ``printer-{id}`` and the worker identity is the printer's camera KIND — neither carries
the configuration, on purpose (a rotated access token must not add a producer). So a saved change has to retire
the running upstream itself: until it did, every viewer, including ones that reconnected, stayed on the old
source for as long as anybody watched, and background captures were lent the old camera's frame.

The relay, the broadcaster and the frame buffers are the real ones; the worker runtime is a fake that records
what it was asked to dial and when its predecessor let go.
"""

import asyncio
from types import SimpleNamespace

import pytest
import pytest_asyncio

from backend.app.api.routes import camera
from backend.app.services.external_camera import format_mjpeg_frame
from backend.tests.unit.live_media_harness import media_runtime, request as make_request, serve  # noqa: F401

FRAME_WAIT_SECONDS = 2.5
PID = 987_670


def _printer(**changes):
    values = {
        "id": PID,
        "connector_identity": "test-printer-incarnation",
        "ip_address": "192.0.2.61",
        "access_code": "synthetic",
        "model": "X1C",
        "external_camera_enabled": False,
        "external_camera_url": None,
        "external_camera_type": None,
        "external_camera_snapshot_url": None,
    }
    values.update(changes)
    return SimpleNamespace(**values)


@pytest_asyncio.fixture
async def relay(media_runtime):  # noqa: F811
    events: list[tuple[str, str]] = []
    views = []

    class Runtime:
        async def _stream(self, name, on_frame, disconnect_event):
            events.append(("dial", name))
            frame = b"\xff\xd8" + name.encode() + b"\xff\xd9"
            try:
                on_frame(frame)
                yield format_mjpeg_frame(frame)
                await disconnect_event.wait()
            finally:
                await asyncio.sleep(0.05)  # the worker letting go of the device
                events.append(("gone", name))

        def stream_builtin(self, **kwargs):
            return self._stream(f"builtin:{kwargs['ip_address']}", kwargs["on_frame"], kwargs["disconnect_event"])

        def stream_external(self, **kwargs):
            return self._stream(f"external:{kwargs['url']}", kwargs["on_frame"], kwargs["disconnect_event"])

    async def view(printer):
        request = make_request()
        external = bool(printer.external_camera_enabled and printer.external_camera_url)
        response = await camera._worker_stream_response(
            printer=printer, printer_id=PID, request=request, fps=5, runtime=Runtime(), builtin=not external
        )
        task, chunks, disconnect = serve(response, request)
        views.append((task, disconnect))
        first = await asyncio.wait_for(chunks.get(), FRAME_WAIT_SECONDS)
        return task, chunks, first["body"]

    yield view, events
    for task, disconnect in views:
        disconnect.set()
        await asyncio.wait_for(task, 2)
    await camera.shutdown_broadcaster(f"printer-{PID}")
    camera._active_worker_streams.pop(PID, None)
    camera._release_printer_frame_state(PID)


def test_only_what_the_live_view_dials_is_the_camera_source():
    builtin = _printer()
    assert camera.camera_source_of(builtin) == camera.camera_source_of(_printer())
    # Saved but unused external fields change nothing while the built-in camera is the source.
    assert camera.camera_source_of(
        _printer(external_camera_url="http://cam-a.invalid", external_camera_type="mjpeg")
    ) == (camera.camera_source_of(builtin))
    external = _printer(
        external_camera_enabled=True, external_camera_url="http://cam-a.invalid", external_camera_type="mjpeg"
    )
    assert camera.camera_source_of(external) != camera.camera_source_of(builtin)
    for change in (
        {"external_camera_url": "http://cam-b.invalid"},
        {"external_camera_type": "snapshot"},
        {"external_camera_snapshot_url": "http://cam-a.invalid/shot"},
        {"external_camera_enabled": False},
    ):
        changed = _printer(**{**vars(external), **change})
        assert camera.camera_source_of(changed) != camera.camera_source_of(external), change
    for change in ({"ip_address": "192.0.2.99"}, {"access_code": "rotated"}, {"model": "A1"}):
        assert camera.camera_source_of(_printer(**change)) != camera.camera_source_of(builtin), change
        # ...and none of the printer's own address matters to an external camera.
        assert camera.camera_source_of(_printer(**{**vars(external), **change})) == camera.camera_source_of(external)


@pytest.mark.asyncio
async def test_retiring_ends_the_old_upstream_before_the_new_source_is_dialled(relay):
    view, events = relay
    old = _printer(
        external_camera_enabled=True, external_camera_url="http://cam-a.invalid", external_camera_type="mjpeg"
    )
    watcher, _chunks, first = await view(old)
    assert b"external:http://cam-a.invalid" in first
    assert camera.live_frame_for_capture(PID)[1] is not None

    assert await camera.retire_printer_stream(PID) is True

    assert events == [("dial", "external:http://cam-a.invalid"), ("gone", "external:http://cam-a.invalid")]
    await asyncio.wait_for(watcher, FRAME_WAIT_SECONDS)  # the viewer's response ended: its page reconnects
    assert camera.live_frame_for_capture(PID) == (False, None), "the old camera's frame is nobody's any more"
    assert PID not in camera._snapshot_frames, "nor does the snapshot cache still answer with it"

    new = _printer(**{**vars(old), "external_camera_url": "http://cam-b.invalid"})
    _task, _chunks, first = await view(new)
    assert b"external:http://cam-b.invalid" in first
    assert events[2] == ("dial", "external:http://cam-b.invalid")
    assert b"cam-b" in camera.live_frame_for_capture(PID)[1]


@pytest.mark.asyncio
async def test_a_viewer_that_reconnects_while_the_old_upstream_is_closing_waits_for_it(relay):
    view, events = relay
    await view(_printer())
    retiring = asyncio.create_task(camera.retire_printer_stream(PID))
    await asyncio.sleep(0)  # the old upstream is being closed, not yet gone
    external = _printer(
        external_camera_enabled=True, external_camera_url="http://cam-a.invalid", external_camera_type="mjpeg"
    )
    _task, _chunks, first = await view(external)
    await retiring
    assert b"external:http://cam-a.invalid" in first
    assert events == [
        ("dial", "builtin:192.0.2.61"),
        ("gone", "builtin:192.0.2.61"),
        ("dial", "external:http://cam-a.invalid"),
    ], "the successor dialled only after its predecessor was confirmed gone"


@pytest.mark.asyncio
async def test_retiring_a_camera_nobody_watches_only_forgets_its_cached_snapshot():
    camera._remember_snapshot(PID, b"\xff\xd8old-camera\xff\xd9")
    try:
        assert await camera.retire_printer_stream(PID) is False
        assert camera._get_cached_snapshot(PID) is None
    finally:
        camera._snapshot_frames.pop(PID, None)
        camera._snapshot_frame_times.pop(PID, None)
