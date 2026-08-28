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
  write in the product. It takes the message, puts it in a bounded deque, and
  returns. All the thinking happens in :meth:`Uplink.drain`, which the link's
  own loop calls.
* **No database on the hot path.** ``drain`` works entirely from in-memory
  state: the publish set, each printer's name and model, and its last-known
  ``connected``. :meth:`Uplink.build_snapshot` is the one method that holds a
  session, and it refreshes all three while it is there — one query answering
  every question ``drain`` is not allowed to ask.
* **Availability is the database's answer, and it reaches BOTH paths.** A
  ``CloudLinkPrinter`` row survives archiving on purpose (the allowlist has no
  opinion about a machine's lifecycle, and rebuilding it on archive would lose
  a user's choice if they restored the printer). So the read side filters:
  "available" here is ``is_active AND NOT archived``, the same definition the
  rest of the codebase uses. ⚠️ The set ``build_snapshot`` hands ``drain`` is
  the FILTERED one — an archived printer stays MQTT-connected and goes on
  broadcasting, so seeding the raw allowlist would keep it out of the snapshot
  and put it in every status frame after it, which is the louder of the two.

⚠️ **A printer's contract id is its local integer as a decimal string** —
printer 7 is ``"7"``. The contract types the field as an opaque string and the
portal's own fixtures use ``"p-001"``, but a prefix here would need parsing
back off every inbound ``cmd``; ``int()`` is the whole mapping and there is
nothing to keep in sync.

⚠️ **A connection change emits two frames**, in this order: the
``printer_online`` / ``printer_offline`` event, then the status carrying the
new state — and that status is exempt from the throttle. A printer that has
gone offline sends no further ``printer_status`` broadcast, so its edge status
is the last word about it; dropping it would leave the portal on whatever the
machine was doing when it vanished.

Two things this module deliberately does NOT do:

* **It emits no ``hms_error`` event.** The kind exists in the contract, but a
  printer's fault already rides on every ``status`` frame in
  ``UplinkPrinter.error``, so an event would be a second telling of the same
  fact with its own de-duplication problem to solve. Add it when the portal
  needs to react to the edge rather than read the state.
* **It does not coalesce a backlog.** When the link has been down, the queue
  holds many statuses for one printer and the throttle answers the first, then
  discards the rest. That first frame is therefore as old as the backlog — for
  about one drain cycle, because the snapshot sent at connect has already told
  the portal where everything stands. Keeping only the newest per printer would
  be a better queue; it is not worth the machinery until a measurement says so.
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
    Snapshot,
    SnapshotData,
    Status,
    StatusData,
    Temps,
    UplinkPrinter,
    frame_timestamp,
    new_frame_id,
)
from backend.app.services.cloud_link.store import get_publish_set

logger = logging.getLogger(__name__)

#: How many broadcast messages may wait for a drain. Two orders of magnitude
#: more than a farm produces between two drains of a healthy link — so reaching
#: it means the link is down, and what is being protected is memory, not
#: latency.
QUEUE_MAXSIZE = 500

#: Seconds between two ``status`` frames for the SAME printer, until the portal
#: says otherwise in ``hello_ok``. Assign to :attr:`Uplink.min_interval_s` when
#: it does.
DEFAULT_MIN_INTERVAL_S = 5.0

#: The broadcast types the uplink has any use for. Filtering here rather than
#: in ``drain`` keeps the bounded queue for messages that can become a frame:
#: during a library scan the archive/library/inventory broadcasts outnumber
#: printer pushes by orders of magnitude, and letting them in would flush every
#: printer status out of a 500-deep queue before the link drained one.
INTERESTING_TYPES = frozenset({"printer_status", "print_start", "print_complete"})

#: What "available" means to this link, as SQL criteria — the product's own
#: definition, ``is_active AND NOT archived``, and the second half of the answer
#: to "may the portal see this printer" (the first is the publish set).
#:
#: A shared constant because it is asked in two places that must never drift:
#: :meth:`Uplink.build_snapshot` filters the whole set with it, and
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

    One instance per link. It owns the queue, the throttle clock and the
    in-memory publish set; the client loop owns when to drain it.
    """

    def __init__(
        self,
        *,
        min_interval_s: float = DEFAULT_MIN_INTERVAL_S,
        now: Callable[[], float] = time.monotonic,
        manager: Any | None = None,
        maxsize: int = QUEUE_MAXSIZE,
    ):
        """
        Args:
            min_interval_s: Seconds between two ``status`` frames for one
                printer. Public and reassignable — the portal sets the real
                value in ``hello_ok``.
            now: The clock. Injected so the throttle can be tested without
                sleeping, and monotonic by default because a wall clock that
                steps backwards over an NTP correction would silence a printer
                until it caught up.
            manager: The printer manager. Defaults to the singleton, resolved
                lazily so importing this module does not drag the MQTT stack
                in behind it.
            maxsize: Queue depth. See :data:`QUEUE_MAXSIZE`.
        """
        self.min_interval_s = min_interval_s
        self._now = now
        self._manager = manager
        # ⚠️ ``maxlen`` IS the overflow policy: a full deque discards from the
        # left on append. When the link is down the newest reading is the one
        # worth keeping — dropping the newest instead would freeze the queue on
        # the moment the connection died and never move on.
        self._queue: deque[dict] = deque(maxlen=maxsize)
        self._dropped = 0
        self._publish: set[int] = set()
        self._last_status_at: dict[int, float] = {}
        # Frames built but not yet handed over. Holds at most one today — a
        # connection edge emits an event and the status behind it, and
        # ``drain`` answers the outbox before it pops the queue again.
        self._outbox: deque[AnyFrame] = deque()
        # Last ``connected`` we reported per printer, for the online/offline
        # edge. Absent means "never seen" — see ``_connection_event``. Seeded
        # by ``build_snapshot`` so a reconnect does not swallow the next edge.
        self._connected: dict[int, bool] = {}
        # id -> (name, model), filled by ``build_snapshot`` from the database.
        # The status path has no session, and this is more authoritative than
        # the manager's connect-time cache: a rename lands here at the next
        # snapshot instead of at the next MQTT reconnect.
        self._identity: dict[int, tuple[str, str]] = {}

    # ------------------------------------------------------------- properties

    @property
    def dropped(self) -> int:
        """How many messages the queue has discarded since this link started."""
        return self._dropped

    @property
    def pending(self) -> int:
        """How many messages are waiting for a drain."""
        return len(self._queue)

    @property
    def published(self) -> frozenset[int]:
        """The printer ids ``drain`` currently lets through.

        A copy, and frozen: the service narrows this set when the allowlist
        changes, and it must do that through :meth:`set_publish_set` — a caller
        mutating the live set would be editing the filter between two pops of
        one drain.
        """
        return frozenset(self._publish)

    # ------------------------------------------------------------- the intake

    def feed(self, message: dict) -> None:
        """Take one broadcast message. Synchronous, enqueue-only, never raises.

        This is the callback registered with
        ``ConnectionManager.add_internal_listener``, so it executes ahead of
        every browser write in the product. It does the least possible: a type
        check, a membership test, an append.
        """
        try:
            if not isinstance(message, dict) or message.get("type") not in INTERESTING_TYPES:
                return
            if len(self._queue) == self._queue.maxlen:
                self._dropped += 1
                # Loud on the first, then every hundredth: a full queue means
                # the link is down and the alternative is either silence or a
                # log line per broadcast for as long as it stays down.
                if self._dropped == 1 or self._dropped % 100 == 0:
                    logger.warning(
                        "Cloud Link: uplink queue full — %d message(s) dropped so far (link not draining?)",
                        self._dropped,
                    )
            self._queue.append(message)
        except Exception as e:  # pragma: no cover — the body above has no reachable raise
            logger.debug("Cloud Link: uplink dropped a malformed broadcast: %s", e)

    def set_publish_set(self, ids: set[int]) -> None:
        """Replace the in-memory allowlist ``drain`` filters against."""
        self._publish = set(ids)

    def reset_transient(self) -> None:
        """Drop frames that were built for a connection which no longer exists.

        The client loop calls this after every successful hello and **before**
        it builds the fresh snapshot. The outbox can be holding the second half
        of a connection edge whose first half went out over a socket that then
        died: delivered after the reconnect it would reach the portal *behind*
        the snapshot, overwriting that printer's current reading with a moment
        the snapshot has already superseded.

        ⚠️ **The message queue is deliberately NOT cleared.** Those are raw
        broadcasts that were never turned into frames, so nothing about them
        was ever sent to anybody; they are the farm's backlog, and the throttle
        in :meth:`drain` already collapses whatever is stale among them.
        Clearing them would throw away the ``print_finished`` that arrived
        while the link was down — the one message with no later push to correct
        it.
        """
        self._outbox.clear()

    # -------------------------------------------------------------- the drain

    async def drain(self) -> AnyFrame | None:
        """The next frame to send, or ``None`` when there is nothing to say.

        Pops until it has a frame rather than returning ``None`` on the first
        unpublished or throttled message: otherwise a queue whose head is a
        throttled status would hide the ``print_finished`` sitting behind it
        until the caller polled again.

        The outbox is answered first, so a connection edge's two frames leave
        in order and neither can be overtaken by a message queued after them.

        Async because the caller is an async loop and because the seam should
        not have to change if a future frame ever needs to await something. It
        deliberately touches no database — see the module docstring.
        """
        if self._outbox:
            return self._outbox.popleft()

        while self._queue:
            frame = self._normalize(self._queue.popleft())
            if frame is not None:
                return frame
        return None

    # --------------------------------------------------------- the normalizer

    def _normalize(self, message: dict) -> AnyFrame | None:
        """One broadcast message → one frame, or ``None`` if it says nothing."""
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
            if kind == "printer_status":
                return self._status_or_connection_event(printer_id, data)
            if kind == "print_start":
                return self._event("print_started", printer_id, {"job_name": self._job_name_of(data)})
            if kind == "print_complete":
                return self._event(
                    "print_finished",
                    printer_id,
                    {"job_name": self._job_name_of(data), "status": str(data.get("status") or "")},
                )
        except Exception as e:
            # A malformed push must cost one frame, not the link. The queue has
            # already been popped, so the loop simply moves on.
            logger.warning("Cloud Link: could not normalize a %s broadcast: %s", message.get("type"), e)
        return None

    def _status_or_connection_event(self, printer_id: int, data: Mapping[str, Any]) -> AnyFrame | None:
        """A ``status`` frame, preceded by an event when the connection changed.

        ⚠️ **A connection edge emits BOTH frames, and the status is not
        throttled.** The event announces the transition; the status is the new
        steady state that follows it, so the event goes first and the status
        waits one ``drain`` in the outbox.

        Sending only the event was a bug with no second chance to correct it: a
        printer that has gone offline produces no further ``printer_status``
        broadcast at all, so the last status the portal ever received was the
        one saying ``printing`` — and it would have gone on saying so until the
        machine came back. Throttling the status would reintroduce exactly that
        hole whenever the edge landed inside a window, which for a printer
        pushing several times a second is almost always.
        """
        edge = self._connection_event(printer_id, data)
        if edge is None:
            if not self._may_send_status(printer_id):
                return None
            return self._status_frame(printer_id, data)

        # Stamp the window as used: the status below IS this printer's report
        # for now, and an unstamped clock would let the next ordinary push
        # through immediately after.
        self._last_status_at[printer_id] = self._now()
        self._outbox.append(self._status_frame(printer_id, data))
        return edge

    def _status_frame(self, printer_id: int, data: Mapping[str, Any]) -> AnyFrame:
        """One ``status`` frame. The throttle is the caller's question."""
        return Status(
            v=1,
            id=new_frame_id(),
            ts=frame_timestamp(),
            type="status",
            data=StatusData(printer=self._printer_from_status(printer_id, data)),
        )

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

    async def build_snapshot(self, session: AsyncSession) -> Snapshot:
        """Every published, available printer as it stands right now.

        Sent at connect, so it is also where everything ``drain`` is forbidden
        to ask about is refreshed: the in-memory publish set, the identity
        cache, and each printer's last-known ``connected``. One pass over the
        database answering all of it.

        ⚠️ **Availability is filtered here, and the filtered set is what
        ``drain`` gets.** A ``CloudLinkPrinter`` row survives archiving — the
        allowlist has no opinion about a machine's lifecycle — so
        ``is_active AND NOT archived`` is what decides whether the portal hears
        about a printer, the same definition used everywhere else in the
        codebase. Seeding ``drain`` with the RAW allowlist would have made that
        filter cosmetic: an archived printer stays MQTT-connected and goes on
        broadcasting, so it would have been absent from the snapshot and
        present in every status frame after it.
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

        return Snapshot(
            v=1,
            id=new_frame_id(),
            ts=frame_timestamp(),
            type="snapshot",
            data=SnapshotData(printers=printers),
        )


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
