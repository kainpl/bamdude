"""Writer/reader for the append-only per-print usage journal.

The journal (``print_usage_events``, m153) is the persisted record of which
spool fed which layers: tray changes, runouts, replacement loads, pause/resume.
Spool identity is FROZEN at event time via :func:`freeze_spool_ids` — both
inventory backends in the same row — so the completion path attributes
segments without re-asking the DB after the fact.

Retention: :func:`prune_finished` runs in main.py's periodic cleanup loop
(``usage_events_retention_hours`` setting); :func:`delete_for_archive` covers
the archive hard-delete path, because the FK CASCADE on the table fires on
PostgreSQL only — this codebase never sets ``PRAGMA foreign_keys``.
"""

import asyncio
import logging
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models.print_usage_event import PrintUsageEvent

logger = logging.getLogger(__name__)

# One per printer: every journal check-and-append (tray change, runout, the tail facts, an
# assignment's identity note) runs under it, so two of them never decide on the same rows
# at once and their rows land in the order they were observed. Taken FIRST by a callback
# (callbacks are scheduled in arrival order, and the lock is FIFO) and after
# ``_get_ams_assignment_lock`` by an assignment — never the other way round. The functions
# below expect the caller to hold it; none takes it itself.
#
# It is also where a restart seed's replay reaches the journal (post-rebuild F3): entering it
# writes the printer's pending replays first, each whole in a transaction of its own, so no
# holder — a live callback, an assignment, the completion — writes anything before them. Every
# holder takes it before its session writes (SQLite's writer is never held while waiting).
_journal_locks: dict[int, tuple[asyncio.AbstractEventLoop, asyncio.Lock]] = {}


def journal_lock(printer_id: int) -> "JournalLock":
    """The per-printer journal lock (see above), created on first use in the running loop.

    One loop in production; a lock is bound to the loop it first waited in, so a new
    loop (each test has its own) gets a new lock instead of one it cannot wait on.
    """
    loop = asyncio.get_running_loop()
    entry = _journal_locks.get(printer_id)
    if entry is None or entry[0] is not loop:
        entry = _journal_locks[printer_id] = (loop, asyncio.Lock())
    return JournalLock(printer_id, entry[1])


class JournalLock:
    """``async with journal_lock(printer_id)``: the lock, then the printer's pending replays.

    Every journal write is a first Spoolman fact of its print (C047 S41, S44): before the
    lock — the owner's boundary, ahead of the shielded replay and the body — the holder
    waits until no change of the Spoolman database is in flight and publishes its
    activity record, kept until the lock is released (after the holder's commit).
    """

    __slots__ = ("_evidence", "_lock", "_printer_id")

    def __init__(self, printer_id: int, lock: asyncio.Lock) -> None:
        self._printer_id = printer_id
        self._lock = lock
        self._evidence = None

    def locked(self) -> bool:
        return self._lock.locked()

    async def __aenter__(self) -> "JournalLock":
        from backend.app.services import spoolman_activity

        evidence = spoolman_activity.first_evidence("usage_journal")
        await evidence.__aenter__()
        try:
            await self._lock.acquire()
        except BaseException:
            await evidence.__aexit__(None, None, None)
            raise
        try:
            await deliver_pending_replay(self._printer_id)
        except BaseException:
            self._lock.release()
            await evidence.__aexit__(None, None, None)
            raise
        self._evidence = evidence
        return self

    async def __aexit__(self, *_exc) -> None:
        self._lock.release()
        evidence, self._evidence = self._evidence, None
        if evidence is not None:
            await evidence.__aexit__(None, None, None)


@dataclass
class _PendingReplay:
    """A replay waiting for its write: the batch, the archive it was seeded for (when the seed
    knew it) and how many of its writes failed."""

    batch: Any
    archive_id: int | None = None
    failures: int = 0


# The replays restart seeds built that no transaction has written yet, per printer, in the
# order they were built; and the writer running now — a holder cancelled while it waited
# leaves it to finish, and the next holder waits for it (post-rebuild F3).
_pending_replays: dict[int, list[_PendingReplay]] = {}
_replay_writers: dict[int, asyncio.Task] = {}
# An unknown feed the journal stated while the printer had no client (post-rebuild F5):
# ``printer_id → (journal generation, layer)``, told to the client of that print by the next
# holder of the journal lock that finds one — so the next tray report counts as a feed again.
# Accounting never waits for it: the journal's marker governs the chronology the writers read.
_unstated_unknowns: dict[int, tuple[int, int]] = {}


def queue_replay(printer_id: int, batches, *, archive_id: int | None = None) -> None:
    """Take over the replays a client built (``ReplayBatch``) for the print of ``archive_id``;
    the journal lock writes them first."""
    from backend.app.services.bambu_mqtt import ReplayBatch

    kept = [b for b in (batches if isinstance(batches, list) else []) if isinstance(b, ReplayBatch) and b.facts]
    if kept:
        _pending_replays.setdefault(printer_id, []).extend(_PendingReplay(b, archive_id) for b in kept)


async def settle_pending_replay(printer_id: int) -> None:
    """Before a completion — or the sweep that may close the print — reads the chronology or the
    journal: write what waits, else state it unknown; nothing of the print is left waiting
    (post-rebuild F4). The caller holds ``journal_lock`` and entered it (or the release it
    makes wrote) just before, so this is each waiting batch's second try at least — and a
    second failure states it unknown."""
    await deliver_pending_replay(printer_id)


async def deliver_pending_replay(printer_id: int) -> None:
    """Write the printer's pending replays, in order; the caller holds ``journal_lock``.

    Each batch goes in one transaction of its own and leaves the queue only once committed: a
    write that fails or rolls back leaves it to the next holder of the lock, whole. The writer
    is shielded, so a holder cancelled while it waits never cuts a batch between its commit and
    its removal — a batch is written exactly once. A batch whose write fails a second time is
    not left behind a holder that would write past it or a completion that would charge
    without it: the feed from its first fact is stated unknown (``_state_unrecorded``,
    post-rebuild F4). Raises only on cancellation.
    """
    loop = asyncio.get_running_loop()
    running = _replay_writers.get(printer_id)
    if running is not None and running.get_loop() is loop and not running.done():
        await asyncio.shield(running)
    client, generation, _subtask = _observed_print(printer_id)
    if client is not None:
        unstated = _unstated_unknowns.pop(printer_id, None)
        if unstated is not None and unstated[0] == generation:
            try:
                client.state_feed_unknown_from(*unstated)
            except Exception:  # noqa: BLE001 - the journal's marker still governs the accounting
                logger.debug("[UsageJournal] Printer %d: the client could not take the unknown feed", printer_id)
        try:
            queue_replay(printer_id, client.take_replay_batches())
        except Exception:  # noqa: BLE001 - a client that cannot hand them over keeps them
            logger.debug("[UsageJournal] Printer %d: no replay to take from the client", printer_id)
    if not _pending_replays.get(printer_id):
        return
    writer = _start_replay_writer(loop, printer_id)
    await asyncio.shield(writer)


def _start_replay_writer(loop: asyncio.AbstractEventLoop, printer_id: int) -> asyncio.Task:
    """The shielded writer, owning its own Spoolman activity record (C047 acceptance A01).

    The record is published before the task exists — while the caller's own record (the
    journal lock's first evidence) is still open, so the protection never lapses — and it
    ends when the task is done: after its commit, or its proven rollback, or a cancellation
    before it ran. A holder cancelled while it waits therefore never leaves a writer
    unprotected: a change of the Spoolman database keeps refusing until the write is over.
    Synchronous: nothing can run between the record and the task.
    """
    from backend.app.services import spoolman_activity, spoolman_source

    if spoolman_source.base_change_in_flight():
        # Only a holder of the journal lock reaches here, and it was admitted with no base
        # change in flight, which none can start while its record is open.
        raise RuntimeError("a restart replay started while a change of the Spoolman database was in flight")
    release = spoolman_activity.hold("usage_journal_replay")
    try:
        writer = loop.create_task(_write_pending_replays(printer_id), name=f"usage-journal-replay-{printer_id}")
    except BaseException:
        release()
        raise
    writer.add_done_callback(lambda _task: release())
    _replay_writers[printer_id] = writer
    return writer


async def _write_pending_replays(printer_id: int) -> None:
    from backend.app.core import database

    pending = _pending_replays.get(printer_id, [])
    while pending:
        entry = pending[0]
        try:
            async with database.async_session() as db:
                written = await _write_replay_batch(db, printer_id, entry.batch)
                if written is not None:
                    await db.commit()
                    pending.pop(0)
        except Exception:
            entry.failures += 1
            if entry.failures < 2:
                logger.exception(
                    "[UsageJournal] The restart replay of printer %d (%d facts) could not be written; "
                    "the next holder of the journal lock writes it",
                    printer_id,
                    len(entry.batch.facts),
                )
                return
            logger.exception(
                "[UsageJournal] The restart replay of printer %d (%d facts) could not be written again",
                printer_id,
                len(entry.batch.facts),
            )
            await _state_unrecorded(printer_id, entry, reason="write_failed")
            pending.pop(0)
            continue
        if written is None:
            # No printing archive of its print takes it any more: whatever its accounting was, it
            # ran without these facts — say so in that print's journal, never drop it silently.
            await _state_unrecorded(printer_id, entry, reason="print_not_printing")
            pending.pop(0)
            continue
        logger.info(
            "[UsageJournal] Printer %d: restart replay written (%d of %d facts)",
            printer_id,
            written,
            len(entry.batch.facts),
        )
    _pending_replays.pop(printer_id, None)


async def _write_replay_batch(db: AsyncSession, printer_id: int, batch) -> int | None:
    """Every fact of ``batch``, in order, into the archive of the print it ran for; no commit.
    None when no printing archive of that print takes it.

    A Spoolman number the batch names was read when its bindings were (the seed's snapshot, a
    held change) — possibly in an earlier hold of the lock than this write. It is written only
    while the Spoolman database is still the one it was read under (``bindings_stamp``); else
    that fact's Spoolman spool is unknown — a frozen ``None``, which no writer fills from the
    binding of the moment — and the built-in spool keeps its name (C047 acceptance A01). The
    writer's own activity record keeps the database from changing until the commit.
    """
    from backend.app.services import spoolman_source

    archive_id = await tail_archive(db, printer_id, generation=batch.generation, subtask_id=batch.subtask_id)
    if archive_id is None:
        return None
    stamp = getattr(batch, "bindings_stamp", None)
    same_base = stamp is not None and stamp == spoolman_source.base_generation()
    unproven = 0
    for fact in batch.facts:
        if not same_base and fact.ids is not None and fact.ids[1] is not None:
            fact = replace(fact, ids=(fact.ids[0], None))
            unproven += 1
        await _write_replay_fact(db, printer_id, archive_id, fact)
    if unproven:
        logger.warning(
            "[UsageJournal] Printer %d: %d replayed fact(s) named a Spoolman spool read under another Spoolman "
            "database (or unstamped); their Spoolman spool is recorded as unknown",
            printer_id,
            unproven,
        )
    return len(batch.facts)


async def _state_unrecorded(printer_id: int, entry: _PendingReplay, *, reason: str) -> None:
    """A replay that never reached the journal (post-rebuild F4): what it observed from its first
    fact is stated unknown — in the chronology the completion's writers read (the client, when
    it is still on that print) and in the print's journal — so no writer charges those grams to
    a reel by the binding of the moment; what came before keeps its reels. Never raises."""
    from backend.app.core import database
    from backend.app.utils.usage_attribution import UNKNOWN_FEED

    batch = entry.batch
    layer = min(fact.layer for fact in batch.facts)
    client, generation, _subtask = _observed_print(printer_id)
    stated = False
    if client is not None and generation == batch.generation:
        try:
            stated = client.state_feed_unknown_from(batch.generation, layer) is True
        except Exception:  # noqa: BLE001 - the journal row below still says it
            stated = False
    elif client is None:
        # No client to tell now: the journal's marker governs the accounting, and the next
        # holder of the lock that finds this print's client tells it (post-rebuild F5).
        _unstated_unknowns[printer_id] = (batch.generation, layer)
    archive_id = entry.archive_id
    try:
        async with database.async_session() as db:
            if archive_id is None:
                archive_id = await tail_archive(
                    db, printer_id, generation=batch.generation, subtask_id=batch.subtask_id
                )
            if archive_id is not None:
                await record_tray_change(
                    db,
                    printer_id=printer_id,
                    archive_id=archive_id,
                    tray=UNKNOWN_FEED,
                    layer_num=layer,
                    ids=(None, None),
                )
                await db.commit()
    except Exception:  # noqa: BLE001
        logger.exception(
            "[UsageJournal] Printer %d: the unknown feed from layer %d was not journaled", printer_id, layer
        )
    logger.warning(
        "[UsageJournal] Printer %d: the restart replay (%d facts, archive %s) was not written (%s); the feed "
        "from layer %d is recorded as unknown%s",
        printer_id,
        len(batch.facts),
        archive_id,
        reason,
        layer,
        "" if stated else " in the journal (the printer's client was not this print's)",
    )


async def _write_replay_fact(db: AsyncSession, printer_id: int, archive_id: int, fact) -> None:
    """One replayed fact with the reel the replay named — or, from a seed without a binding
    snapshot, the binding now, read under the journal lock before anyone can rewrite it."""
    ids = tuple(fact.ids) if fact.ids is not None else await freeze_spool_ids(db, printer_id, fact.tray)
    if fact.kind == "tray_change":
        await record_tray_change(
            db, printer_id=printer_id, archive_id=archive_id, tray=fact.tray, layer_num=fact.layer, ids=ids
        )
    elif fact.kind == "tail_start":
        await record_tail_start(
            db, printer_id=printer_id, archive_id=archive_id, origin=fact.tray, layer_num=fact.layer, ids=ids
        )
    elif fact.kind == "feed_return":
        await record_feed_return(
            db,
            printer_id=printer_id,
            archive_id=archive_id,
            origin=fact.tray,
            layer_num=fact.layer,
            after_gap=fact.after_gap,
            ids=ids,
        )
    elif fact.kind == "held_assignment":
        ams_id, tray_id = _global_tray_to_ams_slot(fact.tray)
        await note_assignment_change(
            db,
            printer_id=printer_id,
            ams_id=ams_id,
            tray_id=tray_id,
            spool_id=ids[0],
            spoolman_spool_id=ids[1],
            layer_num=fact.layer,
            archive_id=archive_id,
            commit=False,
        )


async def binding_snapshot(db: AsyncSession, printer_id: int) -> dict[int, tuple[int | None, int | None]]:
    """Every tray's ``(spool_id, spoolman_spool_id)`` of the printer, read at once — what a
    restart seed names its replay with. A tray absent from it is unbound in both backends."""
    from backend.app.models.spool_assignment import SpoolAssignment
    from backend.app.models.spoolman_slot_assignment import SpoolmanSlotAssignment

    snapshot: dict[int, tuple[int | None, int | None]] = {}
    internal = await db.execute(
        select(SpoolAssignment.ams_id, SpoolAssignment.tray_id, SpoolAssignment.spool_id).where(
            SpoolAssignment.printer_id == printer_id
        )
    )
    for ams_id, tray_id, spool_id in internal.all():
        tray = _global_tray(ams_id, tray_id)
        snapshot[tray] = (spool_id, snapshot.get(tray, (None, None))[1])
    spoolman = await db.execute(
        select(
            SpoolmanSlotAssignment.ams_id, SpoolmanSlotAssignment.tray_id, SpoolmanSlotAssignment.spoolman_spool_id
        ).where(SpoolmanSlotAssignment.printer_id == printer_id)
    )
    for ams_id, tray_id, spoolman_spool_id in spoolman.all():
        tray = _global_tray(ams_id, tray_id)
        snapshot[tray] = (snapshot.get(tray, (None, None))[0], spoolman_spool_id)
    return snapshot


# The one local owner of a print's tail facts (post-rebuild F-R2): the archive the run
# lifecycle bound to the client's journal generation — the print the MQTT client observed
# the facts in. Both tail writers, the queued callback and an assignment writer, resolve
# through it (``tail_archive``). Accounting only: it gates no command and no completion.
_tail_owners: dict[int, tuple[int, int]] = {}


def bind_tail_owner(printer_id: int, archive_id: int, generation) -> None:
    """The run lifecycle bound ``archive_id`` to the print the client is on (its journal generation)."""
    if isinstance(generation, int) and not isinstance(generation, bool):
        _tail_owners[printer_id] = (generation, archive_id)


def bind_tail_owner_to_client(printer_id: int, archive_id: int) -> None:
    """``bind_tail_owner`` for the print the printer's client is on now (the run lifecycle's bind)."""
    _client, generation, _subtask = _observed_print(printer_id)
    bind_tail_owner(printer_id, archive_id, generation)


def forget_tail_state(printer_id: int | None = None) -> None:
    """Drop the in-process tail owners and pending replays — of one printer, or all."""
    if printer_id is None:
        _tail_owners.clear()
        _pending_replays.clear()
        _replay_writers.clear()
        _unstated_unknowns.clear()
    else:
        _tail_owners.pop(printer_id, None)
        _pending_replays.pop(printer_id, None)
        _replay_writers.pop(printer_id, None)
        _unstated_unknowns.pop(printer_id, None)


def _observed_print(printer_id: int):
    """``(client, journal generation, device subtask)`` of the print the printer's client is on now.

    Unknown evidence is None — never a value made up from a client that cannot say.
    """
    try:
        from backend.app.services.printer_manager import printer_manager

        client = printer_manager.get_client(printer_id)
    except Exception:  # noqa: BLE001 - no client proves nothing
        return None, None, None
    if client is None:
        return None, None, None
    try:
        generation = client.journal_generation()
    except Exception:  # noqa: BLE001
        generation = None
    if not isinstance(generation, int) or isinstance(generation, bool):
        generation = None
    subtask = getattr(getattr(client, "state", None), "subtask_id", None)
    return client, generation, subtask if isinstance(subtask, str) else None


def _subtasks_contradict(observed, recorded) -> bool:
    """Two positive device subtask ids that differ — missing or ``0`` proves nothing."""
    observed = str(observed or "").strip()
    recorded = str(recorded or "").strip()
    return observed not in ("", "0") and recorded not in ("", "0") and observed != recorded


@dataclass(frozen=True)
class TailEpisode:
    """One runout tail of a print (post-rebuild F-R2): its ``tail_start`` and the event that
    closed it — a ``feed_return`` on the origin, or a feed from another AMS tray — or None
    while it is still open. Identified by archive, origin tray and the tail_start's id."""

    start: Any
    close: Any | None

    @property
    def origin(self) -> int:
        return self.start.global_tray_id

    def contains(self, event) -> bool:
        return self.start.id < event.id and (self.close is None or event.id < self.close.id)


def _closes_tail(event, origin: int) -> bool:
    from backend.app.models.print_usage_event import EVENT_FEED_RETURN, EVENT_TRAY_CHANGE
    from backend.app.utils.usage_attribution import UNKNOWN_FEED

    if event.event == EVENT_FEED_RETURN:
        return event.global_tray_id == origin
    tray = event.global_tray_id
    if event.event == EVENT_TRAY_CHANGE and tray == UNKNOWN_FEED:
        return True  # nobody saw how the tail went on: the chronology says unknown from here
    return event.event == EVENT_TRAY_CHANGE and tray is not None and 0 <= tray < 254 and tray != origin


def tail_episodes(events: list, tray: int | None = None) -> list[TailEpisode]:
    """The print's tail episodes in journal order — of one origin when ``tray`` is given.

    Read by journal id, never by time. One feed has one tail at a time: a ``tail_start``
    while one is open is a duplicate and ignored, and a tray change onto the external
    sentinel inside one is noise, not its end.
    """
    from backend.app.models.print_usage_event import EVENT_TAIL_START

    episodes: list[TailEpisode] = []
    current = None
    for e in sorted(events or [], key=lambda e: e.id):
        if current is None:
            if e.event == EVENT_TAIL_START and e.global_tray_id is not None:
                current = e
        elif _closes_tail(e, current.global_tray_id):
            episodes.append(TailEpisode(current, e))
            current = None
    if current is not None:
        episodes.append(TailEpisode(current, None))
    return [ep for ep in episodes if tray is None or ep.origin == tray]


def open_tail_episode(events: list, tray: int | None = None) -> TailEpisode | None:
    """The print's open tail — of ``tray`` when given — or None."""
    episodes = tail_episodes(events)
    last = episodes[-1] if episodes else None
    if last is None or last.close is not None or (tray is not None and last.origin != tray):
        return None
    return last


async def _append(db: AsyncSession, **fields) -> PrintUsageEvent:
    """One journal row, flushed into the caller's transaction (the caller commits)."""
    row = PrintUsageEvent(
        **fields, slot_occupied=_slot_occupied_now(fields["printer_id"], fields.get("global_tray_id"))
    )
    db.add(row)
    await db.flush()
    return row


async def record_tail_start(
    db: AsyncSession, *, printer_id: int, archive_id: int, origin: int, layer_num: int, ids: tuple | None = None
) -> bool:
    """Open a tail episode on ``origin``, freezing the reel running out; False when one is open.

    Frozen from the binding as every journal row is — and safe from an assignment that
    arrives while this callback waits, because every assignment writer records the open
    tail itself before it rewrites the binding (``ensure_tail_recorded``). A tail the restart
    seed replayed was seen while no writer could record it: ``ids`` is the reel bound when it
    was seen, which the replay named (F-R3). Does not commit.
    """
    from backend.app.models.print_usage_event import EVENT_TAIL_START

    if open_tail_episode(await load_events(db, printer_id, archive_id)) is not None:
        return False
    spool_id, spoolman_spool_id = ids if ids is not None else await freeze_spool_ids(db, printer_id, origin)
    await _append(
        db,
        printer_id=printer_id,
        archive_id=archive_id,
        layer_num=layer_num,
        event=EVENT_TAIL_START,
        global_tray_id=origin,
        spool_id=spool_id,
        spoolman_spool_id=spoolman_spool_id,
    )
    logger.info(
        "[UsageJournal] Runout tail of tray %d on printer %d from layer %d (spool=%s/%s)",
        origin,
        printer_id,
        layer_num,
        spool_id,
        spoolman_spool_id,
    )
    return True


async def record_tray_change(
    db: AsyncSession, *, printer_id: int, archive_id: int, tray: int, layer_num: int, ids: tuple
) -> bool:
    """A mid-print tray change with the reels ``ids`` names; False for an identical
    immediately-preceding entry (print start seeds the log from PrinterState, which may already
    hold the change). Does not commit."""
    from backend.app.models.print_usage_event import EVENT_START, EVENT_TRAY_CHANGE

    last = (
        (
            await db.execute(
                select(PrintUsageEvent)
                .where(
                    PrintUsageEvent.archive_id == archive_id,
                    PrintUsageEvent.event.in_([EVENT_START, EVENT_TRAY_CHANGE]),
                )
                .order_by(PrintUsageEvent.id.desc())
                .limit(1)
            )
        )
        .scalars()
        .first()
    )
    if last is not None and last.global_tray_id == tray and last.layer_num == layer_num:
        return False
    spool_id, spoolman_spool_id = ids
    await _append(
        db,
        printer_id=printer_id,
        archive_id=archive_id,
        layer_num=layer_num,
        event=EVENT_TRAY_CHANGE,
        global_tray_id=tray,
        spool_id=spool_id,
        spoolman_spool_id=spoolman_spool_id,
    )
    return True


async def record_feed_return(
    db: AsyncSession,
    *,
    printer_id: int,
    archive_id: int,
    origin: int,
    layer_num: int,
    after_gap: bool,
    ids: tuple | None = None,
) -> bool:
    """Close the open tail of ``origin``: the feed boundary, with the reel bound at this moment.

    ``after_gap``: the return was first seen after a restart or a new client — the layer is
    the observed one, and what fed since the tail's last proven layer is not recorded.
    A return the restart seed replayed carries the reel bound when it was seen (``ids``, F-R3).
    A return without an open tail is a no-op (False). Does not commit.
    """
    from backend.app.models.print_usage_event import EVENT_FEED_RETURN, KIND_GAP

    if open_tail_episode(await load_events(db, printer_id, archive_id), origin) is None:
        return False
    spool_id, spoolman_spool_id = ids if ids is not None else await freeze_spool_ids(db, printer_id, origin)
    await _append(
        db,
        printer_id=printer_id,
        archive_id=archive_id,
        layer_num=layer_num,
        event=EVENT_FEED_RETURN,
        kind=KIND_GAP if after_gap else None,
        global_tray_id=origin,
        spool_id=spool_id,
        spoolman_spool_id=spoolman_spool_id,
    )
    logger.info(
        "[UsageJournal] Feed back on tray %d on printer %d at layer %d%s (spool=%s/%s)",
        origin,
        printer_id,
        layer_num,
        " after an observation gap" if after_gap else "",
        spool_id,
        spoolman_spool_id,
    )
    return True


async def active_tail_archive(db: AsyncSession, printer_id: int, subtask_id: str | None):
    """The archive a tail fact observed now belongs to, or None.

    The newest printing archive — and when the observation and the archive both name a
    firmware subtask, they must name the same one: a fact of the print before never
    lands in the next (F-R2).
    """
    from backend.app.models.archive import PrintArchive

    archive = (
        await db.execute(
            select(PrintArchive.id, PrintArchive.subtask_id)
            .where(PrintArchive.printer_id == printer_id, PrintArchive.status == "printing")
            .order_by(PrintArchive.created_at.desc())
            .limit(1)
        )
    ).first()
    if archive is None or _subtasks_contradict(subtask_id, archive.subtask_id):
        return None
    return archive.id


async def tail_archive(db: AsyncSession, printer_id: int, *, generation: int | None, subtask_id: str | None):
    """The archive a tail fact of the client's print ``generation`` belongs to, or None.

    The one owner both tail writers ask (F-R2). A fact of an earlier print — the client has
    moved on — never opens an episode in the next archive, whether it carries device ids or
    not. The archive the run lifecycle bound to this print takes it while still printing;
    before that bind, the newest printing archive, unless it is bound to another print. A
    device subtask that contradicts the archive's names no owner either.
    """
    from backend.app.models.archive import PrintArchive

    _client, current, _subtask = _observed_print(printer_id)
    if generation is not None and current is not None and generation != current:
        return None
    observed = generation if generation is not None else current
    owner = _tail_owners.get(printer_id)
    if owner is not None and observed is not None and owner[0] == observed:
        row = (
            await db.execute(select(PrintArchive.status, PrintArchive.subtask_id).where(PrintArchive.id == owner[1]))
        ).first()
        if row is None or row.status != "printing" or _subtasks_contradict(subtask_id, row.subtask_id):
            return None
        return owner[1]
    candidate = await active_tail_archive(db, printer_id, subtask_id)
    if candidate is not None and owner is not None and observed is not None and owner[1] == candidate:
        return None  # the newest printing archive belongs to another print of this client
    return candidate


async def handle_feed_event(
    db: AsyncSession,
    *,
    printer_id: int,
    event: str,
    origin: int,
    layer_num: int,
    after_gap: bool,
    subtask_id: str | None = None,
    generation: int | None = None,
) -> bool:
    """Journal one tail fact the MQTT client observed; the caller holds ``journal_lock``.

    ``subtask_id`` and ``generation`` are the print the client was on when it observed the
    fact (``tail_archive``). A fact this print does not own is refused before anything of the
    print's state is touched. A restart seed's replay never comes this way: it is one batch,
    written by ``journal_lock`` (post-rebuild F3).
    """
    archive_id = await tail_archive(db, printer_id, generation=generation, subtask_id=subtask_id)
    if archive_id is None:
        logger.debug("Tail fact %s on printer %d has no matching active archive, dropped", event, printer_id)
        return False
    if event == "tail_start":
        written = await record_tail_start(
            db, printer_id=printer_id, archive_id=archive_id, origin=origin, layer_num=layer_num
        )
    elif event == "feed_return":
        written = await record_feed_return(
            db, printer_id=printer_id, archive_id=archive_id, origin=origin, layer_num=layer_num, after_gap=after_gap
        )
    else:
        return False
    if written:
        await db.commit()
    return written


async def ensure_tail_recorded(
    db: AsyncSession,
    *,
    printer_id: int,
    ams_id: int,
    tray_id: int,
    new_spool_id: int | None = None,
    new_spoolman_spool_id: int | None = None,
) -> bool:
    """Before an assignment rewrites a slot's binding: if that slot's reel is feeding its
    tail right now, make sure the tail_start exists, frozen with the reel going out.

    The tail is observed on the MQTT thread and journaled by a queued callback; an
    assignment that commits in between would hand the new reel to the tail. So whoever
    comes first writes it — this, under the caller's ``journal_lock``, or the callback,
    which then finds the episode open. Flushed into the caller's transaction, so the
    tail_start and the new binding commit together. The archive is the one the callback
    would pick (``tail_archive``). While a joined print waits for its restart seed no tail is
    proven, so nothing is written — but the client records the change in its place among the
    held reports: the reel going out and the one coming in (``new_spool_id`` /
    ``new_spoolman_spool_id``, the side this writer sets), for the replay to name (F-R3). A
    writer without the journal lock (RFID) whose note the seed overtook is a live change.
    """
    client, generation, subtask = _observed_print(printer_id)
    tray = _global_tray(ams_id, tray_id)
    try:
        held = client is not None and client.journal_held() is True
    except Exception:  # noqa: BLE001
        held = False
    if held and generation is not None:
        from backend.app.services import spoolman_source

        # Which Spoolman database the numbers read here belong to: the seed writes them much
        # later, and only under the same base (C047 acceptance A01).
        stamp = spoolman_source.base_generation()
        outgoing = await freeze_spool_ids(db, printer_id, tray)
        incoming = None
        if new_spool_id is not None or new_spoolman_spool_id is not None:
            incoming = (
                new_spool_id if new_spool_id is not None else outgoing[0],
                new_spoolman_spool_id if new_spoolman_spool_id is not None else outgoing[1],
            )
        if client.note_held_binding_change(generation, tray, outgoing, incoming, stamp=stamp):
            logger.info(
                "[UsageJournal] Tray %d on printer %d re-linked while its journal waits for the seed; "
                "the reel going out is %s/%s",
                tray,
                printer_id,
                *outgoing,
            )
            return False
        # The hold ended while the reel was read (the seed ran): from here a live change.
    try:
        tail = client.open_tail() if client is not None else None
    except Exception:  # noqa: BLE001 - no reading proves nothing; the callback still records it
        tail = None
    if tail is None:
        return False
    origin, layer = tail
    if origin != tray:
        return False
    archive_id = await tail_archive(db, printer_id, generation=generation, subtask_id=subtask)
    if archive_id is None:
        return False
    return await record_tail_start(db, printer_id=printer_id, archive_id=archive_id, origin=origin, layer_num=layer)


def _global_tray(ams_id: int, tray_id: int) -> int:
    if ams_id >= 254:
        return 254 + tray_id
    if ams_id >= 128:
        return ams_id
    return ams_id * 4 + tray_id


class RunoutResult(StrEnum):
    NEW = "new"
    UPGRADED = "upgraded"
    DUPLICATE = "duplicate"
    REJECTED = "rejected"

    @property
    def should_notify(self):
        return self in {self.NEW, self.UPGRADED}


def _kind_upgrade(previous, current):
    # A less specific replay cannot erase already confirmed auto-switch proof.
    ranks = {None: 0, "ambiguous": 0, "manual": 1, "pause": 2, "external": 3, "autoswitch": 4}
    return (
        current is not None
        and current != previous
        and (previous is None or ranks.get(current, 0) > ranks.get(previous, 0))
    )


def _global_tray_to_ams_slot(global_tray_id: int) -> tuple[int, int]:
    """Decode a global tray id to the ``(ams_id, tray_id)`` the assignment
    tables are keyed by — same encoding as ``spoolman_tracking``:
    254/255 → external (ams 255, tray 0/1), >=128 → AMS-HT (own id, tray 0),
    else a four-slot AMS unit."""
    if global_tray_id >= 254:
        return 255, global_tray_id - 254
    if global_tray_id >= 128:
        return global_tray_id, 0
    return global_tray_id // 4, global_tray_id % 4


def _slot_occupied_now(printer_id: int, global_tray_id: int | None) -> bool | None:
    """Does the AMS say this slot holds filament right now — or ``None``.

    ⚠️ Best-effort by design: the journal row is what matters, the extra fact
    rides along. A printer we cannot reach, an event with no tray (pause,
    resume), an external holder (no presence bit) and a push that arrived
    without the bitfield all answer ``None`` — "we had no reading", which is
    NOT the same as "the slot was empty" and must never be read as such.
    """
    if global_tray_id is None:
        return None
    try:
        from backend.app.services.printer_manager import printer_manager

        state = printer_manager.get_status(printer_id)
        return slot_presence_by_tray(getattr(state, "raw_data", None)).get(global_tray_id)
    except Exception:  # noqa: BLE001 - never fail a journal write over a nicety
        return None


async def record_event(
    db: AsyncSession,
    *,
    printer_id: int,
    archive_id: int,
    layer_num: int,
    event: str,
    kind: str | None = None,
    global_tray_id: int | None = None,
    spool_id: int | None = None,
    spoolman_spool_id: int | None = None,
    commit: bool = True,
) -> None:
    """Append one journal row and commit (``commit=False``: flush only).

    The row also freezes whether the event's own slot physically held filament
    at this moment (m161) — the only fact that later tells a real refill from a
    books-only assignment, and an empty mapped slot from one whose spool id was
    frozen for an unrelated reason. NULL means "no reading", never "empty".
    """
    db.add(
        PrintUsageEvent(
            printer_id=printer_id,
            archive_id=archive_id,
            layer_num=layer_num,
            event=event,
            kind=kind,
            global_tray_id=global_tray_id,
            spool_id=spool_id,
            spoolman_spool_id=spoolman_spool_id,
            slot_occupied=_slot_occupied_now(printer_id, global_tray_id),
        )
    )
    await (db.commit() if commit else db.flush())


async def freeze_spool_ids(db: AsyncSession, printer_id: int, global_tray_id: int) -> tuple[int | None, int | None]:
    """The spools currently bound to a tray, for both backends — or ``None``s.

    Called at event time so the journal row carries the answer the completion
    path needs; an unassigned slot freezes to nothing rather than guessing.
    """
    from backend.app.models.spool_assignment import SpoolAssignment
    from backend.app.models.spoolman_slot_assignment import SpoolmanSlotAssignment

    ams_id, tray_id = _global_tray_to_ams_slot(global_tray_id)

    spool_id = (
        await db.execute(
            select(SpoolAssignment.spool_id).where(
                SpoolAssignment.printer_id == printer_id,
                SpoolAssignment.ams_id == ams_id,
                SpoolAssignment.tray_id == tray_id,
            )
        )
    ).scalar_one_or_none()

    spoolman_spool_id = (
        await db.execute(
            select(SpoolmanSlotAssignment.spoolman_spool_id).where(
                SpoolmanSlotAssignment.printer_id == printer_id,
                SpoolmanSlotAssignment.ams_id == ams_id,
                SpoolmanSlotAssignment.tray_id == tray_id,
            )
        )
    ).scalar_one_or_none()

    return spool_id, spoolman_spool_id


async def active_archive_id(db: AsyncSession, printer_id: int) -> int | None:
    """The printer's newest still-printing archive — the journal's anchor.

    The archive row exists from print start (external prints included), so a
    tracked print always has one; ``None`` means no tracked print is running
    and the event has nothing to attach to.
    """
    from backend.app.models.archive import PrintArchive

    result = await db.execute(
        select(PrintArchive.id)
        .where(PrintArchive.printer_id == printer_id, PrintArchive.status == "printing")
        .order_by(PrintArchive.created_at.desc())
        .limit(1)
    )
    return result.scalars().first()


def _is_stale_refill_runout(event, earlier: list) -> bool:
    """A refilled, inactive AMS slot cannot have exhausted its new reel.

    Require positive presence at load AND at the repeated autoswitch, a
    different reel, and the preceding episode's observed backup within the
    same four-slot AMS. That unit has one feed path, even on dual-nozzle
    printers. Never infer this from a different unit, an external holder,
    missing sensor data, a cooldown or a lack of charged grams.
    """
    from backend.app.models.print_usage_event import (
        EVENT_RUNOUT,
        EVENT_SPOOL_LOADED,
        EVENT_TRAY_CHANGE,
        KIND_AUTOSWITCH,
    )

    tray = event.global_tray_id
    if (
        event.event != EVENT_RUNOUT
        or event.kind != KIND_AUTOSWITCH
        or tray is None
        or not 0 <= tray < 128
        or getattr(event, "slot_occupied", None) is not True
    ):
        return False
    history = [
        e
        for e in earlier
        if getattr(e, "archive_id", None) == getattr(event, "archive_id", None)
        and getattr(e, "printer_id", None) == getattr(event, "printer_id", None)
    ]
    previous = next((e for e in reversed(history) if e.event == EVENT_RUNOUT and e.global_tray_id == tray), None)
    if previous is None or previous.kind != KIND_AUTOSWITCH:
        return False
    loaded = next(
        (
            e
            for e in reversed(history)
            if e.event == EVENT_SPOOL_LOADED and e.global_tray_id == tray and e.id > previous.id
        ),
        None,
    )
    if loaded is None or getattr(loaded, "slot_occupied", None) is not True:
        return False
    identity = (event.spool_id, event.spoolman_spool_id)
    if identity == (None, None) or identity != (loaded.spool_id, loaded.spoolman_spool_id):
        return False
    old_identity = (previous.spool_id, previous.spoolman_spool_id)
    if old_identity == (None, None) or old_identity == identity:
        return False
    backup = next(
        (
            e
            for e in history
            if e.event == EVENT_TRAY_CHANGE
            and previous.id < e.id < loaded.id
            and e.global_tray_id is not None
            and e.global_tray_id != tray
            and e.global_tray_id // 4 == tray // 4
            and previous.layer_num <= e.layer_num <= previous.layer_num + 1
        ),
        None,
    )
    if backup is None:
        return False
    return not any(e.event == EVENT_TRAY_CHANGE and e.global_tray_id == tray and e.id > backup.id for e in history)


def without_stale_refill_runouts(events: list) -> list:
    """Accounting view; keep the persisted journal intact for forensics.

    Earlier rejected repeats cannot themselves become the ancestry of a later
    refill. True returns to the source keep subsequent real runouts intact.
    """
    accepted = []
    for event in sorted(events or [], key=lambda e: e.id):
        if not _is_stale_refill_runout(event, accepted):
            accepted.append(event)
    return accepted


async def record_runout(
    db: AsyncSession,
    *,
    printer_id: int,
    archive_id: int,
    layer_num: int,
    kind: str | None,
    global_tray_id: int | None,
    spool_id: int | None = None,
    spoolman_spool_id: int | None = None,
) -> RunoutResult:
    """One runout row per EPISODE of (archive, tray).

    A repeat while the episode is open (the detector re-fired on HMS flicker,
    or a restart replayed the still-active code) only upgrades the kind — the
    boundary layer and the frozen spool ids of the first sighting stay. But a
    runout whose previous episode is CLOSED (a spool_loaded followed it) is a
    new episode — two short reels in one long print — and gets its own row.

    A positively identified inactive-slot refill replay is REJECTED without
    writing or notifying. NEW/UPGRADED may notify; DUPLICATE (including a
    tray-less fold) is silent. The frozen candidate is shared by gate/insert. Accounting also filters older saved
    replays without deleting the original forensic journal.

    Two hardenings from the X2D incident (2026-08-23):

    * A runout the resolver could NOT map to a tray is folded away when a
      tray-ful episode is already open — the printer fires several codes for
      one physical event (a per-slot HMS plus a generic print_error 24s
      apart), and the tray-less one adds nothing the timeline doesn't have.
    * A runout arriving with no frozen spool inherits it from the journal's
      own prior rows on the same tray. By runout time the slot's assignment
      can already be gone (the AMS-empty report unlinked it, or the user
      unassigned by hand) — but the journal recorded who fed the tray, and
      inheriting recorded lineage is not guessing.
    """
    from backend.app.models.print_usage_event import (
        EVENT_FEED_RETURN,
        EVENT_RUNOUT,
        EVENT_SPOOL_LOADED,
        EVENT_START,
        EVENT_TRAY_CHANGE,
        KIND_AUTOSWITCH,
    )

    occupied = _slot_occupied_now(printer_id, global_tray_id)
    if global_tray_id is not None:
        # A runout inside an open tail is the tail running dry: the reel the tail_start
        # froze, never the one put into the slot meanwhile (post-rebuild F-R3).
        tail = open_tail_episode(await load_events(db, printer_id, archive_id), global_tray_id)
        if tail is not None and (tail.start.spool_id, tail.start.spoolman_spool_id) != (spool_id, spoolman_spool_id):
            logger.info(
                "Runout on tray %s inside its tail: spool %s/%s from the tail's start, not the bound %s/%s",
                global_tray_id,
                tail.start.spool_id,
                tail.start.spoolman_spool_id,
                spool_id,
                spoolman_spool_id,
            )
            spool_id, spoolman_spool_id = tail.start.spool_id, tail.start.spoolman_spool_id
    if spool_id is None and spoolman_spool_id is None and global_tray_id is not None:
        lineage = (
            await db.execute(
                select(PrintUsageEvent)
                .where(
                    PrintUsageEvent.archive_id == archive_id,
                    PrintUsageEvent.global_tray_id == global_tray_id,
                    PrintUsageEvent.event.in_([EVENT_START, EVENT_TRAY_CHANGE, EVENT_SPOOL_LOADED]),
                    (PrintUsageEvent.spool_id.is_not(None)) | (PrintUsageEvent.spoolman_spool_id.is_not(None)),
                )
                .order_by(PrintUsageEvent.id.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if lineage is not None:
            spool_id = lineage.spool_id
            spoolman_spool_id = lineage.spoolman_spool_id
            logger.info(
                "Runout on tray %s had no live assignment — inherited spool %s/%s from the journal's own lineage",
                global_tray_id,
                spool_id,
                spoolman_spool_id,
            )

    if kind == KIND_AUTOSWITCH and global_tray_id is not None and 0 <= global_tray_id < 128:
        candidate = PrintUsageEvent(
            printer_id=printer_id,
            archive_id=archive_id,
            event=EVENT_RUNOUT,
            kind=kind,
            global_tray_id=global_tray_id,
            spool_id=spool_id,
            spoolman_spool_id=spoolman_spool_id,
            layer_num=layer_num,
            slot_occupied=occupied,
        )
        if candidate.slot_occupied is True:
            history = without_stale_refill_runouts(await load_events(db, printer_id, archive_id))
            if _is_stale_refill_runout(candidate, history):
                logger.info(
                    "[UsageJournal] Ignored stale autoswitch after inactive-slot refill: printer=%s archive=%s tray=%s spool=%s/%s layer=%s",
                    printer_id,
                    archive_id,
                    global_tray_id,
                    spool_id,
                    spoolman_spool_id,
                    layer_num,
                )
                return RunoutResult.REJECTED

    if global_tray_id is None:
        tray_rows = (
            (
                await db.execute(
                    select(PrintUsageEvent)
                    .where(
                        PrintUsageEvent.archive_id == archive_id,
                        PrintUsageEvent.event.in_([EVENT_RUNOUT, EVENT_SPOOL_LOADED, EVENT_FEED_RETURN]),
                        PrintUsageEvent.global_tray_id.is_not(None),
                    )
                    .order_by(PrintUsageEvent.id)
                )
            )
            .scalars()
            .all()
        )
        open_trays = set()
        for e in tray_rows:
            if e.event == EVENT_RUNOUT:
                open_trays.add(e.global_tray_id)
            else:
                open_trays.discard(e.global_tray_id)
        if open_trays:
            logger.debug(
                "Tray-less runout folded into open episode on tray(s) %s (archive %d)",
                sorted(open_trays),
                archive_id,
            )
            return RunoutResult.DUPLICATE

    rows = (
        (
            await db.execute(
                select(PrintUsageEvent)
                .where(
                    PrintUsageEvent.archive_id == archive_id,
                    PrintUsageEvent.event.in_([EVENT_RUNOUT, EVENT_SPOOL_LOADED, EVENT_FEED_RETURN]),
                    PrintUsageEvent.global_tray_id == global_tray_id,
                )
                .order_by(PrintUsageEvent.id)
            )
        )
        .scalars()
        .all()
    )
    last_runout = next((e for e in reversed(rows) if e.event == EVENT_RUNOUT), None)
    if last_runout is not None:
        # A reel loaded after it, or the feed back from its tail: the episode is over, and
        # a runout now is the next reel's (F06).
        closed = any(e.event in (EVENT_SPOOL_LOADED, EVENT_FEED_RETURN) and e.id > last_runout.id for e in rows)
        if not closed:
            if _kind_upgrade(last_runout.kind, kind):
                last_runout.kind = kind
                await db.commit()
                return RunoutResult.UPGRADED
            return RunoutResult.DUPLICATE

    db.add(
        PrintUsageEvent(
            printer_id=printer_id,
            archive_id=archive_id,
            layer_num=layer_num,
            event=EVENT_RUNOUT,
            kind=kind,
            global_tray_id=global_tray_id,
            spool_id=spool_id,
            spoolman_spool_id=spoolman_spool_id,
            slot_occupied=occupied,
        )
    )
    await db.commit()
    return RunoutResult.NEW


async def _slot_holds_a_spool(db: AsyncSession, printer_id: int, ams_id: int, tray_id: int) -> bool:
    """Is there a spool bound to this slot that an assignment would replace?

    ⚠️ **The books, not the tray.** A reel that runs out mid-print leaves the
    tray reporting empty while ``on_ams_change`` deliberately keeps the slot
    linked — "the spool is still physically in the AMS, just consumed", and that
    link is the only record of what fed the print. Reading emptiness off the AMS
    would call a runout an empty slot, which is the one case the replacement
    question exists for.
    """
    from backend.app.models.spool_assignment import SpoolAssignment
    from backend.app.models.spoolman_slot_assignment import SpoolmanSlotAssignment

    for model in (SpoolAssignment, SpoolmanSlotAssignment):
        bound = await db.execute(
            select(model.id).where(
                model.printer_id == printer_id,
                model.ams_id == ams_id,
                model.tray_id == tray_id,
            )
        )
        if bound.scalar_one_or_none() is not None:
            return True
    return False


async def manual_replacement_window(
    db: AsyncSession,
    printer_id: int,
    *,
    ams_id: int | None = None,
    tray_id: int | None = None,
) -> dict | None:
    """Is a declared mid-print replacement currently plausible, and how?

    ⚠️ **A replacement is a property of the SLOT, not only of the print.**
    Declaring one charges everything printed so far to the spool that came OUT,
    and an empty slot has none — ``freeze_spool_ids`` freezes it to nothing
    rather than guessing, so the branch would journal a boundary naming nobody.
    Filling an empty slot mid-print is therefore never a replacement, and asking
    about it puts an unanswerable question in front of the operator. When
    ``ams_id``/``tray_id`` are given, an unbound slot answers ``None``; callers
    that ask nothing about a slot keep the printer-wide answer.

    The feeding spool can only be swapped while the print is paused, so the
    window is defined by pauses, not by the assignment's moment:

    * printer PAUSED right now → ``{"mode": "prompt", "pause_layer": current}``
      — a swap is likely, the UI asks a blocking question;
    * RUNNING but this print HAS a pause behind it → ``{"mode": "optin",
      "pause_layer": last-pause layer}`` — the swap, if any, happened back at
      that pause (swap → resume from the printer's screen → only then the UI),
      so the UI offers a default-off checkbox and the boundary lands on the
      pause layer, not on the click;
    * no active print, or never paused → ``None`` — a replacement is
      physically impossible, every assignment is a wrong-link correction and
      must stay friction-free (bulk re-linking mid-print is a real workflow).
    """
    from backend.app.models.print_usage_event import EVENT_PAUSE

    archive_id = await active_archive_id(db, printer_id)
    if archive_id is None:
        return None
    if ams_id is not None and tray_id is not None:
        if not await _slot_holds_a_spool(db, printer_id, ams_id, tray_id):
            return None
    try:
        from backend.app.services.printer_manager import printer_manager

        state = printer_manager.get_status(printer_id)
    except Exception:
        state = None
    if state is not None and (getattr(state, "state", None) or "").upper() == "PAUSE":
        return {"archive_id": archive_id, "mode": "prompt", "pause_layer": getattr(state, "layer_num", 0) or 0}
    last_pause = (
        await db.execute(
            select(PrintUsageEvent)
            .where(
                PrintUsageEvent.archive_id == archive_id,
                PrintUsageEvent.event == EVENT_PAUSE,
            )
            .order_by(PrintUsageEvent.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if last_pause is None:
        return None
    return {"archive_id": archive_id, "mode": "optin", "pause_layer": last_pause.layer_num or 0}


async def note_manual_replacement_intent(
    db: AsyncSession,
    *,
    printer_id: int,
    ams_id: int,
    tray_id: int,
) -> bool:
    """The human declared the assignment that follows a REPLACEMENT — a
    deliberate mid-pause spool change with no firmware event to witness it.

    Journals a ``manual`` runout with the outgoing spool frozen from the
    still-current assignment — call BEFORE the assignment is rewritten — so
    the assignment that follows closes it as the ``spool_loaded`` boundary.
    ``manual`` shares the ambiguous contract: a boundary only through
    spool_loaded, never a zero correction (a preventively swapped reel is
    not empty). Accepted inside the ``manual_replacement_window`` — paused
    now, or resumed from a pause this print — with the boundary on the pause
    layer; returns False (nothing journaled) otherwise so the caller can
    refuse the flag loudly instead of mis-journaling a correction.
    """
    from backend.app.models.print_usage_event import KIND_MANUAL

    # Defence in depth: an older client can still send the flag, and journaling
    # a boundary that names nobody is worse than ignoring the claim.
    window = await manual_replacement_window(db, printer_id, ams_id=ams_id, tray_id=tray_id)
    if window is None:
        return False
    archive_id = window["archive_id"]

    if ams_id >= 254:
        global_tray = 254 + tray_id
    elif ams_id >= 128:
        global_tray = ams_id
    else:
        global_tray = ams_id * 4 + tray_id

    spool_id, spoolman_spool_id = await freeze_spool_ids(db, printer_id, global_tray)
    logger.info(
        "[UsageJournal] Manual replacement declared for tray %d on printer %d (outgoing spool=%s/%s, layer=%s)",
        global_tray,
        printer_id,
        spool_id,
        spoolman_spool_id,
        window["pause_layer"],
    )
    await record_runout(
        db,
        printer_id=printer_id,
        archive_id=archive_id,
        layer_num=window["pause_layer"],
        kind=KIND_MANUAL,
        global_tray_id=global_tray,
        spool_id=spool_id,
        spoolman_spool_id=spoolman_spool_id,
    )
    return True


def slot_presence_by_tray(raw_data) -> dict[int, bool]:
    """Global tray id -> does the AMS's OWN sensor say that slot holds filament.

    ⚠️ The answer comes from ``tray_exist_bits``, the printer's per-slot presence
    bitfield, which ``bambu_mqtt`` decodes onto each tray as ``exists``. It is
    the ONLY independent evidence a reel is physically there.

    ⚠️ **Never substitute ``tray_type``.** BamDude writes that itself
    (``ams_filament_setting``) the moment a spool is assigned, so reading it
    back is reading our own bookkeeping; and a slot holding an unlabelled reel
    the printer cannot name carries an empty type while being very much
    occupied. An empty slot does clear its type, so the implication runs one
    way only — which is exactly the trap.

    Trays whose presence is unknown are ABSENT from the result rather than
    guessed at, so callers can pick their own fallback.

    ⚠️ **AMS slots only — never the external holder (254/255).** Its presence
    flag is not actually read on any Bambu model: an EMPTY external spool holder
    still reports filament, and BambuStudio shows it as loaded too. So this
    walks ``ams`` and deliberately ignores ``vt_tray``, leaving externals with
    no answer rather than a wrong one. Do not "complete" this by reading the
    external's flag — that is the trap it exists to avoid.
    """
    ams = (raw_data or {}).get("ams") if isinstance(raw_data, dict) else None
    if isinstance(ams, dict):  # some pushes nest it as {"ams": {"ams": [...]}}
        ams = ams.get("ams")
    presence: dict[int, bool] = {}
    for unit in ams or []:
        if not isinstance(unit, dict):
            continue
        try:
            ams_id = int(unit.get("id"))
        except (TypeError, ValueError):
            continue
        for tray in unit.get("tray") or []:
            # A missing or None reading is "unknown", never "empty".
            if not isinstance(tray, dict) or tray.get("exists") is None:
                continue
            try:
                tray_id = int(tray.get("id", 0) or 0)
            except (TypeError, ValueError):
                continue
            # A single-spool AMS-HT IS its own global tray (128..135); ordinary
            # units flatten as unit*4 + slot.
            presence[ams_id if ams_id >= 128 else ams_id * 4 + tray_id] = bool(tray.get("exists"))
    return presence


def stale_runouts_to_seed(events: list, state) -> dict[int | None, str]:
    """Of this print's journaled runouts, which are stale codes to stay quiet about.

    HMS holds a runout status for the WHOLE print, so a client created mid-print
    must be told what the journal already has or it replays every active code as
    a new episode. But seeding a tray unconditionally also silences a runout
    that happened FOR REAL while BamDude was down — and there the slot settles
    it: **a slot that physically holds filament cannot be running out**, so a
    code still standing over it is stale. An EMPTY slot is left unseeded on
    purpose: the code over it may be a runout nobody has recorded yet.

    ⚠️ **Unless the episode was closed on paper only.** Assigning a spool to the
    empty slot in the UI journals a ``spool_loaded`` whether or not a reel went
    in, so the slot can read empty while the code standing over it is still the
    ORIGINAL one. The closing row's own ``slot_occupied`` (m161) is what tells
    bookkeeping from a refill: recorded ``False`` means no reel was ever there,
    the code cannot be new, and the tray is seeded regardless of what the slot
    reads now. NULL keeps the plain presence answer — rows written before m161
    behave exactly as they did.

    Presence comes from :func:`slot_presence_by_tray` — the AMS's own sensor.
    Measured 2026-09-01 on an X2D printing from its backup: slot 3 reported
    ``exists=True`` with ``0700230000030002`` still active, the firmware never
    withdrawing it, while a P1S in the same state had cleared hours earlier.
    Trusting the sensor is what makes those two the same case.

    No reading (an external holder has no presence bit; a state that has not
    arrived yet has nothing) falls back to seeding — the safer and far more
    frequent side.
    """
    from backend.app.models.print_usage_event import EVENT_RUNOUT, EVENT_SPOOL_LOADED

    runouts = [e for e in events or [] if e.event == EVENT_RUNOUT and e.kind]
    # Last kind per tray: the journal upgrades a firmware escalation in place,
    # and the detector's own upgrade path expects the newest one.
    already_fired = {e.global_tray_id: e.kind for e in runouts}
    last_runout_id = {}
    for e in runouts:
        last_runout_id[e.global_tray_id] = e.id
    closed_on_paper: set = set()
    for e in events or []:
        if e.event != EVENT_SPOOL_LOADED:
            continue
        after = last_runout_id.get(e.global_tray_id)
        if after is not None and e.id > after and getattr(e, "slot_occupied", None) is False:
            closed_on_paper.add(e.global_tray_id)

    presence = slot_presence_by_tray(getattr(state, "raw_data", None))
    return {tray: kind for tray, kind in already_fired.items() if tray in closed_on_paper or presence.get(tray, True)}


async def note_assignment_change(
    db: AsyncSession,
    *,
    printer_id: int,
    ams_id: int,
    tray_id: int,
    spool_id: int | None = None,
    spoolman_spool_id: int | None = None,
    layer_num: int | None = None,
    archive_id: int | None = None,
    commit: bool = True,
) -> None:
    """A slot got a (re)assigned spool — if it ran out during the active
    print, this IS the replacement: journal EVENT_SPOOL_LOADED.

    ``archive_id``: the print the assignment belongs to, when the caller already resolved it
    (an assignment the restart seed replays); else the active archive. ``commit=False``: only
    flushed, for a caller that commits a batch of facts once.

    The RFID uuid-watch covers tagged spools; this covers the tagless ones
    (external holders, untagged reels), where "assign the replacement in
    BamDude" — exactly what the runout notification asks for — is the only
    signal there is. No runout on the tray, a replacement already noted, or
    re-linking the very reel the runout froze → no-op.
    """
    from backend.app.models.print_usage_event import EVENT_FEED_RETURN, EVENT_RUNOUT, EVENT_SPOOL_LOADED

    if archive_id is None:
        archive_id = await active_archive_id(db, printer_id)
    if archive_id is None:
        return

    if ams_id >= 254:
        global_tray = 254 + tray_id
    elif ams_id >= 128:
        global_tray = ams_id
    else:
        global_tray = ams_id * 4 + tray_id

    events = await load_events(db, printer_id, archive_id)
    # The LAST runout on the tray — episodes are closed in order, and a second
    # runout of the same tray (two short reels) needs its own replacement.
    runout = next((e for e in reversed(events) if e.event == EVENT_RUNOUT and e.global_tray_id == global_tray), None)
    # Or the tray's open runout tail, even before any runout (post-rebuild F-R3): the
    # assignment names the reel now in the slot. That is identity only — the projection
    # takes the feed boundary from the ``feed_return``, never from this click.
    tail = open_tail_episode(events, global_tray)
    if tail is not None and (runout is None or tail.start.id > runout.id):
        runout = tail.start
    elif tail is None:
        # Or a closed tail whose return still named the outgoing reel: the reel the feed came
        # back with is being named now (the projection takes it from the return's layer,
        # ``usage_attribution.tail_adopted_load``).
        episodes = tail_episodes(events, global_tray)
        last = episodes[-1] if episodes else None
        close = last.close if last is not None else None
        if (
            close is not None
            and close.event == EVENT_FEED_RETURN
            and (runout is None or close.id > runout.id)
            and (close.spool_id, close.spoolman_spool_id) == (last.start.spool_id, last.start.spoolman_spool_id)
        ):
            runout = close
    if runout is None:
        return
    already = next(
        (e for e in events if e.id > runout.id and e.event == EVENT_SPOOL_LOADED and e.global_tray_id == global_tray),
        None,
    )
    if already is not None:
        # RFID-refill race: the uuid-watch fires on the first push carrying the
        # new tag and freezes whatever assignment existed at that instant —
        # which can still be the OLD spool (auto-assign commits moments later)
        # or nothing. The late assignment is the authoritative answer, so it
        # CORRECTS the row rather than being skipped; identical ids are a no-op.
        changed = False
        if spool_id is not None and already.spool_id != spool_id:
            already.spool_id = spool_id
            changed = True
        if spoolman_spool_id is not None and already.spoolman_spool_id != spoolman_spool_id:
            already.spoolman_spool_id = spoolman_spool_id
            changed = True
        if changed:
            logger.info(
                "[UsageJournal] Corrected spool_loaded for tray %d on printer %d (spool=%s, spoolman=%s)",
                global_tray,
                printer_id,
                spool_id,
                spoolman_spool_id,
            )
            await (db.commit() if commit else db.flush())
        return
    if spool_id is not None and runout.spool_id == spool_id:
        return  # the same reel re-linked — a correction, not a replacement
    if spool_id is None and spoolman_spool_id is not None and runout.spoolman_spool_id == spoolman_spool_id:
        return

    if layer_num is None:
        try:
            from backend.app.services.printer_manager import printer_manager

            state = printer_manager.get_status(printer_id)
            layer_num = getattr(state, "layer_num", 0) or 0
        except Exception:
            layer_num = 0

    logger.info(
        "[UsageJournal] Replacement assigned for tray %d on printer %d (spool=%s, spoolman=%s) at layer %d",
        global_tray,
        printer_id,
        spool_id,
        spoolman_spool_id,
        layer_num,
    )
    await record_event(
        db,
        printer_id=printer_id,
        archive_id=archive_id,
        layer_num=layer_num,
        event=EVENT_SPOOL_LOADED,
        global_tray_id=global_tray,
        spool_id=spool_id,
        spoolman_spool_id=spoolman_spool_id,
        commit=commit,
    )


async def load_events(db: AsyncSession, printer_id: int, archive_id: int) -> list[PrintUsageEvent]:
    """The print's journal, in insertion order."""
    result = await db.execute(
        select(PrintUsageEvent)
        .where(
            PrintUsageEvent.printer_id == printer_id,
            PrintUsageEvent.archive_id == archive_id,
        )
        .order_by(PrintUsageEvent.id)
    )
    return list(result.scalars().all())


async def prune_finished(db: AsyncSession, retention_hours: int) -> int:
    """Delete journal rows of finished prints older than the retention window.

    Rows of a still-``printing`` archive are never touched, whatever their age
    — an active print's journal is its accounting record, not history yet.
    Returns the number of rows deleted.
    """
    from backend.app.models.archive import PrintArchive

    cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=retention_hours)
    result = await db.execute(
        delete(PrintUsageEvent).where(
            PrintUsageEvent.created_at < cutoff,
            PrintUsageEvent.archive_id.in_(select(PrintArchive.id).where(PrintArchive.status != "printing")),
        )
    )
    await db.commit()
    deleted = result.rowcount or 0
    if deleted:
        logger.info("[UsageJournal] Pruned %d journal row(s) older than %dh", deleted, retention_hours)
    return deleted


async def delete_for_archive(db: AsyncSession, archive_id: int) -> None:
    """Drop an archive's journal rows (SQLite honours no FK cascade)."""
    await db.execute(delete(PrintUsageEvent).where(PrintUsageEvent.archive_id == archive_id))
