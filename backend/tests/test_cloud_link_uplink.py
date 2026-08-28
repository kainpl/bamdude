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

**``flush`` is a tick, not a pop.** It replaced a per-message ``drain`` that a
caller polled until it returned ``None``. Most tests below call it once and
inspect the returned list — where the old suite called ``drain`` twice to get
two frames, this one calls ``flush`` once and indexes ``frames[0]``/``frames[1]``,
because both frames now come back from the SAME tick, in order.
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
    BATCH_MAX_PRINTERS,
    EVENTS_MAXSIZE,
    EVENTS_PER_FLUSH,
    SNAPSHOT_CHUNK_PRINTERS,
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


def one_status(uplink: Uplink):
    """Flush and return the single printer inside the tick's single
    ``status_batch`` — the shape most of these tests need."""
    frames = uplink.flush()
    assert len(frames) == 1, f"expected exactly one frame, got {[f.type for f in frames]}"
    assert frames[0].type == "status_batch"
    assert len(frames[0].data.printers) == 1
    return frames[0].data.printers[0]


async def one_chunk(uplink: Uplink, session: AsyncSession):
    """Build the snapshot and return its single chunk — the shape every test
    of an unfragmented (<= SNAPSHOT_CHUNK_PRINTERS printer) farm needs."""
    chunks = await uplink.build_snapshot_chunks(session)
    assert len(chunks) == 1
    assert chunks[0].data.chunk == 1
    assert chunks[0].data.of == 1
    return chunks[0]


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


def test_a_published_printers_status_becomes_a_batch_frame():
    uplink = make_uplink({1})
    uplink.feed(status_message(1))

    printer = one_status(uplink)

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


def test_a_published_printers_status_frame_round_trips_the_contract():
    """The frame ``flush`` builds is one the portal's own parser accepts."""
    uplink = make_uplink({1})
    uplink.feed(status_message(1))

    frames = uplink.flush()
    assert parse_frame(make_frame(frames[0])).type == "status_batch"


def test_an_unpublished_printers_status_is_dropped():
    """The allowlist is the control that keeps a machine off the internet. A
    printer nobody ticked produces no frame at all — not an anonymised one."""
    uplink = make_uplink({1})
    uplink.feed(status_message(2))

    assert uplink.flush() == []


def test_the_publish_set_can_be_replaced_between_flushes():
    uplink = make_uplink({1})
    uplink.set_publish_set({2})

    uplink.feed(status_message(1))
    uplink.feed(status_message(2))

    printer = one_status(uplink)
    assert printer.id == "2"
    assert uplink.flush() == []


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
def test_the_internal_gcode_state_maps_to_a_contract_state(gcode_state: str, expected: str):
    """The literals are Bambu's ``gcode_state``, taken from
    ``bambu_mqtt._ACTIVE_PRINT_STATES`` and the completion branches beside it.

    ``PREPARE`` / ``SLICING`` deliberately land on ``unknown`` rather than
    ``printing``: the contract has no "preparing", and calling it printing
    would report a progress percentage for a job that has not started.
    """
    uplink = make_uplink({1})
    uplink.feed(status_message(1, state=gcode_state))

    assert one_status(uplink).state == expected


def test_a_disconnected_printer_is_offline_whatever_it_last_said():
    """``connected`` outranks ``gcode_state``. The last push before a printer
    dropped off the network says RUNNING forever, and a portal showing a
    machine as printing hours after it went dark is worse than showing nothing.
    """
    uplink = make_uplink({1})
    uplink.feed(status_message(1, connected=False, state="RUNNING"))

    printer = one_status(uplink)
    assert printer.state == "offline"
    assert printer.progress is None


def test_progress_and_job_name_are_null_when_nothing_is_printing():
    """The contract's ``progress`` is "null while nothing is printing", so a
    stale 100 from the last job must not be reported as this one's."""
    uplink = make_uplink({1})
    uplink.feed(status_message(1, state="FINISH", progress=100.0))

    printer = one_status(uplink)
    assert printer.progress is None
    assert printer.job_name is None


def test_a_progress_reading_outside_the_range_is_clamped_not_raised():
    """The contract bounds progress 0–100 and pydantic enforces it. A firmware
    reading of 255 must cost one clamped frame, not an exception on the tap."""
    uplink = make_uplink({1})
    uplink.feed(status_message(1, progress=255.0))

    assert one_status(uplink).progress == 100.0


def test_a_missing_temperature_is_null_not_zero():
    """A model without a chamber reports nothing for it, and ``0.0`` would read
    as a freezing chamber rather than an absent sensor."""
    uplink = make_uplink({1})
    uplink.feed(status_message(1, temperatures={"bed": 24.0, "nozzle": 25.5}))

    assert one_status(uplink).temps.model_dump() == {
        "bed": 24.0,
        "bed_target": None,
        "nozzle": 25.5,
        "nozzle_target": None,
        "chamber": None,
    }


def test_an_hms_error_crosses_as_a_code_and_a_message():
    """The status dict carries ``{code, attr, module, severity}`` and no text.
    The operator-facing code is the ``MMMM_EEEE`` short form composed from
    ``attr`` and ``code`` — the same one the printer's own screen shows."""
    uplink = make_uplink({1})
    uplink.feed(
        status_message(1, hms_errors=[{"code": "0x8004", "attr": 0x03000000, "module": 3, "severity": 2}]),
    )

    error = one_status(uplink).error
    assert error is not None
    assert error.code == "0300_8004"
    assert error.message, "a code with no message is half an error — the contract says so"


def test_an_hms_error_does_not_by_itself_make_the_state_error():
    """A machine can print through a chamber-regulation warning. The error rides
    alongside the state; it does not replace it, or every PETG print on an
    enclosed machine would show as failed in the portal."""
    uplink = make_uplink({1})
    uplink.feed(status_message(1, hms_errors=[{"code": "0x8004", "attr": 0x03000000, "module": 3, "severity": 4}]))

    printer = one_status(uplink)
    assert printer.state == "printing"
    assert printer.error is not None


# ----------------------------------------------------------------- the events


def test_a_print_start_becomes_a_print_started_event_without_the_raw_payload():
    """``send_print_start`` broadcasts the entire MQTT push under ``raw_data``,
    serial number and all. The event carries the job name and nothing else."""
    uplink = make_uplink({1})
    uplink.feed(print_start_message(1))

    frames = uplink.flush()
    assert len(frames) == 1
    frame = frames[0]
    assert frame.type == "event"
    assert frame.data.kind == "print_started"
    assert frame.data.printer_id == "1"
    assert frame.data.detail == {"job_name": "bracket_v3"}

    on_the_wire = json.dumps(make_frame(frame))
    assert "0309CA471800999" not in on_the_wire
    assert "sequence_id" not in on_the_wire


def test_a_print_complete_becomes_a_print_finished_event_carrying_its_outcome():
    uplink = make_uplink({1})
    uplink.feed(print_complete_message(1, status="failed"))

    frames = uplink.flush()
    assert len(frames) == 1
    assert frames[0].data.kind == "print_finished"
    assert frames[0].data.detail == {"job_name": "bracket_v3", "status": "failed"}


def test_an_unpublished_printers_print_start_is_dropped():
    uplink = make_uplink({1})
    uplink.feed(print_start_message(2))

    assert uplink.flush() == []


def test_the_connection_edge_becomes_an_online_or_offline_event():
    """There is no dedicated connect/disconnect broadcast in the product — the
    connection state travels inside ``printer_status.data.connected``, so the
    edge is what the uplink watches.

    Each edge yields TWO frames from the SAME ``flush`` call, in this order:
    the event that announces the transition, then the batch carrying the new
    steady state.
    """
    uplink = make_uplink({1})

    uplink.feed(status_message(1, connected=True))
    frames = uplink.flush()
    assert len(frames) == 1 and frames[0].type == "status_batch", "the first sighting is a batch, not an event"

    uplink.feed(status_message(1, connected=False, state="IDLE"))
    frames = uplink.flush()
    assert len(frames) == 2
    assert frames[0].type == "event" and frames[0].data.kind == "printer_offline"
    assert frames[1].type == "status_batch" and frames[1].data.printers[0].state == "offline"

    uplink.feed(status_message(1, connected=True, state="IDLE"))
    frames = uplink.flush()
    assert len(frames) == 2
    assert frames[0].data.kind == "printer_online"
    assert frames[1].data.printers[0].state == "idle"


def test_offline_edge_bypasses_throttle_and_event_precedes_batch():
    """The portal must not be left holding "printing" for a printer that is
    gone, and it must never see that status appear before the event that
    explains it.

    A disconnected printer produces no further ``printer_status`` broadcast at
    all, so the batched reading that rides the edge is the LAST word on that
    machine until it comes back. Throttling it away — which is what would
    happen almost every time, since the edge lands inside a window opened
    milliseconds earlier by the previous push — would leave the portal
    thinking the machine was mid-print for as long as it stayed off.
    """
    clock = Clock()
    uplink = make_uplink({1}, min_interval_s=60.0, now=clock)

    uplink.feed(status_message(1, connected=True, state="RUNNING", progress=42.0))
    assert one_status(uplink).state == "printing"

    clock.advance(1.0)
    uplink.feed(status_message(1, connected=False, state="RUNNING", progress=42.0))

    frames = uplink.flush()
    assert len(frames) == 2, "the edge event and the batch carrying its status leave in the SAME tick"
    assert frames[0].type == "event" and frames[0].data.kind == "printer_offline"
    assert frames[1].type == "status_batch"
    offline_printer = frames[1].data.printers[0]
    assert offline_printer.state == "offline"
    assert offline_printer.progress is None


def test_the_edge_status_still_spends_the_throttle_window():
    """The reading that rides an edge IS that printer's report for now. Leaving
    the window unspent would let the very next ordinary push through
    immediately behind it."""
    clock = Clock()
    uplink = make_uplink({1}, min_interval_s=5.0, now=clock)

    uplink.feed(status_message(1, connected=True))
    uplink.flush()

    clock.advance(5.0)
    uplink.feed(status_message(1, connected=False, state="IDLE"))
    frames = uplink.flush()
    assert frames[0].data.kind == "printer_offline"
    assert frames[1].type == "status_batch"

    clock.advance(1.0)
    uplink.feed(status_message(1, connected=False, state="IDLE"))
    assert uplink.flush() == []


def test_the_first_sighting_of_a_printer_raises_no_connection_event():
    """The snapshot sent at connect already says whether each printer is up.
    An event on the first status push would be a duplicate of it, arriving
    every time the agent reconnects."""
    uplink = make_uplink({1})
    uplink.feed(status_message(1, connected=False, state="IDLE"))

    assert one_status(uplink).state == "offline"


# --------------------------------------------------------------- the throttle


def test_two_statuses_inside_the_window_yield_no_batch():
    clock = Clock()
    uplink = make_uplink({1}, min_interval_s=5.0, now=clock)

    uplink.feed(status_message(1))
    assert len(uplink.flush()) == 1

    clock.advance(1.0)
    uplink.feed(status_message(1, progress=43.0))
    assert uplink.flush() == []


def test_a_status_after_the_window_passes():
    clock = Clock()
    uplink = make_uplink({1}, min_interval_s=5.0, now=clock)

    uplink.feed(status_message(1))
    uplink.flush()

    clock.advance(5.0)
    uplink.feed(status_message(1, progress=43.0))
    assert one_status(uplink).progress == 43.0


def test_throttled_printer_stays_dirty_for_the_next_tick():
    """A printer inside its throttle window is not dropped — it stays in the
    dirty map, coalescing whatever arrives, until a LATER tick's window has
    reopened. Two consecutive empty flushes must not lose the reading; the
    third, once the window is open, must send the NEWEST one, not the first."""
    clock = Clock()
    uplink = make_uplink({1}, min_interval_s=5.0, now=clock)

    uplink.feed(status_message(1))
    assert len(uplink.flush()) == 1  # spends the window, stamps last_status_at

    clock.advance(1.0)
    uplink.feed(status_message(1, progress=50.0))
    assert uplink.flush() == [], "still inside the window — nothing sent, nothing lost"

    clock.advance(1.0)
    uplink.feed(status_message(1, progress=75.0))  # coalesces over the 50.0 push
    assert uplink.flush() == [], "still inside the window"

    clock.advance(3.0)  # 5.0s since the window opened — it has now reopened
    printer = one_status(uplink)
    assert printer.progress == 75.0, "the newest coalesced reading — the stale 50.0 never had to be sent"


def test_the_throttle_is_kept_per_printer():
    """A busy machine must not silence a quiet one. The window is one printer's
    minimum reporting interval, not the link's."""
    clock = Clock()
    uplink = make_uplink({1, 2}, min_interval_s=5.0, now=clock)

    uplink.feed(status_message(1))
    uplink.feed(status_message(2))

    frames = uplink.flush()
    assert len(frames) == 1, "both are dirty and unthrottled in the same tick — one batch carries both"
    assert {p.id for p in frames[0].data.printers} == {"1", "2"}


def test_events_are_never_throttled():
    """A status is a sample of a continuous thing and skipping one costs
    latency. An event is discrete: dropping ``print_finished`` means the portal
    shows a print that never ends."""
    clock = Clock()
    uplink = make_uplink({1}, min_interval_s=60.0, now=clock)

    uplink.feed(status_message(1))
    assert uplink.flush()[0].type == "status_batch"

    uplink.feed(print_start_message(1))
    uplink.feed(print_complete_message(1))

    frames = uplink.flush()
    assert len(frames) == 2
    assert frames[0].data.kind == "print_started"
    assert frames[1].data.kind == "print_finished"


def test_a_throttled_printer_does_not_hide_the_event_beside_it():
    """A throttled status produces no batch, but must never swallow an event
    queued in the same tick — the two live in separate structures precisely so
    neither can block the other."""
    clock = Clock()
    uplink = make_uplink({1}, min_interval_s=60.0, now=clock)

    uplink.feed(status_message(1))
    uplink.flush()

    uplink.feed(status_message(1, progress=43.0))  # inside the window — stays dirty
    uplink.feed(print_complete_message(1))

    frames = uplink.flush()
    assert len(frames) == 1
    assert frames[0].data.kind == "print_finished"


# ------------------------------------------------------------------ the queue


def test_a_hundred_updates_one_printer_flush_to_one_entry():
    uplink = make_uplink({1})
    for i in range(100):
        uplink.feed(status_message(1, progress=float(i)))

    frames = uplink.flush()
    batches = [f for f in frames if f.type == "status_batch"]
    assert len(batches) == 1 and len(batches[0].data.printers) == 1
    assert batches[0].data.printers[0].progress == 99.0, "the NEWEST won"
    assert batches[0].data.seq == 1


def test_a_hundred_status_pushes_never_touch_dropped():
    """Coalescing means overflow is impossible by construction for a status —
    only the bounded events backlog can overflow, and only events count
    against ``dropped``."""
    uplink = make_uplink({1}, min_interval_s=0.0)

    for progress in range(600):
        uplink.feed(status_message(1, progress=float(progress)))

    assert uplink.dropped == 0
    assert uplink.pending == 1, "600 pushes for one printer coalesce to a single dirty entry"


def test_seq_increments_across_flushes_and_oversized_ticks_split():
    """``BATCH_MAX_PRINTERS`` published, dirty printers in one tick is split
    into consecutive batches with consecutive ``seq`` values, and the counter
    keeps counting into the NEXT tick rather than restarting."""
    total = BATCH_MAX_PRINTERS * 2 + 200  # 1200 at the current 500/batch
    uplink = make_uplink(set(range(1, total + 1)), min_interval_s=0.0)
    for pid in range(1, total + 1):
        uplink.feed(status_message(pid))

    frames = uplink.flush()
    batches = [f for f in frames if f.type == "status_batch"]
    assert len(batches) == 3
    assert [b.data.seq for b in batches] == [1, 2, 3]
    assert [len(b.data.printers) for b in batches] == [BATCH_MAX_PRINTERS, BATCH_MAX_PRINTERS, 200]
    assert sum(len(b.data.printers) for b in batches) == total

    uplink.feed(status_message(1, progress=99.0))
    next_frames = uplink.flush()
    next_batches = [f for f in next_frames if f.type == "status_batch"]
    assert len(next_batches) == 1
    assert next_batches[0].data.seq == 4, "seq keeps counting from where the oversized tick left off"


def test_an_overflowing_events_backlog_drops_the_oldest_without_raising():
    """The tap runs inside ``broadcast``, so it cannot block and it cannot
    fail. Unlike a status, an event has no later message to correct it — so an
    events backlog left unflushed for a very long time must drop its OLDEST
    entries, the same overflow policy the old unified queue used, now scoped
    to the one structure that can still overflow.

    Drained over several flushes, not one — ``flush`` caps how many events it
    emits per tick (:data:`EVENTS_PER_FLUSH`), so the 90 survivors of a
    100-deep backlog take more than a single call to fully appear; the point
    pinned here is what survives overall, not how many ticks that takes (a
    dedicated test covers the per-tick split).
    """
    uplink = make_uplink({1})

    for i in range(EVENTS_MAXSIZE + 10):
        uplink.feed(print_complete_message(1, status=f"job-{i}"))

    assert uplink.dropped == 10

    finished = []
    while uplink.pending:
        finished.extend(f for f in uplink.flush() if f.data.kind == "print_finished")

    assert len(finished) == EVENTS_MAXSIZE
    assert finished[0].data.detail["status"] == "job-10", "the survivors start after the dropped ten"


def test_flush_caps_events_per_tick_spreading_a_backlog_across_ticks():
    """A long outage's WHOLE event backlog going out the moment the link is
    back is itself a burst the portal never asked for — see
    :data:`EVENTS_PER_FLUSH`. 50 queued events split 20 / 20 / 10 across three
    flushes, in order, nothing dropped; and a dirty printer's own batch still
    rides the SAME tick as its events, capped or not.
    """
    uplink = make_uplink({1}, min_interval_s=0.0)

    for i in range(50):
        uplink.feed(print_complete_message(1, status=f"job-{i}"))
    uplink.feed(status_message(1))

    assert uplink.dropped == 0, "well under EVENTS_MAXSIZE — nothing here should overflow"

    first = uplink.flush()
    first_events = [f for f in first if f.type == "event"]
    first_batches = [f for f in first if f.type == "status_batch"]
    assert len(first_events) == EVENTS_PER_FLUSH
    assert [f.data.detail["status"] for f in first_events] == [f"job-{i}" for i in range(20)]
    assert len(first_batches) == 1, "the dirty printer's own batch still rides this tick, capped events or not"

    second = [f for f in uplink.flush() if f.type == "event"]
    third = [f for f in uplink.flush() if f.type == "event"]

    assert [len(second), len(third)] == [EVENTS_PER_FLUSH, 10]
    assert [f.data.detail["status"] for f in second] == [f"job-{i}" for i in range(20, 40)]
    assert [f.data.detail["status"] for f in third] == [f"job-{i}" for i in range(40, 50)]
    assert uplink.pending == 0, "fully drained after three ticks"


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
def test_feed_never_raises_and_flush_survives_junk(junk):
    """``feed`` is called synchronously from the product's broadcast path. It
    has one job — take the message — and no input may make it throw."""
    uplink = make_uplink({1})
    uplink.feed(junk)

    assert uplink.flush() == []


def test_a_message_the_uplink_has_no_use_for_never_enters_the_dirty_map_or_events():
    """Archive, library and inventory broadcasts outnumber printer pushes
    during a library scan. Letting them into the 100-deep event deque would
    push real ``print_complete`` broadcasts out of it before the link ever
    flushed one."""
    uplink = make_uplink({1})
    uplink.feed({"type": "library_file_added", "data": {"id": 7}})
    uplink.feed({"type": "archive_created", "data": {"id": 9}})

    assert uplink.pending == 0
    assert uplink.flush() == []


# ------------------------------------------------------------ reset_transient


def test_reset_transient_clears_dirty_resets_seq_keeps_events():
    """The client loop calls this on every reconnect, before building the
    fresh snapshot chunks. Three things must all be true at once: the stale
    dirty entry is gone (the snapshot supersedes it), the queued event
    survives (nothing else will ever carry it), and ``seq`` restarts at 1 for
    this connection's first batch."""
    uplink = make_uplink({1}, min_interval_s=0.0)
    uplink.feed(status_message(1))
    first = uplink.flush()
    assert first[0].data.seq == 1, "sanity check on the pre-reset seq"

    uplink.feed(status_message(1, progress=77.0))  # goes dirty, never sent
    uplink.feed(print_start_message(1))  # queued as an event

    uplink.reset_transient()

    assert uplink.pending == 1, "dirty was cleared; the queued event was not"

    uplink.feed(status_message(1, progress=88.0))  # freshly dirty after the reset
    frames = uplink.flush()

    assert [f.type for f in frames] == ["event", "status_batch"]
    assert frames[1].data.seq == 1, "seq restarted — this reads as a brand new connection's first batch"
    assert frames[1].data.printers[0].progress == 88.0, "the pre-reset 77.0 reading was dropped, not sent stale"


def test_flush_with_nothing_dirty_returns_no_frames():
    uplink = make_uplink({1})
    assert uplink.flush() == []


# --------------------------------------------- the per-printer failure boundary


def test_a_broken_printer_build_does_not_cost_the_tick():
    """One printer's build failure must not discard the events, or the OTHER
    printers' batch entries, already computed this same ``flush`` call.

    Before the per-printer ``try/except``, an exception escaping mid-loop
    unwound all the way out of ``flush`` — discarding the local ``frames``
    list (every event, every earlier printer's batch entry) since the
    function never reached its ``return``. A poisoned identity lookup for one
    printer must cost only that printer's own entry.
    """
    uplink = make_uplink({1, 2})
    real_printer_from_status = uplink._printer_from_status

    def poisoned(pid, data):
        if pid == 2:
            raise RuntimeError("a poisoned identity lookup")
        return real_printer_from_status(pid, data)

    uplink._printer_from_status = poisoned  # type: ignore[method-assign]

    uplink.feed(status_message(1))
    uplink.feed(status_message(2))
    uplink.feed(print_start_message(1))

    frames = uplink.flush()

    events = [f for f in frames if f.type == "event"]
    batches = [f for f in frames if f.type == "status_batch"]
    assert len(events) == 1 and events[0].data.kind == "print_started", (
        "the event survives a completely unrelated printer's build failure"
    )
    assert len(batches) == 1
    assert [p.id for p in batches[0].data.printers] == ["1"], "printer 2's broken build cost only its own entry"

    # Popped after the failed attempt is an acceptable outcome — the log is
    # the record. It must not still be sitting there to retry-loop on forever.
    assert 2 not in uplink._dirty


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
    chunk = await one_chunk(uplink, db_session)

    assert [p.id for p in chunk.data.printers] == ["1"]
    only = chunk.data.printers[0]
    assert only.name == "X2D Front-Left"
    assert only.model == "X2D"
    assert only.state == "printing"
    assert only.progress == 42.0
    assert parse_frame(make_frame(chunk)).type == "snapshot_chunk"


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
    chunk = await one_chunk(uplink, db_session)

    assert [p.id for p in chunk.data.printers] == ["2"]


async def test_an_inactive_printer_is_excluded_even_when_it_is_published(db_session: AsyncSession):
    """``is_active`` is Maintenance Mode — the machine is parked and not
    available. "Available" in this codebase is ``is_active AND NOT archived``,
    and the portal sees the same set the farm does."""
    await _add_printer(db_session, 1, "Parked X2D", "X2D", is_active=False)
    await _publish(db_session, 1)

    uplink = Uplink(manager=FakeManager(states={1: _running_state()}))
    chunk = await one_chunk(uplink, db_session)

    assert chunk.data.printers == []


async def test_a_published_printer_with_no_live_state_is_offline_in_the_snapshot(db_session: AsyncSession):
    """A printer that is configured but not connected still belongs in the
    snapshot — its absence would read as "not in the farm" rather than "not
    reachable", and the two need different action from the operator."""
    await _add_printer(db_session, 1, "X2D Front-Left", "X2D")
    await _publish(db_session, 1)

    uplink = Uplink(manager=FakeManager())
    chunk = await one_chunk(uplink, db_session)

    printer = chunk.data.printers[0]
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
    chunk = await one_chunk(uplink, db_session)

    assert chunk.data.printers[0].model == ""


async def test_the_snapshot_refreshes_the_in_memory_publish_set(db_session: AsyncSession):
    """``flush`` may not touch the database, so something has to keep its
    filter current. The snapshot already reads the allowlist for its own sake —
    doing it there means one query answers both questions."""
    await _add_printer(db_session, 1, "X2D Front-Left", "X2D")
    await _publish(db_session, 1)

    uplink = Uplink(manager=FakeManager(names={1: ("X2D Front-Left", "X2D")}))
    uplink.feed(status_message(1))
    assert uplink.flush() == [], "nothing is published until the set has been read"

    await uplink.build_snapshot_chunks(db_session)

    uplink.feed(status_message(1))
    assert uplink.flush() != []


async def test_an_archived_printer_stops_producing_status_frames_too(db_session: AsyncSession):
    """The availability filter has to reach ``flush``, not only the snapshot.

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
    await uplink.build_snapshot_chunks(db_session)

    uplink.feed(status_message(1))
    assert uplink.flush() == [], "archived means gone from the portal, status frames included"

    uplink.feed(status_message(2))
    assert uplink.flush() != []


async def test_a_printer_in_maintenance_mode_stops_producing_status_frames_too(db_session: AsyncSession):
    """Same for ``is_active`` — "available" is one definition, applied once."""
    await _add_printer(db_session, 1, "Parked X2D", "X2D", is_active=False)
    await _publish(db_session, 1)

    uplink = Uplink(manager=FakeManager(names={1: ("Parked X2D", "X2D")}))
    await uplink.build_snapshot_chunks(db_session)

    uplink.feed(status_message(1))
    assert uplink.flush() == []


async def test_the_snapshot_seeds_the_connection_watcher(db_session: AsyncSession):
    """``_connection_event`` stays silent on a printer it has never seen, so
    that the snapshot is not immediately echoed back as an event. That same
    silence swallowed the FIRST real connection change after every agent
    reconnect — the snapshot has to hand the watcher its starting point.
    """
    await _add_printer(db_session, 1, "X2D Front-Left", "X2D")
    await _publish(db_session, 1)

    uplink = Uplink(manager=FakeManager(states={1: _running_state()}, names={1: ("X2D Front-Left", "X2D")}))
    chunk = await one_chunk(uplink, db_session)
    assert chunk.data.printers[0].state == "printing", "the snapshot says it is up"

    uplink.feed(status_message(1, connected=False, state="RUNNING"))

    frames = uplink.flush()
    assert frames[0].type == "event"
    assert frames[0].data.kind == "printer_offline"


async def test_a_snapshot_of_a_disconnected_printer_seeds_the_watcher_the_other_way(db_session: AsyncSession):
    """The seed has to be the reported value, not a hopeful True — otherwise a
    farm that reconnects while a printer is down invents a ``printer_offline``
    the moment it comes back."""
    await _add_printer(db_session, 1, "X2D Front-Left", "X2D")
    await _publish(db_session, 1)

    uplink = Uplink(manager=FakeManager(names={1: ("X2D Front-Left", "X2D")}))
    chunk = await one_chunk(uplink, db_session)
    assert chunk.data.printers[0].state == "offline"

    uplink.feed(status_message(1, connected=True))

    frames = uplink.flush()
    assert frames[0].data.kind == "printer_online"


# ---------------------------------------------------------------- the chunks


async def test_1200_published_printers_split_into_three_chunks_sharing_one_sync_id(db_session: AsyncSession):
    """The other side of :data:`SNAPSHOT_CHUNK_PRINTERS`: a farm larger than
    one chunk splits into consecutive, ``sync_id``-linked acts the portal can
    reassemble."""
    total = SNAPSHOT_CHUNK_PRINTERS * 2 + 200  # 1200 at the current 500/chunk
    db_session.add_all(
        Printer(
            id=pid,
            name=f"Printer {pid}",
            serial_number=f"SN{pid:06d}",
            ip_address="192.168.1.10",
            access_code="12345678",
            model="X2D",
        )
        for pid in range(1, total + 1)
    )
    db_session.add_all(CloudLinkPrinter(printer_id=pid) for pid in range(1, total + 1))
    await db_session.commit()

    uplink = Uplink(manager=FakeManager())
    chunks = await uplink.build_snapshot_chunks(db_session)

    assert len(chunks) == 3
    assert len({c.data.sync_id for c in chunks}) == 1, "every chunk of one act shares one sync_id"
    assert [c.data.chunk for c in chunks] == [1, 2, 3]
    assert all(c.data.of == 3 for c in chunks)
    assert [len(c.data.printers) for c in chunks] == [SNAPSHOT_CHUNK_PRINTERS, SNAPSHOT_CHUNK_PRINTERS, 200]
    assert sum(len(c.data.printers) for c in chunks) == total


async def test_a_mid_connection_resync_carries_the_running_seq_not_zero(db_session: AsyncSession):
    """``base_seq`` on a resync act is the connection's RUNNING ``status_batch``
    count, never 0 — only :meth:`Uplink.reset_transient` (an actual reconnect)
    restarts the numbering. The live portal keys its ``lastSeq`` off this
    value, so a resync that reported 0 here would read as the farm rewinding
    every batch it had already sent, and ``seq`` must keep counting past the
    act afterwards too — a resync is not a second connection.
    """
    await _add_printer(db_session, 1, "X2D Front-Left", "X2D")
    await _publish(db_session, 1)

    uplink = make_uplink({1}, min_interval_s=0.0)
    uplink.feed(status_message(1))
    first = [f for f in uplink.flush() if f.type == "status_batch"]
    assert [f.data.seq for f in first] == [1], "sanity check on the pre-resync seq"

    uplink.feed(status_message(1, progress=50.0))
    second = [f for f in uplink.flush() if f.type == "status_batch"]
    assert [f.data.seq for f in second] == [2]

    chunks = await uplink.build_snapshot_chunks(db_session)
    assert all(c.data.base_seq == 2 for c in chunks), "the resync's act carries the running seq, not a reset one"

    uplink.feed(status_message(1, progress=75.0))
    third = [f for f in uplink.flush() if f.type == "status_batch"]
    assert [f.data.seq for f in third] == [3], "seq keeps counting past the resync — it is not reset mid-connection"


async def test_a_small_farm_snapshot_is_one_chunk_of_one(db_session: AsyncSession):
    await _add_printer(db_session, 1, "X2D Front-Left", "X2D")
    await _publish(db_session, 1)

    uplink = Uplink(manager=FakeManager())
    chunk = await one_chunk(uplink, db_session)

    assert chunk.data.chunk == 1
    assert chunk.data.of == 1


async def test_an_empty_publish_set_still_sends_one_empty_chunk(db_session: AsyncSession):
    """The portal learns "this farm publishes nothing" and gets a
    ``base_seq`` to anchor whatever comes after from this one act — an empty
    return here would tell it nothing at all."""
    uplink = Uplink(manager=FakeManager())
    chunk = await one_chunk(uplink, db_session)

    assert chunk.data.printers == []


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
    chunk = await one_chunk(uplink, db_session)

    temps = chunk.data.printers[0].temps
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
    chunk = await one_chunk(uplink, db_session)

    assert chunk.data.printers[0].temps.chamber == 38.0


async def test_a_printer_with_no_model_recorded_reports_no_chamber(db_session: AsyncSession):
    """Unknown model, no chamber — the same answer ``printer_state_to_dict``
    gives when ``printer_manager.get_model`` returns ``None``. Guessing the
    other way would publish the meaningless reading for exactly the printers
    nobody has identified yet."""
    await _add_printer(db_session, 1, "Nameless", "")
    await _publish(db_session, 1)

    uplink = Uplink(manager=FakeManager(states={1: _running_state()}))
    chunk = await one_chunk(uplink, db_session)

    assert chunk.data.printers[0].temps.chamber is None


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

    printer = one_status(uplink)
    assert printer.temps.chamber is None
    assert printer.temps.bed == 60.0

    manager.remove_internal_listener(uplink.feed)


# --------------------------------------------------------------- end to end


async def test_a_real_broadcast_reaches_the_portal_as_a_status_batch():
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

    frames = uplink.flush()
    assert len(frames) == 1
    assert frames[0].type == "status_batch"
    printer = frames[0].data.printers[0]
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
    # The one test with nothing hand-copied also proves the real broadcast
    # path survives the portal's own contract parser.
    assert parse_frame(make_frame(frames[0])).type == "status_batch"

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

    frames = uplink.flush()
    assert len(frames) == 1
    assert frames[0].data.kind == "print_finished"
    assert frames[0].data.printer_id == "7"

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
    chunk = await one_chunk(uplink, db_session)

    assert chunk.data.printers[0].error.code == "0300_8004"
