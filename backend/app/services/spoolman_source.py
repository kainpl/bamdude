"""Which Spoolman a write belongs to: generation, guard, base changes and charge ownership (C047 part 2).

Spec ``60-specs/c047-spool-id-never-reissued-spec.md`` (vault), §C1 and §C5.

- **Generation** — a monotonic counter raised by every effective change of the
  source (the three Settings rows: ``spoolman_enabled``, ``spoolman_url``,
  ``spoolman_usage_source``) and by the first token of an address (S42). A
  delete takes it (G0) before reading the rows and admits only on the same G0.
- **Source guard** — a non-reentrant lock. Whoever changes the source, proves
  it for a write (Git SPOOLS, a charge) or cleans up after a delete takes it in
  a CLEAN session — no uncommitted writes, no reserved writer
  (``database.writer_reserved``) — before its first SQL write (S30). Order:
  source guard → SQL transaction / SQLite writer → credential anchor and
  account guards → domain rows.
- **Base change in flight** — set by a change of the Spoolman *database* (a new
  address, ``rotate``) in one synchronous step with the check that no activity
  record is open, BEFORE its fresh SQL read of the durable predicate, and
  cleared after its commit or rollback (S41, S44). First-evidence writers wait
  for it (``spoolman_activity.first_evidence``).
- **Mode change in flight** — set by a change of the mode (``spoolman_enabled``)
  before it waits for the slot-ledger writers already in flight, and cleared
  after its commit or rollback (acceptance A08). Slot-ledger writers wait for it
  (``spoolman_activity.slot_write``); nothing else does — a mode change leaves
  the Spoolman database where it was.
- **Base generation** — raised by every base change that passed its predicate
  (a later rollback leaves it raised: a stamp then only errs towards "unknown").
  A Spoolman number read under one base and written later — a restart replay
  that outlived the hold it was read in — is written only under the same base
  generation (acceptance A01).
- **Charge ownership** — the accounting tail of a finished print (report,
  partial report, runout correction, history, tracking delete) registers here
  before its first write (S40). Full restore and shutdown close the gate and
  drain these owners together with the delete tasks (S35).

The asyncio primitives are bound to the running loop and rebuilt when the loop
changes (a test's new loop), so a module-level runtime never meets "bound to a
different event loop".
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# Reasons of 409 ``spoolman_print_in_progress`` (spec §C1, S43).
CHARGE_WINDOW = "charge_window"
PRINT_ACCOUNTING = "print_accounting"
SLOT_WRITES = "slot_writes"  # a mode change while slot-ledger writers did not finish (acceptance A08)


class SourceRefused(Exception):
    """A change of the source refused while print accounting is open."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(eq=False)
class ChargeOwnership:
    """One registered accounting tail; identity-compared."""

    label: str
    done: asyncio.Event = field(default_factory=asyncio.Event)


class _SourceRuntime:
    def __init__(self) -> None:
        self.generation = 0
        self.base_generation = 0
        self.charge_admitting = True
        self._loop: asyncio.AbstractEventLoop | None = None
        self._lock: asyncio.Lock | None = None
        self._holder: asyncio.Task | None = None
        self._base_in_flight = False
        self._base_clear: asyncio.Event | None = None
        self._mode_in_flight = False
        self._mode_clear: asyncio.Event | None = None
        self._charges: set[ChargeOwnership] = set()

    def _bind(self) -> None:
        loop = asyncio.get_running_loop()
        if loop is not self._loop:
            self._loop = loop
            self._lock = asyncio.Lock()
            self._holder = None
            self._base_in_flight = False
            self._base_clear = asyncio.Event()
            self._base_clear.set()
            self._mode_in_flight = False
            self._mode_clear = asyncio.Event()
            self._mode_clear.set()
            self._charges = set()


_rt = _SourceRuntime()


# --- generation ---------------------------------------------------------------


def generation() -> int:
    return _rt.generation


def bump_generation() -> int:
    """Raise the generation — only the guard's holder does this."""
    require_held()
    _rt.generation += 1
    return _rt.generation


def base_generation() -> int:
    """Which Spoolman database a number read now belongs to (a stamp; see the module docstring)."""
    return _rt.base_generation


def bump_base_generation() -> int:
    """Raise the base generation — only a base change, under the guard, with its flag set."""
    require_held()
    if not _rt._base_in_flight:
        raise RuntimeError("the base generation moves only inside a base change")
    _rt.base_generation += 1
    return _rt.base_generation


# --- the guard ----------------------------------------------------------------


def guard_free() -> bool:
    _rt._bind()
    return not _rt._lock.locked()


def held_by_me() -> bool:
    _rt._bind()
    return _rt._holder is not None and _rt._holder is asyncio.current_task()


def require_held() -> None:
    if not held_by_me():
        raise RuntimeError("the Spoolman source guard is not held by this task")


async def _clean(db) -> None:
    """Refuse a dirty session; end a clean caller's read transaction (spec §C1, S30)."""
    if db is None:
        return
    from backend.app.core.database import writer_reserved  # noqa: PLC0415

    if db.new or db.dirty or db.deleted or writer_reserved(db):
        raise RuntimeError("the Spoolman source guard is taken in a session with uncommitted writes")
    if db.in_transaction():
        await db.commit()


@contextlib.asynccontextmanager
async def guard(db=None):
    """Hold the source guard. Not reentrant: a nested attempt is a programming error."""
    await _clean(db)
    _rt._bind()
    if held_by_me():
        raise RuntimeError("the Spoolman source guard is not reentrant")
    await _rt._lock.acquire()
    _rt._holder = asyncio.current_task()
    try:
        yield
    finally:
        _rt._holder = None
        _rt._lock.release()


# --- refusals of a source change ----------------------------------------------


def refuse_during_charge() -> None:
    """First tier (spec §C1): no effective change while a window or a charge is open."""
    from backend.app.services import spoolman_activity  # noqa: PLC0415

    _rt._bind()
    if spoolman_activity.window_open() or _rt._charges:
        raise SourceRefused(CHARGE_WINDOW)


def begin_base_change() -> None:
    """Step 1 of a base change (S44): check open records and set the flag in ONE synchronous step."""
    from backend.app.services import spoolman_activity  # noqa: PLC0415

    require_held()
    refuse_during_charge()
    if spoolman_activity.records_open():
        raise SourceRefused(PRINT_ACCOUNTING)
    _rt._base_in_flight = True
    _rt._base_clear.clear()


def end_base_change() -> None:
    """Step 4 of a base change: cleared after commit or proven rollback, still under the guard."""
    _rt._bind()
    _rt._base_in_flight = False
    _rt._base_clear.set()


@contextlib.contextmanager
def base_change():
    begin_base_change()
    try:
        yield
    finally:
        end_base_change()


def base_change_in_flight() -> bool:
    _rt._bind()
    return _rt._base_in_flight


async def wait_no_base_change() -> None:
    _rt._bind()
    await _rt._base_clear.wait()


def begin_mode_change() -> None:
    """A change of the mode (acceptance A08): from here no new slot-ledger writer starts.

    Synchronous, under the guard; the change then waits for the writers already in
    flight before it clears the ledger of the mode it leaves.
    """
    require_held()
    _rt._mode_in_flight = True
    _rt._mode_clear.clear()


def end_mode_change() -> None:
    """Cleared after the change's commit or proven rollback, still under the guard."""
    _rt._bind()
    _rt._mode_in_flight = False
    _rt._mode_clear.set()


def mode_change_in_flight() -> bool:
    _rt._bind()
    return _rt._mode_in_flight


async def wait_no_mode_change() -> None:
    _rt._bind()
    await _rt._mode_clear.wait()


# --- charge ownership (S33, S35, S40) ------------------------------------------


def charge_gate_open() -> bool:
    return _rt.charge_admitting


def open_charge_admission() -> None:
    _rt.charge_admitting = True


def close_charge_admission() -> None:
    _rt.charge_admitting = False


def register_charge(label: str) -> ChargeOwnership | None:
    """The gate check and the registration in one synchronous step; None when closed."""
    _rt._bind()
    if not _rt.charge_admitting:
        return None
    ownership = ChargeOwnership(label)
    _rt._charges.add(ownership)
    return ownership


def release_charge(ownership: ChargeOwnership | None) -> None:
    if ownership is None:
        return
    _rt._bind()
    _rt._charges.discard(ownership)
    ownership.done.set()


def charges_open() -> int:
    _rt._bind()
    return len(_rt._charges)


async def quiesce_charges(timeout: float) -> bool:
    """Wait — never under the source guard — until every registered owner ended."""
    _rt._bind()
    if held_by_me():
        raise RuntimeError("the charge drain waits outside the Spoolman source guard")
    pending = [ownership.done.wait() for ownership in list(_rt._charges)]
    if not pending:
        return True
    try:
        await asyncio.wait_for(asyncio.gather(*pending), timeout=timeout)
    except TimeoutError:
        return False
    return not _rt._charges


def reset_for_tests() -> None:
    _rt.generation = 0
    _rt.base_generation = 0
    _rt.charge_admitting = True
    _rt._loop = None
