"""The built-in inventory's usage history survives a Git backup and its restore.

It did not: the export wrote the rows under the key ``history`` and without the archive each belongs to, while
the restore read ``usage_history`` — so a restore of our own backup brought back no usage row at all. The export
also kept only the newest 500 rows. Backups made before the fix carry the old key and are still read.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from sqlalchemy import delete, select

from backend.app.models.spool import Spool
from backend.app.models.spool_usage_history import SpoolUsageHistory
from backend.app.services.git_restore import SPOOL_USAGE_PATH
from backend.tests.integration.test_git_backup_spoolman_usage import a_printer, an_archive, backed_up, restore

pytestmark = pytest.mark.integration

WHEN = datetime(2026, 10, 1, 13, 30, 5)


async def seeded(db) -> tuple[Spool, int]:
    """A spool, a printer, an archive and two usage rows; returns the spool and the archive's id."""
    spool = Spool(material="PETG", brand="Bambu Lab", color_name="Green", label_weight=1000, core_weight=250)
    printer, archive = a_printer(), an_archive()
    db.add_all([spool, printer, archive])
    await db.commit()
    db.add_all(
        [
            SpoolUsageHistory(
                spool_id=spool.id,
                printer_id=printer.id,
                archive_id=archive.id,
                print_name="benchy",
                weight_used=12.5,
                percent_used=1,
                status="completed",
                cost=0.31,
                created_at=WHEN,
            ),
            SpoolUsageHistory(
                spool_id=spool.id,
                print_name="bracket",
                weight_used=40.0,
                percent_used=4,
                status="failed",
                created_at=WHEN + timedelta(days=1),
            ),
        ]
    )
    await db.commit()
    return spool, archive.id


async def usage_rows(db) -> list[SpoolUsageHistory]:
    db.expire_all()
    return list((await db.execute(select(SpoolUsageHistory).order_by(SpoolUsageHistory.created_at))).scalars())


def facts(row: SpoolUsageHistory) -> tuple:
    return (row.spool_id, row.print_name, row.weight_used, row.percent_used, row.status, row.cost, row.created_at)


@pytest.mark.asyncio
async def test_the_usage_rows_come_back_from_our_own_backup(db_session):
    spool, archive_id = await seeded(db_session)
    before = [facts(row) for row in await usage_rows(db_session)]
    files = await backed_up(db_session)

    await db_session.execute(delete(SpoolUsageHistory))
    await db_session.commit()
    await restore(db_session, files)

    after = await usage_rows(db_session)
    assert [facts(row) for row in after] == before
    assert [row.archive_id for row in after] == [archive_id, None], "the print a row belongs to travels with it"


@pytest.mark.asyncio
async def test_a_second_restore_adds_nothing(db_session):
    await seeded(db_session)
    files = await backed_up(db_session)
    await restore(db_session, files)
    await restore(db_session, files)
    assert len(await usage_rows(db_session)) == 2


@pytest.mark.asyncio
async def test_a_backup_made_before_the_fix_is_still_read(db_session):
    """Older backups hold the rows under ``history``. They carry no archive, so the rows come back unlinked."""
    await seeded(db_session)
    files = await backed_up(db_session)
    payload = files[SPOOL_USAGE_PATH]
    payload["history"] = [
        {k: v for k, v in entry.items() if k != "archive_id"} for entry in payload.pop("usage_history")
    ]

    await db_session.execute(delete(SpoolUsageHistory))
    await db_session.commit()
    await restore(db_session, files)

    after = await usage_rows(db_session)
    assert [row.print_name for row in after] == ["benchy", "bracket"]
    assert [row.archive_id for row in after] == [None, None]


@pytest.mark.asyncio
async def test_every_row_is_in_the_backup_not_only_the_newest(db_session):
    spool, _archive_id = await seeded(db_session)
    db_session.add_all(
        [
            SpoolUsageHistory(
                spool_id=spool.id,
                print_name=f"part {index}",
                weight_used=1.0,
                status="completed",
                created_at=WHEN + timedelta(days=2, minutes=index),
            )
            for index in range(520)
        ]
    )
    await db_session.commit()
    files = await backed_up(db_session)
    assert len(files[SPOOL_USAGE_PATH]["usage_history"]) == 522
