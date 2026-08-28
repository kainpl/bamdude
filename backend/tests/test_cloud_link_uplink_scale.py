"""Cloud Link uplink — scale and reconnect-burst regressions.

Two things ``test_cloud_link_uplink.py`` pins one printer at a time; this file
pins them at farm scale, where a naive implementation would actually show a
different number.

**The rate test.** Coalescing (:attr:`Uplink._dirty`) plus per-printer
throttling (:attr:`Uplink.min_interval_s`) plus batching
(:data:`BATCH_MAX_PRINTERS`) are three separate mechanisms; each is pinned
alone elsewhere. What is not pinned anywhere else is that stacking them still
holds at farm scale: a busy farm pushing thousands of broadcasts must cost the
portal a small, bounded number of frames per tick — not a number that grows
with the push rate. The portal's own cap (120 frames per ``RATE_WINDOW``,
mentioned in its own review — not a constant this repo carries) is the reason
this matters; the test pins the multiplier this side owns, not the portal's
literal number.

**The no-burst test.** A link that has been down accumulates a real backlog —
:attr:`Uplink._dirty` fills with one entry per printer, however many pushes
each one actually made. ``reset_transient`` (called by the client loop before
every fresh snapshot, on every reconnect) throws that backlog away rather than
flushing it: the snapshot about to follow is newer than anything coalesced
behind it, so turning the backlog into a ``status_batch`` here would send
stale readings behind a picture that already supersedes them. The events
backlog is a different structure with a different rule — no later message
corrects a lost ``print_finished`` — so it must survive the same reset intact.
"""

from __future__ import annotations

from backend.app.services.bambu_mqtt import PrinterState
from backend.app.services.cloud_link.uplink import BATCH_MAX_PRINTERS, Uplink

# --------------------------------------------------------------- the fixtures
#
# Deliberately re-declared rather than imported from test_cloud_link_uplink —
# every other file in this test family (test_cloud_link_client.py,
# test_cloud_link_snapshot.py, ...) does the same rather than reaching across
# test modules for fixtures.


def status_message(printer_id: int, **overrides) -> dict:
    """What ``ConnectionManager.send_printer_status`` puts on the wire, narrowed
    to what a rate/burst test needs — see the sibling file's ``status_message``
    for the full fixture, including the fields the uplink must ignore."""
    data = {
        "connected": True,
        "state": "RUNNING",
        "current_print": "bracket_v3.gcode.3mf",
        "subtask_name": "bracket_v3",
        "progress": 0.0,
        "temperatures": {"bed": 60.0, "bed_target": 60.0, "nozzle": 219.5, "nozzle_target": 220.0, "chamber": 38.0},
        "hms_errors": [],
    }
    data.update(overrides)
    return {"type": "printer_status", "printer_id": printer_id, "data": data}


def print_start_message(printer_id: int) -> dict:
    return {
        "type": "print_start",
        "printer_id": printer_id,
        "data": {"filename": "Metadata/plate_1.gcode", "subtask_name": "bracket_v3", "remaining_time": 3600},
    }


def print_complete_message(printer_id: int, status: str = "completed") -> dict:
    return {
        "type": "print_complete",
        "printer_id": printer_id,
        "data": {"status": status, "filename": "Metadata/plate_1.gcode", "subtask_name": "bracket_v3"},
    }


class FakeManager:
    """``printer_manager`` narrowed to what the uplink asks of it. No identity
    is seeded — every printer in these tests falls back to ``Printer {id}``,
    which is fine: neither test reads names or models."""

    def get_status(self, printer_id: int) -> PrinterState | None:
        return None

    def get_model(self, printer_id: int) -> str | None:
        return None

    def get_printer(self, printer_id: int):
        return None


class Clock:
    """A hand-wound monotonic clock — see the sibling file's identical fixture."""

    def __init__(self, t: float = 1000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


def make_uplink(published: set[int], **kwargs) -> Uplink:
    uplink = Uplink(manager=FakeManager(), **kwargs)
    uplink.set_publish_set(published)
    return uplink


def _reading(tick: int, push: int) -> float:
    """A progress value that encodes exactly when it was fed, and stays inside
    the contract's 0-100 bound (unlike a plain ``tick * 100 + push``, which the
    clamp in ``_printer_from_status`` would flatten every tick past the first
    down to the same 100.0 — silently defeating a "which tick landed" check).
    """
    return tick + push * 0.01


# ------------------------------------------------------------------ the rate


def test_a_busy_farm_stays_orders_of_magnitude_under_the_portals_cap():
    """300 published printers, 10 ticks, 10 pushes per printer over the run —
    3000 broadcasts in total — plus two job events dropped in along the way.

    However fast the printers push, ``flush``'s output is governed by three
    things that all cap it, none of which is the push rate: coalescing means
    one entry per printer survives to a tick no matter how many pushes it got;
    the throttle means a printer already reported this window stays dirty
    rather than being re-sent; and with 300 printers under the 500-printer
    ``BATCH_MAX_PRINTERS`` ceiling, one tick's worth of dirty printers always
    fits in a single ``status_batch``. Ten ticks land nowhere near the
    portal's 120-frame cap.
    """
    clock = Clock()
    total_printers = 300
    ticks = 10
    pushes_per_printer_per_tick = 10
    published = set(range(1, total_printers + 1))
    uplink = make_uplink(published, now=clock)
    assert total_printers <= BATCH_MAX_PRINTERS, "the single-batch-per-tick assumption below depends on this"

    all_frames = []
    for tick in range(ticks):
        for pid in published:
            for push in range(pushes_per_printer_per_tick):
                # Coalescing collapses these ten into the one entry that
                # matters: the last. ``_reading`` carries both the tick and the
                # push index so the final assertion can tell a genuinely last
                # value apart from an earlier one that merely looks plausible.
                uplink.feed(status_message(pid, progress=_reading(tick, push)))
        if tick == 3:
            uplink.feed(print_start_message(1))
        if tick == 7:
            uplink.feed(print_complete_message(2))
        all_frames.extend(uplink.flush())
        clock.advance(uplink.min_interval_s)  # reopen every printer's throttle window before the next tick

    batches = [f for f in all_frames if f.type == "status_batch"]
    events = [f for f in all_frames if f.type == "event"]

    assert len(events) == 2, "sanity check: both fabricated events made it out, somewhere in the run"
    assert len(batches) == ticks, "300 printers fit in one batch every tick — the split path never engages here"
    assert len(all_frames) <= 2 * ticks + len(events), (
        f"expected roughly one status_batch per tick plus the two events, got {len(all_frames)} frames "
        f"across {ticks} ticks and {total_printers * ticks * pushes_per_printer_per_tick} broadcasts — "
        f"nowhere near the portal's 120-frame cap"
    )

    final_batch = batches[-1]
    by_id = {p.id: p for p in final_batch.data.printers}
    last_reading = _reading(ticks - 1, pushes_per_printer_per_tick - 1)
    for pid in (1, 2, 150, 299, 300):
        assert by_id[str(pid)].progress == last_reading, (
            f"printer {pid}: the final batch must carry the LAST fed reading, not an earlier one"
        )


# --------------------------------------------------------------- the no-burst


def test_reconnecting_after_a_long_outage_does_not_flush_the_backlog_as_a_batch():
    """The link was down for a while: 200 printers pushed 50 times each —
    10 000 broadcasts — with nobody there to flush them, plus a couple of job
    events dropped into the mix. On reconnect the client loop calls
    ``reset_transient`` before it asks for the fresh snapshot chunks, so by the
    time this flush runs, none of that backlog can turn into a ``status_batch``:
    the dirty map that would have carried it was already cleared, on the
    premise that the snapshot about to follow is newer than anything coalesced
    behind it. The events backlog is a different structure with a different
    rule — no later message corrects a lost ``print_finished`` — so it
    survives the very same reset intact.
    """
    total_printers = 200
    pushes_per_printer = 50
    published = set(range(1, total_printers + 1))
    uplink = make_uplink(published, min_interval_s=0.0)

    for pid in published:
        for push in range(pushes_per_printer):
            uplink.feed(status_message(pid, progress=float(push)))
    uplink.feed(print_start_message(1))
    uplink.feed(print_complete_message(2))

    assert uplink.pending == total_printers + 2, (
        "sanity check before the reset: one coalesced entry per printer, plus the two queued events"
    )

    uplink.reset_transient()
    assert uplink.pending == 2, "reset_transient must clear the dirty map but leave the events deque alone"

    frames = uplink.flush()

    batches = [f for f in frames if f.type == "status_batch"]
    events = [f for f in frames if f.type == "event"]
    assert batches == [], (
        "no status_batch may emerge here — the backlog that built up while the link was down was dirty "
        "state the reset just discarded, not a queued frame waiting for its turn on the wire"
    )
    assert [e.data.kind for e in events] == ["print_started", "print_finished"], (
        "both queued events must survive the reset and come out on the very next flush"
    )
