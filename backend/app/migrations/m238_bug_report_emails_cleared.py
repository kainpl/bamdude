"""Addresses stored with bug reports are cleared (upstream 346b58ed8).

A bug report may carry the reporter's e-mail address so the maintainers can
answer. It is sent to the report relay with the report; nothing on this
installation ever read the copy kept in ``bug_reports.reporter_email``. A
personal address nobody uses is not kept: new reports no longer store it, and
this clears the ones stored before.

The column stays — dropping it would need a table rebuild on SQLite for no
gain — and stays NULL from here on. A second run finds nothing to clear.

Tables and columns are named here: an ORM model would bring today's columns
into a step of the chain where they may not exist.
"""

import logging

logger = logging.getLogger(__name__)

version = 238
name = "bug_report_emails_cleared"


async def upgrade(conn):
    """No schema change; the stored addresses are cleared by seed."""


async def seed(session_factory):
    from sqlalchemy import Integer, String, column, table, update

    reports = table("bug_reports", column("id", Integer), column("reporter_email", String))

    async with session_factory() as db:
        result = await db.execute(
            update(reports).where(reports.c.reporter_email.is_not(None)).values(reporter_email=None)
        )
        await db.commit()
        cleared = result.rowcount or 0
    if cleared:
        logger.info("m238: cleared the e-mail address stored with %d bug report(s)", cleared)
