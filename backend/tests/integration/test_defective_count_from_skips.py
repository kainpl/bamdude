"""Skipped objects raise the running print's defective-part counter.

The write is ``max(current, len(skipped))`` rather than an increment, and both
halves of that matter. The callback carries the whole skipped list, so adding
would double-count the moment the same list arrives twice; and an operator may
have typed a *higher* number by hand — parts that finished printing but came out
unusable — which a later skip must not pull back down.
"""

from unittest.mock import patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.app.models.archive import PrintArchive
from backend.app.services.printer_manager import _record_skipped_as_defective


@pytest.fixture
def patched_session(test_engine):
    """Point the coroutine's own ``async_session`` at the test database.

    It opens a session itself rather than taking one — it runs from an MQTT
    callback, where there is no request to borrow a session from.
    """
    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    with patch("backend.app.core.database.async_session", maker):
        yield maker


async def _archive(db_session, *, status="printing", printer_id=1, defective=0) -> PrintArchive:
    archive = PrintArchive(
        printer_id=printer_id,
        filename="skips.3mf",
        print_name="Skips",
        file_path="/tmp/skips.3mf",
        file_size=1024,
        content_hash=f"defective_hash_{status}_{printer_id}_{defective}",
        status=status,
        quantity=12,
        defective_count=defective,
    )
    db_session.add(archive)
    await db_session.commit()
    await db_session.refresh(archive)
    return archive


@pytest.mark.asyncio
@pytest.mark.integration
async def test_skipped_objects_raise_the_counter(patched_session, db_session):
    archive = await _archive(db_session)

    await _record_skipped_as_defective(1, [941, 942, 943])

    await db_session.refresh(archive)
    assert archive.defective_count == 3


@pytest.mark.asyncio
@pytest.mark.integration
async def test_the_same_list_arriving_twice_does_not_double_count(patched_session, db_session):
    """The callback fires from two places by design — see the MQTT client."""
    archive = await _archive(db_session)

    await _record_skipped_as_defective(1, [941, 942])
    await _record_skipped_as_defective(1, [941, 942])

    await db_session.refresh(archive)
    assert archive.defective_count == 2, "the list is a total, not a delta"


@pytest.mark.asyncio
@pytest.mark.integration
async def test_a_hand_typed_higher_count_survives_a_later_skip(patched_session, db_session):
    """Parts can be scrap without ever having been skipped."""
    archive = await _archive(db_session, defective=5)

    await _record_skipped_as_defective(1, [941])

    await db_session.refresh(archive)
    assert archive.defective_count == 5, "automatic counting may only raise the number"


@pytest.mark.asyncio
@pytest.mark.integration
async def test_an_empty_list_changes_nothing(patched_session, db_session):
    archive = await _archive(db_session, defective=2)

    await _record_skipped_as_defective(1, [])

    await db_session.refresh(archive)
    assert archive.defective_count == 2


@pytest.mark.asyncio
@pytest.mark.integration
async def test_no_running_print_is_not_an_error(patched_session, db_session):
    """Skips only happen mid-print, so a missing archive is a lost race rather
    than a state to repair — and it must not take the MQTT callback down."""
    archive = await _archive(db_session, status="completed")

    await _record_skipped_as_defective(1, [941, 942])

    await db_session.refresh(archive)
    assert archive.defective_count == 0


@pytest.mark.asyncio
@pytest.mark.integration
async def test_only_the_printer_that_skipped_is_touched(patched_session, db_session):
    mine = await _archive(db_session, printer_id=1)
    other = await _archive(db_session, printer_id=2)

    await _record_skipped_as_defective(1, [941, 942])

    await db_session.refresh(mine)
    await db_session.refresh(other)
    assert mine.defective_count == 2
    assert other.defective_count == 0
