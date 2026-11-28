"""Print scheduler service - processes the print queue."""

import asyncio
import json
import logging
import time
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import MappingProxyType
from typing import Any, Literal

from sqlalchemy import func, inspect, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from sqlalchemy.orm.attributes import set_committed_value

from backend.app.core.database import async_session
from backend.app.core.tasks import spawn_background_task
from backend.app.core.websocket import ws_manager
from backend.app.i18n import current_language, t
from backend.app.models.archive import PrintArchive
from backend.app.models.library import LibraryFile
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.printer import Printer
from backend.app.models.settings import Settings
from backend.app.models.smart_plug import SmartPlug
from backend.app.models.spool_assignment import SpoolAssignment
from backend.app.models.spoolman_slot_assignment import SpoolmanSlotAssignment
from backend.app.schemas.calibration_mode import derive_mode
from backend.app.services import ams_advertised_overlay as overlay, chamber_history, queue_sources, scheduled_drying
from backend.app.services.auto_queue_unsent_return import UnsentAttempt, retry_busy_return_order
from backend.app.services.farm_startup_guard import farm_startup_guard
from backend.app.services.filament_intake import (
    item_descriptor,
    resolve_source_path,
    routing_detail,
    source_display_filename,
)
from backend.app.services.filament_preflight import preflight_item
from backend.app.services.filament_requirements import PrintRequirementsCache
from backend.app.services.filament_routing import RoutingDeferred
from backend.app.services.notification_service import notification_service
from backend.app.services.printer_manager import (
    first_drying_blocking_reason,
    printer_manager,
    supports_drying,
    supports_drying_while_printing,
    tray_layout_of,
)
from backend.app.services.printer_occupancy import (
    PrinterOccupancyConflict,
    active_claim_printer_ids,
    holding_condition,
    read_queue_occupancy,
    require_scheduler_claim,
    row_holds_printer,
    scheduled_for_later,
)
from backend.app.services.queue_ops import queue_claim_scope
from backend.app.services.queue_row_basis import row_basis
from backend.app.services.queue_wait_reason import write_wait_reason
from backend.app.services.smart_plug_manager import is_monitor_only, smart_plug_manager
from backend.app.services.source_io import SOURCE_FAILURES, SourceUnavailable, require_source_file
from backend.app.services.spoolman_weights import spool_net_weight
from backend.app.services.stagger_groups import GroupKey, StaggerGroupResolver, StaggerSplit
from backend.app.utils import failure_reasons
from backend.app.utils.ams_drying import is_countdown_parked, only_parked_timers, running_dry_minutes
from backend.app.utils.ams_humidity import ams_humidity_percent
from backend.app.utils.filament_types import canonical_filament_type
from backend.app.utils.material_keys import resolve_material_key

logger = logging.getLogger(__name__)

# Bambu firmware states that mean the project_file has actually been accepted
# and the printer is now processing / running / paused mid-print. Used by the
# queue-side watchdog (#1370 / B.3) to gate the "command landed" early return.
# A transition from FINISH → IDLE (user dismissing a post-print prompt without
# the printer ever accepting our project_file) is NOT a "command landed"
# signal even though the state value changed. The subtask_id-advance check
# below remains as the definitive path for H2D's slow FINISH → PREPARE flip
# (#1078). Mirrors the same constant in background_dispatch.py — kept
# duplicated rather than imported to avoid coupling the two services.
_ACTIVE_PRINT_STATES: frozenset[str] = frozenset({"PREPARE", "SLICING", "RUNNING", "PAUSE"})

# Max dispatch attempts one row may spend before it fails instead of claiming
# again (#2555). An attempt is spent in the final claim's own commit, so a
# process that dies mid-dispatch has spent it too and a crash loop is bounded; a
# typed refusal before anything was published gives its own one back, and an
# operator's Retry opens a new budget (post-rebuild D-R5). The watchdog no longer
# reverts a sent start to pending at all — its negative verdict fails the row.
DISPATCH_MAX_ATTEMPTS = 3


@dataclass(frozen=True)
class ClaimResult:
    """The final claim's answer as plain values (D-R5): never an ORM row of the claim's own session."""

    claimed: bool
    reason: str | None = None
    started_at: datetime | None = None
    #: The committed failure a refused claim wrote (``budget_exhausted``): its closed class and sentence.
    failure_code: str | None = None
    failure_message: str | None = None
    #: The claimed row as plain values, as it was committed (G03 B, A5): what the dispatch is built from — never
    #: the tick's copy, which may be older than the row the claim locked.
    snapshot: Mapping[str, Any] | None = None
    storage_epoch: int | None = None


@dataclass(frozen=True)
class VerdictResult:
    """A durable verdict of the pass, proved under the lane (G03 B, A8): written, or refused with its code."""

    written: bool
    code: str | None = None
    returned: bool = False

    def __bool__(self) -> bool:
        return self.written


def first_runnable_query(queue_id: int, now: datetime, cooling: set[int]):
    """The row the claim and every verdict take for a queue's next one (G03 B, A3): pending, not staged, due at
    ``now``, not cooling, by position then id — one definition, so the two cannot disagree on which row is next."""
    query = select(PrintQueueItem.id).where(PrintQueueItem.queue_id == queue_id).where(holding_condition(now))
    if cooling:
        query = query.where(PrintQueueItem.id.notin_(cooling))
    return query.order_by(PrintQueueItem.position, PrintQueueItem.id).limit(1)


async def lock_judged_row(cdb, *, item_id: int, queue_id: int, basis: str) -> tuple[PrintQueueItem | None, str | None]:
    """The row the pass judged, locked under its lane (G03 B, A3 step 3): still pending, in the queue the pass
    loaded it in, with the basis the pass recorded. Anything else is a refusal — the next pass reads it fresh."""
    # Only in the queue the pass loaded it in, whose lane the claim holds (L1): a row moved since is never
    # locked -- its lane belongs to someone else.
    row = await cdb.scalar(
        select(PrintQueueItem)
        .where(PrintQueueItem.id == item_id, PrintQueueItem.queue_id == queue_id)
        .with_for_update(key_share=True)
        .execution_options(populate_existing=True)
    )
    if row is None:
        # Why it is not here, read without a lock (a plain read waits for no row lock).
        status = await cdb.scalar(select(PrintQueueItem.status).where(PrintQueueItem.id == item_id))
        return None, "item_changed" if status == "pending" else "not_pending"
    if row.status != "pending":
        return None, "not_pending"
    if row_basis(row) != basis:
        return None, "item_changed"
    return row, None


async def basis_of(db: AsyncSession, item: PrintQueueItem) -> str:
    """The basis of a row a caller passed without one (the webhook start, a direct call), taken at entry."""
    columns = {attr.key for attr in inspect(type(item)).column_attrs}
    unloaded = [name for name in inspect(item).unloaded if name in columns]
    if unloaded:
        await db.refresh(item, attribute_names=unloaded)
    return row_basis(item)


# Kept as a module-level name because ``auto_queue_eligibility`` imports it from
# here and the two must never diverge. The table itself moved to
# ``utils/filament_types``, which the frontend mirrors.
_canonical_filament_type = canonical_filament_type


def _installed_nozzle_diameters(status) -> list[float]:
    """Parse the installed nozzle diameters from a PrinterState (upstream #1899).

    Returns the diameters the printer actually reports (e.g. ``[0.4]``
    single-nozzle, ``[0.4, 0.6]`` dual-nozzle), skipping the empty-string
    defaults that populate a ``NozzleInfo`` before MQTT fills it in. An empty
    list means "the printer hasn't told us its nozzle hardware" — callers must
    treat that as unknown, not as a mismatch, so we never block a print on
    missing data.
    """
    diameters: list[float] = []
    for nozzle in getattr(status, "nozzles", None) or []:
        raw = getattr(nozzle, "nozzle_diameter", "") or ""
        try:
            value = float(raw)
        except (TypeError, ValueError):
            continue
        if value > 0:
            diameters.append(value)
    return diameters


def _nozzle_mismatch_message(sliced_nozzle: float | None, installed: list[float]) -> str | None:
    """Return an actionable error message when the sliced nozzle can't be printed
    on any installed nozzle, else None (upstream #1899).

    Fail-safe: returns None whenever we lack the data to judge — no sliced
    diameter, or the printer reported no nozzles — so a print is only ever
    blocked on a POSITIVE mismatch. On dual-nozzle printers a match against
    EITHER installed nozzle passes (a 0.6 slice is fine if one hotend is 0.6).
    The 0.05 tolerance absorbs float noise while staying well inside the 0.2 gap
    between adjacent nozzle sizes (0.2/0.4/0.6/0.8).
    """
    if not sliced_nozzle or not installed:
        return None
    if any(abs(d - sliced_nozzle) < 0.05 for d in installed):
        return None
    installed_str = " / ".join(f"{d:g}mm" for d in installed)
    return (
        f"File sliced for a {sliced_nozzle:g}mm nozzle, but the printer has "
        f"{installed_str} installed. Re-slice for the installed nozzle, or "
        f"install the matching nozzle before printing."
    )


# How long a stagger slot is held for a printer that has been dispatched to but
# is still reporting the previous print's state. Measured on the farm where this
# went wrong: dispatch to ``on_print_start`` took 8-28 s across eleven printers.
# The value matches ``PrintScheduler._dispatch_max_hold`` deliberately — both
# answer "how long may a printer take to acknowledge a command we sent", and two
# numbers for one question drift apart.
_DISPATCH_SETTLE_SECONDS = 180.0


class _StaggerSlot:
    """Tracks a printer that recently started and is heating up."""

    __slots__ = ("printer_id", "started_at", "temp_reached_at", "interval_seconds", "saw_active")

    def __init__(self, printer_id: int, interval_seconds: int):
        self.printer_id = printer_id
        self.started_at = time.monotonic()
        self.temp_reached_at: float | None = None
        self.interval_seconds = interval_seconds  # per-printer or system default
        # Whether the printer has been seen actually running the print this slot
        # was taken for. Until then its reported state still describes the
        # PREVIOUS print, and reading a terminal state there as "this one is
        # over" releases the slot before the bed has even begun to heat.
        self.saw_active = False


def _timelapse_storage_full(printer_id: int) -> bool:
    """Whether this printer has run out of room for a timelapse.

    ⚠️ Answers False whenever the figure is unknown — no client, no camera
    report, or a machine that keeps its timelapses on an SD card we cannot
    measure. Pausing a queue on a number nobody has would strand a farm over a
    guess, which is worse than the missing video this is meant to protect.
    """
    from backend.app.services.printer_manager import printer_manager as _pm
    from backend.app.utils.timelapse import is_storage_low

    client = _pm.get_client(printer_id)
    if client is None:
        return False

    storage = (client.state.print_option_support or {}).get("internal_timelapse")
    return is_storage_low(client.state.timelapse_storage or {}, "internal" if storage else "external")


# How long a freshly-armed drying claim is exempt from the state sweep. The
# printer needs a report cycle or two before ``dry_time`` reflects the command,
# and a claim dropped in that window is never recovered.
_DRYING_CLAIM_GRACE_SECONDS = 120.0


# Auto-drying re-arm guards.
#
# ⚠️ An AMS reports HIGHER relative humidity while it is warm than once it has
# cooled — upstream #2770 measured an AMS 2 Pro at 10-13% cold against 15-20%
# throughout every cycle. A threshold set inside that band therefore cannot be
# satisfied while the box is hot, and the firmware ends a cycle whenever it
# decides the filament is dry rather than when the clock runs out. So the next
# pass sees dry_time 0 with the reading still above the threshold and arms
# another cycle: five 12-hour cycles inside four hours, one of them six seconds
# after the previous ended.
#
# The cooldown stops the six-second re-arm; the unproductive-cycle cap stops the
# loop. ⚠️ Neither may ever stop a RUNNING cycle — both only gate starting one.
_AUTO_DRY_REARM_COOLDOWN_SECONDS = 30 * 60
_AUTO_DRY_MAX_UNPRODUCTIVE_CYCLES = 2
# A cycle whose timer stood still (upstream #3478): the firmware took the
# command and left the heater off, so dry_time stays put with dry_status Off.
# It tells nothing about humidity, so it has its own counter; this many in a
# row suspends the unit with a message that says the unit did not heat, not
# that the threshold is too low.
_AUTO_DRY_MAX_NEVER_HEATED_CYCLES = 2


def _never_heated(unit_state: dict[str, object]) -> int:
    """Cycles in a row whose timer stood still on this unit (upstream #3478)."""
    count = unit_state.get("never_heated")
    return count if isinstance(count, int) else 0


# The sustained-humidity wait (upstream #2518): auto-drying arms a start nobody
# scheduled only after the unit has read above its threshold for this long -
# one reading is what opening the AMS lid looks like. The default must agree
# with ``schemas/settings.AppSettings.auto_drying_sustained_minutes``.
AUTO_DRY_SUSTAINED_DEFAULT_MINUTES = 15
# A streak is unbroken only while some pass observed it: four missed passes,
# with this floor so a fast loop does not void a streak on one hiccup.
_AUTO_DRY_SUSTAINED_GAP_FLOOR_SECONDS = 120
# Queue drying dries FOR a scheduled job only when the job is this close: the
# longest preset cycle. A job scheduled for next week is no reason to keep its
# printers drying all week (owner, 2026-11-15; upstream looks ahead without limit).
_QUEUE_PREDRY_HORIZON_HOURS = 12


def _drying_ams_ids(status) -> list[int]:
    """AMS unit ids running a drying cycle, per the firmware's own telemetry.

    ``dry_time`` is minutes remaining, so >0 is the printer stating that a cycle
    is active. Used by the dispatch watchdog to say *why* a print never started.

    ⚠️ A DIAGNOSTIC, not a gate, and deliberately not wired to stop drying before
    dispatch. The hardware this was reported on supports drying **through** a
    print (see ``supports_drying_while_printing``), so drying is not
    incompatible with printing in general; what was observed is one printer
    refusing to *begin* a job while two AMS units were drying, one of them
    without its external PSU. Until it is known whether the obstacle is the
    drying or the power budget (``dry_sf_reason`` 1 / 8), acting on this would
    tear down cycles the machine is perfectly happy to continue.
    """
    ids: list[int] = []
    for unit in (getattr(status, "raw_data", None) or {}).get("ams") or []:
        if not isinstance(unit, dict):
            continue
        try:
            if int(unit.get("dry_time") or 0) > 0:
                ids.append(int(unit.get("id", 0)))
        except (TypeError, ValueError):
            continue
    return ids


#: How long a queued job waits after its source copy found the spool busy (swap-source-lifecycle spec r4 §6).
CAPTURE_RETRY_COOLDOWN_SECONDS = 60

#: After a wake that did not bring the printer online (or a plug that would not switch), how long this tier leaves
#: the printer alone before it tries again — the same answer as AutoQueue's ``_WAKE_COOLDOWN_SECONDS``.
_POWER_ON_COOLOFF_SECONDS = 600.0
#: The row's waiting reason while a printer this tier switched on boots.
_POWERING_ON_REASON = "Printer powering on"
_SCHEDULED_WAIT_MESSAGE = "Scheduled start"


@dataclass
class _PowerOnWake:
    """One printer this tier switched on and is waiting for (G03 E-5), in process memory, keyed by printer id.

    ``grace_until``: until then the printer is booting, the row says so and nothing is retried. After it, until
    ``cooloff_until``, a printer still offline is left alone. A printer that connects ends the record (its other
    auto-on plugs follow). Printer deletion takes it with the printer (``printer_deletion._ID_KEYED``).
    """

    primary_plug_id: int
    grace_until: float
    cooloff_until: float
    reported: bool = False


class PrintScheduler:
    """Background scheduler that processes the print queue."""

    # Built-in drying presets per filament type (from BambuStudio filament profiles)
    # Format: { n3f_temp, n3s_temp, n3f_hours, n3s_hours }
    DEFAULT_DRYING_PRESETS: dict[str, dict[str, int]] = {
        "PLA": {"n3f": 45, "n3s": 45, "n3f_hours": 12, "n3s_hours": 12},
        "PETG": {"n3f": 65, "n3s": 65, "n3f_hours": 12, "n3s_hours": 12},
        "TPU": {"n3f": 65, "n3s": 75, "n3f_hours": 12, "n3s_hours": 18},
        "ABS": {"n3f": 65, "n3s": 80, "n3f_hours": 12, "n3s_hours": 8},
        "ASA": {"n3f": 65, "n3s": 80, "n3f_hours": 12, "n3s_hours": 8},
        "PA": {"n3f": 65, "n3s": 85, "n3f_hours": 12, "n3s_hours": 12},
        "PC": {"n3f": 65, "n3s": 80, "n3f_hours": 12, "n3s_hours": 8},
        "PVA": {"n3f": 65, "n3s": 85, "n3f_hours": 12, "n3s_hours": 18},
    }

    def __init__(self):
        self._running = False
        self._check_interval = 30  # seconds
        # After a pass that actually dispatched something, loop again in a few
        # seconds instead of the full interval so a draining batch (fan-out over
        # several passes, a reverted head-of-line job, a freed printer) doesn't
        # stall behind the idle sleep (#2555). Falls back to _check_interval when
        # a pass dispatches nothing, so this can never tight-loop.
        self._fast_check_interval = 3  # seconds
        # How long a printer switched on by this tier may take to boot and connect (3 min).
        self._power_on_wait_time = 180
        # printer_id -> the wake in progress or cooling off (see ``_PowerOnWake``).
        self._power_on_wakes: dict[int, _PowerOnWake] = {}
        # Track which printers are currently auto-drying (printer_id -> start timestamp)
        self._drying_in_progress: dict[int, float] = {}
        # Printers with a running SCHEDULED drying and the (printer, AMS) units a
        # schedule has reserved — rebuilt by every scheduled-drying tick. Auto-drying
        # never arms a reserved unit, and never drops the queue hold of a printer
        # whose scheduled cycle is still running (it may be cooling, dry_time 0).
        self._scheduled_drying_printers: set[int] = set()
        self._scheduled_dry_units: set[tuple[int, int]] = set()
        self._scheduled_holds_seeded = False
        # ⚠️ The units WE armed, as (printer_id, ams_id) — a narrower question
        # than ``_drying_in_progress``, which also adopts a cycle already
        # running when we first look at the printer. Only this set may be
        # stopped for a print: one auto-dried unit used to be enough to kill a
        # cycle the operator had started by hand on a different unit of the
        # same machine. The consequence is deliberate — after a restart we
        # cannot prove a running cycle is ours, so we leave it alone rather
        # than risk stopping somebody's manual dry.
        self._armed_dry_cycles: dict[tuple[int, int], float] = {}
        # ⚠️ A DIFFERENT question from the claim above, and the two must not be
        # merged however alike the keys look. ``_armed_dry_cycles`` answers "is
        # the cycle on this unit ours, right now" and is swept the moment the
        # cycle ends. This answers "what has drying achieved on this unit", and
        # deliberately OUTLIVES the cycle — that is the whole point of a
        # cooldown. Merging them would let a stale judgement authorise stopping
        # a manual dry started later on the same unit, which is exactly the
        # defect the claim exists to prevent.
        #
        # Entries exist only between arming a cycle and the humidity finally
        # coming down, so the steady state is an empty dict. Fields:
        #   running           — a cycle we armed is on the firmware
        #   ended_at          — monotonic when we observed that cycle end
        #   unproductive      — consecutive armed cycles that ended with the
        #                       reading still above the threshold
        #   best_end_humidity — the lowest reading any cycle here has ended at
        #   suspended         — we have stopped arming this unit, and said so
        self._dry_unit_history: dict[tuple[int, int], dict[str, object]] = {}
        # ⚠️ A THIRD question, kept apart from the two above for the same
        # reason: "how long has this unit read above its threshold". A unit that
        # is only waiting out its streak is not ours to stop and has earned no
        # judgement. In process memory on the monotonic clock: after a restart
        # every streak starts over, so a start is only ever delayed, never early.
        #   since - when the current unbroken above-threshold streak began
        #   last  - the last pass that observed it
        self._auto_dry_above: dict[tuple[int, int], dict[str, float]] = {}
        # Staggered start: rolling slots for electrical load management
        self._stagger_slots: list[_StaggerSlot] = []
        # Serialises check+register in ``acquire_stagger_slot`` so multiple
        # parallel direct-print tasks can't all see "slot free" and over-
        # allocate. Queue path pre-registers under check_queue's serial loop
        # so it doesn't depend on this lock.
        self._stagger_acquire_lock = asyncio.Lock()
        # Defensive in-memory dispatch hold (#1157 ported from upstream v0.2.4b1):
        # a printer that just received a project_file command must not get a
        # second dispatch until either it transitions out of pre_state OR the
        # hard timeout expires. The H2D Pro can take 80–210 s to flip
        # FINISH→PREPARE after project_file, and during that window the
        # PrinterQueue.status='printing' DB seed is empirically unreliable on
        # multi-plate batches (same-file plates double-/triple-dispatched onto
        # the same printer 30 s apart). Keyed in-memory per printer; cleared by
        # the watchdog on success or revert. Pure additive — sits alongside
        # _is_printer_idle() and the DB seed.
        # printer_id -> (monotonic_started_at, pre_state, pre_subtask_id)
        self._dispatch_holds: dict[int, tuple[float, str, str | None]] = {}
        # Minimum cooldown between dispatches to the same printer (covers the
        # H2D's project_file digestion window).
        self._dispatch_min_cooldown = 60.0
        # Hard timeout — drop the hold even if we never observed a transition,
        # so a lost MQTT session can't lock a printer out of the queue forever.
        # Matches the watchdog timeout (90 s) plus a safety margin so the
        # watchdog's release runs first on the unhappy path.
        self._dispatch_max_hold = 180.0
        # A queued job whose source copy found the spool busy waits this long before it is asked again
        # (swap-source-lifecycle spec r4 §6): item id -> monotonic deadline, set only after the attempt's
        # own CAS sent it back to pending. In memory on purpose: a restart allows one new attempt.
        self._capture_cooldowns: dict[int, float] = {}

    def _cooling_item_ids(self) -> set[int]:
        """The jobs still waiting out a busy-spool cooldown -- expired entries are dropped here, never kept."""
        now = time.monotonic()
        self._capture_cooldowns = {item_id: until for item_id, until in self._capture_cooldowns.items() if until > now}
        return set(self._capture_cooldowns)

    def clear_capture_cooldown(self, item_id: int) -> None:
        """An explicit edit, delete or Retry of the job ends its cooldown at once."""
        self._capture_cooldowns.pop(item_id, None)

    async def run(self):
        """Main loop - check queue every interval."""
        self._running = True
        logger.info("Print scheduler started")

        while self._running:
            dispatched = False
            try:
                dispatched = await self.check_queue()
            except Exception as e:
                logger.error("Scheduler error: %s", e)

            # Re-check quickly after a productive pass so a draining batch doesn't
            # stall behind the idle interval; otherwise sleep normally (#2555).
            await asyncio.sleep(self._fast_check_interval if dispatched else self._check_interval)

    def stop(self):
        """Stop the scheduler."""
        self._running = False
        logger.info("Print scheduler stopped")

    async def check_queue(self) -> bool:
        """Check for prints ready to start.

        Returns True if this pass dispatched at least one item, so the caller can
        loop again quickly instead of sleeping the full interval (#2555).
        """
        from backend.app.services.farm_startup_guard import farm_startup_guard

        if not farm_startup_guard.allowed():
            await self._tick_scheduled_drying(set())  # Follow already-running cycles; _try_start remains fenced.
            return False
        if not self._scheduled_holds_seeded:
            await self._seed_scheduled_drying_holds()
        async with async_session() as db:
            # Get all pending items with queue loaded, ordered by queue and position
            from sqlalchemy.orm import selectinload

            # ``id`` last, as the claim orders: which row of a printer is its first
            # runnable one is decided below from this order (G03 E2), and on
            # PostgreSQL equal positions otherwise come back in heap order.
            storage_epoch = queue_sources.current_epoch()
            result = await db.execute(
                select(PrintQueueItem)
                .options(selectinload(PrintQueueItem.queue))
                .where(PrintQueueItem.status == "pending")
                .order_by(PrintQueueItem.queue_id, PrintQueueItem.position, PrintQueueItem.id)
            )
            items = list(result.scalars().all())
            # What the pass judges, recorded before any step reads a row (G03 B, A2): the claim and every verdict
            # compare it with the row they lock, so an edit made while the pass awaited is never dispatched or
            # ended on the old values.
            bases = {item.id: row_basis(item) for item in items}
            requirements_cache = PrintRequirementsCache()
            dispatched = False

            if not items:
                # A stale stagger slot must not survive just because its print
                # was the final queue item.  The normal path below performs
                # this maintenance before inspecting pending work, but this
                # early return used to skip it indefinitely.
                if self._stagger_slots:
                    stagger_enabled, _, _, stagger_wait_bed = await self._get_stagger_settings(db)
                    if stagger_enabled:
                        await self._refresh_stagger_slots(stagger_wait_bed)

                # No pending items - still check auto-drying on idle printers.
                # Seed busy_printers from currently-printing printers (same
                # authoritative source as the full path below — PrinterQueue.status
                # == 'printing') so mid-print drying (print_drying_enabled) resumes
                # even when the dispatched print was the LAST queued item and no
                # pending rows remain to drive the full seeding path.
                busy_seed = await active_claim_printer_ids(db)
                await self._tick_scheduled_drying(busy_seed)
                await self._check_auto_drying(db, [], busy_seed)
                return False

            logger.info(
                "Queue check: found %d pending items: %s",
                len(items),
                [(i.id, i.queue_id, i.archive_id, i.library_file_id) for i in items],
            )

            # Seed busy_printers from PrinterQueue.status='printing'. This is
            # the authoritative "this printer is currently dispatched" marker —
            # set_queue_printing() sets queue.status='printing' at dispatch time
            # (whether item, external, or direct-print). Read with no age check
            # and no cross-check against the printer, so a claim left behind by a
            # process that died mid-dispatch would read as live for ever; that is
            # what release_interrupted_dispatch_claims() clears at startup, and
            # why it must run before the first tick here. Without
            # this guard the H2D / P1 IDLE→RUNNING MQTT transition lag made the
            # next check_queue tick see IDLE via _is_printer_idle() and double-
            # dispatch onto the already-running printer (upstream #950286ad /
            # v0.2.3.2 re-release). Queue-per-printer architecture — reading
            # PrinterQueue directly is simpler than joining through
            # PrintQueueItem and also catches external / direct prints that
            # don't have a corresponding item row.
            busy_printers = await active_claim_printer_ids(db)
            from backend.app.services.ha_sensor_manager import ha_sensor_manager

            ha_interlocks = await ha_sensor_manager.blocked_printers(db)
            # Why each printer sits out this pass — a claim and an obstruction
            # are opposite facts, and the summary below must not call a printer
            # this pass just dispatched to "not available" (upstream #3018).
            busy_reasons: dict[int, str] = {}

            def mark_busy(pid: int, reason: str) -> None:
                busy_printers.add(pid)
                busy_reasons.setdefault(pid, reason)

            for claimed_printer_id in busy_printers:
                busy_reasons[claimed_printer_id] = "claimed"

            # Defense-in-depth (#1157): augment busy_printers with any printer
            # still inside its post-dispatch hold window. The DB seed above can
            # miss in-flight items in a multi-plate batch — same-file plates
            # were being dispatched 30 s apart while the H2D was still digesting
            # the first project_file. The hold is keyed in-memory and released
            # by the watchdog on the success path, so it adds a layer that
            # doesn't depend on DB row visibility or completion-callback timing.
            for held_printer_id in list(self._dispatch_holds.keys()):
                if self._printer_in_dispatch_hold(held_printer_id):
                    mark_busy(held_printer_id, "dispatch_hold")

            # ⚠️ Snapshot taken HERE, before the item loop adds anything.
            #
            # Both sources above mean the same thing: a print on this printer is
            # running or imminent. Everything the loop adds below means only
            # "the queue could not dispatch here this pass" — a printer waiting
            # on a plate-clear acknowledgment, an offline one, one with no
            # matching file. Auto-drying must only see the first kind: reading
            # the whole set as "is printing" is what put a plate-held printer
            # down the mid-print path, capping its drying temperature and
            # logging the cycle as (mid-print) while the machine stood idle.
            dispatching_printers: set[int] = set(busy_printers)

            # Cache per-printer require_plate_clear setting
            _plate_clear_cache: dict[int, bool] = {}

            async def _get_require_plate_clear(pid: int) -> bool:
                if pid not in _plate_clear_cache:
                    from backend.app.models.printer import Printer

                    result = await db.execute(select(Printer.require_plate_clear).where(Printer.id == pid))
                    val = result.scalar_one_or_none()
                    _plate_clear_cache[pid] = val if val is not None else True
                return _plate_clear_cache[pid]

            # Staggered start: update temps and clean expired slots
            stagger_enabled, stagger_concurrent, stagger_interval, stagger_wait_bed = await self._get_stagger_settings(
                db
            )
            if stagger_enabled:
                await self._refresh_stagger_slots(stagger_wait_bed)
                stagger_resolver = await self._load_stagger_resolver(db)
            else:
                stagger_resolver = StaggerGroupResolver.global_only()

            # Log skip reasons once per queue check (not per item)
            skip_reasons: dict[str, int] = {}

            # The printers whose first runnable row this pass has already taken up
            # (G03 E2). Only that row is next — the claim refuses every other one as
            # ``not_first_runnable`` — so only that row may be judged by its
            # ``require_previous_success`` gate, preflighted or started. A later row
            # of the same printer used to reach the gate while the head waited for
            # filament or an order restriction, and was SKIPPED for a failure its
            # real predecessor would have cleared. A head that leaves ``pending``
            # here (its own gate, a source failure) leaves the next row first.
            heads_taken: set[int] = set()

            cooling = self._cooling_item_ids()
            for item in items:
                future = scheduled_for_later(item.scheduled_time, datetime.now(timezone.utc))
                wait_code = item.waiting_reason_code
                # m170 retained old wait text without assigning it a code.
                if not future and (
                    wait_code == "scheduled" or (wait_code is None and item.waiting_reason == _SCHEDULED_WAIT_MESSAGE)
                ):
                    if not await self._wait(db, item, None, None):
                        continue

                # A busy spool refused this job's source copy moments ago: it waits out its cooldown while the
                # rest of the queue moves on (swap-source-lifecycle spec r4 §6).
                if item.id in cooling:
                    skip_reasons["source_capture"] = skip_reasons.get("source_capture", 0) + 1
                    continue

                # Skip items in paused or error queues
                if item.queue and item.queue.status in ("paused", "error"):
                    skip_reasons[f"queue_{item.queue.status}"] = skip_reasons.get(f"queue_{item.queue.status}", 0) + 1
                    continue

                # Skip items in operator-paused queues. is_paused is
                # orthogonal to status — a queue can be 'printing' and
                # paused at once: the running print finishes, the next
                # item just doesn't dispatch until the queue is resumed.
                if item.queue and item.queue.is_paused:
                    skip_reasons["queue_is_paused"] = skip_reasons.get("queue_is_paused", 0) + 1
                    continue

                # Get printer_id from queue
                printer_id = item.queue.printer_id if item.queue else None
                if not printer_id:
                    continue

                # Hold only this printer's pending work; a reachable HA sensor
                # in its configured alert state is an operator-defined gate.
                if printer_id in ha_interlocks:
                    await self._wait(db, item, "ha_sensor", f"Sensor alert: {ha_interlocks[printer_id]}")
                    skip_reasons["ha_sensor"] = skip_reasons.get("ha_sensor", 0) + 1
                    continue

                # Check scheduled time first (scheduled_time is stored in UTC from ISO string)
                if future:
                    await self._wait(db, item, "scheduled", _SCHEDULED_WAIT_MESSAGE)
                    skip_reasons["scheduled_future"] = skip_reasons.get("scheduled_future", 0) + 1
                    continue

                # Skip items that require manual start
                if item.manual_start:
                    await self._wait(db, item, "manual_start", "Manual start required")
                    skip_reasons["manual_start"] = skip_reasons.get("manual_start", 0) + 1
                    continue

                # Everything above is either a printer-wide hold or what the claim's
                # "first runnable" itself leaves out (cooling, scheduled for later,
                # manual start). This row is therefore the printer's next one —
                # unless an earlier row of the same printer already was (G03 E2).
                if printer_id in heads_taken:
                    skip_reasons["not_next"] = skip_reasons.get("not_next", 0) + 1
                    continue
                heads_taken.add(printer_id)

                # ⚠️ A timelapse that was asked for and has nowhere to go PAUSES
                # the queue rather than printing without one. That is a farm
                # decision, not BambuStudio's: Studio offers to delete the oldest
                # recording or to untick the box, both of which need somebody
                # present. Unattended, the only choice that does not quietly
                # throw away what was asked for is to stop and say so.
                #
                # ⚠️ Only the SPACE check lives here. Whether the machine can
                # record at all is asked in the scheduling dialog, where a person
                # is choosing printers and can act on the answer — pausing for a
                # missing card would strand work nobody can un-strand from here.
                #
                # Asked of the head only, and before the busy check (G03 B, A14): a
                # staged, future or later row never pauses its queue, while the next
                # row of a printer that is still printing does (the early pause). The
                # pause is a verdict proved under the lane (V4).
                if getattr(item, "timelapse", False) and _timelapse_storage_full(printer_id):
                    outcome = await self._verdict_storage_full(db, item, bases[item.id], printer_id)
                    if outcome.written:
                        logger.warning(
                            "[Scheduler] Queue for printer %s paused: item %s wants a timelapse and "
                            "the printer is out of space for one",
                            printer_id,
                            item.id,
                        )
                    else:
                        mark_busy(printer_id, "verdict_changed")
                    skip_reasons["timelapse_storage_full"] = skip_reasons.get("timelapse_storage_full", 0) + 1
                    continue

                # Skip if printer already busy this iteration (after the head: A14)
                if printer_id in busy_printers:
                    continue

                # Check if printer is idle
                rpc = await _get_require_plate_clear(printer_id)
                printer_idle = self._is_printer_idle(printer_id, require_plate_clear=rpc)
                if not printer_idle and printer_manager.is_awaiting_plate_clear(printer_id):
                    from backend.app.models.printer import Printer
                    from backend.app.services.order_auto_eject import automatic_predecessor

                    held_printer = await db.get(Printer, printer_id)
                    printer_idle = bool(held_printer and await automatic_predecessor(db, held_printer))
                printer_connected = printer_manager.is_connected(printer_id)

                # Preserve automatic_predecessor's plate-gate decision above:
                # the independent veto here is only a conflicting current run.
                from backend.app.services.print_run_binding import prepared_print_run_conflicts

                # Update waiting_reason based on current state
                new_reason = None
                reason_code = None
                from backend.app.services.order_resources import allowed

                if not await allowed(db, item.project_id, printer_id, project_line_id=item.project_line_id):
                    new_reason = "Order printer restrictions changed"
                    reason_code = "order_resources"
                elif not printer_connected:
                    # A printer this tier switched on is still booting: say so rather than "offline".
                    new_reason = (
                        _POWERING_ON_REASON if self._power_on_phase(printer_id) == "booting" else "Printer offline"
                    )
                    reason_code = "printer_offline"
                elif not printer_idle:
                    if self._drying_holds_queue(printer_id):
                        new_reason = "Drying in progress"
                        reason_code = "drying"
                    elif rpc and printer_manager.is_awaiting_plate_clear(printer_id):
                        status = printer_manager.get_status(printer_id)
                        if status and status.state in ("IDLE", "FINISH", "FAILED"):
                            new_reason = "Plate not cleared"
                            reason_code = "plate_not_cleared"
                elif prepared_print_run_conflicts(printer_manager, printer_id):
                    new_reason = (
                        "A previous print context is blocking this start. Check print states on the System page."
                    )
                    reason_code = "dispatch_runtime_conflict"

                if not await self._wait(db, item, reason_code, new_reason):
                    mark_busy(printer_id, "item_changed")
                    continue

                if reason_code == "order_resources":
                    continue

                # If printer not connected, switch it on via its smart plug(s) — and
                # never wait for it here (G03 E-5). The wake is recorded with a boot
                # grace and a cool-off; a later pass dispatches once the printer has
                # connected. Waiting inside the pass used to hold every printer after
                # this one ≈ 3.5 min per pass, with the pass's PostgreSQL connection
                # idle in transaction, and with no cool-off for a printer that never
                # comes up.
                if not printer_connected:
                    plugs = await self._get_smart_plugs(db, printer_id)
                    auto_on_plugs = self._wake_plugs(plugs)
                    if not auto_on_plugs:
                        mark_busy(printer_id, "offline")
                        continue
                    phase = self._power_on_phase(printer_id)
                    if phase is None:
                        phase = await self._begin_power_on(db, printer_id, auto_on_plugs[0])
                        if phase == "booting":
                            await self._wait(db, item, "printer_offline", _POWERING_ON_REASON)
                    if phase == "booting":
                        mark_busy(printer_id, "powering_on")
                    else:
                        mark_busy(printer_id, "power_on_failed")
                    continue
                if printer_id in self._power_on_wakes:
                    # Woken by this tier and connected now: its other auto-on plugs
                    # (filter, secondary power) follow, as they did after the wait.
                    await self._finish_power_on(db, printer_id)

                # Check if printer is idle (busy with another print)
                #
                # ⚠️ Drying used to be handled inside this branch, and could
                # never have helped: it is not one of the things
                # ``_is_printer_idle`` looks at, so stopping a cycle here could
                # not turn a non-idle printer into an idle one. The stop
                # therefore fired only on the passes where the print was NOT
                # going to start — and on a printer held behind an
                # unacknowledged plate that meant tearing drying down once per
                # scheduler tick, for ever, while auto-drying re-armed in
                # between. It now runs where the decision is actually made,
                # just before dispatch.
                if not printer_idle:
                    mark_busy(printer_id, "not_idle")
                    continue

                if prepared_print_run_conflicts(printer_manager, printer_id):
                    await self._wait(
                        db,
                        item,
                        "dispatch_runtime_conflict",
                        "A previous print context is blocking this start. Check print states on the System page.",
                    )
                    mark_busy(printer_id, "dispatch_runtime_conflict")
                    continue

                # Drying blocks the queue, if the user asked it to. A hold is a
                # skip like any other, so it belongs here with the rest of the
                # availability checks.
                #
                # ⚠️ This setting only starts meaning something now. From inside
                # the not-idle branch both of its answers led to the same skip,
                # so all it ever decided was whether a cycle was needlessly
                # killed on the way. Off by default.
                # A parked timer never ends by itself, so it holds nothing; the
                # hold entry stays, and the stop below still reaches a unit we armed.
                if self._drying_holds_queue(printer_id) and await self._get_bool_setting(db, "queue_drying_block"):
                    await self._wait(db, item, "drying", "Drying in progress")
                    mark_busy(printer_id, "drying")
                    continue

                # Staggered start: check if we have a free slot
                if stagger_enabled and not self._can_start_staggered(stagger_concurrent, printer_id, stagger_resolver):
                    stagger_reason = self._stagger_reason(
                        stagger_wait_bed, stagger_concurrent, printer_id, stagger_resolver
                    )
                    await self._wait(db, item, "stagger", stagger_reason)
                    skip_reasons["stagger_wait"] = skip_reasons.get("stagger_wait", 0) + 1
                    continue

                # Clear waiting_reason - printer is ready
                if not await self._wait(db, item, None, None):
                    mark_busy(printer_id, "item_changed")
                    continue

                # The require_previous_success gate (m116). Checked here, at the
                # last moment before the item is prepared, so it reads the most
                # recent outcome on this printer rather than one from whenever
                # the item was queued.
                if item.require_previous_success and not await self.previous_print_succeeded(db, printer_id, item.id):
                    # V1, proved under the lane (G03 B, A8): the row is still the head the pass judged and the
                    # gate is still shut in the verdict's own transaction.
                    outcome = await self._verdict_skip(db, item, bases[item.id], printer_id)
                    if not outcome.written:
                        mark_busy(printer_id, "verdict_changed")
                        continue
                    # The skipped head is no longer next: the row behind it is judged in this pass.
                    heads_taken.discard(printer_id)
                    # The verdict told the operator, after its commit and only because it wrote.
                    logger.info("Skipped queue item %s — the previous print on printer %s failed", item.id, printer_id)
                    continue

                # Resolve the AMS mapping when it's missing OR unresolved (all -1).
                # A stored all-[-1] mapping is a bug artifact — a frontend
                # status-load race can persist [-1] (#2589) — and must be
                # recomputed from live trays rather than trusted (else it's
                # silently downgraded to external-spool and prints against an
                # empty feed).
                try:
                    await preflight_item(db, item, printer_id, cache=requirements_cache)
                except RoutingDeferred as exc:
                    if exc.reason in SOURCE_FAILURES:
                        if (await self._fail_source_item(db, item, exc.reason, basis=bases[item.id])).written:
                            heads_taken.discard(printer_id)  # failed, so no longer next
                        else:
                            mark_busy(printer_id, "verdict_changed")
                        continue
                    await self._wait(db, item, "filament_unavailable", routing_detail(exc.reason)["message"])
                    continue
                # ⚠️ The plan is NOT written onto ``item`` here: this is the tick's
                # session, and the final claim commits the plan in a session of its
                # own (post-rebuild D-R5). ``_start_print`` asks preflight again and
                # hands the plan to that claim as plain values.

                # Print takes priority — stop a cycle WE armed, now that this
                # item is definitely going out.
                #
                # ⚠️ The placement is the whole point. Every skip between the
                # availability checks and here — a failed previous print, an
                # unmappable item, a filament deficit, a full stagger slot — is
                # another way to spend a drying cycle on a print that never
                # happens, so this waits until the decision is actually made.
                # Skipped entirely for hardware that can dry THROUGH a print:
                # there is nothing to yield.
                if self._drying_in_progress.get(printer_id):
                    model = printer_manager.get_model(printer_id)
                    state_now = printer_manager.get_status(printer_id)
                    firmware = state_now.firmware_version if state_now else None
                    dry_through = await self._get_bool_setting(
                        db, "print_drying_enabled"
                    ) and supports_drying_while_printing(model, firmware)
                    if not dry_through:
                        await self._stop_drying(printer_id)

                # A scheduled cycle yields to the print on EVERY model — even one that
                # dries through printing: its temperature is the operator's, not the
                # mid-print cap (spec, owner's decision 6). It resumes when the printer
                # is free, within its window.
                if printer_id in self._scheduled_drying_printers:
                    # Its own session, like the tick: a failure here must not leave the
                    # queue's session unusable for the print it is about to start.
                    try:
                        async with async_session() as drying_db:
                            await scheduled_drying.preempt_for_print(
                                drying_db, printer_id, datetime.now(timezone.utc).replace(tzinfo=None)
                            )
                    except Exception as exc:  # noqa: BLE001 - the print must not depend on drying
                        # The tick's follow sees the print and takes the cycle back.
                        logger.warning("Scheduled drying: preemption failed on printer %d: %s", printer_id, exc)
                    self._scheduled_drying_printers.discard(printer_id)
                    self._drying_in_progress.pop(printer_id, None)

                # Start the print — _start_print spawns a parallel task for the
                # FTP/dispatch pipeline and returns once the queue row is flipped
                # to "printing" + the stagger slot is pre-registered. This lets
                # the next loop iteration dispatch a different printer in
                # parallel instead of serialising through one global await.
                if not await self._start_print(
                    db, item, basis=bases[item.id], requirements_cache=requirements_cache, storage_epoch=storage_epoch
                ):
                    if item.status != "pending":
                        # A refusal that ended the row itself (its source is gone):
                        # the row behind it is next, as it would have been before.
                        heads_taken.discard(printer_id)
                    continue
                dispatched = True
                mark_busy(printer_id, "dispatched")
                # ⚠️ The one addition the narrow set DOES take: this print is
                # imminent. The printer's own state still reads IDLE for a few
                # seconds after dispatch, so without this the drying pass at the
                # end of this very tick would re-arm the cycle we just stopped.
                dispatching_printers.add(printer_id)

            # Log summary of skip reasons (helps diagnose why queue items aren't starting)
            if skip_reasons:
                logger.info("Queue skip summary: %s", skip_reasons)
            if busy_printers:
                self._log_busy_printers(busy_printers, busy_reasons)

            # Scheduled drying first: the units it reserves are off-limits to auto-drying.
            await self._tick_scheduled_drying(dispatching_printers)
            # Auto-drying: start drying on idle printers that have no pending queue items
            await self._check_auto_drying(db, items, dispatching_printers)
            return dispatched

    @staticmethod
    def _log_busy_printers(busy_printers: set[int], busy_reasons: dict[int, str]) -> None:
        """Why each printer sat out this pass (upstream #3018).

        A printer the pass dispatched to is a reservation, not an obstruction.
        For the rest the recorded reason is the cause; the live fields are read
        NOW, after the decision, and are labelled so — they answered "IDLE" for
        a printer one line before it was sent a job.
        """
        for pid in sorted(busy_printers):
            reason = busy_reasons.get(pid, "unknown")
            if reason == "dispatched":
                logger.info("Queue: printer %d reserved - dispatched this pass", pid)
                continue
            state = printer_manager.get_status(pid)
            logger.info(
                "Queue: printer %d skipped this pass: %s (now: connected=%s, state=%s, awaiting_plate_clear=%s)",
                pid,
                reason,
                printer_manager.is_connected(pid),
                state.state if state else "NO_STATUS",
                printer_manager.is_awaiting_plate_clear(pid),
            )

    async def _get_filament_requirements(self, db: AsyncSession, item: PrintQueueItem) -> list[dict] | None:
        """Read the same exact plate evidence as intake and auto assignment."""
        from backend.app.services.filament_intake import read_item_requirements

        requirements = await read_item_requirements(db, item)
        if requirements.status != "ok":
            return None
        # Memory only (G03 B, A23): the claim's plan_fields fixed the plate (A5); this warning only reads it.
        set_committed_value(item, "plate_id", requirements.resolved_plate_id)
        return [dict(f) for f in requirements.used_filaments]

    def _build_loaded_filaments(self, status, printer_id: int | None = None) -> list[dict]:
        """Build list of loaded filaments from printer status.

        Args:
            status: PrinterState from printer_manager
            printer_id: When given, an AMS slot we advertised under a different
                profile (``ams_advertised_overlay``) is reported as the spool it
                REALLY holds — without it the dispatcher would map a job onto
                the masked colour/preset and print with the wrong spool.

        Returns:
            List of loaded filament dicts with type, color, ams_id, tray_id, global_tray_id
        """
        filaments = []

        # Get ams_extruder_map for dual-nozzle printers (H2D, H2D Pro)
        ams_extruder_map = status.raw_data.get("ams_extruder_map", {})

        # Parse AMS units from raw_data
        ams_data = status.raw_data.get("ams", [])
        for ams_unit in ams_data:
            ams_id = int(ams_unit.get("id", 0))
            trays = ams_unit.get("tray", [])
            is_ht = 128 <= ams_id <= 135  # the unit id, not how many trays it listed

            for tray in trays:
                tray_type = tray.get("tray_type")
                if tray_type:
                    tray_id = int(tray.get("id", 0))
                    tray_color = tray.get("tray_color", "")
                    # tray_info_idx identifies the specific spool (e.g., "GFA00", "P4d64437")
                    tray_info_idx = tray.get("tray_info_idx", "")
                    # The three fields the mapping matches on are exactly the
                    # three an advertised profile masks, so they are swapped for
                    # the real spool's before anything downstream compares them.
                    entry = overlay.effective(printer_id, ams_id, tray_id, tray) if printer_id is not None else None
                    if entry is not None:
                        tray_type, tray_color, tray_info_idx = (
                            entry.actual_material,
                            entry.actual_color,
                            entry.actual_variant,
                        )
                    # Normalize color: remove alpha, add hash
                    color = self._normalize_color(tray_color)
                    # Calculate global tray ID
                    # AMS-HT units have IDs starting at 128 with a single tray
                    global_tray_id = ams_id if ams_id >= 128 else ams_id * 4 + tray_id

                    filaments.append(
                        {
                            "type": tray_type,
                            "color": color,
                            "tray_info_idx": tray_info_idx,
                            "ams_id": ams_id,
                            "tray_id": tray_id,
                            "is_ht": is_ht,
                            "is_external": False,
                            "global_tray_id": global_tray_id,
                            "extruder_id": ams_extruder_map.get(str(ams_id)),
                            "remain": tray.get("remain", -1),
                        }
                    )

        # Check external spool(s) (vt_tray is a list)
        for idx, vt in enumerate(status.raw_data.get("vt_tray") or []):
            if vt.get("tray_type"):
                color = self._normalize_color(vt.get("tray_color", ""))
                tray_id = int(vt.get("id", 254))
                filaments.append(
                    {
                        "type": vt["tray_type"],
                        "color": color,
                        "tray_info_idx": vt.get("tray_info_idx", ""),
                        "ams_id": -1,
                        "tray_id": idx,
                        "is_ht": False,
                        "is_external": True,
                        "global_tray_id": tray_id,
                        "extruder_id": (255 - tray_id) if ams_extruder_map else None,
                        "remain": vt.get("remain", -1),
                    }
                )

        return filaments

    def _normalize_color(self, color: str | None) -> str:
        """Normalize color to #RRGGBB format."""
        if not color:
            return "#808080"
        hex_color = color.replace("#", "")[:6]
        return f"#{hex_color}"

    async def _build_inventory_remain_overrides(
        self, db: AsyncSession, printer_id: int, loaded: list[dict]
    ) -> dict[int, float]:
        """Return ``{global_tray_id: remaining_grams}`` for AMS slots the user
        has bound to an inventory spool — BamDude-side or Spoolman-side.

        The MQTT ``remain`` on a tray is the printer firmware's RFID-decremented
        value, which the "Prefer Lowest Remaining Filament" feature has been
        ignoring (#1508): it's meaningful only for Bambu RFID spools (everything
        else reports ``-1`` → clamped → trays tie → sort collapses to slot
        order), and even when set it's the printer's counter, not BamDude's
        ``label_weight - weight_used`` or Spoolman's ``remaining_weight``.

        When a slot is bound to a spool, the user's own inventory tracking is
        authoritative — surface it. Unbound slots are absent from the map (the
        caller falls back to MQTT ``remain``, preserving pre-#1508 behaviour).
        Best-effort: returns ``{}`` on any failure.
        """
        if not loaded:
            return {}
        # External / virtual-tray slots are tracked separately from AMS bindings.
        tracked_slots = [(f["ams_id"], f["tray_id"], f["global_tray_id"]) for f in loaded if not f.get("is_external")]
        if not tracked_slots:
            return {}

        overrides: dict[int, float] = {}
        try:
            if await self._is_spoolman_mode(db):
                result = await db.execute(
                    select(SpoolmanSlotAssignment).where(SpoolmanSlotAssignment.printer_id == printer_id)
                )
                by_slot = {(a.ams_id, a.tray_id): a.spoolman_spool_id for a in result.scalars().all()}
                if not by_slot:
                    return {}
                from backend.app.services.spoolman import get_spoolman_client

                client = await get_spoolman_client()
                if client is None:
                    return {}
                for ams_id, tray_id, gtid in tracked_slots:
                    spoolman_id = by_slot.get((ams_id, tray_id))
                    if spoolman_id is None:
                        continue
                    grams = await self._spoolman_remaining_grams(client, spoolman_id)
                    if grams is not None:
                        overrides[gtid] = grams
                return overrides

            # Internal inventory mode (default). selectinload matches the pattern
            # the inventory routes use, so the row-attribute shape is identical.
            result = await db.execute(
                select(SpoolAssignment)
                .options(selectinload(SpoolAssignment.spool))
                .where(SpoolAssignment.printer_id == printer_id)
            )
            by_slot = {(a.ams_id, a.tray_id): a.spool for a in result.scalars().all()}
            for ams_id, tray_id, gtid in tracked_slots:
                spool = by_slot.get((ams_id, tray_id))
                if spool is None:
                    continue
                overrides[gtid] = max(0.0, float(spool.label_weight or 0) - float(spool.weight_used or 0))
        except Exception as e:  # noqa: BLE001
            logger.warning("prefer-lowest inventory overrides failed for printer %s: %s", printer_id, e)
            return {}
        return overrides

    async def _inventory_label_weights(self, db: AsyncSession, printer_id: int, loaded: list[dict]) -> dict[int, float]:
        """``{global_tray_id: label_weight_grams}`` for the same bound slots
        ``_build_inventory_remain_overrides`` answers for — BamDude ``label_weight``
        or Spoolman ``initial_weight``. The low-filament threshold
        (``services/filament_low.py``) turns the two into a percent, so that
        event reads the SAME remaining figure prefer-lowest does, not a fourth.
        Best-effort: ``{}`` on any failure; a slot without a known weight is absent.
        """
        if not loaded:
            return {}
        tracked_slots = [(f["ams_id"], f["tray_id"], f["global_tray_id"]) for f in loaded if not f.get("is_external")]
        if not tracked_slots:
            return {}
        weights: dict[int, float] = {}
        try:
            if await self._is_spoolman_mode(db):
                result = await db.execute(
                    select(SpoolmanSlotAssignment).where(SpoolmanSlotAssignment.printer_id == printer_id)
                )
                by_slot = {(a.ams_id, a.tray_id): a.spoolman_spool_id for a in result.scalars().all()}
                if not by_slot:
                    return {}
                from backend.app.services.spoolman import get_spoolman_client

                client = await get_spoolman_client()
                if client is None:
                    return {}
                for ams_id, tray_id, gtid in tracked_slots:
                    spoolman_id = by_slot.get((ams_id, tray_id))
                    if spoolman_id is None:
                        continue
                    try:
                        spool = await client.get_spool(spoolman_id)
                    except Exception:  # noqa: BLE001
                        continue
                    # The spool's own size, else its filament's — the label
                    # weight the Filaments page shows; an older spool carries
                    # only the latter.
                    initial = (
                        spool_net_weight(spool) if isinstance(spool, dict) else getattr(spool, "initial_weight", None)
                    )
                    if initial:
                        weights[gtid] = float(initial)
                return weights

            result = await db.execute(
                select(SpoolAssignment)
                .options(selectinload(SpoolAssignment.spool))
                .where(SpoolAssignment.printer_id == printer_id)
            )
            by_slot = {(a.ams_id, a.tray_id): a.spool for a in result.scalars().all()}
            for ams_id, tray_id, gtid in tracked_slots:
                spool = by_slot.get((ams_id, tray_id))
                if spool is None or not spool.label_weight:
                    continue
                weights[gtid] = float(spool.label_weight)
        except Exception as e:  # noqa: BLE001
            logger.warning("inventory label weights failed for printer %s: %s", printer_id, e)
            return {}
        return weights

    @staticmethod
    async def _is_spoolman_mode(db: AsyncSession) -> bool:
        """True when the install is in Spoolman inventory mode."""
        try:
            from backend.app.api.routes.settings import get_setting

            v = await get_setting(db, "spoolman_enabled")
            return bool(v) and v.lower() == "true"
        except Exception:  # noqa: BLE001
            return False

    @staticmethod
    async def _spoolman_remaining_grams(client, spoolman_spool_id: int) -> float | None:
        """Remaining grams for a Spoolman spool via its ``remaining_weight``.
        Returns None on any failure (Spoolman unreachable, spool gone)."""
        try:
            spool = await client.get_spool(spoolman_spool_id)
            rw = spool.get("remaining_weight") if isinstance(spool, dict) else getattr(spool, "remaining_weight", None)
            return float(rw) if rw is not None else None
        except Exception:  # noqa: BLE001
            return None

    @staticmethod
    def _slot_priority(ams_id: int | None, tray_id: int | None) -> int:
        """Deterministic slot-position tie-breaker for the prefer-lowest sort.

        Banded so regular AMS < AMS-HT < external on ties, matching the
        emission order in ``_build_loaded_filaments`` (preserves the
        pre-#1508 stable-sort baseline). ``ams_id < 0`` (VT) must not sort
        to a negative number or it would beat AMS slot 0.
        """
        if ams_id is None or ams_id < 0:
            return 10_000
        if ams_id >= 128:
            return 1_000 + (ams_id - 128) * 4 + (tray_id or 0)
        return ams_id * 4 + (tray_id or 0)

    @staticmethod
    def _prefer_lowest_sort_key(f: dict, overrides: dict[int, float] | None) -> tuple[int, float, int]:
        """Sort key for "Prefer Lowest Remaining Filament" (#1508).

        Two tiers: inventory-tracked spools (0) sort before MQTT-only ones (1),
        then ascending by remaining within each tier, then by slot position.
        The tier flag dominates, so grams (inventory) and percent (MQTT) never
        cross-compare — no unit conversion needed. MQTT ``remain = -1`` maps to
        101 so known spools beat unknown ones (pre-#1508 behaviour preserved).
        """
        gtid = f.get("global_tray_id")
        slot_order = PrintScheduler._slot_priority(f.get("ams_id"), f.get("tray_id"))
        if overrides and gtid in overrides:
            return (0, overrides[gtid], slot_order)
        remain = f.get("remain", -1)
        return (1, float(remain) if remain is not None and remain >= 0 else 101.0, slot_order)

    # ── Staggered start helpers ──────────────────────────────────────────

    async def get_stagger_state_snapshot(self, db: AsyncSession) -> dict:
        """Return current stagger state for UI diagnostics.

        Shape:
            {
              "enabled": bool,
              "concurrent": int,
              "interval_minutes": int,
              "wait_for_bed": bool,
              "split": {"by_tags": bool, "by_location": bool},
              "groups": [
                {"tag_id": int | None, "location_id": int | None,
                 "label": str | None, "color": str | None, "cap": int,
                 "occupied": int, "free_slots": int,
                 "next_free_in_seconds": int | None,
                 "slots": [
                   {"printer_id": int, "printer_name": str,
                    "started_at": float, "temp_reached_at": float | None,
                    "state": "heating" | "interval_wait",
                    "seconds_to_free": int, "interval_seconds": int,
                    "wildcard": bool},
                   ...
                 ]},
                ...
              ],
            }
        """
        enabled, concurrent, interval_seconds, wait_for_bed = await self._get_stagger_settings(db)
        if enabled:
            # The banner is also a maintenance read: without this, an empty
            # queue could expose a slot that the next scheduler pass would
            # have released.
            await self._refresh_stagger_slots(wait_for_bed)
        resolver = await self._load_stagger_resolver(db) if enabled else StaggerGroupResolver.global_only()
        now = time.monotonic()

        def _slot_info(slot: _StaggerSlot) -> dict:
            info = printer_manager.get_printer(slot.printer_id)
            name = info.name if info else f"Printer #{slot.printer_id}"
            if wait_for_bed:
                if slot.temp_reached_at is None:
                    slot_state = "heating"
                    seconds_to_free = max(0, int(slot.interval_seconds))
                else:
                    slot_state = "interval_wait"
                    seconds_to_free = max(0, int(slot.interval_seconds - (now - slot.temp_reached_at)))
            else:
                slot_state = "interval_wait"
                seconds_to_free = max(0, int(slot.interval_seconds - (now - slot.started_at)))
            return {
                "printer_id": slot.printer_id,
                "printer_name": name,
                "started_at": slot.started_at,
                "temp_reached_at": slot.temp_reached_at,
                "state": slot_state,
                "seconds_to_free": seconds_to_free,
                "interval_seconds": slot.interval_seconds,
                # In every group at once — its phase is unknown (spec decision 3).
                "wildcard": resolver.is_wildcard(slot.printer_id),
            }

        groups: list[dict] = []
        for key in sorted(resolver.universe, key=lambda k: (resolver.label(k) or "", k[0] or 0, k[1] or 0)):
            members = [_slot_info(s) for s in self._stagger_slots if key in resolver.groups_for(s.printer_id)]
            cap = resolver.cap_for(key, concurrent)
            free_slots = max(0, cap - len(members))
            times = [m["seconds_to_free"] for m in members]
            groups.append(
                {
                    "tag_id": key[0],
                    "location_id": key[1],
                    "label": resolver.label(key),
                    # The tag's colour, so the banner can badge the group like the manager does.
                    "color": resolver.color_for(key),
                    # The cap THIS group starts under — the global number unless overridden.
                    "cap": cap,
                    "occupied": len(members),
                    "free_slots": free_slots,
                    "next_free_in_seconds": None if free_slots > 0 or not times else min(times),
                    "slots": members,
                }
            )

        return {
            "enabled": enabled,
            "concurrent": concurrent,
            "interval_minutes": interval_seconds // 60,
            "wait_for_bed": wait_for_bed,
            "split": {"by_tags": resolver.tags_split, "by_location": resolver.location_split},
            "groups": groups,
        }

    async def _get_stagger_settings(self, db: AsyncSession) -> tuple[bool, int, int, bool]:
        """Return (enabled, concurrent, interval_seconds, wait_for_bed)."""
        enabled = await self._get_bool_setting(db, "stagger_enabled")
        if not enabled:
            return False, 0, 0, False
        concurrent = int((await self._get_setting_value(db, "stagger_concurrent")) or "2")
        interval_min = int((await self._get_setting_value(db, "stagger_interval_minutes")) or "5")
        wait_for_bed = await self._get_bool_setting(db, "stagger_wait_for_bed")
        return True, max(concurrent, 1), interval_min * 60, wait_for_bed

    def _update_stagger_temps(self) -> bool:
        """Check bed temps for printers in stagger slots, mark reached."""
        changed = False
        for slot in self._stagger_slots:
            if slot.temp_reached_at is not None:
                continue
            state = printer_manager.get_status(slot.printer_id)
            if not state:
                continue
            bed = state.temperatures.get("bed", 0)
            target = state.temperatures.get("bed_target", 0)
            if target > 0 and abs(bed - target) <= 1.0:
                slot.temp_reached_at = time.monotonic()
                changed = True
                logger.info(
                    "Stagger: printer %d bed reached %.1f°C (target %.1f°C), slot freed",
                    slot.printer_id,
                    bed,
                    target,
                )
        return changed

    def _cleanup_stagger_slots(self, wait_for_bed: bool) -> bool:
        """Remove slots that are fully expired (temp reached + interval elapsed)."""
        now = time.monotonic()
        active = []
        for slot in self._stagger_slots:
            iv = slot.interval_seconds

            # Check if the printer is still printing - if not, slot is done
            state = printer_manager.get_status(slot.printer_id)
            live = state.state if state else None
            if live in ("RUNNING", "PREPARE", "PAUSE"):
                # From here on its state describes THIS print, so a terminal
                # state below really does mean the slot's work is over.
                slot.saw_active = True

            if live == "IDLE":
                # IDLE immediately after dispatch still describes the prior
                # print on some firmware, so it needs the same bounded grace
                # period as FINISH/FAILED.  Once this slot has observed its
                # own active state, or that window expires, IDLE means it is
                # no longer heating and must not keep the cap occupied.
                if slot.saw_active or now - slot.started_at >= _DISPATCH_SETTLE_SECONDS:
                    continue
                active.append(slot)
                continue

            if state and live not in ("RUNNING", "PREPARE", "PAUSE"):
                # ⚠️ A printer that has been dispatched to but has not started
                # yet still reports the PREVIOUS print's terminal state —
                # FINISH for seconds to half a minute while the file uploads and
                # the start command lands. Releasing the slot on that reading is
                # how eleven beds came on at once on a farm configured for two:
                # every acquire re-ran this cleanup, evicted the printers still
                # showing FINISH, and found the slots free again (2026-08-19).
                #
                # Bounded by the same window ``_dispatch_holds`` allows a printer
                # to catch up, so a dispatch that never starts cannot hold a slot
                # for ever.
                if not slot.saw_active and now - slot.started_at < _DISPATCH_SETTLE_SECONDS:
                    active.append(slot)
                    continue
                # Printer finished/failed/offline - don't hold the slot
                if slot.temp_reached_at is not None:
                    if now - slot.temp_reached_at < iv:
                        active.append(slot)
                    continue
                continue

            if wait_for_bed:
                if slot.temp_reached_at is None:
                    active.append(slot)  # still heating
                elif now - slot.temp_reached_at < iv:
                    active.append(slot)  # interval not elapsed
            else:
                if now - slot.started_at < iv:
                    active.append(slot)  # interval not elapsed
        changed = len(active) != len(self._stagger_slots)
        self._stagger_slots = active
        return changed

    async def _refresh_stagger_slots(self, wait_for_bed: bool) -> bool:
        """Refresh stagger capacity and notify views if its visible state changed."""
        changed = self._update_stagger_temps()
        changed = self._cleanup_stagger_slots(wait_for_bed) or changed
        if changed:
            try:
                await ws_manager.send_stagger_changed()
            except Exception:
                # A disconnected browser must not prevent the scheduler from
                # releasing capacity for the next printer.
                logger.exception("Could not broadcast stagger capacity change")
        return changed

    async def _load_stagger_resolver(self, db: AsyncSession) -> StaggerGroupResolver:
        """The groups printers heat in, as Settings describe them right now."""
        return await StaggerGroupResolver.load(db, await StaggerSplit.from_settings(db))

    def _occupied(self, group: GroupKey, resolver: StaggerGroupResolver, *, excluding: int | None) -> int:
        return sum(
            1 for s in self._stagger_slots if s.printer_id != excluding and group in resolver.groups_for(s.printer_id)
        )

    def _full_groups(self, concurrent: int, printer_id: int | None, resolver: StaggerGroupResolver) -> list[GroupKey]:
        """The groups of ``printer_id`` (all groups when None) that have no free slot for it.

        Each group is judged by ITS OWN cap (``resolver.cap_for``): the global
        number, lowered by a per-tag or per-location override. This is the one
        comparison site — the queue gate, the strict refusal and the direct-print
        acquire all come through here, so they cannot disagree.
        """
        groups = resolver.groups_for(printer_id) if printer_id is not None else resolver.universe
        return [
            g for g in groups if self._occupied(g, resolver, excluding=printer_id) >= resolver.cap_for(g, concurrent)
        ]

    def _can_start_staggered(
        self,
        concurrent: int,
        printer_id: int | None = None,
        resolver: StaggerGroupResolver | None = None,
    ) -> bool:
        """A free slot in EVERY group this printer heats in.

        With the global resolver and no printer this is ``len(slots) < concurrent``,
        as it always was. A wildcard printer is in every group, so one full group
        anywhere holds it back — its phase is unknown. A printer never blocks
        itself: its own slot is excluded, because ``_register_stagger_start``
        replaces it anyway and the strict-mode check runs after the slot was
        taken (without this, cap 1 refused every direct print).
        """
        resolver = resolver or StaggerGroupResolver.global_only()
        return not self._full_groups(concurrent, printer_id, resolver)

    def _register_stagger_start(self, printer_id: int, interval_seconds: int) -> None:
        """Register a printer as occupying a stagger slot."""
        # Remove any old slot for same printer
        self._stagger_slots = [s for s in self._stagger_slots if s.printer_id != printer_id]
        self._stagger_slots.append(_StaggerSlot(printer_id, interval_seconds))
        logger.info(
            "Stagger: printer %d started (interval=%ds), %d slots occupied",
            printer_id,
            interval_seconds,
            len(self._stagger_slots),
        )

    def _pre_register_stagger_slot(self, printer: Printer, interval_seconds: int) -> None:
        """Take ``printer``'s stagger slot up front, on the queue dispatch path.

        ``interval_seconds`` is the farm-wide default; the printer's own
        override beats it when set.
        """
        per_printer_iv = (printer.stagger_interval_minutes * 60) if printer.stagger_interval_minutes else 0
        # The PRINTER's id — the resolver reads slot.printer_id as the identity whose tags and location decide the
        # group; PrinterQueue.id == printer_id is the invariant, but the printer object is in hand, so its id is
        # the honest key.
        self._register_stagger_start(printer.id, per_printer_iv or interval_seconds)

    async def acquire_stagger_slot(self, printer_id: int) -> None:
        """Block until ``printer_id`` holds a stagger slot.

        Idempotent: returns immediately when stagger is disabled, or when
        the printer already holds a slot (queue path pre-registers in
        ``_start_print``). Otherwise (direct print) this polls until a slot
        frees and then registers one. Called from
        ``background_dispatch._process_job`` so the gate applies to BOTH
        dispatch paths when stagger is enabled.
        """
        async with async_session() as db:
            stagger_enabled, stagger_concurrent, stagger_interval, stagger_wait_bed = await self._get_stagger_settings(
                db
            )
            if not stagger_enabled:
                return
            printer_obj = await self._get_printer(db, printer_id)
            per_printer_iv = (
                (printer_obj.stagger_interval_minutes * 60)
                if printer_obj and printer_obj.stagger_interval_minutes
                else 0
            )
            interval_seconds = per_printer_iv or stagger_interval
            resolver = await self._load_stagger_resolver(db)

        while True:
            async with self._stagger_acquire_lock:
                # Re-check inside the lock so a parallel acquirer can't slip
                # past us between the check and the register.
                await self._refresh_stagger_slots(stagger_wait_bed)
                if any(s.printer_id == printer_id for s in self._stagger_slots):
                    return  # already registered (queue pre-register, or prior loop)
                if self._can_start_staggered(stagger_concurrent, printer_id, resolver):
                    self._register_stagger_start(printer_id, interval_seconds)
                    return
            await asyncio.sleep(2.0)

    def _stagger_reason(
        self,
        wait_for_bed: bool,
        concurrent: int | None = None,
        printer_id: int | None = None,
        resolver: StaggerGroupResolver | None = None,
    ) -> str:
        """Waiting reason for a stagger-blocked item — naming the full group(s) when there are groups."""
        resolver = resolver or StaggerGroupResolver.global_only()
        full = (
            self._full_groups(concurrent, printer_id, resolver) if concurrent is not None else list(resolver.universe)
        )
        labels = sorted(label for label in (resolver.label(g) for g in full) if label)
        where = f" [{', '.join(labels)}]" if labels else ""
        if wait_for_bed:
            heating = []
            for s in self._stagger_slots:
                if s.temp_reached_at is None and any(g in resolver.groups_for(s.printer_id) for g in full):
                    info = printer_manager.get_printer(s.printer_id)
                    heating.append(info.name if info else f"#{s.printer_id}")
            if heating:
                return f"Staggered start{where}: waiting for {', '.join(heating)} to heat up"
        return f"Staggered start{where}: waiting for interval"

    async def stagger_blocks(self, printer_id: int) -> bool:
        """Whether a direct dispatch to ``printer_id`` would exceed the cap right now.

        For the strict-mode check in ``background_dispatch``: settings, resolver,
        cleanup and count in one call, so the two duplicated blocks there need
        no knowledge of groups.
        """
        async with async_session() as db:
            enabled, concurrent, _, wait_for_bed = await self._get_stagger_settings(db)
            if not enabled:
                return False
            resolver = await self._load_stagger_resolver(db)
        await self._refresh_stagger_slots(wait_for_bed)
        return not self._can_start_staggered(concurrent, printer_id, resolver)

    def _mark_printer_dispatched(
        self,
        printer_id: int,
        pre_state: str | None,
        pre_subtask_id: str | None,
    ) -> None:
        """Record that a print command was just sent to ``printer_id``.

        Held until either the watchdog observes a state/subtask transition
        (success path) or the hard timeout expires. See ``_dispatch_holds``.
        """
        if not pre_state:
            # No pre_state means we can't detect a transition — fall back to a
            # pure time-based hold using empty string as a sentinel that won't
            # match any real printer state.
            pre_state = ""
        self._dispatch_holds[printer_id] = (time.monotonic(), pre_state, pre_subtask_id)

    async def release_prepared_dispatch(self, printer_id: int) -> None:
        """The owner of a refused pre-publish attempt releases its reservations."""
        had_slot = any(slot.printer_id == printer_id for slot in self._stagger_slots)
        self._stagger_slots = [slot for slot in self._stagger_slots if slot.printer_id != printer_id]
        self._release_dispatch_hold(printer_id)
        if had_slot:
            try:
                await ws_manager.send_stagger_changed()
            except Exception:
                logger.exception("Could not broadcast released stagger slot for printer %d", printer_id)

    def _release_dispatch_hold(self, printer_id: int) -> None:
        """Drop the dispatch hold for ``printer_id`` (called by the watchdog)."""
        self._dispatch_holds.pop(printer_id, None)

    def _printer_in_dispatch_hold(self, printer_id: int) -> bool:
        """True if ``printer_id`` is still inside its post-dispatch hold window.

        Returns False (and clears the hold) once any of these are true:
          - hard timeout (``_dispatch_max_hold``) has elapsed
          - the printer has transitioned out of pre_state and we're past the
            minimum cooldown
          - the printer's subtask_id has advanced past pre_subtask_id and we're
            past the minimum cooldown
        Otherwise the printer is held — caller should treat it as busy.
        """
        entry = self._dispatch_holds.get(printer_id)
        if not entry:
            return False
        started_at, pre_state, pre_subtask_id = entry
        elapsed = time.monotonic() - started_at

        if elapsed >= self._dispatch_max_hold:
            self._dispatch_holds.pop(printer_id, None)
            return False

        # Without a pre_state we can't detect a transition — fall back to the
        # min cooldown alone, then drop the hold.
        if not pre_state:
            if elapsed >= self._dispatch_min_cooldown:
                self._dispatch_holds.pop(printer_id, None)
                return False
            return True

        status = printer_manager.get_status(printer_id)
        current_state = getattr(status, "state", None) if status else None
        current_subtask_id = getattr(status, "subtask_id", None) if status else None
        transitioned = (current_state is not None and current_state != pre_state) or (
            pre_subtask_id is not None and current_subtask_id is not None and current_subtask_id != pre_subtask_id
        )

        if transitioned and elapsed >= self._dispatch_min_cooldown:
            self._dispatch_holds.pop(printer_id, None)
            return False

        return True

    def has_dispatch_in_flight(self, printer_id: int) -> bool:
        """Read-only: has this printer been handed a job we haven't seen start?

        Same window as :meth:`_printer_in_dispatch_hold`, without its side
        effects — that one *clears* the hold as part of answering, which is
        correct for the dispatch loop that owns the hold and wrong for anyone
        else merely asking a question.
        """
        entry = self._dispatch_holds.get(printer_id)
        if not entry:
            return False
        return (time.monotonic() - entry[0]) < self._dispatch_max_hold

    def _is_printer_idle(self, printer_id: int, require_plate_clear: bool = True) -> bool:
        """Check if a printer is connected and idle."""
        if not printer_manager.is_connected(printer_id):
            logger.debug("Printer %d: not connected", printer_id)
            return False

        state = printer_manager.get_status(printer_id)
        if not state:
            logger.debug("Printer %d: no status available", printer_id)
            return False

        # A firmware file is being fetched for, or written to, this printer's
        # card: a job dispatched now would start under that write, or sit
        # behind it on the printer's file channel.
        from backend.app.services.firmware_update import upload_in_progress

        if upload_in_progress(printer_id):
            logger.debug("Printer %d: firmware upload in progress", printer_id)
            return False

        # IDLE = ready for next print
        # FINISH/FAILED = ready if plate-clear not required, or user confirmed plate is cleared
        # Printer is ready for dispatch when it's IDLE (never printed / user cleared)
        # OR at FINISH/FAILED with the plate-clear gate released. The gate is the
        # persisted ``awaiting_plate_clear`` flag inverted — absent means clear,
        # present means still waiting on user confirmation.
        idle = state.state in ("IDLE", "FINISH", "FAILED") and (
            not require_plate_clear or not printer_manager.is_awaiting_plate_clear(printer_id)
        )
        if not idle:
            logger.debug(
                "Printer %d: not idle - state=%s, awaiting_plate_clear=%s",
                printer_id,
                state.state,
                printer_manager.is_awaiting_plate_clear(printer_id),
            )
        return idle

    def _is_printer_ready_for_dispatch(self, printer_id: int, require_plate_clear: bool = True) -> bool:
        """Dispatch readiness, separate from the physical idle test used by drying."""
        from backend.app.services.print_run_binding import prepared_print_run_conflicts

        return self._is_printer_idle(printer_id, require_plate_clear) and not prepared_print_run_conflicts(
            printer_manager, printer_id
        )

    async def previous_print_succeeded(
        self, db: AsyncSession, printer_id: int, exclude_item_id: int | None = None
    ) -> bool:
        """Did the last finished print on this printer end well?

        Backs the ``require_previous_success`` gate on both queue tiers (m116).
        A thin door: the one reader is ``previous_print_gate`` — its lookback
        (``cancelled`` neutral, ``skipped`` out, acknowledged failures out), its
        order (``completed_at DESC NULLS LAST, id DESC``) and what releases it.
        Kept on the scheduler because the router and the rebalancer ask it here.
        """
        from backend.app.services.previous_print_gate import previous_print_succeeded

        return await previous_print_succeeded(db, printer_id, exclude_item_id=exclude_item_id)

    async def _get_setting_value(self, db: AsyncSession, key: str) -> str | None:
        """Read a raw setting value from the database."""
        result = await db.execute(select(Settings).where(Settings.key == key))
        setting = result.scalar_one_or_none()
        return setting.value if setting else None

    async def _get_bool_setting(self, db: AsyncSession, key: str, default: bool = False) -> bool:
        """Read a boolean setting from the database."""
        val = await self._get_setting_value(db, key)
        if val is not None:
            return val.lower() == "true"
        return default

    async def _get_int_setting(self, db: AsyncSession, key: str, default: int) -> int:
        """Read an integer setting from the database; anything unreadable is the default."""
        val = await self._get_setting_value(db, key)
        try:
            return int(val) if val is not None else default
        except (TypeError, ValueError):
            return default

    async def _get_drying_presets(self, db: AsyncSession) -> dict[str, dict[str, int]]:
        """Get drying presets (user-configured or built-in defaults)."""
        result = await db.execute(select(Settings).where(Settings.key == "drying_presets"))
        setting = result.scalar_one_or_none()
        if setting and setting.value:
            try:
                presets = json.loads(setting.value)
                if isinstance(presets, dict) and presets:
                    return presets
            except json.JSONDecodeError:
                pass
        return self.DEFAULT_DRYING_PRESETS

    def _get_conservative_drying_params(
        self, trays: list[dict], module_type: str, presets: dict[str, dict[str, int]]
    ) -> tuple[int, int, str] | None:
        """Get the most conservative drying params for mixed filament types in an AMS unit.

        Returns (temp, duration_hours, filament_type) or None if no drying-eligible filaments.
        """
        temp_key = module_type if module_type in ("n3f", "n3s") else "n3f"
        hours_key = f"{temp_key}_hours"

        min_temp = None
        max_hours = None
        filament_type = ""

        for tray in trays:
            tray_type = tray.get("tray_type", "")
            if not tray_type:
                continue
            # Normalize filament type for preset lookup (e.g., "PLA Basic" -> "PLA")
            base_type = resolve_material_key(tray_type, presets)
            preset = presets.get(base_type) if base_type else None
            if not preset:
                continue

            temp = preset.get(temp_key, 55)
            hours = preset.get(hours_key, 12)

            # Conservative: lowest temp, longest duration
            if min_temp is None or temp < min_temp:
                min_temp = temp
            if max_hours is None or hours > max_hours:
                max_hours = hours
            if not filament_type:
                filament_type = base_type

        if min_temp is None:
            return None
        return (min_temp, max_hours or 12, filament_type)

    async def _get_humidity_thresholds(self, db: AsyncSession) -> dict[str, int]:
        """Per-filament humidity thresholds (#1605).

        Returns the user-configured overrides map keyed by normalized filament
        type (uppercase base, e.g. ``PLA``, ``ASA``) plus a ``default`` key for
        unknown / unmapped types. Empty / unset → empty dict, in which case
        callers fall back to ``ams_humidity_fair``.
        """
        result = await db.execute(select(Settings).where(Settings.key == "ams_humidity_thresholds"))
        setting = result.scalar_one_or_none()
        if not setting or not setting.value:
            return {}
        try:
            data = json.loads(setting.value)
        except json.JSONDecodeError:
            return {}
        if not isinstance(data, dict):
            return {}
        out: dict[str, int] = {}
        for key, value in data.items():
            try:
                out[str(key).upper() if key != "default" else "default"] = int(value)
            except (TypeError, ValueError):
                continue
        return out

    @staticmethod
    def resolve_humidity_threshold(trays: list[dict], thresholds: dict[str, int], fallback: int) -> int:
        """Resolve the effective humidity threshold for an AMS unit (#1605).

        For mixed filament types loaded into one AMS, returns the most
        restrictive (lowest) threshold across all loaded tray types — matches
        the conservative-params strategy already used for drying temp/hours.
        Empty / unloaded trays contribute no constraint. Unknown types use the
        ``default`` key, falling through to ``fallback`` (= ``ams_humidity_fair``)
        when no per-type map is configured at all.
        """
        default = thresholds.get("default", fallback)
        if not thresholds:
            return fallback
        candidates: list[int] = []
        for tray in trays:
            tray_type = str(tray.get("tray_type") or "").strip()
            if not tray_type:
                continue
            key = resolve_material_key(tray_type, thresholds)
            candidates.append(thresholds[key] if key is not None and key != "default" else default)
        if not candidates:
            return default
        return min(candidates)

    async def _check_auto_drying(
        self, db: AsyncSession, queue_items: list[PrintQueueItem], dispatching_printers: set[int]
    ):
        """Start drying on idle printers based on humidity.

        Three modes (can all be enabled independently):
        - queue_drying_enabled: Dry between scheduled queue prints
        - ambient_drying_enabled: Dry any idle printer when humidity is high, regardless of queue
        - print_drying_enabled: Also evaluate printers that are currently printing,
          when model+firmware supports "Print While Drying" (gated by
          supports_drying_while_printing). Drying temperature is capped at
          max(40, preset_temp - 5) to protect spools mid-print.
        """
        from backend.app.services.farm_startup_guard import farm_startup_guard

        if not farm_startup_guard.allowed():
            return
        queue_drying_enabled = await self._get_bool_setting(db, "queue_drying_enabled")
        ambient_drying_enabled = await self._get_bool_setting(db, "ambient_drying_enabled")
        print_drying_enabled = await self._get_bool_setting(db, "print_drying_enabled")
        if not queue_drying_enabled and not ambient_drying_enabled:
            # Nothing is watched while auto-drying is off, so no streak survives it.
            self._auto_dry_above.clear()
            # Stop active drying on all printers if both features disabled
            if self._drying_in_progress:
                # A scheduled cycle's hold is the schedule's: nothing of ours to stop there.
                for pid in [p for p in self._drying_in_progress if p not in self._scheduled_drying_printers]:
                    logger.info("Auto-drying: printer %d - stopping, auto-drying disabled", pid)
                    await self._stop_drying(pid)
            return
        # How long a unit must read above its threshold before a start nobody
        # scheduled is armed; 0 switches the wait off. Every streak goes the
        # moment the wait is off, so switching it back on inherits none.
        sustained_minutes = await self._get_int_setting(
            db, "auto_drying_sustained_minutes", AUTO_DRY_SUSTAINED_DEFAULT_MINUTES
        )
        sustained_minutes = min(240, max(0, sustained_minutes))
        if sustained_minutes == 0:
            self._auto_dry_above.clear()

        # Update drying state from printer status (handles backend restart)
        self._sync_drying_state()
        # One chamber reading per tick, so preheat can credit a soak that has
        # already happened instead of repeating it. Recording only — nothing
        # here heats or cools. See services/chamber_history.py.
        try:
            chamber_history.sample_all(printer_manager.get_all_statuses())
        except Exception as exc:  # noqa: BLE001 - an observation must never wedge the queue
            logger.warning("Queue: chamber sampling failed: %s", exc)

        # Find printers with scheduled items (for queue drying mode).
        #
        # Two different questions about a printer's pending rows:
        #   scheduled - a job with a time is coming within the horizon, so queue
        #               drying has something to dry FOR;
        #   ready     - a row could start NOW, so under block mode a new cycle
        #               would only stand in its way.
        # ⚠️ "Any pending row" used to answer the second. Every scheduled row is
        # a pending row, so queue drying with block mode on never started a
        # cycle at all (upstream e05a91744).
        now_utc = datetime.now(timezone.utc)
        horizon = now_utc + timedelta(hours=_QUEUE_PREDRY_HORIZON_HOURS)
        printers_with_scheduled: set[int] = set()
        printers_with_ready_items: set[int] = set()
        for item in queue_items:
            if not item.queue_id:
                continue
            sched = item.scheduled_time
            if sched is not None and sched.tzinfo is None:
                sched = sched.replace(tzinfo=timezone.utc)
            if sched is not None and not item.manual_start and sched <= horizon:
                printers_with_scheduled.add(item.queue_id)
            if self._row_could_start_now(item, now_utc):
                printers_with_ready_items.add(item.queue_id)
        # A scheduled «any printer of model X» job stands in no printer's queue
        # until its time, so it counts for every printer it could run on. Only
        # queue drying asks: nothing else dries FOR a job.
        if queue_drying_enabled:
            try:
                printers_with_scheduled |= await self._scheduled_auto_queue_printers(db)
            except Exception as exc:  # noqa: BLE001 - a lookup must never wedge the queue
                logger.warning("Auto-drying: could not read the scheduled AutoQueue jobs: %s", exc)

        # If only queue mode is on and no printers have scheduled items, stop drying.
        # (But skip this short-circuit when print_drying_enabled is on — busy printers
        # may still be eligible for mid-print drying regardless of queue state.)
        if not ambient_drying_enabled and not printers_with_scheduled and not print_drying_enabled:
            for pid in [p for p in self._drying_in_progress if p not in self._scheduled_drying_printers]:
                logger.info("Auto-drying: printer %d - stopping, no scheduled prints in queue", pid)
                await self._stop_drying(pid)
            return

        # Get humidity threshold (global fallback)
        result = await db.execute(select(Settings).where(Settings.key == "ams_humidity_fair"))
        setting = result.scalar_one_or_none()
        global_humidity_threshold = int(setting.value) if setting else 60

        # Per-filament humidity threshold overrides (#1605). Empty → fall back
        # to the global threshold for every AMS unit.
        per_type_thresholds = await self._get_humidity_thresholds(db)

        # Get drying presets
        presets = await self._get_drying_presets(db)

        # Determine if drying should be skipped for printers with pending items
        block_for_drying = await self._get_bool_setting(db, "queue_drying_block")

        # Get all active printers
        all_printers = await db.execute(
            select(Printer).where(Printer.is_active.is_(True)).where(Printer.archived.is_(False))
        )
        for printer in all_printers.scalars():
            pid = printer.id

            # Resolve model+firmware up front — needed to decide whether this printer
            # qualifies for mid-print drying (busy printer on capable hardware).
            state = printer_manager.get_status(pid)
            if not state:
                logger.debug("Auto-drying: printer %d skipped - no state", pid)
                continue
            model = printer_manager.get_model(pid)
            firmware = state.firmware_version

            # ⚠️ "Mid-print" has to mean the printer is ACTUALLY printing. It
            # used to be inferred from the dispatch set, which also holds
            # printers the queue merely could not dispatch to — so a printer
            # sitting in FINISH behind an unacknowledged plate was treated as
            # printing, had its drying temperature capped by the mid-print spool
            # protection, and was logged as (mid-print) while standing idle.
            is_printing = (state.state or "").upper() in ("RUNNING", "PREPARE", "PAUSE")
            mid_print = is_printing and print_drying_enabled and supports_drying_while_printing(model, firmware)

            # A printer whose print is running or imminent is left alone unless
            # it can dry through it. ``dispatching_printers`` is deliberately the
            # narrow set: printing, or held post-dispatch.
            if (is_printing or pid in dispatching_printers) and not mid_print:
                logger.debug("Auto-drying: printer %d skipped - printing or about to", pid)
                continue

            if not mid_print:
                # In queue-only mode, only dry printers that have scheduled prints
                if not ambient_drying_enabled and pid not in printers_with_scheduled:
                    if self._drying_in_progress.get(pid) and pid not in self._scheduled_drying_printers:
                        logger.info("Auto-drying: printer %d - stopping, no scheduled prints for this printer", pid)
                        await self._stop_drying(pid)
                    logger.debug("Auto-drying: printer %d skipped - no scheduled prints", pid)
                    continue
                # When block mode is on, don't START new drying on a printer with a
                # row that could start now: the cycle would hold that row. A row
                # that cannot start yet (scheduled for later, manual start, a
                # paused queue) leaves the printer free to dry ahead of it.
                # But allow already-drying printers through so humidity auto-stop logic still runs.
                if block_for_drying and pid in printers_with_ready_items and not self._drying_in_progress.get(pid):
                    logger.debug("Auto-drying: printer %d skipped - a pending job could start now (block mode)", pid)
                    continue
            if not printer_manager.is_connected(pid):
                logger.debug("Auto-drying: printer %d skipped - not connected", pid)
                continue
            # ⚠️ Plate-clear is deliberately ignored here. It answers "is the
            # bed ready for the next job", which says nothing about whether the
            # AMS may heat — and the gap between a finished print and the
            # acknowledgment is exactly when drying is most useful, because the
            # printer is free and nobody is waiting on it. Leaving a plate
            # unacknowledged is also how people hold the queue by hand, and that
            # hold should not cost them their drying.
            if not mid_print and not self._is_printer_idle(pid, require_plate_clear=False):
                logger.debug("Auto-drying: printer %d skipped - not idle", pid)
                continue

            # Check drying capability. For the mid-print path, supports_drying_while_printing
            # was already verified when computing mid_print above.
            if not mid_print and not supports_drying(model, firmware):
                logger.debug("Auto-drying: printer %d skipped - model %s does not support drying", pid, model)
                continue

            # Check each AMS unit from raw_data
            ams_list = state.raw_data.get("ams", [])
            logger.debug("Auto-drying: printer %d - checking %d AMS units", pid, len(ams_list))
            for ams_data in ams_list:
                module_type = str(ams_data.get("module_type") or "")
                ams_id = int(ams_data.get("id", 0))
                # Only n3f/n3s support drying
                if module_type not in ("n3f", "n3s"):
                    logger.debug("Auto-drying: printer %d AMS %d skipped - module_type=%s", pid, ams_id, module_type)
                    continue
                if (pid, ams_id) in self._scheduled_dry_units:
                    logger.debug("Auto-drying: printer %d AMS %d skipped - reserved by a schedule", pid, ams_id)
                    continue

                dry_time = int(ams_data.get("dry_time") or 0)

                # Read humidity as a percentage. The 1-5 index is never
                # substituted: it runs the other way, and being unable to exceed
                # any threshold it would read as "dry" forever (upstream #3140).
                # ``None`` already means "skip this unit" everywhere below.
                humidity_pct = ams_humidity_percent(ams_data)
                humidity = int(round(humidity_pct)) if humidity_pct is not None else None

                # Resolve per-filament humidity threshold for this AMS unit (#1605).
                # Most-restrictive of all loaded tray types; falls back to the
                # global threshold when no overrides are configured.
                trays = ams_data.get("tray", []) or []
                humidity_threshold = self.resolve_humidity_threshold(
                    trays, per_type_thresholds, global_humidity_threshold
                )

                # Already drying — do NOT stop it here on a humidity re-check. The AMS
                # humidity reading is noisy while the heater runs (moisture shedding can
                # briefly read "dry"), so an early-stop would truncate legitimate manual
                # or preset dries (#1892). A running dry runs to its firmware-set
                # duration; auto-drying is only ever stopped when a print is dispatched to
                # the printer (see _stop_drying). We still record first-seen time so a
                # restart-orphaned dry has a start timestamp.
                unit_key = (pid, ams_id)
                unit_state = self._dry_unit_history.get(unit_key)

                if dry_time > 0 and is_countdown_parked(ams_data):
                    # The printer holds the timer and is not heating: no cycle to
                    # adopt a hold from or to let run. One WE armed is stopped and
                    # handed to the ordinary re-arm cooldown - left there, the unit
                    # would be dead to auto-drying until somebody pressed Stop.
                    # ⚠️ Never anybody else's: a manual timer stays as it is.
                    if unit_key in self._armed_dry_cycles and printer_manager.send_drying_command(
                        pid, ams_id, 0, 0, mode=0
                    ):
                        logger.info(
                            "Auto-drying: printer %d AMS %d - the printer holds our drying timer (%dm) but is "
                            "not drying; stopped, the re-arm cooldown decides the retry",
                            pid,
                            ams_id,
                            dry_time,
                        )
                        self._armed_dry_cycles.pop(unit_key, None)
                        self.forget_auto_dry_cycle(pid, ams_id)
                        # ⚠️ Unlike a stop a person or a dispatch sent, this one IS a
                        # strike: the printer would not run the cycle, and nothing
                        # else ends a start + stop pair every cooldown for ever. It
                        # says nothing about humidity, so it is counted apart from the
                        # cycles that dried nothing (upstream #3478); two in a row
                        # suspend the unit with the not-heating message, and a dry
                        # reading lifts it as it lifts any suspension.
                        if unit_state is not None:
                            never_heated = _never_heated(unit_state) + 1
                            unit_state["never_heated"] = never_heated
                            logger.warning(
                                "Auto-drying: printer %d AMS %d - drying cycle not running: the timer stands still "
                                "at %dm with the status Off, %d in a row",
                                pid,
                                ams_id,
                                dry_time,
                                never_heated,
                            )
                    continue

                if dry_time > 0:
                    # A cycle is running, whoever started it: the streak is spent.
                    self._auto_dry_above.pop(unit_key, None)
                    if pid not in self._drying_in_progress:
                        self._drying_in_progress[pid] = time.monotonic()
                    if unit_state is not None:
                        unit_state["running"] = True
                    logger.debug(
                        "Auto-drying: printer %d AMS %d - drying in progress (%dm left, humidity %s%%) - letting it run",
                        pid,
                        ams_id,
                        dry_time,
                        humidity,
                    )
                    continue

                # Nothing is drying here. Close out a cycle we armed
                # ourselves and judge whether it achieved anything.
                #
                # ⚠️ "Achieved anything" is measured against the LOWEST reading
                # any cycle on this unit has ended at — not against the
                # threshold, and not against the previous cycle. A genuinely wet
                # spool in a humid room comes down slowly (40, 37, 35) and must
                # be allowed to keep going for as long as it is still coming
                # down, however far the threshold still is. What must not
                # continue is a cycle ending exactly where the last one did,
                # which is the reported signature: 15, 15, 16, 15, for ever.
                # Comparing against the running minimum rather than the previous
                # end is what stops a sensor oscillating by a point between two
                # values from reading as progress every other cycle.
                if unit_state is not None and unit_state.pop("running", False):
                    unit_state["ended_at"] = time.monotonic()
                    # The next streak starts fresh and runs THROUGH the re-arm
                    # cooldown below: the two waits overlap, they never add up.
                    self._auto_dry_above.pop(unit_key, None)
                    # A cycle that ran to its end heated: the parked streak is over.
                    unit_state.pop("never_heated", None)
                    if humidity is not None and humidity > humidity_threshold:
                        best = unit_state.get("best_end_humidity")
                        if isinstance(best, int) and humidity < best:
                            unproductive = 0
                        else:
                            unproductive = int(unit_state.get("unproductive", 0)) + 1
                        if not isinstance(best, int) or humidity < best:
                            unit_state["best_end_humidity"] = humidity
                        unit_state["unproductive"] = unproductive
                        logger.info(
                            "Auto-drying: printer %d AMS %d - cycle ended with humidity still %d%% > "
                            "threshold %d%% (best so far %s%%, %d unproductive in a row)",
                            pid,
                            ams_id,
                            humidity,
                            humidity_threshold,
                            unit_state.get("best_end_humidity"),
                            unproductive,
                        )

                # Humidity below threshold - no need to start drying. This is
                # also the only thing that lifts a suspension: the reading we
                # gave up on has come down, so auto-drying works again here.
                #
                # ⚠️ Clear the JUDGEMENT, keep the CLOCK. Dropping the whole
                # entry would drop ``ended_at`` with it, and with that the
                # cooldown — so a unit a point or two above the threshold, whose
                # reading dips below it as the AMS cools and comes back once
                # warm, would wipe its own history and re-arm immediately. That
                # oscillation is the very thing the cooldown exists to ride out,
                # and it is worst at exactly the margin that makes a unit dry
                # repeatedly. (Upstream shipped the pop and repaired it in a
                # later fix; we start from the repaired behaviour.)
                if humidity is None or humidity <= humidity_threshold:
                    if unit_state is not None:
                        if unit_state.get("suspended"):
                            logger.info(
                                "Auto-drying: printer %d AMS %d - humidity %s%% is back at or below the "
                                "%d%% threshold, resuming automatic drying",
                                pid,
                                ams_id,
                                humidity,
                                humidity_threshold,
                            )
                        unit_state.pop("suspended", None)
                        unit_state.pop("unproductive", None)
                        unit_state.pop("best_end_humidity", None)
                        unit_state.pop("never_heated", None)
                    # One real reading at or below the threshold ends the
                    # streak. A missing reading says nothing and leaves it; a
                    # long silence is the observation gap's to judge.
                    if humidity is not None:
                        above = self._auto_dry_above.pop(unit_key, None)
                        if above is not None:
                            logger.info(
                                "Auto-drying: printer %d AMS %d - humidity fell back to %d%% after %.0fs of the "
                                "required %dm above the %d%% threshold; not drying",
                                pid,
                                ams_id,
                                humidity,
                                time.monotonic() - above["since"],
                                sustained_minutes,
                                humidity_threshold,
                            )
                    logger.debug(
                        "Auto-drying: printer %d AMS %d skipped - humidity %s <= threshold %d",
                        pid,
                        ams_id,
                        humidity,
                        humidity_threshold,
                    )
                    continue

                # The streak is kept on every pass that reads the unit wet, and
                # BEFORE the suspension and cooldown gates: a unit cooling off
                # after a cycle is already serving its wait. A unit nobody
                # observed for longer than the ceiling (printing without
                # dry-through, offline, reserved by a schedule) starts over -
                # the minutes in between prove nothing.
                if sustained_minutes > 0:
                    now_mono = time.monotonic()
                    gap_ceiling = max(4 * self._check_interval, _AUTO_DRY_SUSTAINED_GAP_FLOOR_SECONDS)
                    above = self._auto_dry_above.get(unit_key)
                    if above is None:
                        self._auto_dry_above[unit_key] = {"since": now_mono, "last": now_mono}
                    elif now_mono - above["last"] > gap_ceiling:
                        logger.info(
                            "Auto-drying: printer %d AMS %d - nobody observed the unit for %.0fs (ceiling %ds); "
                            "the %dm sustained-humidity wait starts over",
                            pid,
                            ams_id,
                            now_mono - above["last"],
                            gap_ceiling,
                            sustained_minutes,
                        )
                        self._auto_dry_above[unit_key] = {"since": now_mono, "last": now_mono}
                    else:
                        above["last"] = now_mono

                if unit_state is not None:
                    if unit_state.get("suspended"):
                        # Either cause may have suspended it, so the line names none.
                        logger.debug(
                            "Auto-drying: printer %d AMS %d skipped - suspended until humidity is back at or "
                            "below the %d%% threshold",
                            pid,
                            ams_id,
                            humidity_threshold,
                        )
                        continue

                    never_heated = _never_heated(unit_state)
                    if never_heated >= _AUTO_DRY_MAX_NEVER_HEATED_CYCLES:
                        unit_state["suspended"] = True
                        logger.warning(
                            "Auto-drying: printer %d AMS %d - suspending automatic drying. The printer accepted "
                            "%d drying commands in a row, but each time the timer stood still and the unit did "
                            "not heat. Clear the timer with Stop Drying on the printer card and check the AMS power; "
                            "automatic drying resumes once humidity is back at or below the %d%% threshold.",
                            pid,
                            ams_id,
                            never_heated,
                            humidity_threshold,
                        )
                        await self._notify_auto_drying_suspended(
                            db, pid, ams_id, humidity, humidity_threshold, never_heated, never_heated=True
                        )
                        continue

                    if int(unit_state.get("unproductive", 0)) >= _AUTO_DRY_MAX_UNPRODUCTIVE_CYCLES:
                        unit_state["suspended"] = True
                        logger.warning(
                            "Auto-drying: printer %d AMS %d - suspending automatic drying. %d cycles in a "
                            "row ended with humidity at %d%%, still above the %d%% threshold. The AMS reads "
                            "higher while it is warm, so a threshold in that range can never be reached and "
                            "re-arming would loop. Raise the threshold or dry the spools off the printer.",
                            pid,
                            ams_id,
                            int(unit_state.get("unproductive", 0)),
                            humidity,
                            humidity_threshold,
                        )
                        await self._notify_auto_drying_suspended(
                            db, pid, ams_id, humidity, humidity_threshold, int(unit_state.get("unproductive", 0))
                        )
                        continue

                    ended_at = unit_state.get("ended_at")
                    if isinstance(ended_at, float) and time.monotonic() - ended_at < _AUTO_DRY_REARM_COOLDOWN_SECONDS:
                        logger.debug(
                            "Auto-drying: printer %d AMS %d skipped - cooling off for %ds after the last "
                            "cycle before the humidity reading is worth acting on",
                            pid,
                            ams_id,
                            _AUTO_DRY_REARM_COOLDOWN_SECONDS,
                        )
                        continue

                # Check cannot-dry reasons (power constraints etc.) — surface
                # the first human-readable blocker so support logs actually tell
                # the operator why auto-drying skipped, not just a bare code list.
                blocker = first_drying_blocking_reason(ams_data)
                if blocker is not None:
                    logger.debug(
                        "Auto-drying: printer %d AMS %d skipped — blocker code %d: %s",
                        pid,
                        ams_id,
                        blocker[0],
                        blocker[1],
                    )
                    continue

                # The sustained-humidity wait: a start nobody scheduled waits
                # until the unit has read wet for the configured minutes - one
                # reading is what opening the lid looks like. It covers the
                # mid-print start too. Only a printer queue drying is preparing
                # for a scheduled job starts at once: that drying has a deadline.
                if sustained_minutes > 0 and not (queue_drying_enabled and pid in printers_with_scheduled):
                    above = self._auto_dry_above.get(unit_key)
                    waited = time.monotonic() - above["since"] if above else 0.0
                    if waited < sustained_minutes * 60:
                        logger.debug(
                            "Auto-drying: printer %d AMS %d waiting - humidity %d%% above the %d%% threshold "
                            "for %.0fs of the required %dm",
                            pid,
                            ams_id,
                            humidity,
                            humidity_threshold,
                            waited,
                            sustained_minutes,
                        )
                        continue

                # Get conservative drying params for mixed filaments
                trays = ams_data.get("tray", [])
                params = self._get_conservative_drying_params(trays, module_type, presets)
                if not params:
                    logger.debug(
                        "Auto-drying: printer %d AMS %d skipped - no drying-eligible filaments in trays", pid, ams_id
                    )
                    continue

                temp, duration_hours, filament_type = params

                # Mid-print drying: cap drying temperature to protect spools (Bambu warns
                # "drying temperature must not exceed the filament's softening temperature"
                # for Print While Drying). Floor at 40 degC - below that the dryer is
                # ineffective and firmware will reject anyway.
                if mid_print:
                    temp = max(40, temp - 5)

                # Start drying
                logger.info(
                    "Auto-drying: printer %d AMS %d - humidity %d%% > threshold %d%%, "
                    "starting %s drying at %d°C for %dh%s",
                    pid,
                    ams_id,
                    humidity,
                    humidity_threshold,
                    filament_type,
                    temp,
                    duration_hours,
                    " (mid-print)" if mid_print else "",
                )
                success = printer_manager.send_drying_command(
                    pid, ams_id, temp, duration_hours, mode=1, filament=filament_type
                )
                if success:
                    self._auto_dry_above.pop(unit_key, None)
                    self._drying_in_progress[pid] = time.monotonic()
                    self._armed_dry_cycles[(pid, ams_id)] = time.monotonic()
                    armed = self._dry_unit_history.setdefault(
                        unit_key, {"unproductive": 0, "suspended": False, "ended_at": None}
                    )
                    armed["running"] = True

    @staticmethod
    def _row_could_start_now(item: PrintQueueItem, now: datetime) -> bool:
        """Whether nothing but the printer stands between this pending row and its start.

        The standing holds ``check_queue`` skips a row for without looking at
        the printer: a time that has not come, a manual start, a queue paused
        by the operator or by an error. None of them ends by itself within a
        tick, so a drying cycle started meanwhile takes nothing from the row.
        """
        if not row_holds_printer(item, now):
            return False
        queue = getattr(item, "queue", None)
        return not (queue is not None and (queue.is_paused or queue.status in ("paused", "error")))

    async def _scheduled_auto_queue_printers(self, db: AsyncSession) -> set[int]:
        """Printers a scheduled AutoQueue job within the pre-drying horizon could run on.

        Asked of the ONE source of "which printers can this job run on"
        (``auto_queue_eligibility.printers_for_item``: model, location subtree,
        auto-distribute, order restrictions) and never by routing the job
        early - *Routing is NOT dispatching* cuts both ways. A row without a
        target model names no candidates until its file is read, and is left out.
        """
        from backend.app.models.auto_queue import AutoQueueItem
        from backend.app.services.auto_queue_eligibility import printers_for_item

        horizon = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=_QUEUE_PREDRY_HORIZON_HOURS)
        rows = await db.execute(
            select(AutoQueueItem).where(
                AutoQueueItem.status == "pending",
                AutoQueueItem.cancelled_at.is_(None),
                AutoQueueItem.manual_start.is_(False),
                AutoQueueItem.target_model.is_not(None),
                AutoQueueItem.scheduled_time.is_not(None),
                AutoQueueItem.scheduled_time <= horizon,
            )
        )
        printer_ids: set[int] = set()
        candidates: dict[tuple, set[int]] = {}
        for item in rows.scalars().all():
            key = (item.target_model, item.target_location_id, item.project_id, item.project_line_id)
            if key not in candidates:
                printers, _model, _suffix = await printers_for_item(db, item)
                candidates[key] = {printer.id for printer in printers}
            printer_ids |= candidates[key]
        return printer_ids

    def _drying_holds_queue(self, printer_id: int) -> bool:
        """Whether a cycle this scheduler tracks is one the printer's queue can wait on.

        The hold entry alone is not enough: a printer whose only timers are
        parked (``utils/ams_drying.only_parked_timers``) is not drying and the
        wait would never end. The entry itself stays until the timer is gone,
        so the stop paths still reach a unit we armed.
        """
        if not self._drying_in_progress.get(printer_id):
            return False
        state = printer_manager.get_status(printer_id)
        return not only_parked_timers((getattr(state, "raw_data", None) or {}).get("ams") if state else None)

    async def drying_held_printer_ids(self, db: AsyncSession) -> set[int]:
        """Printers whose queue a drying cycle holds right now.

        The same two facts ``check_queue`` holds a row on - a cycle this
        scheduler tracks (auto or scheduled drying) and ``queue_drying_block``.
        With block mode off a print simply takes the printer, so nothing is held.
        For RANKING only (the AutoQueue router prefers an equally fit printer
        that is not held): a held printer still gets work when it is the only
        one - the wait is then visible on its row.
        """
        held = {pid for pid in self._drying_in_progress if self._drying_holds_queue(pid)}
        if not held or not await self._get_bool_setting(db, "queue_drying_block"):
            return set()
        return held

    async def _notify_auto_drying_suspended(
        self,
        db: AsyncSession,
        printer_id: int,
        ams_id: int,
        humidity: int,
        threshold: int,
        cycles: int,
        *,
        never_heated: bool = False,
    ) -> None:
        """Tell the user auto-drying has given up on one AMS unit.

        ``never_heated`` picks the message for cycles the printer accepted but
        never ran (upstream #3478) instead of cycles that left humidity high.

        Fires once per suspension — the caller sets ``suspended`` before calling
        and every later pass short-circuits on it — because the whole point is
        that BamDude has STOPPED acting. Somebody whose printer sits in another
        building needs that to reach them, and the humidity alarm they are
        already getting says the opposite of what happened here.

        ⚠️ Never raises: a notification provider being down must not stop the
        suspension itself from taking effect.
        """
        result = await db.execute(select(Printer.name).where(Printer.id == printer_id))
        printer_name = result.scalar_one_or_none() or f"Printer {printer_id}"
        ams_label = f"HT-{chr(65 + (ams_id - 128))}" if ams_id >= 128 else f"AMS-{chr(65 + ams_id)}"
        try:
            await notification_service.on_ams_drying_suspended(
                printer_id,
                printer_name,
                ams_label,
                float(humidity),
                float(threshold),
                cycles,
                db,
                never_heated=never_heated,
            )
        except Exception as e:  # noqa: BLE001 - see the docstring
            logger.warning("Failed to send auto-drying suspended notification: %s", e)

    def forget_auto_dry_cycle(self, printer_id: int, ams_id: int) -> None:
        """Stop judging the drying cycle currently on this AMS unit.

        ⚠️ The unproductive-cycle counter exists to notice that *drying* is not
        moving the humidity reading. A cycle that ended because somebody sent a
        stop says nothing about that — it was cut short before it had a chance —
        so counting it would suspend auto-drying for a reason that has nothing to
        do with the loop the counter is there to break. Left uncalled, an install
        that dries between queue jobs would suspend its own auto-drying after two
        prints interrupted a dry: exactly the install queue-drying exists for.

        The rest of the unit's history is kept — the cooldown before re-arming
        still applies, and an earlier count still stands.
        """
        state = self._dry_unit_history.get((printer_id, ams_id))
        if state is not None:
            state.pop("running", None)
            state["ended_at"] = time.monotonic()
        # A stopped cycle spends the streak that armed it, as a finished one does.
        self._auto_dry_above.pop((printer_id, ams_id), None)

    def release_auto_dry_claim(self, printer_id: int, ams_id: int) -> None:
        """A person started a cycle on this unit: whatever runs there now is theirs.

        The claim is keyed by unit and lives while the unit shows a timer, so a
        start sent over a timer auto-drying armed (a parked one — the card
        offers a start there) would inherit it, and the next dispatch would stop
        the operator's cycle as ours.
        """
        self._armed_dry_cycles.pop((printer_id, ams_id), None)

    def drying_remaining_seconds(self, printer_id: int) -> int:
        """Seconds left in the drying cycle THIS scheduler runs on ``printer_id`` — 0 when it runs none.

        The forecast's read of the queue gate (``_drying_in_progress`` and
        ``queue_drying_block`` in ``check_queue``): the same claim, timed by the
        AMS countdown the printer reports (``dry_time``, minutes; the longest
        unit wins). A cycle somebody started by hand is not ours and holds
        nothing here, exactly as it holds nothing at the gate; a parked timer
        is no cycle and counts for nothing. Vault
        60-specs/farm-forecast-v2-spec §6.
        """
        if not self._drying_in_progress.get(printer_id):
            return 0
        state = printer_manager.get_status(printer_id)
        if not state:
            return 0
        minutes = max((running_dry_minutes(unit) for unit in state.raw_data.get("ams", [])), default=0)
        return minutes * 60

    def _sync_drying_state(self):
        """Drop what is no longer true: printers that have stopped drying, and
        claims on cycles that have ended.

        ⚠️ One direction only — it prunes, it never adds. The docstring used to
        claim it handled the backend-restart case by adopting a cycle it found
        running; it does not, and must not. Adoption of ``_drying_in_progress``
        happens in the arm loop, where a running cycle is observed in context;
        ``_armed_dry_cycles`` is never adopted at all, because after a restart we
        cannot prove a running cycle is ours and would rather leave a manual dry
        alone than stop it.
        """
        to_remove = []
        for pid in self._drying_in_progress:
            state = printer_manager.get_status(pid)
            if not state:
                to_remove.append(pid)
                continue
            # Check if any AMS unit is still drying
            ams_list = state.raw_data.get("ams", [])
            any_drying = any(int(a.get("dry_time") or 0) > 0 for a in ams_list)
            if not any_drying:
                to_remove.append(pid)
        for pid in to_remove:
            self._release_drying_hold(pid)

        # ⚠️ Swept over ITS OWN keys, not over ``_drying_in_progress``:
        # ``_stop_drying`` pops the printer from that dict, so a claim swept
        # only there would outlive the cycle it describes and later authorise
        # stopping a manual dry on the same unit.
        now = time.monotonic()
        for (pid, ams_id), armed_at in list(self._armed_dry_cycles.items()):
            # ⚠️ A fresh claim is left alone. ``dry_time`` does not appear in
            # the printer's report the instant the command lands, and unlike
            # ``_drying_in_progress`` a claim is never re-adopted — dropping one
            # in that window would lose it for the whole cycle.
            if now - armed_at < _DRYING_CLAIM_GRACE_SECONDS:
                continue
            state = printer_manager.get_status(pid)
            if state is None:
                # A printer we can no longer see says nothing about what its AMS
                # units are doing, so its claims go too.
                self._armed_dry_cycles.pop((pid, ams_id), None)
                continue
            still_drying = any(
                int(ams.get("id", 0)) == ams_id and int(ams.get("dry_time") or 0) > 0
                for ams in state.raw_data.get("ams", [])
            )
            if not still_drying:
                self._armed_dry_cycles.pop((pid, ams_id), None)

        # A printer that has gone away entirely takes its per-AMS drying history
        # with it, so a printer deleted and re-added does not inherit a
        # suspension it never earned.
        for key in [k for k in self._dry_unit_history if printer_manager.get_status(k[0]) is None]:
            self._dry_unit_history.pop(key, None)
        for key in [k for k in self._auto_dry_above if printer_manager.get_status(k[0]) is None]:
            self._auto_dry_above.pop(key, None)

    async def _stop_drying(self, printer_id: int):
        """Stop the drying cycles WE armed on a printer.

        ⚠️ Scoped to ``_armed_dry_cycles``. It used to send a stop to every AMS
        reporting ``dry_time > 0``, so one auto-dried unit was enough to kill a
        cycle the operator had started by hand on a different unit of the same
        printer. That also contradicted the contract ``_sync_drying_state``
        documents: the entry gate only knows about cycles we began, so the
        action must not reach past them either.

        Print takes priority AT DISPATCH: this fires when a print is dispatched onto
        a drying printer. When ``print_drying_enabled`` is on and the printer's
        model+firmware satisfy ``supports_drying_while_printing``, the scheduler
        re-establishes drying mid-print at the capped temperature on the next tick —
        drying yields the dispatch moment then resumes alongside the print, it is not
        abandoned. Otherwise drying stays stopped for the duration of the print.
        """
        state = printer_manager.get_status(printer_id)
        if not state:
            self._release_drying_hold(printer_id)
            return

        ams_list = state.raw_data.get("ams", [])
        for ams_data in ams_list:
            dry_time = int(ams_data.get("dry_time") or 0)
            if dry_time > 0:
                ams_id = int(ams_data.get("id", 0))
                if (printer_id, ams_id) not in self._armed_dry_cycles:
                    logger.debug(
                        "Auto-drying: leaving printer %d AMS %d alone - not a cycle we started",
                        printer_id,
                        ams_id,
                    )
                    continue
                logger.info(
                    "Auto-drying: stopping drying on printer %d AMS %d - print takes priority",
                    printer_id,
                    ams_id,
                )
                printer_manager.send_drying_command(printer_id, ams_id, 0, 0, mode=0)
                self.forget_auto_dry_cycle(printer_id, ams_id)
        self._release_drying_hold(printer_id)

    def _release_drying_hold(self, printer_id: int) -> None:
        """Drop a printer's drying hold — unless a scheduled cycle there is still running."""
        if printer_id in self._scheduled_drying_printers:
            return
        self._drying_in_progress.pop(printer_id, None)

    async def _seed_scheduled_drying_holds(self) -> None:
        """After a restart, hold a printer whose scheduled cycle is running BEFORE the
        first dispatch of the first pass (the tick itself runs after dispatch)."""
        try:
            async with async_session() as db:
                running = await scheduled_drying.running_printer_ids(db)
        except Exception as exc:  # noqa: BLE001 - never block the queue
            logger.warning("Scheduled drying: could not restore holds: %s", exc)
            return
        self._scheduled_drying_printers = set(running)
        for pid in running:
            self._drying_in_progress.setdefault(pid, time.monotonic())
        self._scheduled_holds_seeded = True

    async def _tick_scheduled_drying(self, dispatching_printers: set[int]) -> None:
        """Scheduled drying pass — in its own session; a failure here never stops the queue."""
        try:
            async with async_session() as db:
                result = await scheduled_drying.tick(
                    db,
                    now=datetime.now(timezone.utc).replace(tzinfo=None),
                    drying_in_progress=self._drying_in_progress,
                    dispatching_printers=dispatching_printers,
                )
        except Exception as exc:  # noqa: BLE001 - see the docstring
            logger.warning("Scheduled drying tick failed: %s", exc)
            return
        self._scheduled_drying_printers = result.running_printers
        self._scheduled_dry_units = result.reserved_units

    async def _get_smart_plugs(self, db: AsyncSession, printer_id: int) -> list[SmartPlug]:
        """Get all smart plugs associated with a printer."""
        result = await db.execute(select(SmartPlug).where(SmartPlug.printer_id == printer_id))
        return list(result.scalars().all())

    @staticmethod
    def _wake_plugs(plugs) -> list:
        """The enabled auto-on plugs, the one that feeds the printer first, then by id.

        The first is the one the wake switches; the others follow once the printer has
        connected (``_finish_power_on``). Never "the first row": its order is the heap's on
        PostgreSQL, and a light's plug taken first left the printer off through every grace
        and cool-off.
        """
        return sorted(
            (p for p in plugs if p.auto_on and p.enabled and not is_monitor_only(p)),
            key=lambda p: (not getattr(p, "controls_printer_power", True), p.id),
        )

    def _power_on_phase(self, printer_id: int) -> str | None:
        """Where this tier's wake of ``printer_id`` stands: ``booting``, ``cooling_off`` or None (no wake).

        An expired record is dropped here, so the next pass may switch the printer on again.
        """
        wake = self._power_on_wakes.get(printer_id)
        if wake is None:
            return None
        now = time.monotonic()
        if now < wake.grace_until:
            return "booting"
        if now < wake.cooloff_until:
            if not wake.reported:
                wake.reported = True
                logger.warning(
                    "Printer %s did not connect within %ss after power on; not trying again for %.0fs",
                    printer_id,
                    self._power_on_wait_time,
                    wake.cooloff_until - now,
                )
            return "cooling_off"
        self._power_on_wakes.pop(printer_id, None)
        return None

    async def _begin_power_on(self, db: AsyncSession, printer_id: int, plug: SmartPlug) -> str | None:
        """Switch ``printer_id`` on through its primary auto-on plug, without waiting for it (G03 E-5).

        Returns ``booting`` (switched on, or already on: the boot grace runs), ``cooling_off`` (the plug was
        unreachable or would not switch: no new attempt until the cool-off ends) or None (the farm's start
        admission is closed: nothing switched, nothing recorded).

        The record is registered before the first await, so a printer deleted meanwhile takes it with it
        (``printer_deletion._ID_KEYED``); a deleted record's plug is never switched. The pass's transaction is
        ended before the plug's own I/O, so no PostgreSQL connection sits idle in transaction behind a slow
        plug. Nothing connects here: the printer's MQTT client reconnects by itself once the printer is up, and
        a later pass dispatches.
        """
        from backend.app.services.smart_plug_manager import plug_switched

        life = printer_manager.printer_life(printer_id)
        plug_name = plug.name
        now = time.monotonic()
        wake = _PowerOnWake(
            primary_plug_id=plug.id,
            grace_until=now + self._power_on_wait_time,
            cooloff_until=now + self._power_on_wait_time + _POWER_ON_COOLOFF_SECONDS,
        )
        self._power_on_wakes[printer_id] = wake

        def refused(message: str, *args) -> str:
            logger.warning(message, *args)
            # The cool-off starts now; there is no boot to wait for.
            wake.grace_until = time.monotonic()
            wake.cooloff_until = wake.grace_until + _POWER_ON_COOLOFF_SECONDS
            wake.reported = True
            return "cooling_off"

        logger.info("Printer %s offline, attempting to power on via smart plug '%s'", printer_id, plug_name)
        try:
            # Tasmota or Home Assistant: the one read that needs the session comes first.
            service = await smart_plug_manager.get_service_for_plug(plug, db)
            if db.in_transaction():
                await db.commit()
            status = await service.get_status(plug)
            if not status.get("reachable"):
                return refused("Smart plug '%s' is not reachable", plug_name)
            if status.get("state") == "ON":
                logger.info("Smart plug '%s' is already on; waiting for printer %s to connect", plug_name, printer_id)
                return "booting"
            if not farm_startup_guard.allowed():
                if self._power_on_wakes.get(printer_id) is wake:
                    self._power_on_wakes.pop(printer_id, None)
                return None
            if not printer_manager.printer_life_is_current(printer_id, life):
                logger.info("Printer %s was deleted while powering on -- its plug is left alone", printer_id)
                return None
            if not await service.turn_on(plug):
                return refused("Failed to turn on smart plug '%s'", plug_name)
        except Exception as exc:  # noqa: BLE001 - a plug that fails is a failed wake, never a failed pass
            return refused("Could not power on printer %s via smart plug '%s': %s", printer_id, plug_name, exc)
        # The print start that follows runs the "on" scripts.
        await plug_switched(plug, True, scripts="none")
        logger.info(
            "Powered on smart plug '%s' for printer %s; dispatching once it connects (within %ss)",
            plug_name,
            printer_id,
            self._power_on_wait_time,
        )
        return "booting"

    async def _finish_power_on(self, db: AsyncSession, printer_id: int) -> None:
        """A printer this tier woke has connected: end its record and switch its other auto-on plugs on.

        Filter, secondary power and the like followed the primary plug only once the printer was up, and still
        do. Best-effort: a plug that fails is logged, never the pass.
        """
        from backend.app.services.smart_plug_manager import plug_switched

        wake = self._power_on_wakes.pop(printer_id, None)
        if wake is None:
            return
        extras = [
            plug
            for plug in self._wake_plugs(await self._get_smart_plugs(db, printer_id))
            if plug.id != wake.primary_plug_id
        ]
        services = []
        for plug in extras:
            try:
                services.append((plug, plug.name, await smart_plug_manager.get_service_for_plug(plug, db)))
            except Exception as exc:  # noqa: BLE001
                logger.warning("Failed to power on extra plug '%s': %s", plug.name, exc)
        if services and db.in_transaction():
            await db.commit()
        for plug, plug_name, service in services:
            try:
                if farm_startup_guard.allowed() and await service.turn_on(plug):
                    # The print start that follows runs the "on" scripts.
                    await plug_switched(plug, True, scripts="none")
                    logger.info("Also powered on plug '%s' for printer %s", plug_name, printer_id)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Failed to power on extra plug '%s': %s", plug_name, exc)

    async def _power_off_if_needed(self, db: AsyncSession, item: PrintQueueItem):
        """Schedule power-off if the queue item enabled auto_off_after.

        Delegates to the smart-plug manager so the off honours each plug's
        configured strategy (time delay or temperature threshold), is cancelled
        if the printer starts printing again, and never cuts power on a loaded
        print (#1890). Previously this hardcoded a 50°C / 600s cooldown wait and
        powered off on the timeout regardless of print state.
        """
        if not item.auto_off_after:
            return
        try:
            await smart_plug_manager.schedule_off_after_queue_job(item.queue_id, db)
        except Exception as e:
            logger.warning("Auto-off: Failed to schedule power-off for printer %s: %s", item.queue_id, e)

    async def _get_job_name(self, db: AsyncSession, item: PrintQueueItem) -> str:
        """Get a human-readable name for a queue item."""
        if item.archive_id:
            result = await db.execute(select(PrintArchive).where(PrintArchive.id == item.archive_id))
            archive = result.scalar_one_or_none()
            if archive:
                return archive.filename.replace(".gcode.3mf", "").replace(".3mf", "")
        if item.library_file_id:
            result = await db.execute(LibraryFile.active().where(LibraryFile.id == item.library_file_id))
            library_file = result.scalar_one_or_none()
            if library_file:
                return library_file.filename.replace(".gcode.3mf", "").replace(".3mf", "")
        # A job whose original rows are gone still knows what it is called: the
        # snapshot carries the human filename precisely so the hash never has to
        # stand in for it (§4, A04) — and when the payload cannot answer, the job
        # number is the honest label. Never the object's name: this one goes into
        # a notification, where a 64-hex string tells the operator nothing.
        descriptor = await item_descriptor(db, item)
        if descriptor is not None:
            with suppress(SourceUnavailable):
                return source_display_filename(descriptor).replace(".gcode.3mf", "").replace(".3mf", "")
        return f"Job #{item.id}"

    @staticmethod
    def _failed_dispatch_reason(code, message, *, returned: bool) -> str:
        lang = current_language()
        reason = failure_reasons.dispatch_reason_text(code, message, lang)
        if returned:
            reason = f"{reason} {t(lang, 'queue_return', 'returned')}"
        return reason

    async def _get_printer(self, db: AsyncSession, printer_id: int) -> Printer | None:
        """Get printer by ID."""
        result = await db.execute(select(Printer).where(Printer.id == printer_id))
        return result.scalar_one_or_none()

    async def _wait(self, db: AsyncSession, item: PrintQueueItem, code, message) -> bool:
        """A waiting diagnostic of the pass (G03 B, A8): addressed to the pending row the pass loaded, never a flush
        of its copy. False: the row changed since it was loaded and the pass writes nothing else for it."""
        changed = not (item.waiting_reason == message and getattr(item, "waiting_reason_code", None) == code)
        holds = await write_wait_reason(db, item, code, message, queue_id=item.queue_id)
        if changed:
            # Ends the statement's transaction either way: an UPDATE that matched nothing still took SQLite's
            # writer. A commit, never a rollback — a rollback would expire every copy the pass still reads.
            await db.commit()
        if not holds:
            logger.info("Queue item %s changed since this pass loaded it — left to the next pass", item.id)
        return holds

    async def _prove_verdict(
        self, cdb, item: PrintQueueItem, basis: str, *, admit: bool = True, before_row=None
    ) -> tuple[PrintQueueItem | None, datetime | None, str | None]:
        """A verdict's proof under its lane (G03 B, A8 steps 1-3), the caller holding ``queue_claim_scope``: the
        lane admits a start (``admit=False``: is not already halted), the row is the head at a ``now`` read
        after the lane's guards, and it is the row the pass judged — pending, same queue, same basis."""
        now, refused = await self._prove_next_row(
            cdb, item_id=item.id, queue_id=item.queue_id, printer_id=item.queue_id, admit=admit
        )
        if refused:
            return None, now, refused
        if before_row is not None:
            await before_row()
        row, refused = await lock_judged_row(cdb, item_id=item.id, queue_id=item.queue_id, basis=basis)
        return row, now, refused

    @staticmethod
    def _verdict_refused(item: PrintQueueItem, code: str) -> VerdictResult:
        """Nothing written: no end, no halt, no notification, no auto-off. The next pass reads the row fresh."""
        logger.info("Queue item %s: verdict not written (%s) — the next pass reads it fresh", item.id, code)
        return VerdictResult(False, code)

    @staticmethod
    async def _end_tick(db: AsyncSession) -> None:
        """A verdict runs in a session of its own; the tick's transaction ends first (it may hold SQLite's
        writer, which the verdict's lane would then wait behind)."""
        if db.in_transaction():
            await db.commit()

    async def _verdict_skip(self, db: AsyncSession, item: PrintQueueItem, basis: str, printer_id: int) -> VerdictResult:
        """V1: the previous-success gate skips the head — only while the gate is still shut, asked again in the
        verdict's own transaction (an acknowledgement committed since passes the gate)."""
        from backend.app.services.queue_counters import update_queue_counters

        queue_id = item.queue_id
        await self._end_tick(db)
        async with async_session() as cdb, queue_claim_scope(cdb, queue_id):
            row, now, refused = await self._prove_verdict(cdb, item, basis)
            if not refused and await self.previous_print_succeeded(cdb, printer_id, row.id):
                refused = "gate_passes"
            if refused:
                return self._verdict_refused(item, refused)
            await cdb.execute(
                update(PrintQueueItem)
                .where(
                    PrintQueueItem.id == row.id,
                    PrintQueueItem.status == "pending",
                    PrintQueueItem.queue_id == queue_id,
                )
                .values(
                    status="skipped",
                    error_message="Previous print failed",
                    completed_at=now,
                    deferred_claim_token=None,
                )
                .execution_options(synchronize_session=False)
            )
            await update_queue_counters(cdb, queue_id)
            await cdb.commit()
        set_committed_value(item, "status", "skipped")
        job_name = await self._get_job_name(db, item)
        printer_row = await self._get_printer(db, printer_id)
        await notification_service.on_queue_job_skipped(
            job_name=job_name,
            printer_id=printer_id,
            printer_name=printer_row.name if printer_row else "Unknown",
            reason="Previous print failed",
            db=db,
        )
        return VerdictResult(True)

    async def _verdict_storage_full(
        self, db: AsyncSession, item: PrintQueueItem, basis: str, printer_id: int
    ) -> VerdictResult:
        """V4: a head that wants a timelapse on a full medium pauses its queue — asked of a lane that is only not
        already halted (a pause is legitimate while the printer prints), while the medium is still full."""
        from backend.app.models.printer_queue import PrinterQueue

        queue_id = item.queue_id
        await self._end_tick(db)
        async with async_session() as cdb, queue_claim_scope(cdb, queue_id):
            row, now, refused = await self._prove_verdict(cdb, item, basis, admit=False)
            if not refused and not _timelapse_storage_full(printer_id):
                refused = "storage_free"
            if refused:
                return self._verdict_refused(item, refused)
            await cdb.execute(
                update(PrintQueueItem)
                .where(
                    PrintQueueItem.id == row.id,
                    PrintQueueItem.status == "pending",
                    PrintQueueItem.queue_id == queue_id,
                )
                .values(
                    waiting_reason="Timelapse storage full",
                    waiting_reason_code="storage_full",
                    waiting_reason_checked_at=now,
                )
                .execution_options(synchronize_session=False)
            )
            await cdb.execute(
                update(PrinterQueue)
                .where(PrinterQueue.id == queue_id)
                .values(is_paused=True)
                .execution_options(synchronize_session=False)
            )
            await cdb.commit()
        if item.queue is not None:
            # The tick's copy of the header follows, so the printer's later rows skip as a paused queue.
            set_committed_value(item.queue, "is_paused", True)
        return VerdictResult(True)

    async def _fail_source_item(
        self, db: AsyncSession, item: PrintQueueItem, reason: str, *, basis: str | None = None
    ) -> VerdictResult:
        """V2: fail before dispatch without parking the printer's remaining queue, proved under the lane. Its cause
        came from a file read; its proof is that the row and the head are what the pass judged."""
        from backend.app.services.queue_counters import update_queue_counters

        basis = basis if basis is not None else await basis_of(db, item)
        queue_id = item.queue_id
        await self._end_tick(db)
        async with async_session() as cdb, queue_claim_scope(cdb, queue_id):
            row, now, refused = await self._prove_verdict(cdb, item, basis)
            if refused:
                return self._verdict_refused(item, refused)
            await cdb.execute(
                update(PrintQueueItem)
                .where(
                    PrintQueueItem.id == row.id,
                    PrintQueueItem.status == "pending",
                    PrintQueueItem.queue_id == queue_id,
                )
                .values(
                    status="failed",
                    error_message=routing_detail(reason)["message"],
                    error_code=failure_reasons.DISPATCH_SOURCE_UNAVAILABLE,
                    # No physical print failed; this must not trip require_previous_success.
                    gate_acknowledged=True,
                    completed_at=now,
                    waiting_reason=None,
                    waiting_reason_code=None,
                    waiting_reason_checked_at=None,
                    deferred_claim_token=None,
                )
                .execution_options(synchronize_session=False)
            )
            await update_queue_counters(cdb, queue_id)
            await cdb.commit()
        set_committed_value(item, "status", "failed")
        logger.warning("Queue item %s failed before dispatch: %s", item.id, reason)
        return VerdictResult(True)

    @retry_busy_return_order
    async def _fail_item(
        self,
        db: AsyncSession,
        item: PrintQueueItem,
        error_message: str,
        code: str,
        *,
        basis: str | None = None,
        storage_epoch: int | None = None,
    ) -> VerdictResult:
        """V3: mark a row that never reached a claim as failed with its closed class, and halt its queue.

        Only for the refusals ``_start_print`` decides before the claim, on the tick's own
        row, and proved under the lane (G03 B, A8): the lane admits a start and the row is
        still the head the pass judged. The halt goes through ``halt_lane`` (A9). An attempt
        that was claimed ends through :meth:`_fail_attempt`, addressed to its claim token.
        """
        from backend.app.services.auto_queue_unsent_return import (
            has_auto_ancestor,
            lock_order_for_return,
            return_unsent,
        )
        from backend.app.services.queue_counters import halt_lane, update_queue_counters

        basis = basis if basis is not None else await basis_of(db, item)
        item_id, queue_id = item.id, item.queue_id
        may_return = (
            storage_epoch is not None
            and code == failure_reasons.DISPATCH_PRINTER_UNAVAILABLE
            and has_auto_ancestor(item)
        )
        await self._end_tick(db)
        async with async_session() as cdb, queue_claim_scope(cdb, queue_id):
            order_refusal = None

            async def check_order():
                nonlocal order_refusal
                order_refusal = await lock_order_for_return(cdb, item.project_id, item.project_line_id)

            row, now, refused = await self._prove_verdict(
                cdb, item, basis, before_row=check_order if may_return else None
            )
            if refused:
                return self._verdict_refused(item, refused)
            result = await cdb.execute(
                update(PrintQueueItem)
                .where(
                    PrintQueueItem.id == row.id,
                    PrintQueueItem.status == "pending",
                    PrintQueueItem.queue_id == queue_id,
                )
                .values(
                    status="failed",
                    error_message=error_message,
                    error_code=code,
                    # Never claimed, so never sent: no physical print failed, and the next
                    # job's "only after a successful previous print" must not read one (G03 O-3).
                    gate_acknowledged=True,
                    completed_at=now,
                    deferred_claim_token=None,
                )
                .execution_options(synchronize_session=False)
            )
            if not result.rowcount:
                return self._verdict_refused(item, "not_pending")
            returned = may_return and await return_unsent(
                cdb,
                row,
                UnsentAttempt(storage_epoch),
                code=code,
                message=error_message,
                at=now,
                order_refusal=order_refusal,
            )
            await halt_lane(cdb, queue_id, item_id=item_id, kind="error")
            await update_queue_counters(cdb, queue_id)
            await cdb.commit()
        set_committed_value(item, "status", "failed")
        if returned:
            await ws_manager.send_queue_changed(queue_id)
            printer = await db.get(Printer, queue_id)
            await notification_service.on_queue_job_failed(
                job_name=await self._get_job_name(db, item),
                printer_id=queue_id,
                printer_name=printer.name if printer else None,
                reason=self._failed_dispatch_reason(code, error_message, returned=True),
                db=db,
            )
        return VerdictResult(True, returned=returned)

    async def _cancel_item(
        self,
        db: AsyncSession,
        item: PrintQueueItem,
        *,
        claim_started_at=None,
        queue_id: int | None = None,
        execution_archive_id: int | None = None,
    ) -> bool:
        """Mark THIS claimed attempt cancelled by the user and pause the queue (G03 B, A10).

        Differs from ``_fail_item`` in two ways: status is ``cancelled``
        (not ``failed``) and the queue is paused (not in error). User
        cancel is intentional — pausing lets the operator decide whether
        to resume the rest of the queue.

        A compare-and-set on ``printing`` and the attempt's claim token, in the caller's
        session made clean, under the claim queue's lane (the execution archive's guard
        first when the attempt has one); the pause goes through ``halt_lane``. A row the
        claim no longer owns is left as it is and a header that still names it is
        released (A11). True when this attempt's row was cancelled.
        """
        from backend.app.models.archive import PrintArchive
        from backend.app.services.queue_counters import halt_lane, release_lost_claim, update_queue_counters
        from backend.app.services.queue_ops import claim_lanes

        item_id = item.id
        token = claim_started_at if claim_started_at is not None else item.started_at
        claim_queue = queue_id if queue_id is not None else item.queue_id
        if db.in_transaction():
            await db.commit()
        async with claim_lanes(db, [claim_queue], archive_id=execution_archive_id):
            if execution_archive_id is not None:
                await db.scalar(
                    select(PrintArchive.id)
                    .where(PrintArchive.id == execution_archive_id)
                    .with_for_update(key_share=True)
                )
            result = await db.execute(
                update(PrintQueueItem)
                .where(
                    PrintQueueItem.id == item_id,
                    PrintQueueItem.status == "printing",
                    PrintQueueItem.started_at.is_not_distinct_from(token),
                )
                .values(
                    status="cancelled",
                    error_message="Cancelled by user",
                    completed_at=datetime.now(timezone.utc),
                    deferred_claim_token=None,
                )
                .execution_options(synchronize_session=False)
            )
            if not result.rowcount:
                await release_lost_claim(db, claim_queue, item_id)
                await db.commit()
                return False
            await halt_lane(db, claim_queue, item_id=item_id, kind="paused")
            await update_queue_counters(db, claim_queue)
            await db.commit()
        set_committed_value(item, "status", "cancelled")
        return True

    async def _prove_next_row(
        self, cdb, *, item_id: int, queue_id: int, printer_id: int, admit: bool = True
    ) -> tuple[datetime | None, str | None]:
        """Under the lane, before the claim or a verdict decides (G03 B, A3 / A8 steps 1-2).

        The lane admits a start — the HA interlock, the header locked with ``require_scheduler_claim``, the
        farm's admission — or, ``admit=False``, the lane is only not already halted. Then the row is the queue's
        runnable head at a ``now`` read after the lane's guards were granted: the one clock reading of the
        decision (the claim writes it as its token, and nothing after it reads the clock again — r4, R15).
        """
        from backend.app.services.ha_sensor_manager import ha_sensor_manager

        if admit and printer_id in await ha_sensor_manager.blocked_printers(cdb):
            logger.info("Queue item %s: HA sensor alert on printer %s", item_id, printer_id)
            return None, "ha_interlock"
        try:
            occupancy = await read_queue_occupancy(cdb, queue_id, for_update=True)
            if admit:
                require_scheduler_claim(occupancy)
            elif occupancy.queue.status in {"paused", "error"}:
                return None, "queue_paused"
        except PrinterOccupancyConflict as exc:
            logger.info("Queue item %s: admission lost (%s) — skipping", item_id, exc.code)
            return None, exc.code
        if admit and not farm_startup_guard.allowed():
            return None, "farm_recovery_hold"
        now = datetime.now(timezone.utc)
        # A job cooling down after a busy spool is not "first": its neighbour may start (spec r4 §6).
        cooling = self._cooling_item_ids() - {item_id}
        if await cdb.scalar(first_runnable_query(queue_id, now, cooling)) != item_id:
            logger.info("Queue item %s: no longer first runnable pending item — skipping", item_id)
            return now, "not_first_runnable"
        return now, None

    async def _claim_for_dispatch(
        self,
        *,
        item_id: int,
        queue_id: int,
        printer_id: int,
        plan_fields: dict[str, Any],
        basis: str,
    ) -> "ClaimResult":
        """The final ``pending → printing`` claim, in a short session of its own (post-rebuild D-R5).

        Everything the claim depends on is read again here, under the lane's lock
        (``_prove_next_row``): the HA interlock, the queue's occupancy, the farm's
        admission and the row still being the first runnable one at the claim's own
        ``now``; then the dispatch budget and the order's printer restrictions; then the
        row itself, locked, compared with the basis the pass recorded (G03 B, A3) — a row
        edited since the pass loaded it is refused ``item_changed`` and nothing is
        written. The routing plan and the spent attempt are written in the SAME
        compare-and-set commit as the claim, addressed to the lane (A4), so a crash after
        it has spent the attempt and nothing can claim without the plan. A refusal rolls
        back only this session; the tick that asked keeps every row it read. The answer
        is plain values — the claimed row's snapshot included (A5) — never a row of
        another session.
        """
        from backend.app.services.queue_counters import halt_lane, set_queue_printing, update_queue_counters

        async with async_session() as cdb, queue_claim_scope(cdb, queue_id):
            storage_epoch = queue_sources.current_epoch()
            now, refused = await self._prove_next_row(cdb, item_id=item_id, queue_id=queue_id, printer_id=printer_id)
            if refused:
                return ClaimResult(False, refused)
            spent = await cdb.scalar(select(PrintQueueItem.dispatch_attempts).where(PrintQueueItem.id == item_id))
            if (spent or 0) >= DISPATCH_MAX_ATTEMPTS:
                # The cap is asked BEFORE a new claim: a row whose attempts a crash
                # loop spent fails here, visibly, instead of uploading again.
                message = (
                    f"Dispatch budget exhausted after {spent} attempts. Check the printer, then Retry "
                    "the job and resume the queue."
                )
                result = await cdb.execute(
                    update(PrintQueueItem)
                    .where(
                        PrintQueueItem.id == item_id,
                        PrintQueueItem.status == "pending",
                        PrintQueueItem.queue_id == queue_id,
                    )
                    .values(
                        status="failed",
                        error_code=failure_reasons.DISPATCH_INTERNAL_ERROR,
                        error_message=message,
                        completed_at=now,
                        gate_acknowledged=True,
                        deferred_claim_token=None,
                    )
                    .execution_options(synchronize_session=False)
                )
                if not result.rowcount:
                    return ClaimResult(False, "not_pending")
                await halt_lane(cdb, queue_id, item_id=item_id, kind="error")
                await update_queue_counters(cdb, queue_id)
                await cdb.commit()
                logger.warning("Queue item %s: dispatch budget exhausted (%s attempts)", item_id, spent)
                return ClaimResult(
                    False,
                    "budget_exhausted",
                    failure_code=failure_reasons.DISPATCH_INTERNAL_ERROR,
                    failure_message=message,
                )
            from backend.app.services.order_resources import allowed

            link = (
                await cdb.execute(
                    select(PrintQueueItem.project_id, PrintQueueItem.project_line_id).where(
                        PrintQueueItem.id == item_id
                    )
                )
            ).first()
            if link is None or not await allowed(cdb, link[0], printer_id, lock=True, project_line_id=link[1]):
                return ClaimResult(False, "order_resources_changed")
            row, changed = await lock_judged_row(cdb, item_id=item_id, queue_id=queue_id, basis=basis)
            if changed:
                # Nothing written: no attempt spent, no header, no counters, no plan. The pass leaves this
                # printer's next row alone; the next pass reads the row fresh (G03 B, A3).
                logger.info("Queue item %s: not claimed (%s) — the next pass reads it fresh", item_id, changed)
                return ClaimResult(False, changed)
            claimed = {
                "status": "printing",
                "started_at": now,
                "waiting_reason": None,
                "waiting_reason_code": None,
                "waiting_reason_checked_at": None,
                "gate_acknowledged": False,
                # A new attempt voids the proof of the previous one (A16, R11).
                "deferred_claim_token": None,
                **plan_fields,
            }
            cas_result = await cdb.execute(
                update(PrintQueueItem)
                .where(PrintQueueItem.id == item_id)
                .where(PrintQueueItem.status == "pending")
                .where(PrintQueueItem.queue_id == queue_id)
                .values(dispatch_attempts=func.coalesce(PrintQueueItem.dispatch_attempts, 0) + 1, **claimed)
                .execution_options(synchronize_session=False)
            )
            if cas_result.rowcount == 0:
                logger.info("Queue item %s: no longer pending at dispatch (cancelled or removed) — skipping", item_id)
                return ClaimResult(False, "not_pending")
            snapshot = {column.key: getattr(row, column.key) for column in PrintQueueItem.__table__.columns}
            snapshot.update(claimed, dispatch_attempts=(row.dispatch_attempts or 0) + 1)
            await set_queue_printing(cdb, queue_id, item_id)
            await update_queue_counters(cdb, queue_id)
            await cdb.commit()
        return ClaimResult(True, started_at=now, snapshot=MappingProxyType(snapshot), storage_epoch=storage_epoch)

    @retry_busy_return_order
    async def _fail_attempt(
        self,
        db: AsyncSession,
        *,
        item_id: int,
        claim_started_at,
        message: str,
        code: str,
        sent: bool = True,
        queue_id: int | None = None,
        execution_archive_id: int | None = None,
        execution_archive_identity: str | None = None,
        unsent_attempt: UnsentAttempt | None = None,
    ) -> VerdictResult:
        """End THIS claimed attempt as failed with its class, and halt the printer's queue until Resume.

        Addressed to the attempt's claim token: a row the operator cancelled, a later
        attempt, or a completion that already closed it is left exactly as it is.
        For an unsent attempt, close its prepared archive in this same commit if
        the dispatcher's own close did not land (G03 B.3).

        ``sent=False`` — the attempt published no start — writes the row
        ``gate_acknowledged``: nothing reached the printer, so the next job's "only
        after a successful previous print" has no failed print to read (G03 O-3). A
        published start the printer did not take (``DispatchNotStarted``, a refused
        command) still gates; so does an outcome nobody typed (the default).

        G03 B, A10: in the caller's session made clean, under the CLAIM queue's lane
        (``queue_id``: the printer it was claimed on; the row's current queue only for a
        caller that names none), the execution archive's guard and row first; the halt
        through ``halt_lane`` (A9); a lost CAS releases a header that still names the
        row (A11).
        """
        from backend.app.services.auto_queue_unsent_return import (
            RETURNABLE_FAILURES,
            has_auto_ancestor,
            lock_order_for_return,
            return_unsent,
        )
        from backend.app.services.print_execution import dispatch_archive_terminal_values
        from backend.app.services.queue_counters import halt_lane, release_lost_claim, update_queue_counters
        from backend.app.services.queue_ops import claim_lanes

        if claim_started_at is None:
            return VerdictResult(False)
        claim_queue = queue_id
        if claim_queue is None:
            claim_queue = await db.scalar(select(PrintQueueItem.queue_id).where(PrintQueueItem.id == item_id))
            if claim_queue is None:
                return VerdictResult(False)
        if db.in_transaction():
            await db.commit()
        async with claim_lanes(db, [claim_queue], archive_id=execution_archive_id):
            addressed = (
                PrintQueueItem.id == item_id,
                PrintQueueItem.queue_id == claim_queue,
                PrintQueueItem.status == "printing",
                PrintQueueItem.started_at == claim_started_at,
            )
            may_return = False
            order_refusal = None
            if (
                not sent
                and unsent_attempt is not None
                and code in RETURNABLE_FAILURES
                and (unsent_attempt.archive_id, unsent_attempt.archive_identity)
                == (execution_archive_id, execution_archive_identity)
            ):
                candidate = await db.scalar(
                    select(PrintQueueItem).where(*addressed).execution_options(populate_existing=True)
                )
                may_return = candidate is not None and has_auto_ancestor(candidate)
                if may_return:
                    order_refusal = await lock_order_for_return(db, candidate.project_id, candidate.project_line_id)
            if execution_archive_id is not None:
                await db.scalar(
                    select(PrintArchive.id)
                    .where(PrintArchive.id == execution_archive_id)
                    .with_for_update(key_share=True)
                )
            at = datetime.now(timezone.utc)
            result = await db.execute(
                update(PrintQueueItem)
                .where(*addressed)
                .values(
                    status="failed",
                    error_message=message,
                    error_code=code,
                    completed_at=at,
                    gate_acknowledged=not sent,
                    deferred_claim_token=None,
                )
                .execution_options(synchronize_session=False)
            )
            if not result.rowcount:
                await release_lost_claim(db, claim_queue, item_id)
                await db.commit()
                return VerdictResult(False)
            if not sent and execution_archive_id is not None and execution_archive_identity is not None:
                await db.execute(
                    update(PrintArchive)
                    .where(
                        PrintArchive.id == execution_archive_id,
                        PrintArchive.recovery_identity == execution_archive_identity,
                        PrintArchive.status == "printing",
                        PrintArchive.dispatch_phase == "prepared",
                    )
                    .values(**dispatch_archive_terminal_values("failed", message, code))
                    .execution_options(synchronize_session=False)
                )
            returned = False
            if may_return:
                row = await db.get(PrintQueueItem, item_id, populate_existing=True)
                returned = await return_unsent(
                    db, row, unsent_attempt, code=code, message=message, at=at, order_refusal=order_refusal
                )
            await halt_lane(db, claim_queue, item_id=item_id, kind="error")
            await update_queue_counters(db, claim_queue)
            await db.commit()
        return VerdictResult(True, returned=returned)

    async def _start_print(
        self,
        db: AsyncSession,
        item: PrintQueueItem,
        *,
        basis: str | None = None,
        requirements_cache=None,
        storage_epoch: int | None = None,
    ) -> bool:
        """Upload file and start print for a queue item.

        Supports two sources:
        - archive_id: Print from an existing archive
        - library_file_id: Print from a library file (file manager)

        True once this row was claimed and its dispatch spawned; False for every
        refusal, the claim's included — the caller never reads the row to find out.

        ``basis`` is the row's claim basis as the pass loaded it (G03 B, A2); a caller
        without one (the webhook start) gets the basis of the row it passed, at entry.
        Every refusal before the claim is a verdict proved against it, and the dispatch is
        built from the row the claim locked (A5), never from this copy.
        """
        item_id = item.id
        logger.info("Starting queue item %s", item_id)
        if storage_epoch is None:
            storage_epoch = queue_sources.current_epoch()
        if basis is None:
            basis = await basis_of(db, item)

        # Get printer first (needed for both paths)
        result = await db.execute(select(Printer).where(Printer.id == item.queue_id))
        printer = result.scalar_one_or_none()
        if not printer:
            outcome = await self._fail_item(
                db,
                item,
                "Printer not found",
                failure_reasons.DISPATCH_PRINTER_UNAVAILABLE,
                basis=basis,
                storage_epoch=storage_epoch,
            )
            logger.error("Queue item %s: Printer %s not found", item.id, item.queue_id)
            if outcome.written:
                await self._power_off_if_needed(db, item)
            return False

        # Check printer is connected
        if not printer_manager.is_connected(item.queue_id):
            outcome = await self._fail_item(
                db,
                item,
                "Printer not connected",
                failure_reasons.DISPATCH_PRINTER_UNAVAILABLE,
                basis=basis,
                storage_epoch=storage_epoch,
            )
            logger.error("Queue item %s: Printer %s not connected", item.id, item.queue_id)
            if outcome.written:
                await self._power_off_if_needed(db, item)
            return False

        # re-Connect MQTT if stalled
        if not await printer_manager.ensure_fresh_connection_for_printer(printer):
            outcome = await self._fail_item(
                db,
                item,
                "Printer not connected",
                failure_reasons.DISPATCH_PRINTER_UNAVAILABLE,
                basis=basis,
                storage_epoch=storage_epoch,
            )
            logger.error("Queue item %s: Printer %s not connected", item.id, item.queue_id)
            if outcome.written:
                await self._power_off_if_needed(db, item)
            return False

        # Determine source: the captured copy when this job has one (m173), else
        # its archive or library original. ``file_path`` is kept so we can still
        # do the "file exists on disk" guard before delegating.
        #
        # ⚠️ A job with a ``queue_source_id`` and no original reference at all is
        # a first-class job (spec §7): the rows below are read for what only THEY
        # know (the slice's nozzle diameter, the display name, which runner), and
        # their absence is not a refusal any more — the bytes come from the
        # descriptor either way, and this is the fourth of the four places that
        # used to build a source path of its own.
        archive = None
        library_file = None
        descriptor = await item_descriptor(db, item)

        if item.archive_id:
            result = await db.execute(select(PrintArchive).where(PrintArchive.id == item.archive_id))
            archive = result.scalar_one_or_none()
            if not archive and descriptor is None:
                await self._fail_source_item(db, item, "source_unreadable", basis=basis)
                logger.error("Queue item %s: Archive %s not found", item.id, item.archive_id)
                return False

        elif item.library_file_id:
            result = await db.execute(LibraryFile.active().where(LibraryFile.id == item.library_file_id))
            library_file = result.scalar_one_or_none()
            if not library_file and descriptor is None:
                await self._fail_source_item(db, item, "source_unreadable", basis=basis)
                logger.error("Queue item %s: Library file %s not found", item.id, item.library_file_id)
                return False

        elif descriptor is None:
            # Neither a snapshot nor an archive nor a library file
            await self._fail_source_item(db, item, "source_unreadable", basis=basis)
            logger.error("Queue item %s: No archive_id or library_file_id specified", item.id)
            return False

        file_path: Path | None = resolve_source_path(archive, library_file, descriptor=descriptor)
        if file_path is None:
            # A source row with an empty ``file_path`` (a pending archive) — the
            # same refusal the missing-row branches above give.
            await self._fail_source_item(db, item, "source_unreadable", basis=basis)
            logger.error("Queue item %s: source row carries no file", item.id)
            return False

        # Check file exists on disk (fast fail before any dispatch work) — and,
        # for a job the snapshot has to name, that the snapshot can name it. Both
        # answer with the same refusal and both must happen before the
        # pending→printing flip below, so they share this one try.
        snapshot_name: str | None = None
        try:
            if descriptor is not None:
                snapshot_name = source_display_filename(descriptor)
            await require_source_file(file_path)
        except SourceUnavailable as exc:
            if exc.reason in SOURCE_FAILURES:
                await self._fail_source_item(db, item, exc.reason, basis=basis)
            else:
                await self._wait(db, item, "filament_unavailable", routing_detail(exc.reason)["message"])
            return False

        from backend.app.services import direct_rack_gate

        if direct_rack_gate.print_refused(tray_layout_of(printer.id, printer_manager, printer.model)):
            # A direct-connected rack (H3C, P3C): the job waits, by name, until N01 stage B.
            await self._wait(
                db,
                item,
                direct_rack_gate.UNSUPPORTED_CODE,
                routing_detail(direct_rack_gate.UNSUPPORTED_CODE)["message"],
            )
            return False

        try:
            guard = await preflight_item(db, item, printer.id, cache=requirements_cache)
        except RoutingDeferred as exc:
            if exc.reason in SOURCE_FAILURES:
                await self._fail_source_item(db, item, exc.reason, basis=basis)
                return False
            await self._wait(db, item, "filament_unavailable", routing_detail(exc.reason)["message"])
            return False
        # The plan travels as plain values into the claim's own commit (D-R5): written on
        # this tick session it would be a dirty row the claim's writer waits behind, or
        # work a rollback of the tick throws away.
        plan_fields: dict[str, Any] = (
            {
                "ams_mapping": json.dumps(guard.plan.mapping),
                "use_ams": guard.plan.use_ams,
                "plate_id": guard.plan.resolved_plate_id,
            }
            if guard
            else {}
        )

        # Nozzle-diameter mismatch guard (upstream #1899). A file sliced for one
        # nozzle size dispatched to a printer with a different nozzle installed is
        # rejected by the firmware with a cryptic HMS ("Failed to get AMS mapping
        # table" 0700_8012, or "nozzle diameter … not consistent" 0500_4038) that
        # gives the user no idea what went wrong. Catch it here — before the
        # pending→printing flip, so nothing has to be unwound — and fail with an
        # actionable message instead of spending time preheating and uploading.
        # Fail-safe by construction: only a POSITIVE mismatch blocks. When the
        # slice carries no nozzle diameter (``archive.nozzle_diameter`` is None,
        # and library files never carry one) or the printer hasn't reported its
        # nozzles yet, we fall through and dispatch exactly as before. On
        # dual-nozzle printers (H2D) a match against EITHER installed nozzle
        # passes, so a 0.6 slice is fine as long as one hotend is a 0.6.
        #
        # ⚠️ Since m173 ``archive`` can be None for a job that still dispatches
        # (its snapshot holds the bytes, its archive row was purged), so the
        # question would be asked of unknown data — except that it is not asked at
        # all in that case, and the reason is worth having written down rather than
        # re-derived: the block is gated on ``guard is None``, and a preflight
        # guard is None for exactly two kinds of job — a calibration job (§2's
        # exemption: never captured, so ``archive`` is None only if its own library
        # asset vanished, and it carries no ``nozzle_diameter`` either) and a raw
        # ``.gcode`` job (whose nozzle diameter cannot exist: the value is 3MF
        # metadata). Every job that HAS a snapshot and an archive-shaped source
        # gets a guard, so it never reaches this line. If a future change makes a
        # 3MF job guard-less, read the diameter off the snapshot rather than
        # letting "unknown" mean "dispatch".
        sliced_nozzle = archive.nozzle_diameter if archive else None
        if sliced_nozzle and guard is None:
            installed = _installed_nozzle_diameters(printer_manager.get_status(item.queue_id))
            mismatch_msg = _nozzle_mismatch_message(sliced_nozzle, installed)
            if mismatch_msg:
                if not (
                    await self._fail_item(db, item, mismatch_msg, failure_reasons.DISPATCH_START_REFUSED, basis=basis)
                ).written:
                    return False
                logger.warning("Queue item %s: nozzle mismatch — %s", item.id, mismatch_msg)
                await notification_service.on_queue_job_failed(
                    job_name=archive.filename.replace(".gcode.3mf", "").replace(".3mf", ""),
                    printer_id=printer.id,
                    printer_name=printer.name,
                    reason=mismatch_msg,
                    db=db,
                )
                await self._power_off_if_needed(db, item)
                return False

        # Flip item to "printing" BEFORE dispatch starts so that a crash
        # mid-FTP doesn't leave the item re-dispatchable — in a short session of
        # its own, so a refusal there expires nothing the tick still reads
        # (post-rebuild D-R5). The tick's own pending work is committed first:
        # a claim that took SQLite's writer behind it would wait on itself.
        if db.in_transaction():
            await db.commit()
        claim = await self._claim_for_dispatch(
            item_id=item_id, queue_id=item.queue_id, printer_id=printer.id, plan_fields=plan_fields, basis=basis
        )
        if not claim.claimed:
            if claim.reason == "budget_exhausted":
                # Committed by the claim: the job failed and its queue halted — said once, like
                # every refusal before a claim (the row is no longer pending on the next tick).
                await notification_service.on_queue_job_failed(
                    job_name=await self._get_job_name(db, item),
                    printer_id=printer.id,
                    printer_name=printer.name,
                    reason=failure_reasons.dispatch_reason_text(claim.failure_code, claim.failure_message),
                    db=db,
                )
                await self._power_off_if_needed(db, item)
            return False
        # The tick's copy follows the claim without becoming a write of its own.
        set_committed_value(item, "status", "printing")
        set_committed_value(item, "started_at", claim.started_at)
        # From here on the dispatch reads the row the claim locked and committed (G03 B, A5) — never the tick's
        # copy. The two are equal in every column the claim compared (the basis), so the source rows loaded above
        # for the claimed row's archive / library ids are the claimed row's.
        row = claim.snapshot

        # Parse per-item options into the shape background_dispatch expects.
        ams_mapping: list[int] | None = None
        raw_mapping = row["ams_mapping"]
        if raw_mapping:
            try:
                ams_mapping = json.loads(raw_mapping)
            except json.JSONDecodeError:
                logger.warning("Queue item %s: Invalid AMS mapping JSON, ignoring", item.id)

        swap_events: list[str] = []
        if row["execute_swap_macros"] and row["swap_macro_events"]:
            try:
                swap_events = json.loads(row["swap_macro_events"]) if isinstance(row["swap_macro_events"], str) else []
            except (ValueError, TypeError):
                swap_events = []

        options: dict[str, Any] = {
            "mesh_mode_fast_check": row["mesh_mode_fast_check"],
            "ams_mapping": ams_mapping,
            "plate_id": row["plate_id"],
            # Tri-state calibration → mode string (off/auto/on) so an 'auto'
            # override reaches start_print. NULL *_mode derives from the legacy
            # bool, so existing items emit 'on'/'off' — byte-identical downstream
            # (start_print maps 'on'/'off' to today's exact bool + 0/1).
            "bed_levelling": derive_mode(row["bed_levelling_mode"], row["bed_levelling"]),
            "flow_cali": derive_mode(row["flow_cali_mode"], row["flow_cali"]),
            "layer_inspect": row["layer_inspect"],
            # #1721: the user's explicit timelapse choice flows straight through
            # to the printer. The #1397 force-on at dispatch was removed because it
            # un-gated the per-layer M622 J1 wipe blocks on Smooth-mode slicer
            # profiles. Finish-photo capture is now driven by the stg_cur=22
            # transition in bambu_mqtt.py, not by forcing a timelapse video.
            "timelapse": row["timelapse"],
            # Which medium records it. Passed through raw — the dispatcher
            # re-checks it against the card, which may have left the machine
            # since this item was queued.
            "timelapse_storage": row["timelapse_storage"],
            "use_ams": row["use_ams"],
            "nozzle_offset_cali": derive_mode(row["nozzle_offset_cali_mode"], row["nozzle_offset_cali"]),
            # H2C rack-swap nozzle pick (#1780) — opaque JSON string captured
            # from Bambu Studio's project_file on VP intake; the MQTT layer
            # parses + injects it only for dual-nozzle models, so a null on
            # every other model is a transparent pass-through.
            "nozzle_mapping": row["nozzle_mapping"],
            # The operator's rack-position pick (upstream #1784), the column's
            # JSON text as is; the dispatcher reads it after the upload, against
            # the rack as it stands then.
            "nozzle_rack_choice": row["nozzle_rack_choice"],
            "execute_swap_macros": row["execute_swap_macros"],
            "swap_macro_events": swap_events,
            # Which macros the operator ticked for this job. None means no
            # dialog ever asked — under opt-in that fires nothing.
            "selected_macro_ids": (
                json.loads(row["selected_macro_ids"]) if isinstance(row["selected_macro_ids"], str) else None
            ),
            "gcode_injection": row["gcode_injection"],
            # Preheat / heat-soak per-item override (#1468). Read by the preheat
            # stage in background_dispatch (services/preheat.py) before start_print.
            "preheat_override": row.get("preheat_override", "inherit"),
            "preheat_chamber_target_override": row.get("preheat_chamber_target_override"),
            # Filament-calibration tagging — dispatcher's pre-print bind hook
            # uses this to skip the ``extrusion_cali_sel`` re-bind (the
            # calibration print itself is what determines the new K value),
            # and the archive_print step forwards it to PrintArchive.is_calibration
            # so the resulting row is correctly tagged as a calibration print
            # in the archive listing (#filament-cal-archive-flag).
            "is_calibration": row["is_calibration"],
            "calibration_session_id": row["calibration_session_id"],
        }

        dispatch_source_id: int | None
        if archive:
            dispatch_kind: Literal["reprint_archive", "print_library_file"] = "reprint_archive"
            dispatch_source_id = archive.id
            dispatch_source_name = archive.filename
        elif library_file:
            dispatch_kind = "print_library_file"
            dispatch_source_id = library_file.id
            dispatch_source_name = library_file.filename
        elif descriptor is not None and snapshot_name is not None:
            # No original row left — the snapshot carries everything the runner
            # needs, so ``source_id`` is None and the runner looks nothing up.
            # The provenance decides which runner only so that the archive row,
            # the log lines and the panel keep saying "reprint" for a reprint;
            # the bytes are the same either way (§4: provenance is navigation).
            # ⚠️ A payload whose provenance is unreadable never gets here: it has
            # no usable name either, and ``source_display_filename`` above already
            # refused it — so this mapping is never applied to a guessed ``{}``.
            dispatch_kind = (
                "reprint_archive" if descriptor.provenance.get("kind") == "archive" else "print_library_file"
            )
            dispatch_source_id = None
            dispatch_source_name = snapshot_name
        else:
            # Should have been caught above, but belt-and-braces: the row is claimed by now, so it ends as the
            # attempt it is, addressed to its claim token — in a session of its own: a lost compare-and-set
            # rolls back, and the tick's copies must not expire with it.
            async with async_session() as attempt_db:
                await self._fail_attempt(
                    attempt_db,
                    item_id=item.id,
                    claim_started_at=claim.started_at,
                    message="No source file specified",
                    code=failure_reasons.DISPATCH_SOURCE_UNAVAILABLE,
                    sent=False,
                    queue_id=item.queue_id,
                )
            await self._power_off_if_needed(db, item)
            return False

        job_name_short = dispatch_source_name.replace(".gcode.3mf", "").replace(".3mf", "")

        # Pre-register the stagger slot synchronously so the next check_queue
        # tick (and any racing direct-print) sees this slot occupied. Spawned
        # task uses background_dispatch._process_job → acquire_stagger_slot,
        # which is idempotent: returns immediately because the printer already
        # holds a slot. Direct prints have no pre-register and acquire cold —
        # they wait until a slot frees before doing any FTP work.
        stagger_enabled, _stagger_concurrent, stagger_interval, _stagger_wait_bed = await self._get_stagger_settings(db)
        if stagger_enabled:
            self._pre_register_stagger_slot(printer, stagger_interval)

        # Spawn the dispatch + post-dispatch bookkeeping in its own task so
        # multiple queue items targeting different printers run in parallel
        # (matches the direct-print path through ``_dispatcher_loop``). The
        # caller's ``db`` is the check_queue tick session — the spawned task
        # opens its own.
        spawn_background_task(
            self._dispatch_and_finalize(
                queue_item_id=item.id,
                printer_id=printer.id,
                printer_name=printer.name,
                printer_serial=printer.serial_number,
                dispatch_kind=dispatch_kind,
                dispatch_source_id=dispatch_source_id,
                dispatch_source_name=dispatch_source_name,
                options=options,
                requested_by_user_id=row["created_by_id"],
                project_id=row["project_id"],
                project_line_id=row["project_line_id"],
                job_name_short=job_name_short,
                swap_events=swap_events,
                claim_started_at=claim.started_at,
                storage_epoch=claim.storage_epoch,
            ),
            name=f"queue-dispatch-{item.id}",
        )
        return True

    async def _dispatch_and_finalize(
        self,
        *,
        queue_item_id: int,
        printer_id: int,
        printer_name: str,
        printer_serial: str | None,
        dispatch_kind: Literal["reprint_archive", "print_library_file"],
        dispatch_source_id: int | None,
        dispatch_source_name: str,
        options: dict[str, Any],
        requested_by_user_id: int | None,
        project_id: int | None,
        project_line_id: int | None,
        job_name_short: str,
        swap_events: list[str],
        claim_started_at=None,
        storage_epoch: int | None = None,
    ) -> None:
        """Run the full dispatch pipeline for a queue item as a spawned task.

        Caller (``_start_print``) returns as soon as it's stamped the queue
        row to ``printing`` and pre-registered the stagger slot, so the next
        check_queue tick can dispatch other printers in parallel. We open
        our own session — the scheduler tick that spawned us is long gone.
        """
        # Lazy import — background_dispatch + print_scheduler have a two-way
        # relationship (dispatcher calls back into scheduler's stagger helper).
        from backend.app.services.background_dispatch import background_dispatch

        async with async_session() as db:
            # Resolve the owner's username so the dispatcher can populate the
            # live "current print user" — set_current_print_user needs both id
            # and name, so passing None here left queue-started prints showing
            # no user on the printer status card even though the archive was
            # correctly attributed via requested_by_user_id (upstream #1670,
            # _propagate_owner_to_printer_manager).
            requested_by_username: str | None = None
            if requested_by_user_id is not None:
                from backend.app.models.user import User

                _owner = await db.get(User, requested_by_user_id)
                requested_by_username = _owner.username if _owner else None
            # Nothing is written above; end the read before a dispatch that may copy a source for minutes
            # (swap-source-lifecycle spec r4 §6: no long transaction during the copy).
            await db.commit()
            try:
                outcome = await background_dispatch.run_from_queue_item(
                    kind=dispatch_kind,
                    source_id=dispatch_source_id,
                    source_name=dispatch_source_name,
                    printer_id=printer_id,
                    printer_name=printer_name,
                    options=options,
                    requested_by_user_id=requested_by_user_id,
                    requested_by_username=requested_by_username,
                    project_id=project_id,
                    project_line_id=project_line_id,
                    queue_item_id=queue_item_id,
                )
            except Exception as e:  # pragma: no cover — belt-and-braces
                logger.exception("Queue item %s: run_from_queue_item raised: %s", queue_item_id, e)
                # Nothing typed came back, so nothing proves the start was refused:
                # never a pending row on a guess. The dispatcher closed its archive.
                if await self._fail_attempt(
                    db,
                    item_id=queue_item_id,
                    claim_started_at=claim_started_at,
                    message=f"Dispatch error: {e}",
                    code=failure_reasons.DISPATCH_INTERNAL_ERROR,
                    queue_id=printer_id,
                ):
                    item = await db.get(PrintQueueItem, queue_item_id)
                    if item is not None:
                        await self._power_off_if_needed(db, item)
                return

            if outcome.get("deferred"):
                from backend.app.services.filament_deferred import defer_claim

                published = outcome.get("send_fact") in ("sent", "accepted")
                restored = await defer_claim(
                    db,
                    item_id=queue_item_id,
                    started_at=outcome.get("claim_started_at"),
                    reason=outcome["reason"]["code"],
                    claim_detail=outcome.get("claim_detail"),
                    revision=outcome.get("revision"),
                    source_archive_id=outcome.get("source_archive_id"),
                    source_library_file_id=outcome.get("source_library_file_id"),
                    source_archive_identity=outcome.get("source_archive_identity"),
                    source_library_identity=outcome.get("source_library_identity"),
                    # The claim queue, never the row's current one (A10).
                    queue_id=printer_id,
                    restore_source=True,
                    disposition=outcome.get("disposition"),
                    execution_archive_id=outcome.get("execution_archive_id"),
                    swap_start_effect=outcome.get("swap_start_effect"),
                    published=published,
                    failure_code=outcome.get("failure_code"),
                )
                await db.commit()
                if restored:
                    from backend.app.services.filament_deferred import DISPOSITION_RETRY_LATER

                    if outcome.get("disposition") == DISPOSITION_RETRY_LATER:
                        # Only after THIS attempt's CAS sent the job back: a stale refusal never cools a newer one.
                        self._capture_cooldowns[queue_item_id] = time.monotonic() + CAPTURE_RETRY_COOLDOWN_SECONDS
                    await self.release_prepared_dispatch(printer_id)
                if restored.terminal:
                    # Committed: THIS attempt's row failed with its class and its queue halted.
                    # Said once — a lost CAS is no refusal of ours, a waiting row no failure.
                    await notification_service.on_queue_job_failed(
                        job_name=job_name_short,
                        printer_id=printer_id,
                        printer_name=printer_name,
                        reason=failure_reasons.dispatch_reason_text(restored.code, restored.message),
                        db=db,
                    )
                    if not published and not restored.swap_started:
                        # The policy of a refusal before the start. After a sent start, or a
                        # swap start that ran or may have, the printer's state is unknown and
                        # no physical action is added: it is left as it is for the operator.
                        item = await db.get(PrintQueueItem, queue_item_id)
                        if item is not None:
                            await self._power_off_if_needed(db, item)
                return

            if outcome.get("uncertain"):
                # The start may be running on the printer and nothing decided what it
                # became: no failure, no pending, no notification — the row keeps its
                # claim for the print's own completion or the operator's recovery.
                logger.error(
                    "Queue item %s: dispatch outcome unknown after the start was sent (%s); leaving it printing",
                    queue_item_id,
                    outcome.get("error"),
                )
                return

            if not outcome.get("success"):
                # Whether the start reached the printer: nothing published means no
                # print failed, and the reservations taken for it go at once.
                published = outcome.get("send_fact") in ("sent", "accepted")
                item = await db.get(PrintQueueItem, queue_item_id)
                if outcome.get("cancelled"):
                    # User-initiated cancel — mark item cancelled and pause
                    # the queue. Don't fire the "queue job failed"
                    # notification: nothing failed, the user stopped it.
                    if item:
                        if await self._cancel_item(
                            db,
                            item,
                            claim_started_at=claim_started_at,
                            queue_id=printer_id,
                            execution_archive_id=outcome.get("archive_id"),
                        ):
                            await self._power_off_if_needed(db, item)
                    if not published:
                        # The pre-registered stagger slot of a start that never went
                        # out would hold its group back for the settle window (G03 E-10).
                        await self.release_prepared_dispatch(printer_id)
                    return
                err = outcome.get("error") or "Dispatch failed"
                # ⚠️ No busy check here (#2598 moved): a refusal before publish
                # arrives typed, through ``deferred``. A printer seen busy NOW may
                # be busy with this very job, and pending would print it twice.
                failed = await self._fail_attempt(
                    db,
                    item_id=queue_item_id,
                    claim_started_at=claim_started_at,
                    message=err,
                    code=outcome.get("failure_code") or failure_reasons.DISPATCH_INTERNAL_ERROR,
                    sent=published,
                    queue_id=printer_id,
                    execution_archive_id=outcome.get("execution_archive_id"),
                    execution_archive_identity=outcome.get("execution_archive_identity"),
                    unsent_attempt=UnsentAttempt.from_runner(outcome, storage_epoch),
                )
                if not failed:
                    return
                if not published:
                    await self.release_prepared_dispatch(printer_id)
                if failed.returned:
                    await ws_manager.send_queue_changed(printer_id)
                await notification_service.on_queue_job_failed(
                    job_name=job_name_short,
                    printer_id=printer_id,
                    printer_name=printer_name,
                    reason=self._failed_dispatch_reason(outcome.get("failure_code"), err, returned=failed.returned),
                    db=db,
                )
                item = await db.get(PrintQueueItem, queue_item_id)
                if item:
                    await self._power_off_if_needed(db, item)
                return

            item = await db.get(PrintQueueItem, queue_item_id)
            if item is None:
                logger.warning("Queue item %s vanished after successful dispatch", queue_item_id)
                return
            logger.info("Queue item %s: Dispatch succeeded — archive_id=%s", queue_item_id, item.archive_id)

            # Watchdog for pre_state → transition, swap-aware. Upstream #1078 —
            # see the big comment block in ``_watchdog_print_start``.
            _post_status = printer_manager.get_status(printer_id)
            _pre_state = _post_status.state if _post_status else None
            _pre_subtask_id = _post_status.subtask_id if _post_status else None
            # Capture gcode_file at dispatch time so the watchdog can distinguish
            # #1150 (slow parse — command landed, file changed on the printer) from
            # #887/#936 (half-broken session — publish swallowed, file unchanged).
            # Used only on the timeout path. Ported from upstream v0.2.4b1 (#1150).
            _pre_gcode_file = _post_status.gcode_file if _post_status else None

            # Hold the printer against further dispatches until the watchdog
            # confirms the printer transitioned (or until the hard timeout).
            # Prevents multi-plate batches from triple-dispatching onto the same
            # H2D Pro while it digests the first project_file (#1157, ported from
            # upstream v0.2.4b1). Marked even when pre_state is None so a
            # disconnected-at-dispatch printer still gets a time-based hold.
            self._mark_printer_dispatched(printer_id, _pre_state, _pre_subtask_id)

            if _pre_state:
                spawn_background_task(
                    self._watchdog_print_start(
                        queue_item_id,
                        printer_id,
                        _pre_state,
                        _pre_subtask_id,
                        # What the swap start DID on this attempt, not what the row asked for: the
                        # dispatcher may have muted or refused it (swap-source-lifecycle spec r4 §8).
                        swap_start_fired=outcome.get("swap_start_effect") in ("fired", "possibly_fired"),
                        pre_gcode_file=_pre_gcode_file,
                    ),
                    name=f"watchdog-print-start-{queue_item_id}",
                )

            # Estimated time for the notification — read from the archive row
            # the dispatcher created (3MF parser fills print_time_seconds).
            estimated_time = None
            if item.archive_id:
                arch_row = await db.get(PrintArchive, item.archive_id)
                if arch_row is not None and arch_row.print_time_seconds:
                    estimated_time = arch_row.print_time_seconds

            await notification_service.on_queue_job_started(
                job_name=job_name_short,
                printer_id=printer_id,
                printer_name=printer_name,
                db=db,
                estimated_time=estimated_time,
            )

            try:
                from backend.app.services.mqtt_relay import mqtt_relay

                await mqtt_relay.on_queue_job_started(
                    job_id=queue_item_id,
                    filename=dispatch_source_name,
                    printer_id=printer_id,
                    printer_name=printer_name,
                    printer_serial=printer_serial,
                )
            except Exception:
                pass  # Don't fail the scheduler if the relay misbehaves.

    @staticmethod
    async def _watchdog_print_start(
        queue_item_id: int,
        printer_id: int,
        pre_state: str,
        pre_subtask_id: str | None = None,
        swap_start_fired: bool = False,
        timeout: float = 90.0,
        poll_interval: float = 3.0,
        pre_gcode_file: str | None = None,
    ) -> None:
        """Fail a queue item whose printer never acknowledges the start command.

        Optimistically we mark the queue item as "printing" right after the MQTT
        project_file publish succeeds locally. If the printer drops/ignores the
        command (half-broken MQTT session — #887/#936), the state never
        transitions and the item would otherwise stay stuck in "printing"
        forever (#967).

        Watchdog exits early on EITHER of two "command landed" signals:
          * ``gcode_state`` advances past ``pre_state`` — the classic signal.
          * ``subtask_id`` advances past ``pre_subtask_id`` — the printer
            echoes the submission_id we minted (``bambu_mqtt._publish_project``,
            #1042) in its next push_status.subtask_id. On H2D Pro firmware
            01.01.00.00 the state stays at FINISH 48–55 s after accepting
            the command, but subtask_id echoes back within the first push —
            without this guard the old 45 s timeout reverted items the
            printer had already started physically printing, which then
            re-dispatched on the next scheduler tick and looked like a
            reprint (upstream #1078).

        ``timeout`` bumped to 90 s (was 45 s) as belt-and-braces for printers
        that neither flip state nor echo subtask_id inside the window —
        genuinely half-broken sessions still revert + force-reconnect as
        before, just a bit later.

        A negative verdict never puts the row back to pending (post-rebuild D-R4):
        it fails with its closed class — ``dispatchAcceptedNotStarted`` when the
        printer's file changed, else ``dispatchNoAcknowledgement`` — and halts the
        queue until Resume, so a start that may still be on its way is never sent
        twice. ``swap_start_fired`` adds the plate warning to that message: our
        swap start already moved the table. The MQTT session is still
        force-reconnected so subsequent commands land.
        """
        deadline = time.monotonic() + timeout
        last_status = None  # Captured for #1150 gcode_file discriminator on timeout
        # ⚠️ Latched, not read at the end: drying can finish, or be stopped by
        # the user, part-way through the dispatch window. What matters is the
        # state the printer was in when it declined to start.
        drying_ams_ids: list[int] = []
        while time.monotonic() < deadline:
            await asyncio.sleep(poll_interval)
            status = printer_manager.get_status(printer_id)
            if not status:
                # Printer disconnected — don't mess with the DB. Drop the
                # in-memory dispatch hold (#1157) too so a fresh dispatch can
                # retry once the printer comes back; the hard timeout would
                # otherwise hold the printer unnecessarily.
                scheduler._release_dispatch_hold(printer_id)
                return
            last_status = status
            drying_ams_ids = drying_ams_ids or _drying_ams_ids(status)
            if status.state in _ACTIVE_PRINT_STATES:
                # Printer is actively processing the job. We do NOT accept
                # arbitrary state transitions: a printer going FINISH → IDLE
                # (user dismissed the post-print prompt without accepting our
                # project_file) would otherwise look like "command landed"
                # and the queue row would stay stuck at 'printing' while the
                # scheduler permanently marked the printer as busy. Upstream
                # Bambuddy #1370 / commit 5680f5d3. Inactive transitions
                # (IDLE / FINISH / FAILED) no longer short-circuit the revert.
                scheduler._release_dispatch_hold(printer_id)
                return
            if pre_subtask_id and status.subtask_id and status.subtask_id != pre_subtask_id:
                # subtask_id advanced — command landed even though state
                # hasn't flipped yet (#1157 release).
                scheduler._release_dispatch_hold(printer_id)
                return

        # Logged on EVERY failed window, not only the last attempt, so a support
        # bundle shows the correlation from the first one rather than only after
        # the retry budget is spent.
        if drying_ams_ids:
            logger.info(
                "Queue item %s: printer %d never started while AMS %s drying - this may be why",
                queue_item_id,
                printer_id,
                ", ".join(str(i) for i in drying_ams_ids),
            )

        # ⚠️ The verdict ENDS the attempt (owner, post-rebuild D-R4): the start was
        # published, so it is never sent again by itself — not even when nothing seems
        # to have arrived. The row fails with its class and the queue halts until
        # Resume; the operator checks the printer and presses Retry. No attempt is
        # spent here: the claim already spent it.
        current_gcode_file = getattr(last_status, "gcode_file", None) if last_status else None
        file_received = current_gcode_file is not None and current_gcode_file != pre_gcode_file
        code = (
            failure_reasons.DISPATCH_ACCEPTED_NOT_STARTED
            if file_received
            else failure_reasons.DISPATCH_NO_ACKNOWLEDGEMENT
        )
        if drying_ams_ids:
            # ⚠️ The generic message below sent the reporter looking at the SD card
            # while the actual obstacle — AMS units in a drying cycle — was on screen
            # the whole time. Name what was observed and let the operator judge it:
            # we do not stop the cycle ourselves, because on this hardware drying can
            # run alongside a print and stopping it may not be the fix. Both
            # possibilities are named rather than one asserted.
            units = ", ".join(f"AMS {i}" for i in drying_ams_ids)
            message = (
                f"The printer did not start the print. {units} "
                f"{'was' if len(drying_ams_ids) == 1 else 'were'} drying throughout - some printers refuse to "
                f"begin a print while an AMS is in a drying cycle, and an AMS drying without its "
                f"external power supply can also leave too little power for the start-of-print calibration. "
                f"Stop the drying, or connect the AMS power supply, then Retry the job."
            )
        elif file_received:
            message = (
                "The printer received the file but had not started printing when the wait ended — it may still "
                "be reading it. Check the printer before you Retry the job."
            )
        else:
            message = (
                "The printer did not respond to the print command. Check its screen for a prompt or error, "
                "confirm its SD card is readable, then Retry the job."
            )
        if swap_start_fired:
            message += " The swap start macro ran before the print command, so check the plate before a Retry."
        scheduler._release_dispatch_hold(printer_id)
        async with async_session() as db:
            item = await db.get(PrintQueueItem, queue_item_id)
            if not item or item.status != "printing":
                return  # Already moved on (completed/cancelled/etc.)
            claim_started_at, execution_archive_id = item.started_at, item.archive_id
            failed = await scheduler._fail_attempt(
                db,
                item_id=queue_item_id,
                claim_started_at=claim_started_at,
                message=message,
                code=code,
                queue_id=printer_id,
                execution_archive_id=execution_archive_id,
            )
        if failed:
            from backend.app.services.background_dispatch import BackgroundDispatchService

            await BackgroundDispatchService._mark_dispatch_archive_terminal(
                execution_archive_id, "failed", message, failure_reason=code
            )
            logger.warning(
                "Queue item %s: printer %d did not start within %.0fs (state still %s) — failed as %s (#967/#2555)",
                queue_item_id,
                printer_id,
                timeout,
                pre_state,
                code,
            )
        # Drop any swap config that was registered post-start_print so a
        # subsequent dispatch can arm it fresh.
        try:
            from backend.app.main import _active_swap_config

            _active_swap_config.pop(printer_id, None)
        except Exception:  # pragma: no cover — import failure is non-fatal
            pass

        # Same #1150 / #887/#936 discriminator as background_dispatch: if the
        # printer's gcode_file changed since pre-dispatch, the project_file
        # command landed and the printer is parsing — a forced reconnect mid-
        # parse triggers 0500_4003 ("can't parse print file") on P1P firmware
        # 01.10.00.00 (slow ~135s parse). If gcode_file is unchanged, the
        # publish was silently swallowed (#887/#936) and the original
        # force_reconnect recovery is what we want. Caveat: in the rare retry-
        # same-file slow-parse scenario the gcode_file looks identical pre/post
        # publish, so the watchdog falls through to reconnect and the user
        # still hits 0500_4003 on that retry — accepted to avoid breaking the
        # half-broken-session recovery path. Ported from upstream v0.2.4b1.
        client = printer_manager.get_client(printer_id)
        if file_received:
            logger.warning(
                "Queue item %s: gcode_file changed to %r (was %r) — printer "
                "received the command and is parsing slowly. Skipping forced "
                "MQTT reconnect to avoid 0500_4003 mid-parse (#1150).",
                queue_item_id,
                current_gcode_file,
                pre_gcode_file,
            )
        elif client and hasattr(client, "force_reconnect_stale_session"):
            client.force_reconnect_stale_session(
                f"queue print command unacknowledged after {timeout:.0f}s "
                f"(state still {pre_state}, gcode_file {current_gcode_file!r})"
            )


# Global scheduler instance
scheduler = PrintScheduler()
