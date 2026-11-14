"""The plate check lights the chamber whatever its frame comes from (upstream cf0ba31f1).

A live view lends the check its frame, and used to lend it with no light: the lease was taken only inside
the check's own capture. With the farm's "light while the camera is in use" off, a watched dark chamber was
compared with a reference calibrated in light and the print paused for an object that was not there.

The relay, its frame buffer and the lease are the real ones; the printer is a fake with the fields the lease
reads, and its ``lights_report`` is played back through ``note_light_report`` as printer_manager wires it.
"""

import asyncio
from types import SimpleNamespace

import pytest

from backend.app.services import camera_light, plate_detection as pd
from backend.tests.unit.live_media_harness import media_runtime, request, serve  # noqa: F401
from backend.tests.unit.test_camera_live_admission import FRAME_WAIT_SECONDS, live  # noqa: F401


class _Client:
    def __init__(self, *, lit: bool):
        self.state = SimpleNamespace(connected=True, chamber_light=lit, has_chamber_light=True)
        self.commands: list[bool] = []

    def set_chamber_light(self, on: bool) -> bool:
        self.commands.append(on)
        self.state.chamber_light = on
        return True


@pytest.fixture
def chamber(monkeypatch):
    def make(*, lit: bool) -> _Client:
        client = _Client(lit=lit)
        monkeypatch.setattr(camera_light, "_get_client", lambda _pid: client)
        return client

    camera_light._lights.clear()
    yield make
    for light in camera_light._lights.values():
        if light.release_task is not None:
            light.release_task.cancel()
    camera_light._lights.clear()


def _check(pid: int, fresh: bool) -> asyncio.Task:
    return asyncio.create_task(pd.capture_camera_image(pid, "192.0.2.60", "synthetic", "A1M", fresh=fresh))


@pytest.mark.asyncio
@pytest.mark.parametrize("fresh", [False, True])
async def test_a_watched_dark_chamber_is_lit_and_only_a_frame_from_after_the_light_is_judged(
    live,  # noqa: F811
    chamber,
    fresh,
    monkeypatch,
):
    pid, start, observer, calls, capture, old = live
    client = chamber(lit=False)
    monkeypatch.setattr(camera_light, "LIT_FRAME_MARGIN_SECONDS", 0.3)
    await start()  # the farm toggle is off: the viewer holds no light
    assert client.commands == []

    task = _check(pid, fresh)
    await asyncio.sleep(0.05)
    assert client.commands == [True], "the plate check lights the chamber although a view lends the frame"
    assert not task.done(), "the view's frame from before the light is not evidence"

    observer[0](b"\xff\xd8still-dark\xff\xd9")  # arrives before the printer reports the light on
    await asyncio.sleep(0.05)
    assert not task.done()

    await camera_light.note_light_report(pid, True)
    await asyncio.sleep(0.05)
    # Relayed after the report but inside the margin: exposed before the light reached the sensor.
    observer[0](b"\xff\xd8relayed-inside-the-margin\xff\xd9")
    await asyncio.sleep(0.05)
    assert not task.done(), "a frame relayed right after the report can still be a dark one"

    await asyncio.sleep(0.3)
    lit = b"\xff\xd8lit-frame\xff\xd9"
    observer[0](lit)
    frame, _source = await asyncio.wait_for(task, FRAME_WAIT_SECONDS)

    assert frame == lit
    assert len(calls) == 1, "no second reader beside the view"
    capture.assert_not_awaited()
    assert camera_light._lights[pid].refcount == 0, "the check gave its light back"


@pytest.mark.asyncio
async def test_a_chamber_already_lit_lends_its_buffered_frame_at_once(live, chamber):  # noqa: F811
    pid, start, observer, calls, capture, old = live
    client = chamber(lit=True)
    await start()

    frame, _source = await asyncio.wait_for(pd.capture_camera_image(pid, "192.0.2.60", "synthetic", "A1M"), 1)

    assert frame == old
    assert client.commands == [], "a light that was on is nobody's to switch"
    capture.assert_not_awaited()
