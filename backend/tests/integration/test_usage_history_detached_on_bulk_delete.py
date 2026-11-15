"""Usage history outlives the archive and the printer it names — unlinked, in code.

``spoolman_usage_history.archive_id`` (like the built-in ``spool_usage_history``'s)
references ``print_archives`` with no ``ON DELETE``: the row is a spool's
consumption record and stays. Only ``ArchiveService.delete_archive`` unlinked
it. The two bulk Core deletes — a user deleted with their items, and the trash
sweeper's leftover delete — never pass through it: on PostgreSQL the delete is
refused by the foreign key, on SQLite the row keeps an id naming nothing and
the next archive to take that id is shown as the charge's print (review
accounting I3). ``printer_id`` is ``SET NULL``, which SQLite does not run: a
printer added later under the reused id was shown as those charges' printer
(review accounting M2).
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from backend.app.models.archive import PrintArchive
from backend.app.models.settings import Settings
from backend.app.models.spool import Spool
from backend.app.models.spool_usage_history import SpoolUsageHistory
from backend.app.models.spoolman_usage_history import SpoolmanUsageHistory
from backend.app.models.user import User


async def _archive(db, **fields) -> PrintArchive:
    row = PrintArchive(file_path="", file_size=0, print_name="job", filename="job.gcode.3mf", status="completed")
    for key, value in fields.items():
        setattr(row, key, value)
    db.add(row)
    await db.flush()
    return row


async def _history(db, archive_id: int | None, printer_id: int | None = None) -> tuple[int, int]:
    """One Spoolman-mode row and one built-in row naming the archive."""
    spool = Spool(material="PLA", rgba="FF0000FF", label_weight=1000, weight_used=0)
    db.add(spool)
    await db.flush()
    spoolman_row = SpoolmanUsageHistory(
        source_token="a" * 32,
        spoolman_spool_id=7,
        printer_id=printer_id,
        archive_id=archive_id,
        weight_used=42.0,
        created_at=datetime(2026, 11, 1),
    )
    builtin_row = SpoolUsageHistory(spool_id=spool.id, printer_id=printer_id, archive_id=archive_id, weight_used=42.0)
    db.add_all([spoolman_row, builtin_row])
    await db.flush()
    return spoolman_row.id, builtin_row.id


async def _links(db, spoolman_id: int, builtin_id: int) -> tuple:
    db.expire_all()
    spoolman_row = (
        await db.execute(select(SpoolmanUsageHistory).where(SpoolmanUsageHistory.id == spoolman_id))
    ).scalar_one()
    builtin_row = (await db.execute(select(SpoolUsageHistory).where(SpoolUsageHistory.id == builtin_id))).scalar_one()
    return spoolman_row, builtin_row


@pytest.mark.asyncio
@pytest.mark.integration
async def test_deleting_a_user_with_their_items_unlinks_the_history_of_their_prints(db_session):
    from backend.app.api.routes.users import delete_user
    from backend.tests.auth_helpers import mint_test_authority

    owner = User(username="leaver")
    admin = User(username="admin", role="admin")
    db_session.add_all([owner, admin])
    await db_session.flush()
    archive = await _archive(db_session, created_by_id=owner.id)
    archive_id = archive.id
    ids = await _history(db_session, archive_id)
    await db_session.commit()
    admin._auth_capture = await mint_test_authority(db_session, username=admin.username)

    await delete_user(user_id=owner.id, delete_items=True, _admin=admin, current_user=admin, db=db_session)

    spoolman_row, builtin_row = await _links(db_session, *ids)
    assert (
        await db_session.execute(select(PrintArchive).where(PrintArchive.id == archive_id))
    ).scalar_one_or_none() is None
    assert spoolman_row.archive_id is None and spoolman_row.weight_used == 42.0
    assert builtin_row.archive_id is None and builtin_row.weight_used == 42.0


@pytest.mark.asyncio
@pytest.mark.integration
async def test_the_sweepers_leftover_delete_unlinks_the_history_too(db_session):
    """The row ``delete_archive`` left behind is removed by a bulk statement."""
    from backend.app.services.archive_purge import TRASH_RETENTION_KEY, archive_purge_service

    db_session.add(Settings(key=TRASH_RETENTION_KEY, value="7"))
    archive = await _archive(db_session, deleted_at=datetime.now(timezone.utc) - timedelta(days=30))
    archive_id = archive.id
    ids = await _history(db_session, archive_id)
    await db_session.commit()

    with patch.object(type(archive_purge_service), "hard_delete_now", AsyncMock(return_value=False)):
        await archive_purge_service._sweep(db_session)

    spoolman_row, builtin_row = await _links(db_session, *ids)
    assert (
        await db_session.execute(select(PrintArchive).where(PrintArchive.id == archive_id))
    ).scalar_one_or_none() is None
    assert spoolman_row.archive_id is None
    assert builtin_row.archive_id is None


@pytest.mark.asyncio
@pytest.mark.integration
async def test_deleting_a_printer_unlinks_it_from_spoolman_usage_history(
    async_client: AsyncClient, db_session, printer_factory
):
    printer = await printer_factory(name="Leaving")
    spoolman_id, _builtin_id = await _history(db_session, None, printer_id=printer.id)
    await db_session.commit()

    response = await async_client.delete(f"/api/v1/printers/{printer.id}")

    assert response.status_code == 200
    db_session.expire_all()
    row = (
        await db_session.execute(select(SpoolmanUsageHistory).where(SpoolmanUsageHistory.id == spoolman_id))
    ).scalar_one()
    assert row.printer_id is None and row.weight_used == 42.0
