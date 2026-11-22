"""«Result not checked yet»: the one predicate and the one writer.

Whether the parts of a print are good is the DEFECT COUNT, and it has its own
single writer (``services/archive_defects``). This module adds no verdict beside
it. It only knows whether somebody has LOOKED at what came off the plate:

* a print is **unchecked** when it is ``completed``, not in the trash, and its
  ``outcome_checked_at`` is NULL — :func:`is_unchecked` for a loaded row and
  :func:`unchecked_clause`, its SQL twin, for the list's filter and the sweep.
  A failed, cancelled or still running print is never «unchecked», and neither is
  a calibration print — its result is read in the calibration wizard, and it has
  no parts to grade;
* the print is marked by every defects write (an explicit zero too — the
  defects writer calls :func:`mark_checked`) and by the explicit «checked, no
  defects» doors (the archive route, the Telegram button, a 👍 reaction);
* each unchecked print gets ONE reminder (:func:`remind_due`), stamped and
  committed before it is sent: at most once, a failed send is not repeated.

``outcome_checked_at`` and ``outcome_reminded_at`` are written here and nowhere
else (``tests/unit/test_archive_outcome_has_one_writer.py``). Nothing here
commits a caller's transaction; the sweep owns its own short one.

m248 stamped every print that existed at the upgrade as checked, so only prints
that end afterwards can read as unchecked or be reminded about.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, select, update

from backend.app.models.archive import PrintArchive

logger = logging.getLogger(__name__)

#: The list filter's value (``GET /archives?outcome=unchecked``).
UNCHECKED = "unchecked"

SETTING_KEY = "outcome_reminder_after_minutes"
DEFAULT_REMINDER_MINUTES = 60
MIN_REMINDER_MINUTES = 5
MAX_REMINDER_MINUTES = 10080
#: Prints stamped per sweep. A farm finishes far fewer per minute; what is left waits one tick.
REMINDER_BATCH = 20


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def is_unchecked(archive: PrintArchive) -> bool:
    """Whether nobody has looked at this completed print's result yet."""
    return (
        archive.status == "completed"
        and archive.outcome_checked_at is None
        and archive.deleted_at is None
        and not archive.is_calibration
    )


def unchecked_clause():
    """The SQL twin of :func:`is_unchecked`."""
    return and_(
        PrintArchive.status == "completed",
        PrintArchive.outcome_checked_at.is_(None),
        PrintArchive.deleted_at.is_(None),
        PrintArchive.is_calibration.is_not(True),
    )


async def mark_checked(db, archive: PrintArchive, *, by: int | None = None) -> bool:
    """Somebody looked at this print's result. True when this call made the mark.

    Idempotent: the first mark wins and a later one changes nothing. Says nothing
    about fitness and never touches the defect count. Flushes; the caller commits.
    ``by`` is the acting user, for the log — the mark itself carries no author
    (who recorded defects is the order journal's and the completion receipt's).
    """
    if archive.outcome_checked_at is not None:
        return False
    archive.outcome_checked_at = _now()
    await db.flush()
    logger.info("archive %s: result checked (user %s)", archive.id, by if by is not None else "-")
    return True


async def mark_checked_by_swap(db, archive: PrintArchive) -> bool:
    """The plate was changed by the swap itself: our change_table answered, or the file's own sequence cleared it.

    Nobody takes such a print off the plate, so nobody is asked to grade it (owner, 2026-11-20). A swap that
    failed or was held leaves the print on the plate, and the person who comes to it grades it there — that is
    why the swap, not the printer's swap-mode setting, decides. No status check: the live completion runs the
    macro BEFORE it writes the archive's terminal status, so the mark is in place before the print could read
    as unchecked. First mark wins; flushes, the caller commits.
    """
    if archive.outcome_checked_at is not None:
        return False
    archive.outcome_checked_at = _now()
    await db.flush()
    logger.info("archive %s: result counted as checked — the plate was swapped automatically", archive.id)
    return True


def forget(archive: PrintArchive) -> None:
    """A row taken back as the print now running keeps nothing of the check its earlier end got."""
    archive.outcome_checked_at = None
    archive.outcome_reminded_at = None


def carry_restored(archive: PrintArchive, checked_at: datetime | None, *, insert: bool) -> bool:
    """The Git metadata restore's door; True when a mark was written. Never commits.

    A local mark is never replaced (first mark wins). The backup's own moment is
    taken as recorded; a restored print the backup has none for — an older backup,
    an unreadable value — comes back as checked at its own end: a restore must not
    turn the history into «not checked yet» or release reminders about it. An
    existing local print without a mark in either place stays what it is.
    """
    if archive.outcome_checked_at is not None:
        return False
    if isinstance(checked_at, datetime):
        stamp = checked_at.astimezone(timezone.utc).replace(tzinfo=None) if checked_at.tzinfo else checked_at
    elif insert:
        stamp = archive.completed_at or archive.created_at or _now()
        if stamp.tzinfo is not None:
            stamp = stamp.astimezone(timezone.utc).replace(tzinfo=None)
    else:
        return False
    archive.outcome_checked_at = stamp
    return True


class NotCheckable(Exception):
    """The explicit mark was asked of a print that is not a completed one."""


async def mark_checked_explicitly(db, archive: PrintArchive, *, by: int | None = None) -> bool:
    """The «checked, no defects» doors: only a completed print outside the trash takes the mark."""
    if archive.status != "completed" or archive.deleted_at is not None:
        raise NotCheckable(str(archive.status))
    return await mark_checked(db, archive, by=by)


# ── the reminder ──


@dataclass(frozen=True)
class DueReminder:
    archive_id: int
    identity: str | None
    printer_id: int | None
    print_name: str
    completed_at: datetime | None


def reminder_delay_minutes(raw: str | None) -> int:
    """The stored setting as minutes, inside its bounds; anything unreadable is the default."""
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return DEFAULT_REMINDER_MINUTES
    return max(MIN_REMINDER_MINUTES, min(MAX_REMINDER_MINUTES, value))


async def claim_due_reminders(db, *, delay_minutes: int, now: datetime | None = None) -> list[DueReminder]:
    """Stamp the unchecked prints whose reminder is due and COMMIT; returns the ones this call stamped.

    At most once per print: the stamp is a compare-and-set on ``outcome_reminded_at
    IS NULL`` (and on the print still being unchecked), committed before anything
    is sent. Whether anybody subscribed is not asked — a print is stamped either
    way, so switching the event on later never releases a backlog of old prints.
    """
    now = now or _now()
    cutoff = now - timedelta(minutes=delay_minutes)
    rows = (
        await db.execute(
            select(
                PrintArchive.id,
                PrintArchive.recovery_identity,
                PrintArchive.printer_id,
                PrintArchive.print_name,
                PrintArchive.filename,
                PrintArchive.completed_at,
            )
            .where(
                unchecked_clause(),
                PrintArchive.outcome_reminded_at.is_(None),
                PrintArchive.completed_at.is_not(None),
                PrintArchive.completed_at <= cutoff,
            )
            .order_by(PrintArchive.completed_at, PrintArchive.id)
            .limit(REMINDER_BATCH)
        )
    ).all()
    if not rows:
        return []
    claimed: list[DueReminder] = []
    for archive_id, identity, printer_id, print_name, filename, completed_at in rows:
        result = await db.execute(
            update(PrintArchive)
            .where(PrintArchive.id == archive_id, unchecked_clause(), PrintArchive.outcome_reminded_at.is_(None))
            .values(outcome_reminded_at=now)
        )
        if result.rowcount:
            claimed.append(DueReminder(archive_id, identity, printer_id, print_name or filename or "", completed_at))
    await db.commit()
    return claimed


async def remind_due(session_factory=None, send=None) -> int:
    """One pass of the reminder sweep; returns how many reminders were handed to ``send``.

    Driven by the notification service's once-a-minute loop. The claim is one
    short transaction; each reminder is then sent in a session of its own, and a
    send that fails is logged and not repeated.
    """
    if session_factory is None:
        from backend.app.core.database import async_session as session_factory
    from backend.app.api.routes.settings import get_setting

    async with session_factory() as db:
        delay = reminder_delay_minutes(await get_setting(db, SETTING_KEY))
        due = await claim_due_reminders(db, delay_minutes=delay)
    if not due:
        return 0
    if send is None:
        from backend.app.services.notification_service import notification_service

        send = notification_service.on_print_outcome_unchecked
    sent = 0
    for reminder in due:
        try:
            async with session_factory() as db:
                await send(reminder, db)
            sent += 1
        except Exception:  # noqa: BLE001 — at most once: a failed reminder is not sent again
            logger.exception("Reminder about unchecked print %s was not sent", reminder.archive_id)
    return sent
