"""Cloud Link uplink — what leaves this farm, and what never does.

The uplink is the read side of the link: it taps the broadcast every browser
already receives, keeps the handful of fields the contract asks for, and
throws the rest away. Three things are being pinned here and they are not
equally interesting.

**The tap must be invisible to the browsers.** ``ws_manager`` fans out every
status push in the product; a listener that raises, or one registered while
nobody is watching, must change nothing about that fan-out. That is why the
hook tests use a fake connection rather than a mock manager — the assertion is
that the browser still got its JSON.

**The message shapes are copied, not invented.** Every fixture below is the
literal dict one of the typed helpers in ``core/websocket.py`` builds, with the
``data`` half being the subset of ``printer_manager.printer_state_to_dict`` the
uplink reads. If those helpers change shape, these fixtures are what should
fail — a hand-written approximation would keep passing while the real
normalizer read nothing.

**The allowlist is the point.** ``on_print_start`` broadcasts the entire MQTT
payload under ``raw_data``, and the internal temperature dict carries private
bookkeeping keys. Both are in the fixtures on purpose, so the tests can assert
they do not come out the other end.
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.websocket import ConnectionManager
from backend.app.models.cloud_link import CloudLinkPrinter
from backend.app.models.printer import Printer
from backend.app.services.bambu_mqtt import HMSError, PrinterState
from backend.app.services.cloud_link.schemas import Temps, make_frame, parse_frame
from backend.app.services.cloud_link.uplink import (
    QUEUE_MAXSIZE,
    STATUS_FIELDS,
    TEMPERATURE_FIELDS,
    Uplink,
    _status_of,
)
from backend.app.services.printer_manager import printer_state_to_dict

# --------------------------------------------------------------- the fixtures


def status_message(printer_id: int = 1, **overrides) -> dict:
    """What ``ConnectionManager.send_printer_status`` puts on the wire.

    Outer keys copied from the helper; ``data`` is ``printer_state_to_dict``'s
    output narrowed to the keys the uplink reads, plus two it must ignore —
    ``gcode_file`` (a path, never a job name) and the internal
    ``_chamber_target_set_time`` / ``*_heating`` temperature bookkeeping.
    """
    data = {
        "connected": True,
        "state": "RUNNING",
        "current_print": "bracket_v3.gcode.3mf",
        "subtask_name": "bracket_v3",
        "gcode_file": "Metadata/plate_1.gcode",
        "progress": 42.0,
        "temperatures": {
            "bed": 60.0,
            "bed_target": 60.0,
            "nozzle": 219.5,
            "nozzle_target": 220.0,
            "chamber": 38.0,
            "chamber_target": 0.0,
            "bed_heating": False,
            "nozzle_heating": True,
            "chamber_heating": False,
            "_chamber_target_set_time": 1756000000.0,
        },
        "hms_errors": [],
    }
    data.update(overrides)
    return {"type": "printer_status", "printer_id": printer_id, "data": data}


def print_start_message(printer_id: int = 1) -> dict:
    """What ``send_print_start`` broadcasts — the callback payload, verbatim.

    ⚠️ ``raw_data`` is the whole MQTT push. It is here because it really is
    broadcast, and the uplink must not carry it.
    """
    return {
        "type": "print_start",
        "printer_id": printer_id,
        "data": {
            "filename": "Metadata/plate_1.gcode",
            "subtask_name": "bracket_v3",
            "remaining_time": 3600,
            "raw_data": {"print": {"gcode_state": "RUNNING", "sequence_id": "2031"}, "sn": "0309CA471800999"},
            "ams_mapping": [0, 1],
        },
    }


def print_complete_message(printer_id: int = 1, status: str = "completed") -> dict:
    """What ``send_print_complete`` broadcasts — ``on_print_complete`` narrows
    the callback payload to these four keys before handing it over."""
    return {
        "type": "print_complete",
        "printer_id": printer_id,
        "data": {
            "status": status,
            "filename": "Metadata/plate_1.gcode",
            "subtask_name": "bracket_v3",
            "timelapse_was_active": True,
        },
    }


class FakeBrowser:
    """A WebSocket as far as ``ConnectionManager`` is concerned."""

    def __init__(self):
        self.sent: list[str] = []

    async def accept(self):
        pass

    async def send_text(self, data: str):
        self.sent.append(data)


class FakeManager:
    """``printer_manager`` narrowed to what the uplink asks of it."""

    def __init__(self, states: dict[int, PrinterState] | None = None, names: dict[int, tuple[str, str]] | None = None):
        self._states = states or {}
        self._names = names or {}

    def get_status(self, printer_id: int):
        return self._states.get(printer_id)

    def get_model(self, printer_id: int):
        entry = self._names.get(printer_id)
        return entry[1] if entry else None

    def get_printer(self, printer_id: int):
        entry = self._names.get(printer_id)
        if entry is None:
            return None
        return type("Info", (), {"name": entry[0], "serial_number": ""})()


def make_uplink(published: set[int] | None = None, **kwargs) -> Uplink:
    uplink = Uplink(manager=FakeManager(names={1: ("X2D Front-Left", "X2D"), 2: ("P1S Shelf", "P1S")}), **kwargs)
    uplink.set_publish_set(published if published is not None else {1})
    return uplink


class Clock:
    """A hand-wound monotonic clock."""

    def __init__(self, t: float = 1000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


# ------------------------------------------------------- the tap on broadcast


async def test_a_listener_that_raises_never_reaches_the_browsers():
    """The link is a guest on the product's fan-out.

    Every printer card in the app is fed by ``broadcast``. If a listener could
    take it down, enabling Cloud Link would be able to freeze the dashboard —
    so the listener's exception is swallowed at the hook and the send loop
    never sees it.
    """
    manager = ConnectionManager()
    browser = FakeBrowser()
    await manager.connect(browser, user_id=1)

    seen: list[dict] = []

    def explodes(message: dict) -> None:
        raise RuntimeError("the uplink is having a bad day")

    manager.add_internal_listener(explodes)
    manager.add_internal_listener(seen.append)

    await manager.broadcast({"type": "printer_status", "printer_id": 1, "data": {}})

    assert json.loads(browser.sent[0])["type"] == "printer_status"
    assert seen == [{"type": "printer_status", "printer_id": 1, "data": {}}], (
        "one listener failing must not rob the next one of the message"
    )


async def test_listeners_hear_a_broadcast_with_no_browser_attached():
    """``broadcast`` returns early when nobody is connected — that early return
    is the whole reason the hook sits above it.

    An agent is not a browser. A farm running headless overnight is exactly the
    case Cloud Link exists for, and a tap that only fired when somebody had a
    tab open would report nothing precisely then.
    """
    manager = ConnectionManager()
    assert manager.active_connections == []

    seen: list[dict] = []
    manager.add_internal_listener(seen.append)

    await manager.broadcast({"type": "print_complete", "printer_id": 3, "data": {}})

    assert len(seen) == 1


async def test_a_removed_listener_stops_hearing_and_removing_twice_is_harmless():
    """Unregistering is what a disabled link does; it must not need the caller
    to remember whether it ever registered."""
    manager = ConnectionManager()
    seen: list[dict] = []

    manager.add_internal_listener(seen.append)
    manager.remove_internal_listener(seen.append)
    manager.remove_internal_listener(seen.append)

    await manager.broadcast({"type": "printer_status", "printer_id": 1, "data": {}})

    assert seen == []


# -------------------------------------------------------------- the allowlist


def test_the_status_allowlist_is_what_the_snapshot_adapter_actually_reads():
    """``STATUS_FIELDS`` documents the projection — this makes it enforce it.

    A constant that claims to be the allowlist while the code reads whatever it
    likes is worse than no constant, because a reviewer trusts it.
    """
    assert set(_status_of(_running_state(), "X2D")) == set(STATUS_FIELDS)


def test_the_temperature_allowlist_is_exactly_the_contracts_temps():
    """If the contract grows a sixth reading, this fails here rather than as a
    missing-field error on the first frame after a portal upgrade."""
    assert set(TEMPERATURE_FIELDS) == set(Temps.model_fields)


# ------------------------------------------------------------- the normalizer


async def test_a_published_printers_status_becomes_a_status_frame():
    uplink = make_uplink({1})
    uplink.feed(status_message(1))

    frame = await uplink.drain()

    assert frame is not None
    assert frame.type == "status"
    printer = frame.data.printer
    assert printer.id == "1"
    assert printer.name == "X2D Front-Left"
    assert printer.model == "X2D"
    assert printer.state == "printing"
    assert printer.progress == 42.0
    assert printer.job_name == "bracket_v3"
    assert printer.error is None
    assert printer.temps.model_dump() == {
        "bed": 60.0,
        "bed_target": 60.0,
        "nozzle": 219.5,
        "nozzle_target": 220.0,
        "chamber": 38.0,
    }, "exactly the five the contract names — the heating flags and the internal set-time stay home"

    # And what comes out is a frame the portal's own parser accepts.
    assert parse_frame(make_frame(frame)).type == "status"


async def test_an_unpublished_printers_status_is_dropped():
    """The allowlist is the control that keeps a machine off the internet. A
    printer nobody ticked produces no frame at all — not an anonymised one."""
    uplink = make_uplink({1})
    uplink.feed(status_message(2))

    assert await uplink.drain() is None


async def test_the_publish_set_can_be_replaced_between_drains():
    uplink = make_uplink({1})
    uplink.set_publish_set({2})

    uplink.feed(status_message(1))
    uplink.feed(status_message(2))

    frame = await uplink.drain()
    assert frame is not None
    assert frame.data.printer.id == "2"
    assert await uplink.drain() is None


@pytest.mark.parametrize(
    ("gcode_state", "expected"),
    [
        ("RUNNING", "printing"),
        ("PAUSE", "paused"),
        ("IDLE", "idle"),
        ("FINISH", "idle"),
        ("FAILED", "error"),
        ("PREPARE", "unknown"),
        ("SLICING", "unknown"),
        ("unknown", "unknown"),
        ("SOMETHING_FIRMWARE_INVENTED", "unknown"),
    ],
)
async def test_the_internal_gcode_state_maps_to_a_contract_state(gcode_state: str, expected: str):
    """The literals are Bambu's ``gcode_state``, taken from
    ``bambu_mqtt._ACTIVE_PRINT_STATES`` and the completion branches beside it.

    ``PREPARE`` / ``SLICING`` deliberately land on ``unknown`` rather than
    ``printing``: the contract has no "preparing", and calling it printing
    would report a progress percentage for a job that has not started.
    """
    uplink = make_uplink({1})
    uplink.feed(status_message(1, state=gcode_state))

    frame = await uplink.drain()
    assert frame is not None
    assert frame.data.printer.state == expected


async def test_a_disconnected_printer_is_offline_whatever_it_last_said():
    """``connected`` outranks ``gcode_state``. The last push before a printer
    dropped off the network says RUNNING forever, and a portal showing a
    machine as printing hours after it went dark is worse than showing nothing.
    """
    uplink = make_uplink({1})
    uplink.feed(status_message(1, connected=False, state="RUNNING"))

    frame = await uplink.drain()
    assert frame is not None
    assert frame.data.printer.state == "offline"
    assert frame.data.printer.progress is None


async def test_progress_and_job_name_are_null_when_nothing_is_printing():
    """The contract's ``progress`` is "null while nothing is printing", so a
    stale 100 from the last job must not be reported as this one's."""
    uplink = make_uplink({1})
    uplink.feed(status_message(1, state="FINISH", progress=100.0))

    frame = await uplink.drain()
    assert frame is not None
    assert frame.data.printer.progress is None
    assert frame.data.printer.job_name is None


async def test_a_progress_reading_outside_the_range_is_clamped_not_raised():
    """The contract bounds progress 0–100 and pydantic enforces it. A firmware
    reading of 255 must cost one clamped frame, not an exception on the tap."""
    uplink = make_uplink({1})
    uplink.feed(status_message(1, progress=255.0))

    frame = await uplink.drain()
    assert frame is not None
    assert frame.data.printer.progress == 100.0


async def test_a_missing_temperature_is_null_not_zero():
    """A model without a chamber reports nothing for it, and ``0.0`` would read
    as a freezing chamber rather than an absent sensor."""
    uplink = make_uplink({1})
    uplink.feed(status_message(1, temperatures={"bed": 24.0, "nozzle": 25.5}))

    frame = await uplink.drain()
    assert frame is not None
    assert frame.data.printer.temps.model_dump() == {
        "bed": 24.0,
        "bed_target": None,
        "nozzle": 25.5,
        "nozzle_target": None,
        "chamber": None,
    }


async def test_an_hms_error_crosses_as_a_code_and_a_message():
    """The status dict carries ``{code, attr, module, severity}`` and no text.
    The operator-facing code is the ``MMMM_EEEE`` short form composed from
    ``attr`` and ``code`` — the same one the printer's own screen shows."""
    uplink = make_uplink({1})
    uplink.feed(
        status_message(1, hms_errors=[{"code": "0x8004", "attr": 0x03000000, "module": 3, "severity": 2}]),
    )

    frame = await uplink.drain()
    assert frame is not None
    error = frame.data.printer.error
    assert error is not None
    assert error.code == "0300_8004"
    assert error.message, "a code with no message is half an error — the contract says so"


async def test_an_hms_error_does_not_by_itself_make_the_state_error():
    """A machine can print through a chamber-regulation warning. The error rides
    alongside the state; it does not replace it, or every PETG print on an
    enclosed machine would show as failed in the portal."""
    uplink = make_uplink({1})
    uplink.feed(status_message(1, hms_errors=[{"code": "0x8004", "attr": 0x03000000, "module": 3, "severity": 4}]))

    frame = await uplink.drain()
    assert frame is not None
    assert frame.data.printer.state == "printing"
    assert frame.data.printer.error is not None


# ----------------------------------------------------------------- the events


async def test_a_print_start_becomes_a_print_started_event_without_the_raw_payload():
    """``send_print_start`` broadcasts the entire MQTT push under ``raw_data``,
    serial number and all. The event carries the job name and nothing else."""
    uplink = make_uplink({1})
    uplink.feed(print_start_message(1))

    frame = await uplink.drain()
    assert frame is not None
    assert frame.type == "event"
    assert frame.data.kind == "print_started"
    assert frame.data.printer_id == "1"
    assert frame.data.detail == {"job_name": "bracket_v3"}

    on_the_wire = json.dumps(make_frame(frame))
    assert "0309CA471800999" not in on_the_wire
    assert "sequence_id" not in on_the_wire


async def test_a_print_complete_becomes_a_print_finished_event_carrying_its_outcome():
    uplink = make_uplink({1})
    uplink.feed(print_complete_message(1, status="failed"))

    frame = await uplink.drain()
    assert frame is not None
    assert frame.data.kind == "print_finished"
    assert frame.data.detail == {"job_name": "bracket_v3", "status": "failed"}


async def test_an_unpublished_printers_print_start_is_dropped():
    uplink = make_uplink({1})
    uplink.feed(print_start_message(2))

    assert await uplink.drain() is None


async def test_the_connection_edge_becomes_an_online_or_offline_event():
    """There is no dedicated connect/disconnect broadcast in the product — the
    connection state travels inside ``printer_status.data.connected``, so the
    edge is what the uplink watches.

    Each edge yields TWO frames, in this order: the event that announces the
    transition, then the status that is the new steady state.
    """
    uplink = make_uplink({1})

    uplink.feed(status_message(1, connected=True))
    assert (await uplink.drain()).type == "status", "the first sighting is a status, not an event"

    uplink.feed(status_message(1, connected=False, state="IDLE"))
    assert (await uplink.drain()).data.kind == "printer_offline"
    assert (await uplink.drain()).data.printer.state == "offline"

    uplink.feed(status_message(1, connected=True, state="IDLE"))
    assert (await uplink.drain()).data.kind == "printer_online"
    assert (await uplink.drain()).data.printer.state == "idle"


async def test_the_offline_edge_carries_its_status_even_inside_the_throttle_window():
    """The portal must not be left holding "printing" for a printer that is gone.

    A disconnected printer produces no further ``printer_status`` broadcast at
    all, so the status accompanying the edge is the LAST word on that machine
    until it comes back. Throttling it away — which is what would happen almost
    every time, since the edge lands inside a window opened milliseconds
    earlier by the previous push — would leave the last delivered status saying
    the machine was mid-print, for as long as it stayed off.
    """
    clock = Clock()
    uplink = make_uplink({1}, min_interval_s=60.0, now=clock)

    uplink.feed(status_message(1, connected=True, state="RUNNING", progress=42.0))
    assert (await uplink.drain()).data.printer.state == "printing"

    clock.advance(1.0)
    uplink.feed(status_message(1, connected=False, state="RUNNING", progress=42.0))

    assert (await uplink.drain()).data.kind == "printer_offline"
    stale_check = await uplink.drain()
    assert stale_check is not None
    assert stale_check.type == "status"
    assert stale_check.data.printer.state == "offline"
    assert stale_check.data.printer.progress is None


async def test_the_edge_status_still_spends_the_throttle_window():
    """The status that rides an edge IS that printer's report for now. Leaving
    the window unspent would let the very next ordinary push through
    immediately behind it."""
    clock = Clock()
    uplink = make_uplink({1}, min_interval_s=5.0, now=clock)

    uplink.feed(status_message(1, connected=True))
    await uplink.drain()

    clock.advance(5.0)
    uplink.feed(status_message(1, connected=False, state="IDLE"))
    assert (await uplink.drain()).data.kind == "printer_offline"
    assert (await uplink.drain()).type == "status"

    clock.advance(1.0)
    uplink.feed(status_message(1, connected=False, state="IDLE"))
    assert await uplink.drain() is None


async def test_the_first_sighting_of_a_printer_raises_no_connection_event():
    """The snapshot sent at connect already says whether each printer is up.
    An event on the first status push would be a duplicate of it, arriving
    every time the agent reconnects."""
    uplink = make_uplink({1})
    uplink.feed(status_message(1, connected=False, state="IDLE"))

    frame = await uplink.drain()
    assert frame is not None
    assert frame.type == "status"
    assert frame.data.printer.state == "offline"


# --------------------------------------------------------------- the throttle


async def test_two_statuses_inside_the_window_yield_one_frame():
    clock = Clock()
    uplink = make_uplink({1}, min_interval_s=5.0, now=clock)

    uplink.feed(status_message(1))
    assert await uplink.drain() is not None

    clock.advance(1.0)
    uplink.feed(status_message(1, progress=43.0))
    assert await uplink.drain() is None


async def test_a_status_after_the_window_passes():
    clock = Clock()
    uplink = make_uplink({1}, min_interval_s=5.0, now=clock)

    uplink.feed(status_message(1))
    await uplink.drain()

    clock.advance(5.0)
    uplink.feed(status_message(1, progress=43.0))
    frame = await uplink.drain()
    assert frame is not None
    assert frame.data.printer.progress == 43.0


async def test_the_throttle_is_kept_per_printer():
    """A busy machine must not silence a quiet one. The window is one printer's
    minimum reporting interval, not the link's."""
    clock = Clock()
    uplink = make_uplink({1, 2}, min_interval_s=5.0, now=clock)

    uplink.feed(status_message(1))
    uplink.feed(status_message(2))

    first = await uplink.drain()
    second = await uplink.drain()
    assert {first.data.printer.id, second.data.printer.id} == {"1", "2"}


async def test_events_are_never_throttled():
    """A status is a sample of a continuous thing and skipping one costs
    latency. An event is discrete: dropping ``print_finished`` means the portal
    shows a print that never ends."""
    clock = Clock()
    uplink = make_uplink({1}, min_interval_s=60.0, now=clock)

    uplink.feed(status_message(1))
    assert (await uplink.drain()).type == "status"

    uplink.feed(print_start_message(1))
    uplink.feed(print_complete_message(1))

    assert (await uplink.drain()).data.kind == "print_started"
    assert (await uplink.drain()).data.kind == "print_finished"


async def test_a_throttled_status_does_not_hide_the_event_behind_it():
    """``drain`` pops until it has something to send. A queue whose head is a
    throttled status must not make the caller poll again to reach the event
    sitting behind it."""
    clock = Clock()
    uplink = make_uplink({1}, min_interval_s=60.0, now=clock)

    uplink.feed(status_message(1))
    await uplink.drain()

    uplink.feed(status_message(1, progress=43.0))
    uplink.feed(print_complete_message(1))

    frame = await uplink.drain()
    assert frame is not None
    assert frame.data.kind == "print_finished"


# ------------------------------------------------------------------ the queue


async def test_an_overflowing_queue_drops_the_oldest_without_raising():
    """The tap runs inside ``broadcast``, so it cannot block and it cannot
    fail. When the link is down and nothing drains, the newest reading is the
    one worth keeping — a queue that dropped the newest would hold a snapshot
    of the moment the connection died and never move on.
    """
    uplink = make_uplink({1}, min_interval_s=0.0)

    for progress in range(QUEUE_MAXSIZE + 10):
        uplink.feed(status_message(1, progress=float(progress % 100)))

    assert uplink.dropped == 10
    assert uplink.pending == QUEUE_MAXSIZE

    frame = await uplink.drain()
    assert frame is not None
    assert frame.data.printer.progress == 10.0, "the survivors start after the dropped ten"


@pytest.mark.parametrize(
    "junk",
    [
        None,
        "not a dict",
        {},
        {"type": "printer_status"},
        {"type": "printer_status", "printer_id": "not-an-int", "data": {}},
        {"type": "printer_status", "printer_id": 1, "data": None},
    ],
)
async def test_feed_never_raises_and_drain_survives_junk(junk):
    """``feed`` is called synchronously from the product's broadcast path. It
    has one job — take the message — and no input may make it throw."""
    uplink = make_uplink({1})
    uplink.feed(junk)

    assert await uplink.drain() is None


async def test_a_message_the_uplink_has_no_use_for_never_enters_the_queue():
    """Archive, library and inventory broadcasts outnumber printer pushes
    during a library scan. Letting them into a 500-deep queue would flush every
    printer status out of it before the link ever drained one."""
    uplink = make_uplink({1})
    uplink.feed({"type": "library_file_added", "data": {"id": 7}})
    uplink.feed({"type": "archive_created", "data": {"id": 9}})

    assert uplink.pending == 0
    assert await uplink.drain() is None


# --------------------------------------------------------------- the snapshot


async def _add_printer(session: AsyncSession, printer_id: int, name: str, model: str, **kwargs) -> None:
    session.add(
        Printer(
            id=printer_id,
            name=name,
            serial_number=f"SN{printer_id:06d}",
            ip_address="192.168.1.10",
            access_code="12345678",
            model=model,
            **kwargs,
        )
    )
    await session.commit()


async def _publish(session: AsyncSession, *printer_ids: int) -> None:
    session.add_all([CloudLinkPrinter(printer_id=pid) for pid in printer_ids])
    await session.commit()


def _running_state() -> PrinterState:
    return PrinterState(
        connected=True,
        state="RUNNING",
        subtask_name="bracket_v3",
        progress=42.0,
        temperatures={"bed": 60.0, "bed_target": 60.0, "nozzle": 219.5, "nozzle_target": 220.0, "chamber": 38.0},
    )


async def test_the_snapshot_holds_exactly_the_published_printers(db_session: AsyncSession):
    await _add_printer(db_session, 1, "X2D Front-Left", "X2D")
    await _add_printer(db_session, 2, "P1S Shelf", "P1S")
    await _publish(db_session, 1)

    uplink = Uplink(manager=FakeManager(states={1: _running_state(), 2: _running_state()}))
    snapshot = await uplink.build_snapshot(db_session)

    assert [p.id for p in snapshot.data.printers] == ["1"]
    only = snapshot.data.printers[0]
    assert only.name == "X2D Front-Left"
    assert only.model == "X2D"
    assert only.state == "printing"
    assert only.progress == 42.0
    assert parse_frame(make_frame(snapshot)).type == "snapshot"


async def test_an_archived_printer_is_excluded_even_when_it_is_published(db_session: AsyncSession):
    """A ``CloudLinkPrinter`` row survives archiving — the allowlist has no
    opinion about a printer's lifecycle, and rebuilding it on archive would
    lose the user's choice if they ever restored the machine. So the read side
    is what filters: archived means gone from the whole app, portal included.
    """
    await _add_printer(db_session, 1, "Retired X1C", "X1C", archived=True)
    await _add_printer(db_session, 2, "P1S Shelf", "P1S")
    await _publish(db_session, 1, 2)

    uplink = Uplink(manager=FakeManager(states={1: _running_state(), 2: _running_state()}))
    snapshot = await uplink.build_snapshot(db_session)

    assert [p.id for p in snapshot.data.printers] == ["2"]


async def test_an_inactive_printer_is_excluded_even_when_it_is_published(db_session: AsyncSession):
    """``is_active`` is Maintenance Mode — the machine is parked and not
    available. "Available" in this codebase is ``is_active AND NOT archived``,
    and the portal sees the same set the farm does."""
    await _add_printer(db_session, 1, "Parked X2D", "X2D", is_active=False)
    await _publish(db_session, 1)

    uplink = Uplink(manager=FakeManager(states={1: _running_state()}))
    snapshot = await uplink.build_snapshot(db_session)

    assert snapshot.data.printers == []


async def test_a_published_printer_with_no_live_state_is_offline_in_the_snapshot(db_session: AsyncSession):
    """A printer that is configured but not connected still belongs in the
    snapshot — its absence would read as "not in the farm" rather than "not
    reachable", and the two need different action from the operator."""
    await _add_printer(db_session, 1, "X2D Front-Left", "X2D")
    await _publish(db_session, 1)

    uplink = Uplink(manager=FakeManager())
    snapshot = await uplink.build_snapshot(db_session)

    printer = snapshot.data.printers[0]
    assert printer.state == "offline"
    assert printer.progress is None
    assert printer.job_name is None
    assert printer.temps.model_dump() == {
        "bed": None,
        "bed_target": None,
        "nozzle": None,
        "nozzle_target": None,
        "chamber": None,
    }


async def test_a_printer_with_no_model_recorded_still_makes_the_snapshot(db_session: AsyncSession):
    """``Printer.model`` is nullable — a machine added before its first MQTT
    push has none. ``model`` is a required string in the contract, so the
    snapshot must not be the thing that breaks over it."""
    await _add_printer(db_session, 1, "Fresh printer", None)
    await _publish(db_session, 1)

    uplink = Uplink(manager=FakeManager())
    snapshot = await uplink.build_snapshot(db_session)

    assert snapshot.data.printers[0].model == ""


async def test_the_snapshot_refreshes_the_in_memory_publish_set(db_session: AsyncSession):
    """``drain`` may not touch the database, so something has to keep its
    filter current. The snapshot already reads the allowlist for its own sake —
    doing it there means one query answers both questions."""
    await _add_printer(db_session, 1, "X2D Front-Left", "X2D")
    await _publish(db_session, 1)

    uplink = Uplink(manager=FakeManager(names={1: ("X2D Front-Left", "X2D")}))
    uplink.feed(status_message(1))
    assert await uplink.drain() is None, "nothing is published until the set has been read"

    await uplink.build_snapshot(db_session)

    uplink.feed(status_message(1))
    assert await uplink.drain() is not None


async def test_an_archived_printer_stops_producing_status_frames_too(db_session: AsyncSession):
    """The availability filter has to reach ``drain``, not only the snapshot.

    An archived printer is retired from the app, not unplugged: it may still be
    MQTT-connected at the moment it is archived, and every push it makes is
    still broadcast. Seeding the in-memory set with the raw allowlist made the
    filter cosmetic — the machine was absent from the snapshot and present in
    every status frame that followed it, which is the louder of the two.
    """
    await _add_printer(db_session, 1, "Retired X1C", "X1C", archived=True)
    await _add_printer(db_session, 2, "P1S Shelf", "P1S")
    await _publish(db_session, 1, 2)

    uplink = Uplink(manager=FakeManager(names={1: ("Retired X1C", "X1C"), 2: ("P1S Shelf", "P1S")}))
    await uplink.build_snapshot(db_session)

    uplink.feed(status_message(1))
    assert await uplink.drain() is None, "archived means gone from the portal, status frames included"

    uplink.feed(status_message(2))
    assert await uplink.drain() is not None


async def test_a_printer_in_maintenance_mode_stops_producing_status_frames_too(db_session: AsyncSession):
    """Same for ``is_active`` — "available" is one definition, applied once."""
    await _add_printer(db_session, 1, "Parked X2D", "X2D", is_active=False)
    await _publish(db_session, 1)

    uplink = Uplink(manager=FakeManager(names={1: ("Parked X2D", "X2D")}))
    await uplink.build_snapshot(db_session)

    uplink.feed(status_message(1))
    assert await uplink.drain() is None


async def test_the_snapshot_seeds_the_connection_watcher(db_session: AsyncSession):
    """``_connection_event`` stays silent on a printer it has never seen, so
    that the snapshot is not immediately echoed back as an event. That same
    silence swallowed the FIRST real connection change after every agent
    reconnect — the snapshot has to hand the watcher its starting point.
    """
    await _add_printer(db_session, 1, "X2D Front-Left", "X2D")
    await _publish(db_session, 1)

    uplink = Uplink(manager=FakeManager(states={1: _running_state()}, names={1: ("X2D Front-Left", "X2D")}))
    snapshot = await uplink.build_snapshot(db_session)
    assert snapshot.data.printers[0].state == "printing", "the snapshot says it is up"

    uplink.feed(status_message(1, connected=False, state="RUNNING"))

    frame = await uplink.drain()
    assert frame is not None
    assert frame.data.kind == "printer_offline"


async def test_a_snapshot_of_a_disconnected_printer_seeds_the_watcher_the_other_way(db_session: AsyncSession):
    """The seed has to be the reported value, not a hopeful True — otherwise a
    farm that reconnects while a printer is down invents a ``printer_offline``
    the moment it comes back."""
    await _add_printer(db_session, 1, "X2D Front-Left", "X2D")
    await _publish(db_session, 1)

    uplink = Uplink(manager=FakeManager(names={1: ("X2D Front-Left", "X2D")}))
    assert (await uplink.build_snapshot(db_session)).data.printers[0].state == "offline"

    uplink.feed(status_message(1, connected=True))

    frame = await uplink.drain()
    assert frame is not None
    assert frame.data.kind == "printer_online"


# ------------------------------------------------ the chamber that isn't there


async def test_a_model_without_a_chamber_sensor_reports_no_chamber_in_the_snapshot(
    db_session: AsyncSession,
):
    """P1P, P1S, A1 and A1 mini report a ``chamber_temper`` that means nothing,
    and ``printer_state_to_dict`` drops it before any browser sees it.

    The snapshot reads ``state.temperatures`` raw, so without the same filter
    the portal received a number here and ``null`` in every status frame after
    it — the reading flip-flopping once per agent reconnect, which is worse
    than either answer on its own.
    """
    await _add_printer(db_session, 1, "P1S Shelf", "P1S")
    await _publish(db_session, 1)

    uplink = Uplink(manager=FakeManager(states={1: _running_state()}))
    snapshot = await uplink.build_snapshot(db_session)

    temps = snapshot.data.printers[0].temps
    assert temps.chamber is None
    # The rest of the readings are untouched — this filter is about one sensor.
    assert temps.bed == 60.0
    assert temps.nozzle == 219.5


async def test_a_model_with_a_chamber_sensor_keeps_its_reading_in_the_snapshot(
    db_session: AsyncSession,
):
    """The other half, or the test above would pass against a filter that
    simply never reports a chamber."""
    await _add_printer(db_session, 1, "X2D Front-Left", "X2D")
    await _publish(db_session, 1)

    uplink = Uplink(manager=FakeManager(states={1: _running_state()}))
    snapshot = await uplink.build_snapshot(db_session)

    assert snapshot.data.printers[0].temps.chamber == 38.0


async def test_a_printer_with_no_model_recorded_reports_no_chamber(db_session: AsyncSession):
    """Unknown model, no chamber — the same answer ``printer_state_to_dict``
    gives when ``printer_manager.get_model`` returns ``None``. Guessing the
    other way would publish the meaningless reading for exactly the printers
    nobody has identified yet."""
    await _add_printer(db_session, 1, "Nameless", "")
    await _publish(db_session, 1)

    uplink = Uplink(manager=FakeManager(states={1: _running_state()}))
    snapshot = await uplink.build_snapshot(db_session)

    assert snapshot.data.printers[0].temps.chamber is None


def test_the_snapshot_adapter_strips_the_same_chamber_keys_the_browser_path_does():
    """Shape parity, not just the one key the contract carries.

    ``_status_of``'s output has to be interchangeable with a broadcast's
    ``data`` — that is the whole reason both paths can share one builder — so
    it drops ``chamber_target`` and ``chamber_heating`` too, even though
    :data:`TEMPERATURE_FIELDS` never reads them.
    """
    state = PrinterState(
        connected=True,
        state="RUNNING",
        temperatures={"bed": 60.0, "chamber": 38.0, "chamber_target": 0.0, "chamber_heating": False},
    )
    assert set(_status_of(state, "P1S")["temperatures"]) == {"bed"}
    assert set(_status_of(state, "X1C")["temperatures"]) == {"bed", "chamber", "chamber_target", "chamber_heating"}


async def test_the_status_path_arrives_already_filtered_for_a_chamberless_model():
    """The claim the snapshot fix rests on, pinned against the real helpers.

    Status frames are built from ``printer_status`` broadcasts, and every
    broadcast of that type in the product is ``printer_state_to_dict``'s output
    — so the status path needs no filter of its own. If that ever stops being
    true this fails here rather than as a chamber reading nobody can explain.
    """
    manager = ConnectionManager()
    uplink = make_uplink({7})
    uplink._identity[7] = ("P1S Shelf", "P1S")
    manager.add_internal_listener(uplink.feed)

    state = PrinterState(
        connected=True,
        state="RUNNING",
        subtask_name="bracket_v3",
        progress=42.0,
        temperatures={"bed": 60.0, "bed_target": 60.0, "nozzle": 219.5, "nozzle_target": 220.0, "chamber": 38.0},
    )
    await manager.send_printer_status(7, printer_state_to_dict(state, 7, "P1S"))

    frame = await uplink.drain()
    assert frame is not None
    assert frame.data.printer.temps.chamber is None
    assert frame.data.printer.temps.bed == 60.0

    manager.remove_internal_listener(uplink.feed)


# --------------------------------------------------------------- end to end


async def test_a_real_broadcast_reaches_the_portal_as_a_status_frame():
    """The one test with nothing hand-copied in it.

    Every other fixture here is a transcription of what a helper builds, and a
    transcription cannot fail when the helper drifts. This one registers
    ``feed`` on a real ``ConnectionManager``, calls the real
    ``send_printer_status`` with a real ``printer_state_to_dict`` over a real
    ``PrinterState``, and asserts the frame that comes out — so a renamed key
    in any of the three lands here instead of in production.
    """
    manager = ConnectionManager()
    uplink = make_uplink({7})
    uplink._identity[7] = ("X2D Front-Left", "X2D")
    manager.add_internal_listener(uplink.feed)

    state = PrinterState(
        connected=True,
        state="RUNNING",
        subtask_name="bracket_v3",
        progress=42.0,
        temperatures={"bed": 60.0, "bed_target": 60.0, "nozzle": 219.5, "nozzle_target": 220.0, "chamber": 38.0},
    )
    await manager.send_printer_status(7, printer_state_to_dict(state, 7, "X2D"))

    frame = await uplink.drain()
    assert frame is not None
    assert frame.type == "status"
    printer = frame.data.printer
    assert printer.id == "7"
    assert printer.state == "printing"
    assert printer.progress == 42.0
    assert printer.job_name == "bracket_v3"
    assert printer.temps.model_dump() == {
        "bed": 60.0,
        "bed_target": 60.0,
        "nozzle": 219.5,
        "nozzle_target": 220.0,
        "chamber": 38.0,
    }
    assert parse_frame(make_frame(frame)).type == "status"

    manager.remove_internal_listener(uplink.feed)


async def test_a_real_print_complete_broadcast_reaches_the_portal_as_an_event():
    """The same wiring for the other direction the product pushes in —
    ``send_print_complete``'s outer keys, against the real helper."""
    manager = ConnectionManager()
    uplink = make_uplink({7})
    manager.add_internal_listener(uplink.feed)

    await manager.send_print_complete(
        7,
        {"status": "completed", "filename": "Metadata/plate_1.gcode", "subtask_name": "bracket_v3"},
    )

    frame = await uplink.drain()
    assert frame is not None
    assert frame.data.kind == "print_finished"
    assert frame.data.printer_id == "7"

    manager.remove_internal_listener(uplink.feed)


async def test_a_listener_can_unregister_itself_without_skipping_its_neighbour():
    """A link shutting down does it on the message that told it to. Mutating
    the list mid-iteration would silently drop whoever came next."""
    manager = ConnectionManager()
    seen: list[str] = []

    def leaves(message: dict) -> None:
        seen.append("first")
        manager.remove_internal_listener(leaves)

    manager.add_internal_listener(leaves)
    manager.add_internal_listener(lambda message: seen.append("second"))

    await manager.broadcast({"type": "printer_status", "printer_id": 1, "data": {}})

    assert seen == ["first", "second"]


async def test_an_hms_error_on_a_snapshot_printer_crosses(db_session: AsyncSession):
    await _add_printer(db_session, 1, "X2D Front-Left", "X2D")
    await _publish(db_session, 1)

    state = _running_state()
    state.hms_errors = [HMSError(code="0x8004", attr=0x03000000, module=3, severity=2)]

    uplink = Uplink(manager=FakeManager(states={1: state}))
    snapshot = await uplink.build_snapshot(db_session)

    assert snapshot.data.printers[0].error.code == "0300_8004"
