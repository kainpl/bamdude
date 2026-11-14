"""The worker's "still scene" verdict reaches the main process with each live frame (G09 review F2).

The verdict is the freeze watch's: after a redial the source showed the same picture. It is announced by the
stream loop, kept per producer in the worker's live registry, stamped on every relayed media frame and handed
to the relay by ``camera_runtime`` — the existing frame channel, no new RPC. What the main process does with
it is pinned in ``test_camera_still_scene_plate_check.py``.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest

from backend.app.services import external_camera as ec
from backend.app.services.camera_runtime import WorkerCameraRuntime
from backend.app.services.camera_worker_live import LiveProducerRegistry
from backend.app.services.camera_worker_media import WorkerMediaFrame, read_media_frame, write_media_frame
from backend.app.services.camera_worker_protocol import CameraWorkerProtocolError
from backend.tests.unit.test_camera_frozen_stream import BUILTIN, URL, Source, moving, picture, still

JPEG = b"\xff\xd8frame\xff\xd9"


# ------------------------------------------------------------ the stream loop


async def _timeline(source: Source, stream_id: str, *, profile=BUILTIN) -> list[tuple[bytes, bool]]:
    """Every published frame with the verdict that stood when it was published."""
    verdict = [False]
    published: list[tuple[bytes, bool]] = []
    stream = ec.generate_mjpeg_stream(
        URL,
        "rtsp",
        1000,
        rtsp_profile=profile,
        stop_event=source.stop_event,
        stream_id=stream_id,
        on_frame=lambda frame: published.append((frame, verdict[0])),
        on_still_scene=lambda value: verdict.__setitem__(0, value),
    )
    try:
        async for _chunk in stream:
            pass
    finally:
        await stream.aclose()
    return published


@pytest.mark.asyncio
@pytest.mark.parametrize("profile", [BUILTIN, None], ids=["builtin", "external"])
async def test_the_verdict_is_announced_when_the_redial_shows_the_same_picture(monkeypatch, profile):
    dark = picture("dark-chamber")
    source = Source(monkeypatch, [[dark] * 2000, [dark] * 30])
    published = await _timeline(source, f"verdict-still-{'b' if profile else 'e'}", profile=profile)
    first_session = [stood for _frame, stood in published[:21]]
    second_session = [stood for _frame, stood in published[21:]]
    assert not any(first_session), "one connection repeating a frame proves nothing: it may be frozen"
    assert second_session and all(second_session), "the redial showed the same picture: a still scene"


@pytest.mark.asyncio
async def test_the_verdict_is_withdrawn_with_the_first_changed_picture(monkeypatch):
    dark = picture("dark-chamber")
    source = Source(monkeypatch, [[dark] * 2000, [dark] * 5 + moving(3, "lights-on")])
    published = await _timeline(source, "verdict-withdrawn")
    tail = published[21:]
    assert [stood for _frame, stood in tail] == [True] * 5 + [False] * 3


@pytest.mark.asyncio
async def test_a_redial_that_shows_a_new_picture_earns_no_verdict(monkeypatch):
    source = Source(monkeypatch, [moving(5, "a") + still(2000, "a-4"), moving(5, "b")])
    published = await _timeline(source, "verdict-frozen")
    assert not any(stood for _frame, stood in published)


@pytest.mark.asyncio
async def test_a_failing_verdict_callback_does_not_take_the_stream_down(monkeypatch):
    dark = picture("dark-chamber")
    source = Source(monkeypatch, [[dark] * 2000, [dark] * 3])
    frames = 0

    def boom(_value):
        raise RuntimeError("relay gone")

    stream = ec.generate_mjpeg_stream(
        URL, "rtsp", 1000, rtsp_profile=BUILTIN, stop_event=source.stop_event, stream_id="verdict-boom",
        on_still_scene=boom,
    )  # fmt: skip
    try:
        async for _chunk in stream:
            frames += 1
    finally:
        await stream.aclose()
    assert frames == 21 + 3


# ------------------------------------------------------ the worker's registry


@pytest.mark.asyncio
async def test_the_registry_keeps_the_verdict_per_producer_and_drops_it_with_the_producer():
    registry = LiveProducerRegistry()
    gate = asyncio.Event()

    async def producer():
        await gate.wait()

    lease, _queue = await registry.subscribe("cam-a", producer)
    other, _other_queue = await registry.subscribe("cam-b", producer)
    assert registry.is_still("cam-a") is False

    registry.mark_still("cam-a", True)
    assert registry.is_still("cam-a") is True
    assert registry.is_still("cam-b") is False
    registry.mark_still("cam-a", False)
    assert registry.is_still("cam-a") is False

    registry.mark_still("cam-a", True)
    await registry.unsubscribe(lease)  # the last viewer left: the producer is cancelled
    assert registry.is_still("cam-a") is False, "a later producer of this camera starts without a verdict"

    registry.mark_still("cam-b", True)
    gate.set()  # the producer ends by itself
    await asyncio.sleep(0.01)
    assert registry.is_still("cam-b") is False
    await registry.unsubscribe(other)


# ------------------------------------------------------------ the media frame


class _Pipe:
    def __init__(self) -> None:
        self.reader = asyncio.StreamReader()

    def write(self, data: bytes) -> None:
        self.reader.feed_data(data)

    async def drain(self) -> None:
        pass


def _media(**extra) -> WorkerMediaFrame:
    return WorkerMediaFrame(
        generation=str(uuid.uuid4()),
        session_id=str(uuid.uuid4()),
        frame=JPEG,
        source="fresh",
        attempt_id=None,
        first_frame_ms=None,
        cleanup_ms=None,
        **extra,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("stands", [True, False])
async def test_a_media_frame_carries_the_verdict_across_the_process_boundary(stands):
    pipe = _Pipe()
    await write_media_frame(pipe, _media(still_scene=stands))
    received = await read_media_frame(pipe.reader)
    assert received.still_scene is stands
    assert received.frame == JPEG


def test_a_media_frame_has_no_verdict_unless_it_is_given_one():
    assert _media().still_scene is False


def test_a_verdict_that_is_not_a_boolean_is_a_protocol_error():
    with pytest.raises(CameraWorkerProtocolError):
        _media(still_scene="yes")


# ------------------------------------------------------- the runtime's adapter


class _Supervisor:
    def __init__(self, frames) -> None:
        self.queue: asyncio.Queue = asyncio.Queue()
        for frame in frames:
            self.queue.put_nowait(frame)
        self.queue.put_nowait(None)

    async def subscribe_external(self, **_kwargs):
        return "lease", self.queue

    subscribe_builtin = subscribe_external

    async def unsubscribe(self, _lease_id, _queue) -> None:
        pass


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["external", "builtin"])
async def test_the_runtime_hands_the_relay_each_frames_verdict_before_the_frame(kind):
    runtime = WorkerCameraRuntime(_Supervisor([_media(), _media(still_scene=True), _media()]))
    seen: list[object] = []
    common = {
        "identity": str(uuid.uuid4()),
        "fps": 5,
        "disconnect_event": asyncio.Event(),
        "on_frame": lambda frame: seen.append("frame"),
        "on_still_scene": seen.append,
    }
    stream = (
        runtime.stream_external(url="http://127.0.0.1/live", camera_type="rtsp", **common)
        if kind == "external"
        else runtime.stream_builtin(ip_address="192.0.2.9", access_code="code", model="X1C", **common)
    )
    async for _chunk in stream:
        pass
    assert seen == [False, "frame", True, "frame", False, "frame"]
