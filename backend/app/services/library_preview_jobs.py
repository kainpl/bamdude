"""One bounded, volatile STEP thumbnail queue in main (spec §4.1–§4.3, §5; plan A4).

An EXECUTION is one conversion per library file and source fingerprint; a RECEIPT is one actor's claim on it, with
that actor's own rights. Two actors asking for the same file share one CAD; cancelling or losing one receipt
never cancels the execution while another valid receipt remains, and no receipt reveals another's actor.

Nothing here is stored: no table, no replay after a restart. A job's budgets run from its FIRST admission to the
preview slot — 120 s of cumulative active time and 600 s of wall time, both across preemptions — and a separate
1800 s bounds the wait before that admission. Only a sliced attempt preempts an active STEP (the arbiter's rule);
a preempted execution goes back to the head of the queue with its original order, at most three times. A user
cancel, a shutdown or a restore is decided once and a later preemption never overwrites it. A backup pausing the
library's pictures DEFERS an execution it catches before its hand-off — requeued with its order, never counted as
a preemption — and no attempt starts while the pause holds: the runner waits without a slot, a session or a lease,
the wait spends no active budget and resets no limit, and a limit passing meanwhile ends the job as
``backup_in_progress``. The jobs epoch is this service's own random id — a preview service relaunch keeps it, a
main restart or a restore changes it.

Each attempt runs in a child task of the runner, so the runner never becomes the runtime's ``active_task`` and
a preemption cancels no task. No DB session is held while CAD runs.
"""

from __future__ import annotations

import asyncio
import itertools
import logging
import re
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime

from backend.app.services.preview_protocol import STEP_QUEUE_LIMIT

logger = logging.getLogger(__name__)

QUEUE_LIMIT = STEP_QUEUE_LIMIT  # pending executions, the active one included (spec §4.1 formula, ledger A3)
ACTIVE_SECONDS = 120.0
WALL_SECONDS = 600.0
WAIT_BEFORE_START = 1800.0
PREEMPTION_LIMIT = 3
REQUEST_LIMIT = 64
EXECUTION_RECEIPTS = 64
TERMINAL_RECEIPTS = 256
TERMINAL_TTL = 900.0
BUSY_BACKOFF = 1.0
#: an attempt still running this long past the deadline its stage had (admission, or its active budget) is held by
#: owned I/O that will not return — a hung share: its user gets the job's terminal, its owner's end is proven later
STUCK_MARGIN = 30.0
WATCH_TICK = 1.0

STEP_TYPES = frozenset({"step", "stp"})
PURPOSES = ("auto", "manual")  # auto: the picture of a NEW upload; manual: the explicit button
TERMINAL = frozenset({"succeeded", "failed", "canceled"})
#: what a job or the API may answer beside the preview outcomes (spec §6 closed reasons)
JOB_REASONS = frozenset(
    {
        "queue_full",
        "preemption_limit",
        "expired_before_start",
        "job_expired",
        "backup_in_progress",
        "authority_missing",
        "source_unverified",
        "superseded",
        "file_missing",
        "cad_unavailable",
        "cancel_too_late",
    }
)
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")


class Refusal(Exception):
    """A closed reason code for a door or an action (never free text)."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class EpochExpired(Exception):
    """The epoch named belongs to an earlier jobs service: 410 preview_job_expired."""


@dataclass(frozen=True)
class Authority:
    """Who a receipt belongs to — an explicit, immutable descriptor carried from the door. None is never the
    system: a door without one gets authority_missing."""

    kind: str  # "user" | "api_key" | "telegram_chat"
    user_id: int | None = None
    key_id: int | None = None
    provider_id: int | None = None
    chat_row_id: int | None = None
    chat_id: int | None = None
    capture: object | None = None

    @classmethod
    def user(cls, user_id: int, *, capture=None) -> Authority:
        return cls("user", user_id=user_id, capture=capture)

    @classmethod
    def api_key(cls, key_id: int, *, capture=None) -> Authority:
        return cls("api_key", key_id=key_id, capture=capture)

    @classmethod
    def telegram_chat(cls, row_id: int, provider_id: int, chat_id: int) -> Authority:
        return cls("telegram_chat", provider_id=provider_id, chat_row_id=row_id, chat_id=chat_id)

    def same_actor(self, other: Authority) -> bool:
        """Receipt ownership is an actor identity; the original capture still gates its writer."""
        if not isinstance(other, Authority) or self.kind != other.kind:
            return False
        if self.kind == "telegram_chat":
            return (self.chat_row_id, self.provider_id, self.chat_id) == (
                other.chat_row_id,
                other.provider_id,
                other.chat_id,
            )
        from backend.app.core.auth_lifetime import FrozenAuthority

        first, second = self.capture, other.capture
        if not isinstance(first, FrozenAuthority) or not isinstance(second, FrozenAuthority):
            return False
        if not first.account_identity or first.account_identity != second.account_identity:
            return False
        if self.kind == "user":
            return first.kind == second.kind == "session"
        return (
            self.kind == "api_key"
            and first.kind == second.kind == "key"
            and bool(first.key_identity)
            and first.key_identity == second.key_identity
        )


@dataclass(frozen=True)
class StepSource:
    """The library row as it was when the job was asked for — never the mutable row itself."""

    id: int
    filename: str
    file_type: str
    file_path: str
    file_hash: str | None
    fs_modified_at: datetime | None
    thumbnail_path: str | None

    @classmethod
    def capture(cls, row) -> StepSource:
        return cls(**{name: getattr(row, name) for name in cls.__dataclass_fields__})

    @property
    def key(self) -> tuple:
        return (
            self.id,
            self.file_type,
            self.file_path,
            self.file_hash,
            self.fs_modified_at,
            self.thumbnail_path,
        )


@dataclass(eq=False)
class Receipt:
    id: str
    authority: Authority
    purpose: str
    execution: Execution
    created: float
    state: str = "queued"
    error_code: str | None = None
    ended: float | None = None

    @property
    def live(self) -> bool:
        return self.state not in TERMINAL


@dataclass(eq=False)
class Execution:
    id: str
    source: StepSource
    order: int
    enqueued: float
    receipts: list[Receipt] = field(default_factory=list)
    state: str = "queued"
    error_code: str | None = None
    first_admission: float | None = None
    admitted: float | None = None
    active_used: float = 0.0
    preemptions: int = 0
    attempts: int = 0
    wall_bound: bool = False
    handed_off: bool = False
    cause: str | None = None  # user | shutdown | restore | authority — decided once
    attempt: asyncio.Task | None = None

    def live_receipts(self) -> list[Receipt]:
        return [r for r in self.receipts if r.live]


Perform = Callable[..., Awaitable[str]]


class LibraryPreviewJobs:
    def __init__(
        self,
        *,
        perform: Perform | None = None,
        checks=None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        publication=None,
    ):
        self.epoch = uuid.uuid4().hex
        self._publication = publication  # None: library_preview.publication, read at use time
        self.open = False
        self._perform = perform or perform_step
        self._checks = checks or DatabaseChecks()
        self._clock = clock
        self._sleep = sleep
        self._order = itertools.count(1)
        self._pending: dict[tuple, Execution] = {}
        self._receipts: dict[str, Receipt] = {}
        self._wake = asyncio.Event()
        self._runner: asyncio.Task | None = None
        self._stuck: asyncio.Task | None = None  # an attempt whose owned I/O outlived its job: no new reader meanwhile

    # --- lifecycle --------------------------------------------------------------------------------------------

    def start(self) -> None:
        if self._runner is not None:
            raise RuntimeError("library preview jobs already started")
        self.open = True
        self._runner = asyncio.create_task(self._run(), name="library-preview-jobs")

    def alive(self) -> bool:
        return self._runner is not None and not self._runner.done()

    async def stop(self, cause: str = "shutdown") -> bool:
        """Close admission for good, cancel everything before its hand-off, await an owned publication, then the
        runner. True only when every attempt and the runner are over."""
        self.open = False
        if cause == "restore":
            self.epoch = uuid.uuid4().hex  # every receipt of the old epoch answers 410 from here
        for execution in list(self._pending.values()):
            execution.cause = execution.cause or cause
            if execution.state == "queued":
                self._finish(execution, "canceled", None)
            elif execution.attempt is not None and not execution.handed_off:
                execution.attempt.cancel()
        attempts = [e.attempt for e in self._pending.values() if e.attempt is not None]
        if self._stuck is not None and not self._stuck.done():
            self._stuck.cancel()  # owned: it ends when its I/O returns, and only then is the stop proven
            attempts.append(self._stuck)
        if attempts:
            await asyncio.wait(attempts)  # a handed-off attach completes; a cancelled one proves its end
        if self._runner is not None and not self._runner.done():
            self._runner.cancel()
            await asyncio.wait({self._runner})
        return not self.alive() and all(a.done() for a in attempts)

    # --- the doors --------------------------------------------------------------------------------------------

    def enqueue(self, source: StepSource, authority: Authority | None, purpose: str) -> Receipt:
        """A receipt for this actor on the execution of this source — the existing one when this actor asked
        already. Refusals: unavailable, protocol_error, source_unverified, authority_missing, queue_full."""
        if not self.open:
            raise Refusal("unavailable")
        if purpose not in PURPOSES or source.file_type not in STEP_TYPES:
            raise Refusal("protocol_error")
        if not isinstance(source.file_hash, str) or not _SHA256.match(source.file_hash):
            raise Refusal("source_unverified")
        if not isinstance(authority, Authority):
            raise Refusal("authority_missing")
        now = self._clock()
        self._prune(now)
        execution = self._pending.get(source.key)
        if execution is not None:
            for receipt in execution.live_receipts():
                if receipt.authority == authority:
                    return receipt  # the same actor asking again: the same receipt
            if len(execution.live_receipts()) >= EXECUTION_RECEIPTS:
                raise Refusal("queue_full")
        elif len(self._pending) >= QUEUE_LIMIT:
            raise Refusal("queue_full")
        if sum(1 for r in self._receipts.values() if r.live) >= EXECUTION_RECEIPTS * QUEUE_LIMIT:
            raise Refusal("queue_full")
        if execution is None:
            execution = Execution(id=uuid.uuid4().hex[:12], source=source, order=next(self._order), enqueued=now)
            self._pending[source.key] = execution
            logger.info("Preview job=%s file=%s queued", execution.id, source.id)
            self._wake.set()
        receipt = Receipt(id=uuid.uuid4().hex, authority=authority, purpose=purpose, execution=execution, created=now)
        execution.receipts.append(receipt)
        self._receipts[receipt.id] = receipt
        if execution.state == "running":
            receipt.state = "running"
        return receipt

    def lookup(self, epoch: str, receipt_id: str) -> Receipt | None:
        """The receipt of this epoch, or None (unknown). An earlier epoch raises EpochExpired. Holding an id is no
        right to its metadata — the caller checks the receipt's actor and the file first."""
        if epoch != self.epoch:
            raise EpochExpired()
        self._prune(self._clock())
        return self._receipts.get(receipt_id)

    def cancel(self, receipt: Receipt) -> Receipt:
        """Detach this receipt. The execution is cancelled only when no other live receipt remains; after the
        hand-off of its result nothing is cancelled (cancel_too_late) and the real terminal follows."""
        if not receipt.live:
            return receipt
        execution = receipt.execution
        if execution.handed_off:
            raise Refusal("cancel_too_late")
        self._end_receipt(receipt, "canceled", None)
        if not execution.live_receipts():
            execution.cause = execution.cause or "user"
            if execution.state == "queued":
                self._finish(execution, "canceled", None)
            elif execution.attempt is not None:
                execution.attempt.cancel()
        return receipt

    @staticmethod
    def describe(receipt: Receipt) -> dict:
        """What a receipt's reader may see: never the actor, never another receipt."""
        return {
            "receipt_id": receipt.id,
            "file_id": receipt.execution.source.id,
            "state": receipt.state,
            "error_code": receipt.error_code,
        }

    # --- the runner -------------------------------------------------------------------------------------------

    def _gate(self):
        if self._publication is not None:
            return self._publication
        from backend.app.services import library_preview

        return library_preview.publication

    def _head(self) -> Execution | None:
        queued = [e for e in self._pending.values() if e.state == "queued"]
        return min(queued, key=lambda e: e.order) if queued else None

    async def _run(self) -> None:
        while True:
            if self._stuck is not None:
                if not self._stuck.done():
                    # no new reader on a share that already holds one; the queue still ends on time
                    for queued in [e for e in self._pending.values() if e.state == "queued"]:
                        self._expired(queued)
                    await asyncio.wait({self._stuck}, timeout=WATCH_TICK)
                    continue
                self._stuck = None
            execution = self._head()
            if execution is None:
                self._wake.clear()
                await self._wake.wait()
                continue
            if self._expired(execution):
                continue
            gate = self._gate()
            if gate.paused() or not gate.open:
                await self._wait_for_publication(gate)
                continue  # the head, its limits and its prechecks are asked again
            if not await self._prechecks(execution):
                continue
            outcome = await self._attempt(execution)
            if execution.state != "running":
                continue  # finished meanwhile (a stop)
            await self._settle(execution, outcome)

    def _expired(self, execution: Execution, *, backup: bool = False) -> bool:
        """``backup``: the job was held by a backup's pause of the pictures when its limit passed — it says so,
        never that its file was at fault (Codex A-r3 §5). The active budget is the CAD's own: still ``timeout``."""
        now = self._clock()
        if execution.first_admission is None and now - execution.enqueued >= WAIT_BEFORE_START:
            self._finish(execution, "failed", "backup_in_progress" if backup else "expired_before_start")
        elif execution.first_admission is not None and now - execution.first_admission >= WALL_SECONDS:
            self._finish(execution, "failed", "backup_in_progress" if backup else "job_expired")
        elif execution.active_used >= ACTIVE_SECONDS:
            self._finish(execution, "failed", "timeout")
        else:
            return False
        return True

    async def _wait_for_publication(self, gate) -> None:
        """A backup is copying the library's pictures, or a restore closed them: nothing could be published, so
        no attempt starts — no stat, no slot, no CAD (Codex A-r3, the owner's (b)). Wait without a slot, a session
        or a lease while the queue still ends on time, until the pause lifts; a closed gate waits for the stop
        that follows it. The wait spends no active budget and resets no limit."""
        logger.info("Preview jobs wait: the library pictures are %s", "paused by a backup" if gate.open else "closed")
        while self._head() is not None and (gate.paused() or not gate.open):
            for queued in [e for e in self._pending.values() if e.state == "queued"]:
                self._expired(queued, backup=gate.open)
            waiter = asyncio.ensure_future(gate.until_unpaused() if gate.open else asyncio.Event().wait())
            try:
                await asyncio.wait({waiter}, timeout=WATCH_TICK)
            finally:
                waiter.cancel()
                await asyncio.gather(waiter, return_exceptions=True)

    async def _prechecks(self, execution: Execution) -> bool:
        """Read-only, before any CAD: the receipts whose actor still holds its right, and the live source."""
        try:
            for receipt in execution.live_receipts():
                if not await self._checks.authority_holds(receipt.authority, receipt.purpose):
                    self._end_receipt(receipt, "failed", "authority_missing")
            if not execution.live_receipts():
                execution.cause = execution.cause or "authority"
                self._finish(execution, "failed", "authority_missing")
                return False
            if not await self._checks.source_holds(execution.source):
                self._finish(execution, "failed", "superseded")
                return False
        except Exception:
            logger.warning("Preview job=%s prechecks failed", execution.id, exc_info=True)
            self._finish(execution, "failed", "unavailable")
            return False
        return execution.state == "queued"

    async def _before_attach(self, execution: Execution, session=None) -> bool:
        """At least one receipt whose actor still holds its right; the others get nothing (spec §5). With the
        attach's own session the rights are read inside that transaction, before its write."""
        try:
            if session is not None:
                marker = ("preview_authority_guard", execution.id)
                if marker not in session.info or session.info[marker] is not session.get_transaction():
                    from backend.app.services.domain_authority import captured, guard

                    receipts = sorted(
                        execution.live_receipts(), key=lambda r: getattr(captured(r.authority), "account_identity", "")
                    )
                    for receipt in receipts:
                        try:
                            await guard(session, receipt.authority, permissions=(_permission(receipt.purpose),))
                        except Exception:
                            self._end_receipt(receipt, "failed", "authority_missing")
                    session.info[marker] = session.get_transaction()
            for receipt in execution.live_receipts():
                if not await self._checks.authority_holds(receipt.authority, receipt.purpose, session=session):
                    self._end_receipt(receipt, "failed", "authority_missing")
        except Exception:
            logger.warning("Preview job=%s authority recheck failed", execution.id, exc_info=True)
            return False
        return bool(execution.live_receipts())

    async def _attempt(self, execution: Execution) -> str:
        now = self._clock()
        if execution.first_admission is None:
            wait = WAIT_BEFORE_START - (now - execution.enqueued)
        else:
            wait = WALL_SECONDS - (now - execution.first_admission)
        execution.state = "running"
        execution.attempts += 1
        execution.wall_bound = False
        for receipt in execution.live_receipts():
            receipt.state = "running"

        def on_admit() -> int:
            admitted = self._clock()
            execution.admitted = admitted
            if execution.first_admission is None:
                execution.first_admission = admitted
                logger.info("Preview job=%s file=%s admitted", execution.id, execution.source.id)
            active_left = ACTIVE_SECONDS - execution.active_used
            wall_left = execution.first_admission + WALL_SECONDS - admitted
            execution.wall_bound = wall_left <= active_left
            return time.monotonic_ns() + int(max(0.0, min(active_left, wall_left)) * 1e9)

        def on_hand_off() -> None:
            execution.handed_off = True

        from backend.app.services.preview_protocol import PREVIEW_OWNER

        # the attempt's log lines name this job and file beside the attempt id (the task copies this context)
        owner = PREVIEW_OWNER.set(f"job={execution.id} file={execution.source.id}")
        try:
            task = asyncio.create_task(
                self._perform(
                    execution.source,
                    order=execution.order,
                    wait_seconds=max(0.0, wait),
                    on_admit=on_admit,
                    on_hand_off=on_hand_off,
                    before_attach=lambda session=None: self._before_attach(execution, session),
                ),
                name=f"library-preview-job-{execution.id}",
            )
        finally:
            PREVIEW_OWNER.reset(owner)
        execution.attempt = task
        try:
            if not await self._supervise(execution, task):  # the child's cancellation never propagates into it
                return "stuck"
        except asyncio.CancelledError:
            if not execution.handed_off:
                task.cancel()
            await asyncio.wait({task})
            raise
        finally:
            if execution.admitted is not None:
                execution.active_used += self._clock() - execution.admitted
                execution.admitted = None
            execution.attempt = None
        if task.cancelled():
            return "canceled"
        if task.exception() is not None:
            logger.warning("Preview job=%s attempt failed", execution.id, exc_info=task.exception())
            return "unavailable"
        return task.result()

    def _overdue(self, execution: Execution) -> str | None:
        """The terminal an attempt that runs past every deadline of its stage gives its job, else None."""
        now = self._clock()
        if execution.admitted is None:  # still before its admission: the first wait, or the wall of a retry
            if execution.first_admission is None:
                limit, reason = execution.enqueued + WAIT_BEFORE_START, "expired_before_start"
            else:
                limit, reason = execution.first_admission + WALL_SECONDS, "job_expired"
        else:
            active_left = ACTIVE_SECONDS - execution.active_used
            limit = execution.admitted + max(0.0, active_left)
            reason = "job_expired" if execution.wall_bound else "timeout"
        return reason if now >= limit + STUCK_MARGIN else None

    async def _supervise(self, execution: Execution, task: asyncio.Task) -> bool:
        """Wait for the attempt; True when it ended. One held past every deadline of its stage by owned I/O (a
        hung share) gives its job the terminal the user is owed and stays tracked as ``_stuck``: False."""
        while True:
            done, _ = await asyncio.wait({task}, timeout=WATCH_TICK)
            if task in done:
                return True
            reason = self._overdue(execution)
            if reason is not None:
                self._finish(execution, "failed", reason)
                self._stuck = task
                logger.warning("Preview job=%s attempt overdue (%s): its owned I/O still runs", execution.id, reason)
                return False

    async def _settle(self, execution: Execution, outcome: str) -> None:
        if execution.cause is not None and outcome != "succeeded":
            self._finish(execution, "canceled", None)  # user/shutdown/restore: never a requeue
            return
        if outcome == "succeeded":
            self._finish(execution, "succeeded", None)
        elif outcome == "preempted":
            execution.preemptions += 1
            execution.handed_off = False
            if execution.preemptions >= PREEMPTION_LIMIT:
                self._finish(execution, "failed", "preemption_limit")
            else:
                self._requeue(execution)  # the head again: its order is unchanged
        elif outcome == "publication_deferred":
            # a backup began while it converted: its picture was not published, its end proven, the slot freed.
            # Requeued with its order and never a preemption; the runner waits out the pause before any retry.
            execution.handed_off = False
            logger.info(
                "Preview job=%s file=%s deferred: a backup is copying the pictures", execution.id, execution.source.id
            )
            self._requeue(execution)
        elif outcome == "busy":
            self._requeue(execution)
            await self._sleep(BUSY_BACKOFF)  # the wait budgets end it at the head of the loop
        elif outcome == "timeout":
            self._finish(execution, "failed", "job_expired" if execution.wall_bound else "timeout")
        elif outcome == "canceled":
            self._finish(execution, "canceled", None)
        else:
            self._finish(execution, "failed", outcome)

    def _requeue(self, execution: Execution) -> None:
        execution.state = "queued"
        for receipt in execution.live_receipts():
            receipt.state = "queued"
        self._wake.set()

    # --- bookkeeping ------------------------------------------------------------------------------------------

    def _end_receipt(self, receipt: Receipt, state: str, code: str | None) -> None:
        receipt.state, receipt.error_code, receipt.ended = state, code, self._clock()

    def _finish(self, execution: Execution, state: str, code: str | None) -> None:
        if execution.state in TERMINAL:
            return
        execution.state, execution.error_code = state, code
        for receipt in execution.live_receipts():
            self._end_receipt(receipt, state, code)
        self._pending.pop(execution.source.key, None)
        logger.info(
            "Preview job=%s file=%s outcome=%s reason=%s attempts=%d preemptions=%d active_s=%.1f",
            execution.id,
            execution.source.id,
            state,
            code,
            execution.attempts,
            execution.preemptions,
            execution.active_used,
        )
        self._prune(self._clock())

    def _prune(self, now: float) -> None:
        ended = sorted((r for r in self._receipts.values() if not r.live), key=lambda r: r.ended or 0.0)
        excess = len(ended) - TERMINAL_RECEIPTS
        for index, receipt in enumerate(ended):
            if index < excess or (receipt.ended is not None and now - receipt.ended > TERMINAL_TTL):
                self._receipts.pop(receipt.id, None)


# --- production checks and the real attempt ---------------------------------------------------------------------


class DatabaseChecks:
    """Read-only checks in short sessions of their own; nothing is written (never resolve_api_key_authority,
    whose last_used write takes SQLite's writer)."""

    async def authority_holds(self, authority: Authority, purpose: str, session=None) -> bool:
        if session is not None:  # inside the attach's own transaction
            return await authority_holds(session, authority, purpose)
        from backend.app.core import database

        async with database.async_session() as db:
            return await authority_holds(db, authority, purpose)

    async def source_holds(self, source: StepSource) -> bool:
        from sqlalchemy import select

        from backend.app.core import database
        from backend.app.models.library import LibraryFile

        async with database.async_session() as db:
            row = (
                await db.execute(
                    select(
                        LibraryFile.file_type,
                        LibraryFile.file_path,
                        LibraryFile.file_hash,
                        LibraryFile.fs_modified_at,
                        LibraryFile.thumbnail_path,
                    ).where(LibraryFile.id == source.id, LibraryFile.deleted_at.is_(None))
                )
            ).first()
        return row is not None and (source.id, *row) == source.key


def _permission(purpose: str) -> str:
    from backend.app.core.permissions import Permission

    return (Permission.LIBRARY_UPLOAD if purpose == "auto" else Permission.LIBRARY_UPDATE_ALL).value


async def authority_holds(db, authority: Authority, purpose: str) -> bool:
    """The actor's CURRENT right for this purpose, read on ``db`` with no cache: auto (a new upload's picture) asks
    library:upload, manual asks library:update_all."""
    from datetime import timezone

    from fastapi import HTTPException
    from sqlalchemy import select
    from sqlalchemy.orm import selectinload

    permission = _permission(purpose)
    if authority.kind in ("user", "api_key"):
        from backend.app.services.domain_authority import fresh

        try:
            await fresh(db, authority, permissions=(permission,))
            return True
        except HTTPException:
            return False
    if authority.kind == "user":
        from backend.app.models.user import User

        user = (
            await db.execute(
                select(User)
                .options(selectinload(User.groups))
                .where(User.id == authority.user_id)
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        return user is not None and user.is_active and user.has_permission(permission)
    if authority.kind == "api_key":
        from backend.app.core.auth import authorize_api_key
        from backend.app.core.permissions import Permission
        from backend.app.models.api_key import APIKey

        key = await db.get(APIKey, authority.key_id, populate_existing=True) if authority.key_id else None
        if key is None or not key.enabled:
            return False
        if key.expires_at is not None:
            expires = key.expires_at if key.expires_at.tzinfo else key.expires_at.replace(tzinfo=timezone.utc)
            if expires < datetime.now(timezone.utc):
                return False
        try:
            await authorize_api_key(db, key, [Permission(permission)])  # the key and its owner, read only
        except HTTPException:
            return False
        return True
    if authority.kind == "telegram_chat":
        from backend.app.models.notification import NotificationProvider
        from backend.app.models.telegram_chat import TelegramChat

        chat = (
            await db.execute(
                select(TelegramChat)
                .options(selectinload(TelegramChat.group))
                .where(TelegramChat.id == authority.chat_row_id)
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if (
            chat is None
            or not chat.is_active
            or chat.provider_id != authority.provider_id
            or chat.chat_id != authority.chat_id
            or purpose != "auto"  # a chat only ever gets the picture of its own new upload
        ):
            return False
        provider = await db.get(NotificationProvider, chat.provider_id, populate_existing=True)
        if provider is None or not provider.enabled or provider.provider_type != "telegram":
            return False
        return chat.has_permission(permission)
    return False


def source_state(path) -> str | None:
    """None when the source can be read; file_missing ONLY for a positive ENOENT on it; anything else (a
    permission, a hung share's error) is unavailable — never a missing file (spec §6)."""
    import os

    try:
        os.stat(path)
    except FileNotFoundError:
        return "file_missing"
    except OSError:
        return "unavailable"
    return None


_ATTACHED = {"attached": "succeeded", "superseded": "superseded", "authority_missing": "authority_missing"}


async def perform_step(source: StepSource, *, order, wait_seconds, on_admit, on_hand_off, before_attach) -> str:
    """One real attempt: snapshot through the low slot (its hand-off takes a publication lease), digest equality
    with the row's hash, then the main-only attach in a fresh short session whose transaction reads the actors'
    rights again before its CAS write. No session is open while CAD runs."""
    from backend.app.api.routes.library import to_absolute_path
    from backend.app.core import database
    from backend.app.services import library_preview
    from backend.app.services.preview_artifacts import disk
    from backend.app.services.preview_runtime import preview_attempt

    began = time.monotonic()
    path = to_absolute_path(source.file_path)
    if path is None:
        return "file_missing"
    state = await disk(source_state, path)
    if state is not None:
        return state
    left = wait_seconds - (time.monotonic() - began)  # the stat spent its share of the one wait (Codex A-R3)
    if left <= 0:
        return "busy"  # never admitted: the runner's own clock names the terminal (expired_before_start, ...)
    async with preview_attempt(
        {"mesh": (path, source.file_type)},
        priority="low",
        wait_seconds=left,
        order=order,
        on_admit=on_admit,
        on_hand_off=on_hand_off,
        publication=library_preview.publication,
    ) as result:
        if result.outcome == "source_changed":
            return "superseded"  # its bytes moved while they were read: the row will name new ones
        if result.outcome != "ok":
            return result.outcome  # publication_deferred included: the runner requeues it
        if not result.publishable:
            return "unavailable"  # no lease, no attach (an ok of a required publication always holds one)
        png = result.files.get("preview")
        if png is None or result.input_digests.get("mesh") != source.file_hash:
            return "superseded"  # the bytes read are not the bytes the row names
        library_source = library_preview.Source(
            id=source.id,
            filename=source.filename,
            file_type=source.file_type,
            file_path=source.file_path,
            file_hash=source.file_hash,
            thumbnail_path=source.thumbnail_path,
            fs_modified_at=source.fs_modified_at,
        )
        async with database.async_session() as db:
            attached = await library_preview.attach_result(db, library_source, png, authorize=before_attach)
        return _ATTACHED.get(attached, "unavailable")


# --- the singleton ------------------------------------------------------------------------------------------------

jobs: LibraryPreviewJobs | None = None


def start_library_preview_jobs(**kwargs) -> LibraryPreviewJobs:
    global jobs
    if jobs is not None and jobs.alive():
        raise RuntimeError("library preview jobs are running")
    jobs = LibraryPreviewJobs(**kwargs)
    jobs.start()
    return jobs


async def stop_library_preview_jobs(cause: str = "shutdown") -> bool:
    """Stop for good; the stopped service stays so its old receipts answer 410 until the process restarts."""
    if jobs is None:
        return True
    return await jobs.stop(cause)


def get_library_preview_jobs() -> LibraryPreviewJobs | None:
    return jobs


_left_running: set[asyncio.Future] = set()


async def stop_library_preview_jobs_within(cause: str, seconds: float) -> bool:
    """The stop, bounded: a job inside an owned read of a hung share cannot be cancelled out of it. Past ``seconds``
    the stop goes on running and proves itself later; the caller is answered False now — a restore is refused
    instead of hanging with the share, a shutdown goes on without the proof. A stop still running is shared: a
    shutdown after a refused restore waits for the same one, never a second."""
    pending = [future for future in _left_running if not future.done()]
    if pending:
        stopping = pending[0]
    else:
        stopping = asyncio.ensure_future(stop_library_preview_jobs(cause))
        _left_running.add(stopping)
        stopping.add_done_callback(_left_running.discard)
    done, _ = await asyncio.wait({stopping}, timeout=seconds)
    return stopping in done and not stopping.cancelled() and stopping.exception() is None and stopping.result()


async def quiesce_library_previews_for_restore(*, drain_seconds: float = 30.0) -> bool:
    """Before a restore touches files or the database (spec §4.3): close library preview publication for good
    (legacy STL/OBJ and sliced-source attaches included), stop the jobs with a new epoch, wait for every lease
    already handed out to finish its owned attach, then stop the preview runtime. True only with the proof of
    all of it; the caller refuses the restore otherwise. Nothing reopens until the process restarts."""
    from backend.app.services import library_preview
    from backend.app.services.preview_runtime import stop_preview_runtime_within

    library_preview.publication.close()
    jobs_quiet = await stop_library_preview_jobs_within("restore", drain_seconds)
    drained = await library_preview.publication.drain(drain_seconds)
    runtime_quiet = await stop_preview_runtime_within(drain_seconds)  # bounded too: a stuck snapshot keeps its slot
    proven = jobs_quiet and drained and runtime_quiet
    logger.info(
        "Library previews quiesced for restore: jobs=%s publications=%s runtime=%s", jobs_quiet, drained, runtime_quiet
    )
    return proven


def reset_library_preview_jobs() -> None:
    """Tests only: refused while a runner or an attempt is alive."""
    global jobs
    if jobs is not None and (jobs.alive() or any(e.attempt is not None for e in jobs._pending.values())):
        raise RuntimeError("library preview jobs still running")
    jobs = None
