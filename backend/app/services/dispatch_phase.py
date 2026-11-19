"""How far one dispatch attempt got, on its execution archive (post-rebuild D-R3, m207).

The RAM half of the fact is ``PrintDispatchJob.send_fact`` — refused / sent / accepted,
set right where the runner learns it. This module is its durable half and the ONE
writer of ``print_archives.dispatch_phase`` / ``dispatch_boot_id``:

* ``prepared`` — stamped on the execution archive in the transaction that inserts it,
  with the boot of the process that owns the attempt;
* ``sending`` — committed by the runner's own session immediately before the start
  command; a commit that fails or a compare-and-set that loses forbids the send;
* ``sent`` — after the command was queued for publish;
* ``accepted`` — after the printer was seen running it, never on an acknowledgement
  alone.

Every transition is a compare-and-set on this attempt's archive: still ``printing``,
still the phase it comes from, still the boot that wrote it. Nothing moves a phase
back, and nothing writes over a terminal row. A refusal is not a phase: the
``sending`` row of a refused start is closed by its owner, not rolled back to
``prepared``. NULL is unknown, and is never inferred from a name or an age.
"""

from __future__ import annotations

from sqlalchemy import update

from backend.app.models.archive import PrintArchive

PREPARED = "prepared"
SENDING = "sending"
SENT = "sent"
ACCEPTED = "accepted"

#: An attempt our dispatch never saw running: nothing but its own owner, or a proof, may end it.
UNACCEPTED = (PREPARED, SENDING, SENT)

#: Where each phase may be reached from. ``accepted`` also from ``sending``: the
#: ``sent`` write is bookkeeping, and a failed one must not stop the next fact.
_FROM: dict[str, tuple[str, ...]] = {
    SENDING: (PREPARED,),
    SENT: (SENDING,),
    ACCEPTED: (SENDING, SENT),
}


def current_boot_id() -> str | None:
    """The boot of this process as the farm startup guard owns it, or None when it owns nothing."""
    from backend.app.services.farm_startup_guard import farm_startup_guard

    return farm_startup_guard.boot_id()


def stamp_prepared(archive) -> str | None:
    """Mark a just-created execution archive as this process's prepared attempt; returns the boot written."""
    boot = current_boot_id()
    archive.dispatch_phase = PREPARED
    archive.dispatch_boot_id = boot
    return boot


def mark_seen_running(archive) -> bool:
    """The printer was seen running this archive's print: an unaccepted attempt is ``accepted``.

    Called where ``on_print_start`` takes the archive as the print now running — the
    expected archive of a dispatch (also after the watchdog had failed a slow parse) and
    the adoption of a printing row after a restart. Whoever's attempt it was, the start
    is a fact now. Set on the caller's loaded row, in the caller's transaction.
    """
    if archive.dispatch_phase in UNACCEPTED:
        archive.dispatch_phase = ACCEPTED
        return True
    return False


def forget_earlier_outcome(archive) -> None:
    """A row taken back as the print now running keeps nothing of the end it had.

    The dispatch watchdog fails a slow attempt (``failed`` + its reason, its
    message and an end time); when the printer then reports that very job
    running, the row is the live print again. Without this the print that
    completed still read "dispatch: no acknowledgement", and one that really
    failed kept the dispatch reason instead of the printer's own (upstream
    a8270b8fa / bb0bf03f6, adapted: our only row reuse is this one).
    Set on the caller's loaded row, in the caller's transaction.
    """
    archive.failure_reason = None
    archive.error_message = None
    archive.completed_at = None
    # Nor the «result checked» mark and reminder of that end (the outcome writer's columns).
    from backend.app.services import archive_outcome

    archive_outcome.forget(archive)


async def advance(db, archive_id: int | None, to: str, *, boot_id: str | None) -> bool:
    """Move this attempt's archive forward to ``to``; False when the row is no longer this attempt's.

    Executes on the caller's session and does not commit: the caller decides what the
    write is part of (the ``sending`` commit before a start, a bookkeeping write after).
    """
    if archive_id is None:
        return False
    boot = PrintArchive.dispatch_boot_id.is_(None) if boot_id is None else PrintArchive.dispatch_boot_id == boot_id
    result = await db.execute(
        update(PrintArchive)
        .where(
            PrintArchive.id == archive_id,
            PrintArchive.status == "printing",
            PrintArchive.dispatch_phase.in_(_FROM[to]),
            boot,
        )
        .values(dispatch_phase=to)
        .execution_options(synchronize_session=False)
    )
    return result.rowcount == 1
