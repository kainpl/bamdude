"""Cloud Link client loop — the socket, and everything that can go wrong on it.

The portal here is a real aiohttp WebSocket server on a loopback port, scripted
per test. Mocking ``ws_connect`` would test our idea of aiohttp; a socket tests
the URL we actually built, the frames we actually serialised, and the way a
half-closed connection actually surfaces.

Four things these tests exist to pin, none of which a happy path would notice:

* **A reconnect is a fresh start, not a resumption.** Every successful hello
  clears the uplink's outbox and sends a new snapshot, so a frame built for a
  socket that has died can never arrive *after* the picture that replaced it.
* **The reader outlives what it dispatches.** A handler that raises must cost
  one command, not the link — and the frame it failed on is deliberately left
  unanswered rather than answered with a guess.
* **A post-action that raises costs no more than a handler that raises.** The
  camera snapshot runs on the reader task after its ``cmd_result`` has gone
  out, so an escaping exception would drop the socket over one unplugged
  camera. Pinned by making the upload explode and then insisting the next
  command is still answered on the same connection.
* **An attacker cannot make us write.** ``dispatch`` audits every unknown
  command, so a portal that has been taken over could fill the audit table by
  spamming names. The reader stops feeding it after a handful and answers on
  its own, which keeps the wire contract and bounds the table.

Timings are injected everywhere — heartbeat, backoff base, pump idle — so the
suite runs in milliseconds and never sleeps on a real interval.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import uuid
from contextlib import asynccontextmanager

import aiohttp
import pytest
from aiohttp import WSMsgType, web
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.app.core.config import APP_VERSION
from backend.app.models.cloud_link import CloudLinkAudit
from backend.app.services.cloud_link import client as client_module, snapshot as snapshot_module
from backend.app.services.cloud_link.client import (
    AGENT_CAPABILITIES,
    BACKOFF_CAP_S,
    DEFAULT_HEARTBEAT_INTERVAL_S,
    LINK_PATH,
    MIN_THROTTLE_S,
    CloudLinkClient,
    ws_url,
)
from backend.app.services.cloud_link.commands import ALLOWED_COMMANDS, CommandContext, PostAction, dispatch
from backend.app.services.cloud_link.schemas import Cmd, CmdData, Event, EventData
from backend.app.services.cloud_link.store import get_config, save_credentials
from backend.app.services.cloud_link.uplink import Uplink

# --------------------------------------------------------------- the fixtures


def portal_frame(frame_type: str, data: dict | None = None, **extra) -> dict:
    """One envelope v1 frame as the portal would put it on the wire."""
    return {
        "v": 1,
        "id": uuid.uuid4().hex,
        "ts": "2026-08-24T12:00:00Z",
        "type": frame_type,
        "data": data if data is not None else {},
        **extra,
    }


def hello_ok_frame(heartbeat_interval_s: float = 0.05, throttle_min_interval_s: float = 0.0) -> dict:
    return portal_frame(
        "hello_ok",
        {
            "envelope_version": 1,
            "heartbeat_interval_s": heartbeat_interval_s,
            "throttle_min_interval_s": throttle_min_interval_s,
        },
    )


def hello_err_frame(code: str) -> dict:
    return portal_frame("hello_err", {"code": code})


def cmd(name: str, frame_id: str, args: dict | None = None) -> dict:
    return portal_frame("cmd", {"cmd": name, "args": args or {}}, id=frame_id)


def is_type(frame_type: str):
    """A predicate for :meth:`Portal.expect`."""

    def matches(frame: dict) -> bool:
        return frame.get("type") == frame_type

    return matches


class Portal:
    """An in-process portal running one scripted conversation per connection.

    Everything the agent sends is recorded by a pump task, so a script only has
    to *wait for* a frame and *send* one — it never has to interleave reads with
    the frames the agent produces on its own (snapshots, heartbeats, statuses).
    """

    def __init__(self, script):
        self._script = script
        self._bell = asyncio.Event()
        #: Every frame received, across every connection, in arrival order.
        self.frames: list[dict] = []
        #: The same frames, split by connection index.
        self.per_connection: list[list[dict]] = []
        self.connections = 0

    async def handler(self, request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        index = self.connections
        self.connections += 1
        self.per_connection.append([])
        pump = asyncio.create_task(self._pump(ws, index))
        try:
            await self._script(ws, self, index)
            # Hold the socket open until the agent lets go — a handler that
            # returns closes the connection, which every test would then read
            # as an unexpected drop.
            await pump
        finally:
            pump.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await pump
        return ws

    async def _pump(self, ws, index: int) -> None:
        async for msg in ws:
            if msg.type is not WSMsgType.TEXT:
                continue
            frame = json.loads(msg.data)
            self.frames.append(frame)
            self.per_connection[index].append(frame)
            self._bell.set()

    async def expect(self, predicate, *, connection: int | None = None, timeout: float = 5.0) -> dict:
        """Wait for a recorded frame matching ``predicate``, or fail the test."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        seen = 0
        while True:
            # Cleared BEFORE the scan: a frame arriving between the scan and the
            # wait must leave the bell ringing, or this hangs to the deadline.
            self._bell.clear()
            if connection is None:
                source = self.frames
            elif connection < len(self.per_connection):
                source = self.per_connection[connection]
            else:
                source = []  # that connection has not been opened yet
            for frame in source[seen:]:
                if predicate(frame):
                    return frame
            seen = len(source)
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise AssertionError(f"no matching frame within {timeout}s — got {[f['type'] for f in self.frames]}")
            try:
                await asyncio.wait_for(self._bell.wait(), remaining)
            except TimeoutError:
                raise AssertionError(
                    f"no matching frame within {timeout}s — got {[f['type'] for f in self.frames]}"
                ) from None

    async def accept(self, ws, index: int, **hello_ok_kwargs) -> dict:
        """Wait for the agent's hello and answer it. Returns the hello."""
        hello = await self.expect(is_type("hello"), connection=index)
        await ws.send_json(hello_ok_frame(**hello_ok_kwargs))
        return hello


@pytest.fixture
async def portal():
    """Start scripted portals, hand back their base URL, take them down after."""
    runners = []

    async def _start(script) -> tuple[Portal, str]:
        instance = Portal(script)
        app = web.Application()
        app.router.add_get(LINK_PATH, instance.handler)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        runners.append(runner)
        port = site._server.sockets[0].getsockname()[1]
        return instance, f"http://127.0.0.1:{port}"

    yield _start

    for runner in runners:
        await runner.cleanup()


@pytest.fixture
def session_factory(test_engine):
    """The shape ``core/database.async_session`` has — the client opens its own
    short session per read and per write, exactly as the store expects."""
    return async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)


class FakeManager:
    """``printer_manager`` narrowed to what the uplink asks of it."""

    def get_status(self, printer_id: int):
        return None

    def get_model(self, printer_id: int):
        return None

    def get_printer(self, printer_id: int):
        return None


async def pair_with(session_factory, portal_url: str, instance_id: str = "inst_1", secret: str = "s3cr3t") -> None:
    async with session_factory() as session:
        config = await get_config(session)
        config.portal_url = portal_url
        config.enabled = True
        await session.commit()
        await save_credentials(session, instance_id, secret)


def make_client(session_factory, uplink: Uplink | None = None, **overrides) -> CloudLinkClient:
    settings = {
        "backoff_base_s": 0.01,
        "backoff_cap_s": 0.05,
        "idle_sleep_s": 0.01,
        "connect_timeout_s": 2.0,
        "handshake_timeout_s": 2.0,
    }
    settings.update(overrides)
    return CloudLinkClient(
        session_factory=session_factory,
        uplink=uplink if uplink is not None else Uplink(manager=FakeManager()),
        **settings,
    )


@asynccontextmanager
async def running(client: CloudLinkClient, timeout: float = 10.0):
    """Run the client for the body, then stop it and insist that it stopped."""
    stop = asyncio.Event()
    task = asyncio.create_task(client.run(stop))
    try:
        yield task
    finally:
        stop.set()
        with contextlib.suppress(TimeoutError, asyncio.CancelledError):
            await asyncio.wait_for(task, timeout)


async def audit_kinds(session_factory) -> list[str]:
    async with session_factory() as session:
        rows = (await session.execute(select(CloudLinkAudit).order_by(CloudLinkAudit.id))).scalars()
        return [row.kind for row in rows]


async def read_config(session_factory):
    async with session_factory() as session:
        return await get_config(session)


#: asyncio's own transport machinery. The proactor loop keeps a pending
#: ``accept`` task alive for the test portal's listening socket for as long as
#: the server runs, and it is created lazily — during the client's run, which
#: puts it squarely in the diff below. It is the event loop's, not the
#: client's. ⚠️ ``locks.py`` is deliberately NOT in this list: ``stop_event
#: .wait()`` lives there and is precisely the task the assertion must catch.
LOOP_TRANSPORT_FILES = ("windows_events.py", "proactor_events.py", "selector_events.py")


def is_loop_transport(task: asyncio.Task) -> bool:
    code = getattr(task.get_coro(), "cr_code", None)
    if code is None:
        return False
    path = code.co_filename.replace("\\", "/")
    return "/asyncio/" in path and path.rsplit("/", 1)[-1] in LOOP_TRANSPORT_FILES


async def leaked_since(before: set, timeout: float = 2.0) -> set:
    """Tasks that are still pending and were not there before the client ran.

    A snapshot diff rather than a filter on the coroutine's repr: the tasks
    ``run()`` owns are not only its own methods — ``stop_event.wait()`` is a
    bare asyncio coroutine, and a version of ``_live`` that forgot to cancel it
    would leak a task that no name-based filter would ever name.

    The wait exists for the portal's own request handler, which finishes a loop
    turn or two after the agent closes the socket; a genuinely leaked task
    never finishes and so survives the whole window.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        leaked = {t for t in asyncio.all_tasks() if not t.done() and not is_loop_transport(t)} - before
        if not leaked or loop.time() >= deadline:
            return leaked
        await asyncio.sleep(0.01)


# ------------------------------------------------------------------ the URL


def test_the_ws_url_is_derived_by_parsing_not_by_string_surgery():
    """``https`` → ``wss``, and the portal's own path prefix survives.

    Parsed rather than matched on a prefix because a stored URL may carry an
    uppercase scheme (``HTTPS://…`` is the same URL to every browser) and a
    path (a portal proxied under ``/cloud``). A ``startswith``/``replace`` pair
    gets both wrong in a way that surfaces as "the portal is down".
    """
    assert ws_url("https://cloud.bamdude.top") == "wss://cloud.bamdude.top/link/v1"
    assert ws_url("HTTPS://Cloud.Bamdude.Top") == "wss://Cloud.Bamdude.Top/link/v1"
    assert ws_url("http://localhost:3002") == "ws://localhost:3002/link/v1"
    assert ws_url("https://example.test/cloud/") == "wss://example.test/cloud/link/v1"
    assert ws_url("  https://example.test/cloud  ") == "wss://example.test/cloud/link/v1"


def test_a_url_already_written_as_a_websocket_is_left_alone():
    """``wss://`` is what the agent was going to build anyway."""
    assert ws_url("wss://cloud.bamdude.top") == "wss://cloud.bamdude.top/link/v1"
    assert ws_url("ws://127.0.0.1:9000") == "ws://127.0.0.1:9000/link/v1"


def test_a_query_or_fragment_belongs_to_a_pasted_url_not_to_the_socket():
    assert ws_url("https://example.test/cloud?ref=email#top") == "wss://example.test/cloud/link/v1"


# -------------------------------------------------------------- the backoff


def test_the_backoff_doubles_from_one_second_and_stops_at_five_minutes():
    """Base 1 s × 2, capped at 300 s. Pinned with the jitter wound to nothing."""
    client = CloudLinkClient(
        session_factory=None,
        uplink=Uplink(manager=FakeManager()),
        rng=lambda: 0.5,  # 0.5 → the jitter term is exactly zero
    )
    delays = [client._next_delay() for _ in range(12)]
    assert delays[:6] == [1.0, 2.0, 4.0, 8.0, 16.0, 32.0]
    assert delays[-1] == BACKOFF_CAP_S
    assert max(delays) == BACKOFF_CAP_S


def test_the_backoff_jitter_stays_inside_twenty_percent():
    """±20 %, so a farm of agents reconnecting after a portal restart does not
    arrive as one synchronised wave."""
    lowest = CloudLinkClient(session_factory=None, uplink=Uplink(manager=FakeManager()), rng=lambda: 0.0)
    highest = CloudLinkClient(session_factory=None, uplink=Uplink(manager=FakeManager()), rng=lambda: 1.0)
    assert lowest._next_delay() == pytest.approx(0.8)
    assert highest._next_delay() == pytest.approx(1.2)


# ------------------------------------------------------------ the happy path


async def test_the_handshake_says_who_we_are_and_what_we_speak(session_factory, portal):
    async def script(ws, instance, index):
        await instance.accept(ws, index)

    instance, url = await portal(script)
    await pair_with(session_factory, url, instance_id="inst_abc", secret="the-secret")

    async with running(make_client(session_factory)):
        hello = await instance.expect(is_type("hello"))

    assert hello["v"] == 1
    assert hello["data"] == {
        "instance_id": "inst_abc",
        "secret": "the-secret",
        "agent_version": APP_VERSION,
        "envelope_versions": [1],
        "capabilities": ["camera_snapshot"],
    }, "the version comes from the constant, never a literal"


def test_every_capability_the_hello_claims_is_a_command_the_agent_answers():
    """Drift guard between the two halves of one promise.

    ``capabilities`` is what the portal reads to decide which buttons to offer;
    ``ALLOWED_COMMANDS`` is what the agent will actually run. A capability with
    no command behind it is a portal feature that fails on click, and it is
    invisible on both sides until a user finds it.
    """
    assert set(AGENT_CAPABILITIES) <= set(ALLOWED_COMMANDS)


def test_the_hello_cannot_be_talked_into_a_shared_capability_list():
    """The frame gets a copy, not the module constant.

    ``capabilities`` is ``list[str]`` on the wire and pydantic keeps whatever
    object it was handed. Passing the constant itself would let anything that
    mutated one frame's list edit what every later hello claims — which is why
    the constant is a tuple and the frame is built with ``list(...)``.
    """
    client = CloudLinkClient(session_factory=None, uplink=Uplink(manager=FakeManager()))

    first = client._hello("inst", "secret")
    first.data.capabilities.append("reboot_printer")

    assert client._hello("inst", "secret").data.capabilities == list(AGENT_CAPABILITIES)


async def test_a_successful_hello_is_followed_by_a_snapshot_then_heartbeats(session_factory, portal):
    """The snapshot is the portal's whole picture of the farm; the heartbeats
    are what tell it the picture is still current."""

    async def script(ws, instance, index):
        await instance.accept(ws, index, heartbeat_interval_s=0.05)

    instance, url = await portal(script)
    await pair_with(session_factory, url)

    async with running(make_client(session_factory)):
        snapshot = await instance.expect(is_type("snapshot"))
        await instance.expect(is_type("heartbeat"))

    assert snapshot["data"] == {"printers": []}, "nothing is published, so the portal is told exactly that"
    types = [frame["type"] for frame in instance.frames]
    assert types[0] == "hello"
    assert types[1] == "snapshot", "the snapshot goes out before anything else the agent has to say"


async def test_connecting_stamps_the_row_and_records_it_in_the_audit(session_factory, portal):
    async def script(ws, instance, index):
        await instance.accept(ws, index)

    instance, url = await portal(script)
    await pair_with(session_factory, url)
    async with session_factory() as session:
        config = await get_config(session)
        config.last_error = "whatever went wrong last time"
        await session.commit()

    async with running(make_client(session_factory)):
        await instance.expect(is_type("snapshot"))

    config = await read_config(session_factory)
    assert config.last_connected_at is not None
    assert config.last_error is None, "a working link is the answer to the last one that did not"
    assert "connect" in await audit_kinds(session_factory)


# ------------------------------------------------------------- the commands


async def test_a_ping_is_answered_with_a_result_carrying_the_requests_id(session_factory, portal):
    async def script(ws, instance, index):
        await instance.accept(ws, index)
        await instance.expect(is_type("snapshot"), connection=index)
        await ws.send_json(cmd("ping", "cmd-ping-1"))

    instance, url = await portal(script)
    await pair_with(session_factory, url)

    async with running(make_client(session_factory)):
        result = await instance.expect(is_type("cmd_result"))

    assert result["re"] == "cmd-ping-1"
    assert result["id"] != "cmd-ping-1", "a result is its own frame"
    assert result["data"]["ok"] is True
    assert result["data"]["payload"] == {"pong": True}


async def test_a_resync_produces_a_second_snapshot_after_its_result(session_factory, portal):
    """The post-action runs after the acknowledgement, never instead of it."""

    async def script(ws, instance, index):
        await instance.accept(ws, index)
        await instance.expect(is_type("snapshot"), connection=index)
        await ws.send_json(cmd("resync", "cmd-resync-1"))

    instance, url = await portal(script)
    await pair_with(session_factory, url)

    async with running(make_client(session_factory)):
        first = await instance.expect(is_type("snapshot"))
        await instance.expect(is_type("cmd_result"))
        await instance.expect(lambda f: f["type"] == "snapshot" and f is not first)

    types = [frame["type"] for frame in instance.frames if frame["type"] in {"snapshot", "cmd_result"}]
    assert types[:3] == ["snapshot", "cmd_result", "snapshot"]


async def test_a_requested_snapshot_is_sent_by_the_pump(session_factory, portal):
    """The service's way of saying "the publish set changed".

    It goes out on the pump rather than from the caller, because the socket
    belongs to the three tasks of a connection — a caller sending directly
    would be a fourth writer racing them.
    """

    async def script(ws, instance, index):
        await instance.accept(ws, index)
        await instance.expect(is_type("snapshot"), connection=index)

    instance, url = await portal(script)
    await pair_with(session_factory, url)
    client = make_client(session_factory)

    async with running(client):
        first = await instance.expect(is_type("snapshot"))
        client.request_snapshot()
        await instance.expect(lambda f: f["type"] == "snapshot" and f is not first)

    assert [f["type"] for f in instance.frames].count("snapshot") == 2


async def test_a_snapshot_asked_for_while_the_link_was_down_is_not_sent_twice(session_factory, portal):
    """A reconnect sends a snapshot of its own, so the pending ask is answered.

    Left standing, the flag would fire a second, identical snapshot one pump
    cycle after every connect — for a farm that had asked once, weeks ago.
    """

    async def script(ws, instance, index):
        await instance.accept(ws, index)
        await instance.expect(is_type("snapshot"), connection=index)

    instance, url = await portal(script)
    await pair_with(session_factory, url)
    client = make_client(session_factory)
    client.request_snapshot()  # while there is no connection at all

    async with running(client):
        await instance.expect(is_type("snapshot"))
        # Several pump cycles at the injected 0.01 s idle sleep.
        await asyncio.sleep(0.1)

    assert [f["type"] for f in instance.frames].count("snapshot") == 1


async def test_an_inbound_heartbeat_from_the_portal_is_tolerated(session_factory, portal):
    """The portal may beat back. It is not a command and it is not an error —
    a reader that treated an unhandled frame as fatal would drop the link on a
    portal that was doing nothing wrong."""

    async def script(ws, instance, index):
        await instance.accept(ws, index)
        await instance.expect(is_type("snapshot"), connection=index)
        await ws.send_json(portal_frame("heartbeat"))
        await ws.send_json(cmd("ping", "after-the-beat"))

    instance, url = await portal(script)
    await pair_with(session_factory, url)

    async with running(make_client(session_factory)):
        result = await instance.expect(is_type("cmd_result"))

    assert result["re"] == "after-the-beat", "the reader kept reading past the heartbeat"


async def test_a_frame_that_is_not_a_valid_envelope_is_ignored_not_fatal(session_factory, portal):
    """Garbage on the wire costs one frame. The link is the farm's only channel
    to its portal — dropping it over a malformed message would hand anything
    able to inject one a way to keep the farm offline."""

    async def script(ws, instance, index):
        await instance.accept(ws, index)
        await instance.expect(is_type("snapshot"), connection=index)
        await ws.send_str("not json at all")
        await ws.send_str("[1, 2, 3]")
        await ws.send_json({"v": 1, "type": "cmd"})  # no id, no ts, no data
        await ws.send_json(portal_frame("snapshot", {"printers": []}))  # valid, but not ours to act on
        await ws.send_json(cmd("ping", "still-here"))

    instance, url = await portal(script)
    await pair_with(session_factory, url)

    async with running(make_client(session_factory)):
        result = await instance.expect(is_type("cmd_result"))

    assert result["re"] == "still-here"


async def test_a_handler_that_raises_costs_one_command_and_leaves_it_unanswered(session_factory, portal, monkeypatch):
    """A dispatcher fault must not end the connection.

    The failed frame is deliberately NOT answered: the handler got far enough
    to raise, so the agent does not know whether the work happened, and
    ``ok=false`` would be a claim it cannot make. The portal times the request
    out — which is the honest outcome — and the audit row is what an operator
    finds afterwards.
    """
    real_dispatch = dispatch

    async def exploding_dispatch(cmd_frame, ctx):
        if cmd_frame.data.cmd == "ping" and cmd_frame.id == "boom":
            raise RuntimeError("the handler fell over")
        return await real_dispatch(cmd_frame, ctx)

    monkeypatch.setattr(client_module, "dispatch", exploding_dispatch)

    async def script(ws, instance, index):
        await instance.accept(ws, index)
        await instance.expect(is_type("snapshot"), connection=index)
        await ws.send_json(cmd("ping", "boom"))
        await ws.send_json(cmd("ping", "survivor"))

    instance, url = await portal(script)
    await pair_with(session_factory, url)

    async with running(make_client(session_factory)):
        result = await instance.expect(is_type("cmd_result"))

    assert result["re"] == "survivor", "the reader survived the raise and answered the next frame"
    assert [f for f in instance.frames if f["type"] == "cmd_result"] == [result], "the failed frame got no answer"
    assert "cmd:failed" in await audit_kinds(session_factory)


# ------------------------------------------------------- the camera snapshot


async def test_a_camera_snapshot_is_answered_first_and_uploaded_after(session_factory, portal, monkeypatch):
    """The ``cmd_result`` goes out before the camera is ever touched.

    A snapshot is a network round trip to a printer and another to the portal's
    upload URL; doing it before the answer would hold the reader — and the
    portal's request — open for the whole of it. The client loop is handed the
    two arguments the dispatcher validated and the session factory and uplink
    it owns, so the capture never has to reach back into the loop for them.
    """
    called = asyncio.Event()
    seen: dict = {}

    async def recording_capture_and_upload(**kwargs):
        seen.update(kwargs)
        called.set()

    monkeypatch.setattr(snapshot_module, "capture_and_upload", recording_capture_and_upload)

    async def script(ws, instance, index):
        await instance.accept(ws, index)
        await instance.expect(is_type("snapshot"), connection=index)
        await ws.send_json(
            cmd(
                "camera_snapshot",
                "cmd-snap-1",
                args={"printer_id": "7", "upload_url": "https://portal.test/put/abc"},
            )
        )

    instance, url = await portal(script)
    await pair_with(session_factory, url)
    uplink = Uplink(manager=FakeManager())

    async with running(make_client(session_factory, uplink=uplink)):
        result = await instance.expect(is_type("cmd_result"))
        await asyncio.wait_for(called.wait(), 5)

    assert result["re"] == "cmd-snap-1"
    assert result["data"] == {"ok": True}
    assert seen["printer_id"] == "7"
    assert seen["upload_url"] == "https://portal.test/put/abc"
    assert seen["uplink"] is uplink
    assert seen["session_factory"] is session_factory, "its own session, opened where the work happens"


async def test_a_camera_snapshot_with_bad_arguments_never_reaches_the_camera(session_factory, portal, monkeypatch):
    """The refusal is the dispatcher's and the loop respects it.

    ``bad_args`` comes back with no post-action, so there is nothing for the
    reader to run — pinned from the socket end because a reader that decided
    for itself which commands imply an upload would bypass the validation
    entirely.
    """

    async def never(**kwargs):
        raise AssertionError("a refused command must not reach the camera")

    monkeypatch.setattr(snapshot_module, "capture_and_upload", never)

    async def script(ws, instance, index):
        await instance.accept(ws, index)
        await instance.expect(is_type("snapshot"), connection=index)
        await ws.send_json(cmd("camera_snapshot", "cmd-snap-bad", args={"printer_id": "7"}))
        await ws.send_json(cmd("ping", "after-bad"))

    instance, url = await portal(script)
    await pair_with(session_factory, url)

    async with running(make_client(session_factory)):
        await instance.expect(lambda f: f.get("re") == "after-bad")

    refusal = next(f for f in instance.frames if f.get("re") == "cmd-snap-bad")
    assert refusal["data"] == {"ok": False, "error": "bad_args"}
    assert "cmd:camera_snapshot" in await audit_kinds(session_factory)


async def test_a_failing_upload_costs_the_snapshot_and_never_the_link(session_factory, portal, monkeypatch):
    """A camera that is unreachable is an ordinary Tuesday on a print farm.

    The upload runs on the reader task, so an exception escaping it would end
    the reader, drop the socket and take the whole link down over one offline
    camera. It is contained the same way a raising dispatch is — logged,
    audited ``ok=False``, and the next frame is answered as if nothing had
    happened.
    """

    async def exploding_capture_and_upload(**kwargs):
        raise RuntimeError("the camera is not answering")

    monkeypatch.setattr(snapshot_module, "capture_and_upload", exploding_capture_and_upload)

    async def script(ws, instance, index):
        await instance.accept(ws, index)
        await instance.expect(is_type("snapshot"), connection=index)
        await ws.send_json(
            cmd(
                "camera_snapshot",
                "cmd-snap-boom",
                args={"printer_id": "7", "upload_url": "https://portal.test/put/abc"},
            )
        )
        await ws.send_json(cmd("ping", "survivor"))

    instance, url = await portal(script)
    await pair_with(session_factory, url)

    async with running(make_client(session_factory)) as task:
        survivor = await instance.expect(lambda f: f.get("re") == "survivor")
        assert not task.done(), "the link outlived the failed upload"

    assert survivor["data"] == {"ok": True, "payload": {"pong": True}}
    snapped = next(f for f in instance.frames if f.get("re") == "cmd-snap-boom")
    assert snapped["data"] == {"ok": True}, (
        "the command was accepted before the upload was attempted — the audit carries the outcome"
    )

    async with session_factory() as session:
        failed = await session.scalar(
            select(func.count())
            .select_from(CloudLinkAudit)
            .where(CloudLinkAudit.kind == "cmd:camera_snapshot", CloudLinkAudit.ok.is_(False))
        )
    assert failed == 1, "the operator's only record that the snapshot never happened"
    assert instance.connections == 1, "a failed upload is not a reconnect"


async def test_a_post_action_nobody_wired_is_logged_and_not_silently_dropped(session_factory, monkeypatch, caplog):
    """The one branch that exists only to be loud.

    Unreachable while ``PostAction.kind`` and the loop's chain agree — which is
    exactly why it earns a line. A kind added to the dataclass and forgotten
    here would be a command that answers ``ok=true``, does nothing at all, and
    leaves no log, no audit row and no failing test to say so.
    """
    sent: list[dict] = []

    class FakeWebSocket:
        async def send_str(self, raw: str) -> None:
            sent.append(json.loads(raw))

    async def dispatch_with_an_unwired_kind(cmd_frame, ctx):
        result, _post = await dispatch(cmd_frame, ctx)
        return result, PostAction("dance")  # type: ignore[arg-type]

    monkeypatch.setattr(client_module, "dispatch", dispatch_with_an_unwired_kind)
    client = make_client(session_factory)
    frame = Cmd(v=1, id="c-1", ts="2026-08-24T12:00:00Z", type="cmd", data=CmdData(cmd="ping"))

    with caplog.at_level(logging.ERROR, logger="backend.app.services.cloud_link.client"):
        outcome = await client._handle_cmd(FakeWebSocket(), frame)

    assert outcome is None, "an unwired follow-up is not a reason to end the reader"
    assert [f["re"] for f in sent] == ["c-1"], "and the portal still got its answer"
    assert "dance" in caplog.text


# --------------------------------------------------- the unknown-command cap


async def test_unknown_commands_are_answered_forever_but_audited_only_five_times(session_factory, portal):
    """A compromised portal must not be able to make this farm write.

    ``dispatch`` records every command it refuses, which is right for the
    handful an operator will ever see and wrong for a portal that has been
    taken over and is spraying names. After the cap the reader answers on its
    own: the wire contract is unchanged — every request still gets its result —
    and the table stops growing.
    """
    names = [f"nope-{i}" for i in range(7)]

    async def script(ws, instance, index):
        await instance.accept(ws, index)
        await instance.expect(is_type("snapshot"), connection=index)
        for i, name in enumerate(names):
            await ws.send_json(cmd(name, f"unknown-{i}"))

    instance, url = await portal(script)
    await pair_with(session_factory, url)

    async with running(make_client(session_factory)):
        await instance.expect(lambda f: f.get("re") == "unknown-6")

    results = [f for f in instance.frames if f["type"] == "cmd_result"]
    assert [f["re"] for f in results] == [f"unknown-{i}" for i in range(7)], "every request is still answered"
    assert all(f["data"] == {"ok": False, "error": "unknown_command"} for f in results)

    async with session_factory() as session:
        audited = await session.scalar(
            select(func.count()).select_from(CloudLinkAudit).where(CloudLinkAudit.kind == "cmd:unknown")
        )
    assert audited == 5, "the cap is five per connection, and the sixth onward is answered without a row"


async def test_the_capped_refusal_is_the_same_answer_the_dispatcher_would_give(session_factory):
    """Drift guard. The reader short-circuits the dispatcher, so the two must
    agree on the answer — a divergence would be invisible until a portal
    noticed its seventh refusal looked different from its sixth."""
    frame = Cmd(v=1, id="c-1", ts="2026-08-24T12:00:00Z", type="cmd", data=CmdData(cmd="nope"))
    ctx = CommandContext(session_factory=session_factory, uplink=Uplink(manager=FakeManager()))

    dispatched, post_action = await dispatch(frame, ctx)
    refused = client_module.refuse_unknown(frame)

    assert post_action is None
    assert refused.re == dispatched.re == "c-1"
    assert refused.data.model_dump() == dispatched.data.model_dump()


# ------------------------------------------------------------- the teardown


async def test_a_revoke_command_persists_the_revocation_and_ends_the_run(session_factory, portal):
    """The kill switch. The result goes out first, then the link is torn down
    for good — a reconnect after a revoke would be the agent arguing with the
    portal about a decision the portal already made."""

    async def script(ws, instance, index):
        await instance.accept(ws, index)
        await instance.expect(is_type("snapshot"), connection=index)
        await ws.send_json(cmd("revoke", "cmd-revoke-1"))

    instance, url = await portal(script)
    await pair_with(session_factory, url)
    client = make_client(session_factory)

    before = set(asyncio.all_tasks())
    stop = asyncio.Event()
    task = asyncio.create_task(client.run(stop))
    try:
        result = await instance.expect(is_type("cmd_result"))
        await asyncio.wait_for(task, 5)
        # ⚠️ Checked HERE, before the stop below is ever set. This is the only
        # ending in the suite that ``stop_event`` never resolves, so it is the
        # only one where a stop-waiter that ``_live`` forgot to cancel stays
        # pending forever instead of quietly completing.
        assert await leaked_since(before | {task}) == set(), "the teardown left tasks behind"
    finally:
        stop.set()
        with contextlib.suppress(TimeoutError, asyncio.CancelledError):
            await asyncio.wait_for(task, 5)

    assert result["re"] == "cmd-revoke-1"
    assert result["data"]["ok"] is True, "the instruction was accepted — not a claim that the link survived"

    config = await read_config(session_factory)
    assert config.revoked is True
    assert config.last_error
    assert instance.connections == 1, "a revoked agent does not come back"
    kinds = await audit_kinds(session_factory)
    assert "cmd:revoke" in kinds and "revoked" in kinds


async def test_a_hello_err_revoked_stops_before_the_live_phase_ever_starts(session_factory, portal):
    """The same end reached from the other direction — the portal refuses the
    handshake outright. Nothing is published, nothing is heard, and the row
    ends up saying exactly what a revoke by command would have said."""

    async def script(ws, instance, index):
        await instance.expect(is_type("hello"), connection=index)
        await ws.send_json(hello_err_frame("revoked"))

    instance, url = await portal(script)
    await pair_with(session_factory, url)
    client = make_client(session_factory)

    stop = asyncio.Event()
    task = asyncio.create_task(client.run(stop))
    try:
        await asyncio.wait_for(task, 5)
    finally:
        stop.set()
        with contextlib.suppress(TimeoutError, asyncio.CancelledError):
            await asyncio.wait_for(task, 5)

    config = await read_config(session_factory)
    assert config.revoked is True
    assert config.last_error
    assert instance.connections == 1
    assert [f["type"] for f in instance.frames] == ["hello"], "no snapshot — the live phase never started"
    assert "revoked" in await audit_kinds(session_factory)


async def test_other_hello_errors_are_retried_because_they_may_be_the_portals_problem(session_factory, portal):
    """``bad_credentials`` is NOT ``revoked``.

    A portal mid-deploy, mid-migration or reading a replica can answer
    ``bad_credentials`` for a credential that is perfectly good. Stopping there
    would need a human to notice and re-enable the link; reconnecting on the
    full backoff costs one socket every few minutes and heals itself. Only
    ``revoked`` — the one code that states a decision rather than an outcome —
    stops the loop.
    """

    async def script(ws, instance, index):
        await instance.expect(is_type("hello"), connection=index)
        if index == 0:
            await ws.send_json(hello_err_frame("bad_credentials"))
            await ws.close()
            return
        await ws.send_json(hello_ok_frame())

    instance, url = await portal(script)
    await pair_with(session_factory, url)

    async with running(make_client(session_factory)):
        await instance.expect(is_type("snapshot"))

    assert instance.connections >= 2, "the agent came back"
    config = await read_config(session_factory)
    assert config.revoked is False, "a refused handshake is not a revocation"
    assert "hello_err" in await audit_kinds(session_factory)


# ---------------------------------------------------------- the reconnection


async def test_a_socket_that_drops_mid_session_brings_the_agent_back(session_factory, portal):
    """A dropped link is re-established from scratch: a new hello, and a new
    snapshot — because everything the portal knew about this farm was told to
    it over a socket that no longer exists."""

    async def script(ws, instance, index):
        await instance.accept(ws, index)
        await instance.expect(is_type("snapshot"), connection=index)
        if index < 2:  # the observer's cap: drop twice, then stay up
            await ws.close()

    instance, url = await portal(script)
    await pair_with(session_factory, url)

    async with running(make_client(session_factory)):
        await instance.expect(is_type("snapshot"), connection=2)

    assert instance.connections == 3
    for conversation in instance.per_connection[:3]:
        assert [f["type"] for f in conversation][:2] == ["hello", "snapshot"]
    assert "disconnect" in await audit_kinds(session_factory)


async def test_a_portal_that_accepts_then_drops_keeps_escalating_the_backoff(session_factory, portal):
    """A handshake is not a healthy link.

    This is the shape of a half-deployed portal: the upgrade succeeds, the
    ``hello_ok`` arrives, and the socket dies immediately. Resetting the
    attempt counter at ``hello_ok`` would clear it on every one of those, so
    the delay would never leave its base and the agent would spin at roughly
    1 Hz — a connect audit, a config UPDATE, a full snapshot build and a
    disconnect audit each time round, forever. The delays below have to GROW.
    """

    async def script(ws, instance, index):
        # A heartbeat interval this connection cannot possibly survive.
        await instance.accept(ws, index, heartbeat_interval_s=30.0)
        await ws.close()

    instance, url = await portal(script)
    await pair_with(session_factory, url)
    client = make_client(session_factory, backoff_base_s=0.01, backoff_cap_s=10.0, rng=lambda: 0.5)

    delays: list[float] = []
    real_next_delay = client._next_delay

    def recording_next_delay() -> float:
        delays.append(real_next_delay())
        return delays[-1]

    client._next_delay = recording_next_delay  # type: ignore[method-assign]

    async with running(client):
        await instance.expect(is_type("hello"), connection=3)

    assert delays[:3] == [0.01, 0.02, 0.04], f"the backoff must escalate, got {delays}"


async def test_a_link_that_survives_a_heartbeat_settles_the_backoff(session_factory, portal):
    """The other half of the rule: a connection that proves itself is forgiven.

    One heartbeat interval is the cheapest available proof that the link is
    more than a successful upgrade, so the counter clears at the first
    heartbeat and the next failure starts again from the base delay.

    ⚠️ **The settle is awaited, not read at the instant the heartbeat lands.**
    ``asyncio.sleep`` may return up to one clock resolution EARLY — 15.6 ms on
    Windows, most of a 20 ms interval — while ``_settle`` measures real elapsed
    time against the interval it was given. So the first heartbeat can go out
    just short of proving anything, and the counter clears at the second one
    instead: the same rule, one tick later, and harmless in production (a
    backoff forgiven 20 ms late). Read at the instant, this failed two runs in
    six on a Windows dev box.
    """

    async def script(ws, instance, index):
        await instance.accept(ws, index, heartbeat_interval_s=0.02)
        if index == 0:
            await ws.close()

    instance, url = await portal(script)
    await pair_with(session_factory, url)
    client = make_client(session_factory)

    async with running(client):
        await instance.expect(is_type("hello"), connection=1)
        assert client._attempt == 1, "the first connection's failure is still on the counter"
        await instance.expect(is_type("heartbeat"), connection=1)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 2.0
        while client._attempt != 0 and loop.time() < deadline:
            await asyncio.sleep(0.005)
        assert client._attempt == 0, "a link that lasted a heartbeat starts the backoff over"


async def test_every_successful_hello_clears_what_the_dead_socket_left_behind(session_factory, portal):
    """A frame built for the previous connection must not arrive after the
    snapshot that replaced it.

    The reset therefore has to happen before the snapshot is **built**, not
    merely before it is sent — building it is also what reseeds the uplink's
    identity and connection caches. The two calls are spied on the client's own
    side rather than inferred from the portal's arrivals: a socket write and
    the far end recording it are a loop turn apart, and an assertion on that
    gap would pass whichever order the client used.
    """
    uplink = Uplink(manager=FakeManager())
    calls: list[str] = []
    real_reset = uplink.reset_transient
    real_build = uplink.build_snapshot

    def spy_reset():
        calls.append("reset")
        real_reset()

    async def spy_build(session):
        calls.append("snapshot")
        return await real_build(session)

    uplink.reset_transient = spy_reset  # type: ignore[method-assign]
    uplink.build_snapshot = spy_build  # type: ignore[method-assign]

    async def script(ws, instance_, index):
        await instance_.accept(ws, index)
        await instance_.expect(is_type("snapshot"), connection=index)
        if index == 0:
            await ws.close()

    instance, url = await portal(script)
    await pair_with(session_factory, url)

    async with running(make_client(session_factory, uplink=uplink)):
        await instance.expect(is_type("snapshot"), connection=1)

    assert calls == ["reset", "snapshot", "reset", "snapshot"], (
        "one reset per successful hello, each before that hello's snapshot is built"
    )


async def test_resetting_the_transient_state_empties_the_outbox():
    """The unit behind the ordering test above. The outbox is the only state
    that survives a dead socket holding a frame nobody will ever want."""
    uplink = Uplink(manager=FakeManager())
    uplink.set_publish_set({1})
    uplink._connected[1] = False  # a first sighting, so the next push is an edge
    uplink.feed({"type": "printer_status", "printer_id": 1, "data": {"connected": True, "state": "IDLE"}})

    edge = await uplink.drain()
    assert edge is not None and edge.type == "event", "the edge frame, with its status waiting behind it"

    uplink.reset_transient()
    assert await uplink.drain() is None, "the status the dead socket never got is gone"


# ------------------------------------------------------------------ the pump


class TwoFrameUplink(Uplink):
    """An uplink holding a connection edge's two frames — the real producer of
    more than one frame per drain (``_status_or_connection_event``)."""

    def __init__(self):
        super().__init__(manager=FakeManager())
        self._scripted = [
            Event(
                v=1,
                id=f"edge-{n}",
                ts="2026-08-24T12:00:00Z",
                type="event",
                data=EventData(kind="printer_online", printer_id="1", detail={"n": n}),
            )
            for n in (1, 2)
        ]

    async def drain(self):
        return self._scripted.pop(0) if self._scripted else None


async def test_the_pump_drains_until_it_is_told_there_is_nothing_left(session_factory, portal):
    """One drain is not one cycle's work.

    ``Uplink.drain`` answers an outbox before it pops the queue, so a
    connection edge hands over two frames in two calls. A pump that drained
    once per cycle would deliver the second one an idle-sleep late — which the
    long sleep injected here turns from a latency bug into a visible failure.
    """

    async def script(ws, instance, index):
        await instance.accept(ws, index)

    instance, url = await portal(script)
    await pair_with(session_factory, url)
    client = make_client(session_factory, uplink=TwoFrameUplink(), idle_sleep_s=30.0)

    async with running(client):
        await instance.expect(lambda f: f.get("id") == "edge-2")

    ids = [f["id"] for f in instance.frames if f["type"] == "event"]
    assert ids == ["edge-1", "edge-2"], "both frames of one edge, in order, in one cycle"


async def test_the_portals_throttle_becomes_the_uplinks(session_factory, portal):
    """``hello_ok`` carries the portal's rate limit; the uplink is where it is
    enforced, so the handshake has to land it there."""

    async def script(ws, instance, index):
        await instance.accept(ws, index, throttle_min_interval_s=12.5)

    instance, url = await portal(script)
    await pair_with(session_factory, url)
    uplink = Uplink(manager=FakeManager())
    assert uplink.min_interval_s != 12.5

    async with running(make_client(session_factory, uplink=uplink)):
        await instance.expect(is_type("snapshot"))

    assert uplink.min_interval_s == 12.5


async def test_a_portal_asking_for_no_throttle_at_all_gets_the_floor(session_factory, portal):
    """``0`` means "send me everything", and everything is several frames a
    second per printer.

    ``throttle_min_interval_s`` is the single setting a portal can use to make
    this farm do more work, so it is the one place a compromised portal could
    turn a farm into its own uplink flood. The floor is what makes that not a
    thing the portal decides.
    """

    async def script(ws, instance, index):
        await instance.accept(ws, index, throttle_min_interval_s=0.0)

    instance, url = await portal(script)
    await pair_with(session_factory, url)
    uplink = Uplink(manager=FakeManager())

    async with running(make_client(session_factory, uplink=uplink)):
        await instance.expect(is_type("snapshot"))

    assert uplink.min_interval_s == MIN_THROTTLE_S


async def test_a_negative_throttle_gets_the_floor_too(session_factory, portal):
    """Below the floor is below the floor. Ignoring a nonsense value instead
    would leave whatever the previous connection negotiated in place, which is
    a different answer depending on how the agent got here."""

    async def script(ws, instance, index):
        await instance.accept(ws, index, throttle_min_interval_s=-1.0)

    instance, url = await portal(script)
    await pair_with(session_factory, url)
    uplink = Uplink(manager=FakeManager())
    uplink.min_interval_s = 42.0

    async with running(make_client(session_factory, uplink=uplink)):
        await instance.expect(is_type("snapshot"))

    assert uplink.min_interval_s == MIN_THROTTLE_S


# -------------------------------------------------------------- ws liveness


async def test_the_socket_is_opened_with_a_protocol_level_heartbeat(session_factory, portal, monkeypatch):
    """A blackholed path has to FAIL, not hang.

    Our own ``heartbeat`` frame proves nothing about the return leg: after a
    NAT rebind or behind a hung proxy both ends go on believing they are
    connected and TCP retransmit takes minutes to disagree. ``heartbeat=`` puts
    aiohttp's ping/pong under the conversation, which fails the reader instead
    — and a failed reader is a reconnect.

    The assertion is the argument rather than a severed connection: killing a
    path convincingly enough to out-wait a retransmit is not something a unit
    test can do honestly, and the argument is the whole of what this agent
    contributes.
    """
    seen: list[float | None] = []
    original = aiohttp.ClientSession.ws_connect

    def spy(self, url, **kwargs):
        seen.append(kwargs.get("heartbeat"))
        return original(self, url, **kwargs)

    monkeypatch.setattr(aiohttp.ClientSession, "ws_connect", spy)

    async def script(ws, instance, index):
        await instance.accept(ws, index)

    instance, url = await portal(script)
    await pair_with(session_factory, url)

    async with running(make_client(session_factory)):
        await instance.expect(is_type("snapshot"))

    assert seen == [DEFAULT_HEARTBEAT_INTERVAL_S], "the first connection has nothing negotiated yet"


async def test_the_next_socket_carries_the_interval_the_portal_negotiated(session_factory, portal, monkeypatch):
    """The asymmetry, stated as a test: the socket exists before ``hello_ok``
    can say anything, so connection N+1 is the first that can carry the value
    connection N settled on. Harmless — this is a liveness probe, not a rate
    the portal is promised."""
    seen: list[float | None] = []
    original = aiohttp.ClientSession.ws_connect

    def spy(self, url, **kwargs):
        seen.append(kwargs.get("heartbeat"))
        return original(self, url, **kwargs)

    monkeypatch.setattr(aiohttp.ClientSession, "ws_connect", spy)

    async def script(ws, instance, index):
        await instance.accept(ws, index, heartbeat_interval_s=7.5)
        if index == 0:
            # Drop the first connection so the agent comes back with what it
            # has just learned.
            await instance.expect(is_type("snapshot"), connection=0)
            await ws.close()

    instance, url = await portal(script)
    await pair_with(session_factory, url)

    async with running(make_client(session_factory)):
        await instance.expect(is_type("snapshot"), connection=1)

    assert seen[0] == DEFAULT_HEARTBEAT_INTERVAL_S
    assert seen[1] == 7.5


# ----------------------------------------------------------------- the guards


async def test_an_unpaired_agent_does_not_open_a_socket_at_all(session_factory, portal):
    """There is nothing to say hello with. Retrying would be a connection per
    backoff window for as long as the farm stays unpaired."""

    async def script(ws, instance, index):  # pragma: no cover — must never run
        await instance.accept(ws, index)

    instance, url = await portal(script)
    async with session_factory() as session:
        config = await get_config(session)
        config.portal_url = url
        config.enabled = True
        await session.commit()

    client = make_client(session_factory)
    stop = asyncio.Event()
    task = asyncio.create_task(client.run(stop))
    try:
        await asyncio.wait_for(task, 5)
    finally:
        stop.set()
        with contextlib.suppress(TimeoutError, asyncio.CancelledError):
            await asyncio.wait_for(task, 5)

    assert instance.connections == 0


async def test_stopping_the_agent_closes_the_socket_and_returns(session_factory, portal):
    """``run()`` owns every task it spawns and outlives none of them."""

    async def script(ws, instance, index):
        await instance.accept(ws, index)

    instance, url = await portal(script)
    await pair_with(session_factory, url)
    client = make_client(session_factory)

    before = set(asyncio.all_tasks())
    stop = asyncio.Event()
    task = asyncio.create_task(client.run(stop))
    await instance.expect(is_type("snapshot"))
    stop.set()
    await asyncio.wait_for(task, 5)

    assert task.done() and not task.cancelled()
    assert await leaked_since(before | {task}) == set(), "run() left tasks behind"
    assert "disconnect" in await audit_kinds(session_factory)


async def test_a_portal_that_never_answers_the_handshake_is_retried(session_factory, portal):
    """Silence is not a refusal, and it is not a reason to stay down."""

    async def script(ws, instance, index):
        await instance.expect(is_type("hello"), connection=index)
        if index == 0:
            return  # never answers — the handshake timeout is what ends this one
        await ws.send_json(hello_ok_frame())

    instance, url = await portal(script)
    await pair_with(session_factory, url)

    async with running(make_client(session_factory, handshake_timeout_s=0.2)):
        await instance.expect(is_type("snapshot"))

    assert instance.connections >= 2
    assert (await read_config(session_factory)).revoked is False


async def test_a_stop_during_a_long_backoff_returns_at_once(session_factory, portal):
    """Shutting down must not wait out a five-minute reconnect delay.

    The backoff is a wait on the stop event, not a sleep, so a farm being
    restarted comes down in milliseconds rather than whenever the next attempt
    happened to be due. A plain ``asyncio.sleep`` here would hold the whole
    application's shutdown for as long as the cap.
    """

    async def script(ws, instance, index):
        await instance.accept(ws, index)
        await ws.close()

    instance, url = await portal(script)
    await pair_with(session_factory, url)
    client = make_client(session_factory, backoff_base_s=30.0, backoff_cap_s=300.0)

    stop = asyncio.Event()
    task = asyncio.create_task(client.run(stop))
    try:
        # Wait until the client is genuinely inside the backoff: the counter is
        # advanced by ``_next_delay``, which is the first thing the wait does.
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 5
        while client._attempt == 0:
            assert loop.time() < deadline, "the client never reached its backoff"
            await asyncio.sleep(0.01)

        stop.set()
        await asyncio.wait_for(task, 2.0)
    finally:
        stop.set()
        with contextlib.suppress(TimeoutError, asyncio.CancelledError):
            await asyncio.wait_for(task, 5)

    assert task.done() and not task.cancelled()


async def test_an_audit_that_cannot_be_written_never_breaks_the_loop(session_factory, portal, monkeypatch):
    """An audit row is the operator's record, never a step of the protocol.

    A database that is locked, mid-migration or simply gone must cost the
    record and not the link — otherwise the one condition an operator most
    needs the link for is the condition that takes it away. Both bookkeeping
    paths are covered here: the ``disconnect`` written when the first socket
    dies, and the ``connect`` written when the second one comes up.
    """

    async def explodes(*args, **kwargs):
        raise RuntimeError("the audit table is locked")

    monkeypatch.setattr(client_module, "write_audit", explodes)

    async def script(ws, instance, index):
        await instance.accept(ws, index)
        await instance.expect(is_type("snapshot"), connection=index)
        if index == 0:
            await ws.close()
            return
        await ws.send_json(cmd("ping", "after-a-failed-audit"))

    instance, url = await portal(script)
    await pair_with(session_factory, url)

    async with running(make_client(session_factory)):
        result = await instance.expect(is_type("cmd_result"))

    assert result["re"] == "after-a-failed-audit", "the link reconnected and kept working"
    assert instance.connections >= 2
    assert await audit_kinds(session_factory) == [], "every row failed to write, and none of them mattered"
