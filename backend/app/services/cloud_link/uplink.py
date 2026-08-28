"""Cloud Link uplink — everything this farm tells the portal about itself.

The uplink is a **projection**, not a pipe. It taps the broadcast the browsers
already receive, keeps the dozen fields the envelope contract names, and drops
the rest on the floor. That direction of travel is the whole design: a farm
decides what leaves it, message by message, rather than forwarding what it
happens to have.

Four rules hold the module together.

* **The allowlist is the interface.** :data:`STATUS_FIELDS` and
  :data:`TEMPERATURE_FIELDS` are the complete list of what a printer
  contributes to a frame. Nothing else is read, so nothing else can leak —
  and the product's own broadcasts are full of things that must not: the
  ``print_start`` message carries the entire MQTT push under ``raw_data``,
  serial number included, and the temperature dict carries internal
  bookkeeping keys. Adding a field to a frame means adding it here, in the
  open, where a reviewer sees it.
* **:meth:`Uplink.feed` cannot fail and cannot block.** It runs inside
  ``ConnectionManager.broadcast``, on the event loop, ahead of every browser
  write in the product. For a status it does the least work that still keeps
  the projection correct: a type check, a printer-id check, and an overwrite
  into :attr:`_dirty` — the newest reading replaces whatever was there, so a
  printer that pushes fifty times between two ticks costs one dict write, not
  fifty queued messages. For an event it appends to a small bounded deque and
  returns. All the thinking happens in :meth:`Uplink.flush`, which the link's
  own loop calls once per tick.
* **No database on the hot path.** ``flush`` works entirely from in-memory
  state: the dirty map, the event backlog, the publish set, each printer's
  name and model, and its last-known ``connected``.
  :meth:`Uplink.build_snapshot_chunks` is the one method that holds a
  session, and it refreshes all three while it is there — one query
  answering every question ``flush`` is not allowed to ask.
* **Availability is the database's answer, and it reaches BOTH paths.** A
  ``CloudLinkPrinter`` row survives archiving on purpose (the allowlist has no
  opinion about a machine's lifecycle, and rebuilding it on archive would lose
  a user's choice if they restored the printer). So the read side filters:
  "available" here is ``is_active AND NOT archived``, the same definition the
  rest of the codebase uses. ⚠️ The set ``build_snapshot_chunks`` hands
  ``flush`` is the FILTERED one — an archived printer stays MQTT-connected and
  goes on broadcasting, so seeding the raw allowlist would keep it out of the
  snapshot and put it in every status batch after it, which is the louder of
  the two.

⚠️ **A printer's contract id is its local integer as a decimal string** —
printer 7 is ``"7"``. The contract types the field as an opaque string and the
portal's own fixtures use ``"p-001"``, but a prefix here would need parsing
back off every inbound ``cmd``; ``int()`` is the whole mapping and there is
nothing to keep in sync.

⚠️ **A connection change emits two frames within the SAME tick, in order**:
the ``printer_online`` / ``printer_offline`` event first, then that printer's
reading inside the batch that follows — and that reading is exempt from the
throttle. A printer that has gone offline sends no further ``printer_status``
broadcast, so its edge status is the last word about it; throttling it away
would leave the portal on whatever the machine was doing when it vanished.
:meth:`flush` guarantees the order by construction — every event it produces
goes into the returned list before the first batch does — rather than by
holding the status back for a second call the way the old per-message
``drain`` had to.

Two things this module deliberately does NOT do:

* **It emits no ``hms_error`` event.** The kind exists in the contract, but a
  printer's fault already rides on every batched reading in
  ``UplinkPrinter.error``, so an event would be a second telling of the same
  fact with its own de-duplication problem to solve. Add it when the portal
  needs to react to the edge rather than read the state.
* **It does not keep more than one reading in flight per printer.** A printer
  that pushes between two ticks has its :attr:`_dirty` entry overwritten each
  time — coalescing is the whole point of the dirty map, not an optimisation
  bolted onto a queue afterwards. The one place that changes real behaviour: a
  printer still inside its throttle window when :meth:`flush` runs stays
  dirty rather than being dropped, so the NEXT tick sends whatever is newest
  *then*, not nothing.
"""

from __future__ import annotations

import logging
import time
from collections import deque
from collections.abc import Callable, Mapping
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models.printer import Printer
from backend.app.services.cloud_link.schemas import (
    AnyFrame,
    Event,
    EventData,
    PrinterError,
    SnapshotChunk,
    SnapshotChunkData,
    StatusBatch,
    StatusBatchData,
    Temps,
    UplinkPrinter,
    frame_timestamp,
    new_frame_id,
)
from backend.app.services.cloud_link.store import get_publish_set

logger = logging.getLogger(__name__)

#: How many printers one ``status_batch`` frame may carry. An oversized tick —
#: more dirty, published, unthrottled printers than this — is split into
#: consecutive batches, each with its own :attr:`Uplink._seq`, rather than one
#: frame growing without bound.
BATCH_MAX_PRINTERS = 500

#: How many printers one ``snapshot_chunk`` act may carry. Deliberately equal
#: to :data:`BATCH_MAX_PRINTERS` — the portal caps a sync at ``of <= 64`` acts
#: (its ``MAX_SNAPSHOT_CHUNKS``), so at this size the ceiling is a ~32,000
#: printer farm, which nothing here approaches. There is no pressure to raise
#: it and no reason it should differ from the batch size for a system meant to
#: be understood as one shape.
SNAPSHOT_CHUNK_PRINTERS = 500

#: How many ``print_start``/``print_complete`` broadcasts may wait for a
#: flush. Comfortably more than a healthy link produces between two ticks, so
#: reaching it means the link has been down for a long time. Unlike a status,
#: an event has no later message to correct it, so what overflows here is
#: genuinely lost, not merely stale.
EVENTS_MAXSIZE = 100

#: Seconds between two ``status_batch`` frames for the SAME printer, until the
#: portal says otherwise in ``hello_ok``. Assign to :attr:`Uplink.min_interval_s`
#: when it does.
DEFAULT_MIN_INTERVAL_S = 5.0

#: The broadcast types the uplink has any use for. Filtering here — in
#: ``feed``, before a message reaches either the dirty map or the event deque
#: — matters most for the events: during a library scan the archive/library/
#: inventory broadcasts outnumber printer pushes by orders of magnitude, and
#: letting them into a 100-deep event deque would push a real
#: ``print_complete`` out of it before the link ever flushed one. The dirty
#: map cannot be flooded the same way — it holds at most one entry per printer
#: this farm owns, however fast ``feed`` is called.
INTERESTING_TYPES = frozenset({"printer_status", "print_start", "print_complete"})

#: What "available" means to this link, as SQL criteria — the product's own
#: definition, ``is_active AND NOT archived``, and the second half of the answer
#: to "may the portal see this printer" (the first is the publish set).
#:
#: A shared constant because it is asked in two places that must never drift:
#: :meth:`Uplink.build_snapshot_chunks` filters the whole set with it, and
#: :mod:`~backend.app.services.cloud_link.snapshot` re-asks it for the one
#: printer a ``camera_snapshot`` names. A camera is the more sensitive of the
#: two, so the looser of two definitions would be the wrong one to have there.
AVAILABLE_PRINTER = (Printer.is_active.is_(True), Printer.archived.is_(False))

#: Everything a printer's status contributes to a frame. THE allowlist — see
#: the module docstring. Keys are ``printer_manager.printer_state_to_dict``'s.
STATUS_FIELDS = ("connected", "state", "progress", "subtask_name", "current_print", "temperatures", "hms_errors")

#: The five readings the contract's ``Temps`` carries, and no others. The live
#: dict also holds ``*_heating`` flags, ``chamber_target`` and an internal
#: ``_chamber_target_set_time``; none of them are the portal's business.
TEMPERATURE_FIELDS = ("bed", "bed_target", "nozzle", "nozzle_target", "chamber")

#: Bambu's ``gcode_state`` → the contract's six states.
#:
#: ⚠️ ``PREPARE`` and ``SLICING`` are absent on purpose and fall through to
#: ``unknown``. The contract has no "preparing", and calling them ``printing``
#: would report a progress percentage for a job that has not started.
STATE_MAP = {
    "RUNNING": "printing",
    "PAUSE": "paused",
    "IDLE": "idle",
    "FINISH": "idle",
    "FAILED": "error",
}

#: The two states in which a progress percentage and a job name mean anything.
_JOB_STATES = frozenset({"printing", "paused"})


def _as_float(value: Any) -> float | None:
    """A number, or ``None`` — never a zero standing in for a missing reading.

    A model without a chamber reports nothing for it, and ``0.0`` there reads
    as a freezing chamber rather than an absent sensor.
    """
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _hms_short_code(entry: Mapping[str, Any]) -> str:
    """The ``MMMM_EEEE`` code off a broadcast ``hms_errors`` entry.

    The status dict carries ``{code, attr, module, severity}`` — the operator's
    code is composed from ``attr`` bits 16–31 and the numeric part of ``code``,
    which is the form the printer's own screen and Bambu's wiki use. Mirrors
    ``bambu_mqtt.HMSError.short_code`` and ``main._format_hms_error_summary``;
    the entry arrives here as a plain dict, so the property is out of reach.
    """
    raw = str(entry.get("code", "")).replace("0x", "").replace("0X", "")
    try:
        error = int(raw, 16) & 0xFFFF
        module = (int(entry.get("attr", 0)) >> 16) & 0xFFFF
    except (TypeError, ValueError):
        return raw
    return f"{module:04X}_{error:04X}"


class Uplink:
    """Turns this farm's broadcasts into envelope frames for one portal link.

    One instance per link. It owns the dirty map, the event backlog, the
    throttle clock and the in-memory publish set; the client loop owns when to
    flush it.
    """

    def __init__(
        self,
        *,
        min_interval_s: float = DEFAULT_MIN_INTERVAL_S,
        now: Callable[[], float] = time.monotonic,
        manager: Any | None = None,
    ):
        """
        Args:
            min_interval_s: Seconds between two ``status_batch`` frames
                carrying the same printer. Public and reassignable — the
                portal sets the real value in ``hello_ok``.
            now: The clock. Injected so the throttle can be tested without
                sleeping, and monotonic by default because a wall clock that
                steps backwards over an NTP correction would silence a printer
                until it caught up.
            manager: The printer manager. Defaults to the singleton, resolved
                lazily so importing this module does not drag the MQTT stack
                in behind it.
        """
        self.min_interval_s = min_interval_s
        self._now = now
        self._manager = manager
        # printer_id -> the NEWEST broadcast data for a printer with
        # something unsent to say. Overwritten, never appended to —
        # coalescing IS the storage, not a policy layered on top of a queue.
        # Bounded by construction: it can never hold more entries than this
        # farm has printers, whatever the message rate.
        self._dirty: dict[int, Mapping[str, Any]] = {}
        # Raw print_start/print_complete broadcasts, oldest first. Bounded so
        # a link that is down for a very long time cannot grow this without
        # limit; unlike a status, one lost here has no later message to
        # correct it — see EVENTS_MAXSIZE.
        self._events: deque[dict] = deque(maxlen=EVENTS_MAXSIZE)
        self._dropped = 0
        # This connection's status_batch counter. The first batch this
        # instance ever sends is seq 1 — see ``flush``. Reset to 0 by
        # ``reset_transient`` on every reconnect: the portal's protocol scopes
        # seq to one connection, not to this process's lifetime.
        self._seq = 0
        self._publish: set[int] = set()
        self._last_status_at: dict[int, float] = {}
        # Last ``connected`` we reported per printer, for the online/offline
        # edge. Absent means "never seen" — see ``_connection_event``. Seeded
        # by ``build_snapshot_chunks`` so a reconnect does not swallow the
        # next edge.
        self._connected: dict[int, bool] = {}
        # id -> (name, model), filled by ``build_snapshot_chunks`` from the
        # database. The status path has no session, and this is more
        # authoritative than the manager's connect-time cache: a rename lands
        # here at the next snapshot instead of at the next MQTT reconnect.
        self._identity: dict[int, tuple[str, str]] = {}

    # ------------------------------------------------------------- properties

    @property
    def dropped(self) -> int:
        """How many event broadcasts the event backlog has discarded since
        this link started. Statuses never count here — they coalesce, they
        cannot overflow."""
        return self._dropped

    @property
    def pending(self) -> int:
        """How many printers and events are waiting for a flush.

        The sum of the dirty map and the event backlog — the two things a
        flush drains. Not a queue depth any more, but the same question a
        caller asks it for: "is there a backlog building up".
        """
        return len(self._dirty) + len(self._events)

    @property
    def published(self) -> frozenset[int]:
        """The printer ids ``flush`` currently lets through.

        A copy, and frozen: the service narrows this set when the allowlist
        changes, and it must do that through :meth:`set_publish_set` — a caller
        mutating the live set would be editing the filter mid-flush.
        """
        return frozenset(self._publish)

    # ------------------------------------------------------------- the intake

    def feed(self, message: dict) -> None:
        """Take one broadcast message. Synchronous, never blocks, never raises.

        This is the callback registered with
        ``ConnectionManager.add_internal_listener``, so it executes ahead of
        every browser write in the product. A ``printer_status`` overwrites
        this printer's entry in :attr:`_dirty` — the coalescing itself, done
        here rather than deferred to :meth:`flush` so a flush never has to
        choose among several readings for one printer, only ever has one. A
        ``print_start``/``print_complete`` is appended to the bounded event
        backlog instead; unlike a status it has no later message to correct
        it, so it cannot be coalesced away.
        """
        try:
            if not isinstance(message, dict) or message.get("type") not in INTERESTING_TYPES:
                return
            printer_id = message.get("printer_id")
            if not isinstance(printer_id, int) or isinstance(printer_id, bool):
                return

            if message["type"] == "printer_status":
                data = message.get("data")
                if isinstance(data, Mapping):
                    self._dirty[printer_id] = data
                return

            if len(self._events) == self._events.maxlen:
                self._dropped += 1
                # Loud on the first, then every hundredth: a full backlog
                # means the link is down and the alternative is either
                # silence or a log line per broadcast for as long as it stays
                # down.
                if self._dropped == 1 or self._dropped % 100 == 0:
                    logger.warning(
                        "Cloud Link: uplink events backlog full — %d message(s) dropped so far (link not draining?)",
                        self._dropped,
                    )
            self._events.append(message)
        except Exception as e:  # pragma: no cover — the body above has no reachable raise
            logger.debug("Cloud Link: uplink dropped a malformed broadcast: %s", e)

    def set_publish_set(self, ids: set[int]) -> None:
        """Replace the in-memory allowlist ``flush`` filters against."""
        self._publish = set(ids)

    def reset_transient(self) -> None:
        """Drop the per-connection state that describes a link which no
        longer exists.

        The client loop calls this after every successful hello and **before**
        it builds the fresh snapshot chunks.

        ⚠️ **``_dirty`` is cleared, and ``_seq`` restarts at 0.** The snapshot
        about to be built reads the LIVE state, so it is newer than anything
        still coalesced in ``_dirty`` for the connection that just died —
        keeping it would flush stale readings behind a snapshot that has
        already superseded them. ``_seq`` is scoped to one connection by the
        portal's own protocol (reset on reconnect, the snapshot's ``base_seq``
        is whatever it is when the chunks are built), so restarting it here,
        before those chunks are built, is what keeps the two in step.

        ⚠️ **``_events`` is deliberately NOT cleared.** Those are raw
        ``print_start``/``print_complete`` broadcasts that arrived while the
        link was down; unlike a status they have no later message to correct
        them — a ``print_finished`` that never went out is gone for good, not
        merely superseded. The next flush drains them, ahead of the fresh
        state's first batch.
        """
        self._dirty.clear()
        self._seq = 0

    # -------------------------------------------------------------- the flush

    def flush(self) -> list[AnyFrame]:
        """This tick's frames, in order: every event first, then a batch per
        :data:`BATCH_MAX_PRINTERS` published, unthrottled, dirty printers.

        Events go first and unconditionally — see the module docstring on why
        a connection edge's event must precede the batch that carries its
        printer's own reading. A dirty printer whose window has not reopened
        **stays dirty**: it is neither popped nor batched, so the next flush
        sees it again with whatever is newest by then. Only a dirty printer
        that is emitted (batched, or as an edge) or unpublished is popped.

        Never touches the database — see the module docstring.
        """
        frames: list[AnyFrame] = []
        while self._events:
            frame = self._normalize_event(self._events.popleft())
            if frame is not None:
                frames.append(frame)

        batch: list[UplinkPrinter] = []
        for pid in list(self._dirty):
            if pid not in self._publish:
                self._dirty.pop(pid)
                continue

            data = self._dirty[pid]
            edge = self._connection_event(pid, data)
            if edge is not None:
                frames.append(edge)
                # Stamp the window as used: the batched reading below IS this
                # printer's report for now, and an unstamped clock would let
                # the very next ordinary push through immediately behind it.
                self._last_status_at[pid] = self._now()
            elif not self._may_send_status(pid):
                continue  # stays dirty for a later tick
            self._dirty.pop(pid)
            batch.append(self._printer_from_status(pid, data))

        for start in range(0, len(batch), BATCH_MAX_PRINTERS):
            self._seq += 1
            frames.append(
                StatusBatch(
                    v=1,
                    id=new_frame_id(),
                    ts=frame_timestamp(),
                    type="status_batch",
                    data=StatusBatchData(seq=self._seq, printers=batch[start : start + BATCH_MAX_PRINTERS]),
                )
            )
        return frames

    # --------------------------------------------------------- the normalizer

    def _normalize_event(self, message: dict) -> AnyFrame | None:
        """One ``print_start``/``print_complete`` broadcast → one event frame,
        or ``None`` if it says nothing.

        The event half of what used to be one status-and-event normalizer —
        the status half dissolved into :meth:`flush`'s own loop, which already
        holds a validated, published printer id and its data.
        """
        try:
            printer_id = message.get("printer_id")
            if not isinstance(printer_id, int) or isinstance(printer_id, bool):
                return None
            if printer_id not in self._publish:
                return None

            data = message.get("data")
            if not isinstance(data, Mapping):
                return None

            kind = message.get("type")
            if kind == "print_start":
                return self._event("print_started", printer_id, {"job_name": self._job_name_of(data)})
            if kind == "print_complete":
                return self._event(
                    "print_finished",
                    printer_id,
                    {"job_name": self._job_name_of(data), "status": str(data.get("status") or "")},
                )
        except Exception as e:
            # A malformed push must cost one frame, not the link. The event
            # has already been popped, so the loop simply moves on.
            logger.warning("Cloud Link: could not normalize a %s broadcast: %s", message.get("type"), e)
        return None

    def _connection_event(self, printer_id: int, data: Mapping[str, Any]) -> AnyFrame | None:
        """``printer_online`` / ``printer_offline`` on a change, else ``None``.

        There is no dedicated connect/disconnect broadcast in the product — the
        connection state travels inside ``printer_status.data.connected`` — so
        the edge is what the uplink watches.

        ⚠️ **The first sighting raises nothing.** The snapshot sent at connect
        already states whether each printer is up; an event on the first status
        push would duplicate it every time the agent reconnects.
        """
        connected = bool(data.get("connected"))
        previous = self._connected.get(printer_id)
        self._connected[printer_id] = connected
        if previous is None or previous == connected:
            return None
        return self._event("printer_online" if connected else "printer_offline", printer_id, {})

    def _event(self, kind: str, printer_id: int, detail: dict) -> AnyFrame:
        """Build one event frame. Never throttled — see :meth:`_may_send_status`."""
        return Event(
            v=1,
            id=new_frame_id(),
            ts=frame_timestamp(),
            type="event",
            data=EventData(kind=kind, printer_id=str(printer_id), detail=detail),
        )

    def _may_send_status(self, printer_id: int) -> bool:
        """Whether this printer's status window has reopened.

        Per printer, not per link: a busy machine must not silence a quiet one.
        Events never ask — a status is a sample of something continuous and
        skipping one costs latency, while dropping a ``print_finished`` leaves
        the portal showing a print that never ends.
        """
        now = self._now()
        last = self._last_status_at.get(printer_id)
        if last is not None and (now - last) < self.min_interval_s:
            return False
        self._last_status_at[printer_id] = now
        return True

    # ---------------------------------------------------------- the projection

    def _printer_from_status(self, printer_id: int, data: Mapping[str, Any]) -> UplinkPrinter:
        """The contract's view of one printer, built from :data:`STATUS_FIELDS`.

        The single place an ``UplinkPrinter`` is constructed, so the allowlist
        is enforced once for both the status path and the snapshot.
        """
        name, model = self._identity_of(printer_id)

        if not data.get("connected"):
            # ⚠️ ``connected`` outranks ``gcode_state``. The last push before a
            # printer dropped off the network says RUNNING forever, and a
            # portal showing a machine as printing hours after it went dark is
            # worse than one showing nothing.
            state = "offline"
        else:
            state = STATE_MAP.get(str(data.get("state") or ""), "unknown")

        progress: float | None = None
        job_name: str | None = None
        if state in _JOB_STATES:
            raw = _as_float(data.get("progress"))
            # Clamped rather than passed through: the contract bounds this 0–100
            # and pydantic enforces it, so a firmware reading of 255 would cost
            # an exception on a path whose whole job is not to have one.
            progress = None if raw is None else max(0.0, min(100.0, raw))
            job_name = self._job_name_of(data)

        return UplinkPrinter(
            id=str(printer_id),
            name=name,
            model=model,
            state=state,
            progress=progress,
            job_name=job_name,
            temps=self._temps_of(data.get("temperatures")),
            error=self._error_of(printer_id, data.get("hms_errors")),
        )

    @staticmethod
    def _temps_of(temperatures: Any) -> Temps:
        """The five allowlisted readings. Anything else in the dict stays home.

        A fresh model each time rather than one shared "nothing to report"
        constant: pydantic models are mutable, and a shared instance would put
        every offline printer in a snapshot behind the same object.
        """
        source = temperatures if isinstance(temperatures, Mapping) else {}
        return Temps(**{key: _as_float(source.get(key)) for key in TEMPERATURE_FIELDS})

    @staticmethod
    def _job_name_of(data: Mapping[str, Any]) -> str | None:
        """What a human calls the job.

        ⚠️ ``gcode_file`` is deliberately not a fallback: it is a path inside
        the 3MF (``Metadata/plate_1.gcode``), identical on every print, and
        naming every job that would make the portal's list useless.
        """
        name = data.get("subtask_name") or data.get("current_print")
        return str(name) if name else None

    def _error_of(self, printer_id: int, hms_errors: Any) -> PrinterError | None:
        """The printer's first active HMS fault, code and text.

        ⚠️ **An HMS error does not change the reported state.** A machine can
        print through a chamber-regulation warning; the contract carries the
        error in its own field precisely so the two are separable, and folding
        it into ``state`` would show every enclosed-machine PETG print as
        failed.

        The first entry rather than the worst: the same choice
        ``hms_errors.classify_pause_reason`` makes, and the firmware reports
        the fault it is acting on first.
        """
        if not isinstance(hms_errors, list) or not hms_errors:
            return None
        first = hms_errors[0]
        if not isinstance(first, Mapping):
            return None

        code = _hms_short_code(first)
        if not code:
            return None
        return PrinterError(code=code, message=self._describe(printer_id, code))

    @staticmethod
    def _describe(printer_id: int, short_code: str) -> str:
        """Bambu's own English text for a code, falling back to the code itself.

        English, not the operator's locale: the portal renders in whatever
        language its viewer chose, and a farm has no say in that. The catalogue
        is per model and cached; ``device_of`` answers ``""`` for a printer the
        manager has no info on, which ``describe`` turns into ``None`` — never
        a guess, because 879 codes describe different mechanisms on different
        machines.
        """
        try:
            from backend.app.services.hms_catalogue import describe, device_of

            return describe(device_of(printer_id), None, short_code.replace("_", ""), "en") or short_code
        except Exception:
            return short_code

    def _identity_of(self, printer_id: int) -> tuple[str, str]:
        """``(name, model)`` — from the last snapshot, else the manager.

        Both halves are required strings in the contract, so there is always an
        answer: a printer whose model nobody has recorded yet gets ``""``, which
        is honest, and one we know nothing about at all is named by its id
        rather than dropped — a machine missing from the portal reads as "not in
        this farm", which is a different and worse claim.
        """
        known = self._identity.get(printer_id)
        if known is not None:
            return known

        manager = self._printer_manager()
        name = ""
        model = ""
        try:
            info = manager.get_printer(printer_id) if manager else None
            name = getattr(info, "name", "") or ""
            model = (manager.get_model(printer_id) if manager else None) or ""
        except Exception as e:  # pragma: no cover — defensive around a foreign object
            logger.debug("Cloud Link: could not resolve printer %s identity: %s", printer_id, e)
        return (name or f"Printer {printer_id}", model)

    def _printer_manager(self) -> Any:
        """The injected manager, or the singleton — imported on first use so
        this module can be imported without the MQTT stack behind it."""
        if self._manager is None:
            from backend.app.services.printer_manager import printer_manager

            self._manager = printer_manager
        return self._manager

    # ----------------------------------------------------------- the snapshot

    async def build_snapshot_chunks(self, session: AsyncSession) -> list[SnapshotChunk]:
        """Every published, available printer as it stands right now, split
        into chunks of :data:`SNAPSHOT_CHUNK_PRINTERS` sharing one ``sync_id``.

        Sent at connect (and on a portal-requested resync), so this is also
        where everything ``flush`` is forbidden to ask about is refreshed: the
        in-memory publish set, the identity cache, and each printer's
        last-known ``connected``. One database pass answers all of it.

        ⚠️ **Availability is filtered here, and the filtered set is what
        ``flush`` gets.** A ``CloudLinkPrinter`` row survives archiving — the
        allowlist has no opinion about a machine's lifecycle — so
        ``is_active AND NOT archived`` is what decides whether the portal hears
        about a printer, the same definition used everywhere else in the
        codebase. Seeding ``flush`` with the RAW allowlist would have made that
        filter cosmetic: an archived printer stays MQTT-connected and goes on
        broadcasting, so it would have been absent from the snapshot and
        present in every status batch after it.

        ⚠️ **An empty farm still returns one chunk.** ``chunk=1, of=1,
        printers=[]`` is how the portal learns "this farm publishes nothing"
        and gets a ``base_seq`` to anchor whatever comes after — an empty
        return here would tell it nothing at all.

        ``base_seq`` is ``self._seq`` at the moment the chunks are built, which
        is 0 for the connect snapshot — the client loop calls
        ``reset_transient`` before this — and whatever the running count is for
        a portal-requested resync mid-connection.
        """
        published = await get_publish_set(session)

        printers: list[UplinkPrinter] = []
        available: set[int] = set()
        if published:
            rows = (
                await session.execute(
                    select(Printer.id, Printer.name, Printer.model)
                    .where(Printer.id.in_(published))
                    .where(*AVAILABLE_PRINTER)
                    .order_by(Printer.id)
                )
            ).all()

            manager = self._printer_manager()
            for printer_id, name, model in rows:
                available.add(printer_id)
                self._identity[printer_id] = (name or f"Printer {printer_id}", model or "")
                state = None
                try:
                    state = manager.get_status(printer_id) if manager else None
                except Exception as e:  # pragma: no cover — defensive around a foreign object
                    logger.debug("Cloud Link: no live state for printer %s: %s", printer_id, e)
                status = _status_of(state, model)
                # ⚠️ Seed the connection watcher from what the snapshot reports.
                # ``_connection_event`` stays silent on a printer it has never
                # seen, so without this every agent reconnect would swallow the
                # first real connection change that followed it.
                self._connected[printer_id] = bool(status.get("connected"))
                printers.append(self._printer_from_status(printer_id, status))

        self.set_publish_set(available)

        sync_id = new_frame_id()
        chunks = [
            printers[i : i + SNAPSHOT_CHUNK_PRINTERS] for i in range(0, len(printers), SNAPSHOT_CHUNK_PRINTERS)
        ] or [[]]
        of = len(chunks)
        return [
            SnapshotChunk(
                v=1,
                id=new_frame_id(),
                ts=frame_timestamp(),
                type="snapshot_chunk",
                data=SnapshotChunkData(sync_id=sync_id, chunk=i + 1, of=of, base_seq=self._seq, printers=chunk),
            )
            for i, chunk in enumerate(chunks)
        ]


#: The chamber keys ``printer_state_to_dict`` removes for a model without a
#: real sensor. Copied from it deliberately rather than narrowed to the one key
#: :data:`TEMPERATURE_FIELDS` reads: the point is that ``_status_of``'s output
#: is shape-identical to a broadcast's ``data``, so the two paths cannot drift.
_CHAMBER_KEYS = ("chamber", "chamber_target", "chamber_heating")


def _chamber_filtered(temperatures: Mapping[str, Any], model: str | None) -> dict[str, Any]:
    """``temperatures`` as the browser would receive it for this model.

    Imported inside the function on purpose — the module is deliberately
    importable without the MQTT stack behind it (see :meth:`Uplink._printer_manager`),
    and ``supports_chamber_temp`` lives in ``printer_manager``.
    """
    from backend.app.services.printer_manager import supports_chamber_temp

    if supports_chamber_temp(model):
        return dict(temperatures)
    return {k: v for k, v in temperatures.items() if k not in _CHAMBER_KEYS}


def _status_of(state: Any, model: str | None) -> dict[str, Any]:
    """A live ``PrinterState`` narrowed to :data:`STATUS_FIELDS`.

    The adapter that lets the snapshot and the broadcast tap share one builder.
    It reads the allowlisted attributes and nothing else — the same discipline
    ``printer_state_to_dict`` applies for the browser, minus the ninety fields
    the portal has no business seeing.

    ``None`` — a printer that is configured but not connected — becomes a
    status that says exactly that. Leaving it out of the snapshot instead would
    read as "not in this farm".

    ⚠️ ``model`` is not optional, and it is not decoration. The snapshot reads
    ``state.temperatures`` RAW, while every ``printer_status`` broadcast the
    status path taps has already been through ``printer_state_to_dict`` — which
    drops the chamber readings for the models that report a meaningless one
    (P1P, P1S, A1, A1 mini have no chamber sensor). Without this the same
    printer arrived at the portal with a number in the snapshot and ``null`` in
    every status frame after it, flip-flopping once per agent reconnect. So the
    filter is applied here too, with the product's own predicate — the model
    list lives in ``printer_manager`` and must never be re-derived here.
    """
    if state is None:
        return {"connected": False}

    hms_errors = [
        {"code": e.code, "attr": e.attr, "module": e.module, "severity": e.severity}
        for e in (getattr(state, "hms_errors", None) or [])
    ]
    return {
        "connected": bool(getattr(state, "connected", False)),
        "state": getattr(state, "state", ""),
        "progress": getattr(state, "progress", None),
        "subtask_name": getattr(state, "subtask_name", None),
        "current_print": getattr(state, "current_print", None),
        "temperatures": _chamber_filtered(getattr(state, "temperatures", None) or {}, model),
        "hms_errors": hms_errors,
    }
