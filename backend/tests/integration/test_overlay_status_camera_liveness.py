"""The OBS overlay learns from ITS OWN feed whether the camera stream is alive.

A stream the server ends cleanly (the worker's reconnect budget ran out, the
viewer's authority was retired) fires nothing on the browser's ``<img>``: the
broadcast keeps a frozen frame. The floating window and the camera page ask
``/printers/{id}/camera/status`` — a session route an ``overlay`` token cannot
read — so the same answer rides the feed that token already polls.
"""

import time

import pytest

from backend.app.api.routes import camera, printers
from backend.tests.integration.test_layer_projection_surfaces import live  # noqa: F401 -- shared fixture
from backend.tests.integration.test_layer_source_writers import storage  # noqa: F401 -- shared fixture


@pytest.fixture
def stream(monkeypatch):
    """Register a worker stream for a printer, as ``_prepare`` does for a viewer."""

    def start(printer_id: int, *, uptime: float, since_frame: float | None) -> None:
        now = time.time()
        monkeypatch.setitem(camera._active_worker_streams, printer_id, ("generation", "rtsp"))
        monkeypatch.setitem(camera._stream_start_times, printer_id, now - uptime)
        if since_frame is not None:
            monkeypatch.setitem(camera._last_frame_times, printer_id, now - since_frame)

    return start


async def _camera(printer_id: int, db_session) -> dict:
    return (await printers.get_overlay_status(printer_id, db=db_session))["camera"]


async def test_no_stream_is_reported_as_not_active(db_session, printer_factory):
    printer = await printer_factory()

    assert await _camera(printer.id, db_session) == {"active": False, "stalled": False}


async def test_a_delivering_stream_is_alive(db_session, printer_factory, stream):
    printer = await printer_factory()
    stream(printer.id, uptime=120.0, since_frame=0.2)

    assert await _camera(printer.id, db_session) == {"active": True, "stalled": False}


@pytest.mark.parametrize("since_frame", [30.0, None])
async def test_a_stream_without_frames_is_stalled(db_session, printer_factory, stream, since_frame):
    printer = await printer_factory()
    stream(printer.id, uptime=60.0, since_frame=since_frame)

    assert await _camera(printer.id, db_session) == {"active": True, "stalled": True}


async def test_a_stream_that_has_just_started_is_given_its_grace(db_session, printer_factory, stream):
    printer = await printer_factory()
    stream(printer.id, uptime=2.0, since_frame=None)

    assert await _camera(printer.id, db_session) == {"active": True, "stalled": False}


@pytest.mark.parametrize(("uptime", "since_frame"), [(120.0, 0.2), (60.0, 30.0), (2.0, None)])
async def test_the_feed_and_the_session_route_give_one_answer(db_session, printer_factory, stream, uptime, since_frame):
    printer = await printer_factory()
    stream(printer.id, uptime=uptime, since_frame=since_frame)

    session = await camera.camera_status(printer.id)

    assert await _camera(printer.id, db_session) == {"active": session["active"], "stalled": session["stalled"]}


async def test_another_printers_stream_says_nothing_about_this_one(db_session, printer_factory, stream):
    watched = await printer_factory()
    other = await printer_factory()
    stream(other.id, uptime=60.0, since_frame=30.0)

    assert await _camera(watched.id, db_session) == {"active": False, "stalled": False}


async def test_a_connected_printer_reports_it_too_and_nothing_more(live, db_session, stream):  # noqa: F811
    printer = live[0]
    stream(printer.id, uptime=120.0, since_frame=0.2)

    overlay = await printers.get_overlay_status(printer.id, db=db_session)

    assert overlay["connected"] is True
    # Two flags: no source type, no telemetry, no viewer count for a URL token.
    assert overlay["camera"] == {"active": True, "stalled": False}


async def test_an_overlay_that_names_its_stream_learns_whether_that_one_is_attached(
    db_session, printer_factory, stream
):
    """Two overlays of one printer share the upstream: the one the server ended must not read "alive"."""
    from backend.app.services import camera_viewers

    printer = await printer_factory()
    stream(printer.id, uptime=120.0, since_frame=0.2)
    release = camera_viewers.attach(f"printer-{printer.id}", "obsScene_one_0001")
    try:
        mine = await printers.get_overlay_status(printer.id, viewer="obsScene_one_0001", db=db_session)
        ended = await printers.get_overlay_status(printer.id, viewer="obsScene_two_0002", db=db_session)
        malformed = await printers.get_overlay_status(printer.id, viewer="not an id", db=db_session)
    finally:
        release()

    assert mine["camera"] == {"active": True, "stalled": False, "viewer_attached": True}
    assert ended["camera"] == {"active": True, "stalled": False, "viewer_attached": False}
    # A malformed id is ignored: the two flags, as for a feed that names no stream.
    assert malformed["camera"] == {"active": True, "stalled": False}
