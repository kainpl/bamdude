"""«Result not checked yet»: when a print's outcome was looked at, and when it was reminded about.

``print_archives.outcome_checked_at`` is stamped when somebody records defects on
a print (an explicit zero too) or marks it «checked, no defects»; NULL on a
completed print means nobody has looked at what came off the plate.
``print_archives.outcome_reminded_at`` is the one reminder such a print gets.
Both are naive UTC, nullable, and written only by ``services/archive_outcome.py``.
This is not a verdict: whether the parts are good stays the defect count.

Every print that exists when the column is added is stamped as checked -- at its
own end, else its creation, else now -- so the history does not light up as
unchecked and no reminder is ever sent about a print from before the upgrade.
A print still running at that moment is left alone: it ends after the upgrade
and is new work. The stamp is written only in the run that ADDS the column, so
a repeated run (``DEBUG`` re-runs the latest migration) never marks prints that
finished since.

The index serves ``outcome_checked_at IS NULL`` -- the list's «Unchecked» filter
and the reminder sweep, which asks once a minute and must not walk the table.
"""

from datetime import datetime, timezone

from sqlalchemy import DateTime, bindparam, text

from backend.app.migrations.helpers import add_column

version = 248
name = "archive_outcome_checked"


async def upgrade(conn):
    added = await add_column(conn, "print_archives", "outcome_checked_at DATETIME")
    await add_column(conn, "print_archives", "outcome_reminded_at DATETIME")
    await conn.execute(
        text("CREATE INDEX IF NOT EXISTS ix_print_archives_outcome_checked_at ON print_archives (outcome_checked_at)")
    )
    if not added:
        return
    await conn.execute(
        text(
            "UPDATE print_archives "
            "SET outcome_checked_at = COALESCE(completed_at, created_at, :now) "
            "WHERE outcome_checked_at IS NULL AND (status IS NULL OR status <> 'printing')"
        ).bindparams(bindparam("now", datetime.now(timezone.utc).replace(tzinfo=None), type_=DateTime()))
    )
