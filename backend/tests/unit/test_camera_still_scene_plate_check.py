"""A still, lit plate under a live view passes the fresh plate check (G09 review F2).

Freshness advances only on CHANGED bytes, so a watched camera that shows a perfectly still plate from a
noise-free source failed every fresh check for as long as the wall stayed open — and ``order_auto_eject``
deferred on every tick. For a frozen source that is right. What tells the two apart is the worker's own
verdict: it ended the session, redialled, and the new connection showed the same picture — a still scene, a
live camera. That verdict travels with each relayed frame; while it stands for the current producer, a repeat
that ARRIVED after the check began is fresh evidence. Without it a repeat stays "not fresh", and the check
says so truthfully instead of calling the buffer empty.

The relay, its buffer accessor and the plate capture are the real ones (the ``live`` harness).
"""

from __future__ import annotations

import asyncio

import pytest

from backend.app.api.routes import camera
from backend.app.services import plate_detection as pd
from backend.tests.unit.live_media_harness import media_runtime, request, serve  # noqa: F401
from backend.tests.unit.test_camera_live_admission import FRAME_WAIT_SECONDS, live  # noqa: F401
from backend.tests.unit.test_plate_check_holds_the_light import chamber  # noqa: F401


def _fresh_check(pid: int) -> asyncio.Task:
    return asyncio.create_task(pd.capture_camera_image(pid, "192.0.2.60", "synthetic", "A1M", fresh=True))


def _relay(calls, observer, frame: bytes, *, still: bool) -> None:
    """One frame as ``camera_runtime`` hands it to the relay: the worker's verdict, then the bytes."""
    calls[0]["on_still_scene"](still)
    observer[0](frame)


@pytest.mark.asyncio
async def test_a_still_scene_passes_the_fresh_check_on_a_repeat_that_arrived_after_it_began(live):  # noqa: F811
    pid, start, observer, calls, capture, old = live
    await start()
    _relay(calls, observer, bytes(old), still=True)  # the redial showed the same picture

    task = _fresh_check(pid)
    await asyncio.sleep(0.05)
    assert not task.done(), "a frame that arrived before the check began is never evidence, verdict or not"

    _relay(calls, observer, bytes(old), still=True)
    frame, source = await asyncio.wait_for(task, FRAME_WAIT_SECONDS)
    assert frame == old
    assert "live view" in source
    assert len(calls) == 1, "no second reader beside the view"
    capture.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_repeat_without_the_verdict_is_not_fresh_and_the_reason_says_so(live, monkeypatch):  # noqa: F811
    """A frozen source, or a still one the worker has not judged yet: fail closed, truthfully."""
    pid, start, observer, calls, capture, old = live
    monkeypatch.setattr(pd, "LIVE_FRAME_WAIT_SECONDS", 0.4)
    await start()

    async def repeat():
        while True:
            _relay(calls, observer, bytes(old), still=False)
            await asyncio.sleep(0.05)

    repeating = asyncio.create_task(repeat())
    try:
        frame, reason = await asyncio.wait_for(_fresh_check(pid), FRAME_WAIT_SECONDS)
    finally:
        repeating.cancel()
        await asyncio.gather(repeating, return_exceptions=True)
    assert frame is None
    assert reason == pd.LIVE_VIEW_UNCHANGED
    assert "not changed" in reason and "empty" not in reason
    capture.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_view_that_delivers_nothing_is_still_reported_as_an_empty_buffer(live, monkeypatch):  # noqa: F811
    pid, start, observer, calls, capture, old = live
    monkeypatch.setattr(pd, "LIVE_FRAME_WAIT_SECONDS", 0.2)
    await start()
    frame, reason = await asyncio.wait_for(_fresh_check(pid), FRAME_WAIT_SECONDS)
    assert frame is None
    assert reason == pd.LIVE_VIEW_EMPTY


@pytest.mark.asyncio
async def test_the_verdict_falls_when_the_picture_changes(live, monkeypatch):  # noqa: F811
    pid, start, observer, calls, capture, old = live
    monkeypatch.setattr(pd, "LIVE_FRAME_WAIT_SECONDS", 0.3)
    await start()
    _relay(calls, observer, bytes(old), still=True)
    assert camera.still_scene_stands(pid)

    moved = b"\xff\xd8something-moved\xff\xd9"
    # Even a frame the worker stamped before it saw the change: a changed picture is no still scene.
    _relay(calls, observer, moved, still=True)
    assert not camera.still_scene_stands(pid)

    task = _fresh_check(pid)
    await asyncio.sleep(0.05)
    _relay(calls, observer, bytes(moved), still=False)  # the new picture, repeating, not judged yet
    frame, reason = await asyncio.wait_for(task, FRAME_WAIT_SECONDS)
    assert frame is None and reason == pd.LIVE_VIEW_UNCHANGED


@pytest.mark.asyncio
async def test_the_verdict_does_not_pass_to_another_producer(live, monkeypatch):  # noqa: F811
    pid, start, observer, calls, capture, old = live
    monkeypatch.setattr(pd, "LIVE_FRAME_WAIT_SECONDS", 0.3)
    await start()
    _relay(calls, observer, bytes(old), still=True)
    assert camera.still_scene_stands(pid)

    first = camera._active_worker_streams[pid]
    camera._active_worker_streams[pid] = ("a-new-producer", first[1])
    try:
        assert not camera.still_scene_stands(pid), "the verdict was about the producer that earned it"
        frame, _reason = await asyncio.wait_for(_fresh_check(pid), FRAME_WAIT_SECONDS)
        assert frame is None
    finally:
        camera._active_worker_streams[pid] = first


@pytest.mark.asyncio
async def test_the_verdict_goes_with_the_stream(live):  # noqa: F811
    pid, start, observer, calls, capture, old = live
    await start()
    _relay(calls, observer, bytes(old), still=True)
    camera._active_worker_streams.pop(pid)
    camera._release_printer_frame_state(pid)
    assert pid not in camera._still_scene_streams


@pytest.mark.asyncio
async def test_a_still_dark_scene_is_not_evidence_under_a_light_the_check_just_switched_on(live, chamber):  # noqa: F811
    """The verdict says the camera is alive, not that the picture is lit. A check that lit the chamber
    waits for the picture to CHANGE: a camera relays frames exposed a second or two earlier, and the
    dark picture goes on repeating for that long after the printer reports the light on."""
    from backend.app.services import camera_light

    pid, start, observer, calls, capture, old = live
    client = chamber(lit=False)
    await start()
    _relay(calls, observer, bytes(old), still=True)  # dark, still, confirmed alive

    task = _fresh_check(pid)
    await asyncio.sleep(0.05)
    assert client.commands == [True]
    await camera_light.note_light_report(pid, True)
    await asyncio.sleep(0.05)
    _relay(calls, observer, bytes(old), still=True)  # exposed before the light reached the sensor
    await asyncio.sleep(0.05)
    assert not task.done(), "a dark repeat was judged as the lit plate"

    lit = b"\xff\xd8lit-plate\xff\xd9"
    _relay(calls, observer, lit, still=False)
    frame, _source = await asyncio.wait_for(task, FRAME_WAIT_SECONDS)
    assert frame == lit
    capture.assert_not_awaited()
