"""One predicate for "this printer already has a live row for these bytes" (spec §3.4),
shared by on_print_start's restart-recovery download and the adoption path."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from backend.app.main import _find_live_hash_twin
from backend.app.models.archive import PrintArchive
from backend.app.models.printer import Printer


async def _printer(db, serial):
    p = Printer(name=serial, ip_address="10.0.0.1", access_code="00000000", serial_number=serial, model="X1C")
    db.add(p)
    await db.flush()
    return p


def _row(pid, *, h, plate=None, file_path="a/p.3mf", status="printing"):
    return PrintArchive(
        printer_id=pid,
        filename="p.3mf",
        file_path=file_path,
        file_size=1,
        print_name="p",
        content_hash=h,
        source_content_hash=h,
        plate_index=plate,
        status=status,
    )


@pytest.mark.asyncio
async def test_twin_found_self_excluded_and_plate_respected(db_session):
    """⚠️ ``mine`` carries a real ``file_path`` on purpose.

    That is the adoption path's own shape: it hashes the temp file and asks for
    a twin, and by the time a second caller could ask, its own row may already
    have the file attached. With ``file_path=""`` the ``file_path != ""`` clause
    alone would drop it and ``id != exclude_id`` would be untested — and "adopt
    yourself, then delete yourself" is the one outcome here that is silently
    catastrophic.
    """
    p = await _printer(db_session, "T1")
    twin = _row(p.id, h="a" * 64, plate=2)
    other_plate = _row(p.id, h="a" * 64, plate=1)
    mine = _row(p.id, h="a" * 64, plate=2, file_path="a/mine.3mf")
    db_session.add_all([twin, other_plate, mine])
    await db_session.commit()
    found = await _find_live_hash_twin(db_session, p.id, exclude_id=mine.id, content_hash="a" * 64, plate_index=2)
    assert found is not None and found.id == twin.id
    # ...and symmetrically: asking as the twin never answers with the twin.
    asked_as_twin = await _find_live_hash_twin(
        db_session, p.id, exclude_id=twin.id, content_hash="a" * 64, plate_index=2
    )
    assert asked_as_twin is not None and asked_as_twin.id == mine.id
    assert (
        await _find_live_hash_twin(db_session, p.id, exclude_id=mine.id, content_hash="b" * 64, plate_index=2) is None
    )


@pytest.mark.asyncio
async def test_the_newest_candidate_wins(db_session):
    """``created_at DESC``: a stuck older row must never outrank the live one."""
    p = await _printer(db_session, "T4")
    older = _row(p.id, h="d" * 64, plate=3, file_path="a/older.3mf")
    older.created_at = datetime.now(timezone.utc) - timedelta(days=1)
    newer = _row(p.id, h="d" * 64, plate=3, file_path="a/newer.3mf")
    db_session.add_all([older, newer])
    await db_session.commit()
    found = await _find_live_hash_twin(db_session, p.id, exclude_id=-1, content_hash="d" * 64, plate_index=3)
    assert found is not None and found.id == newer.id


@pytest.mark.asyncio
async def test_only_live_populated_rows_on_this_printer_count(db_session):
    p1 = await _printer(db_session, "T2")
    p2 = await _printer(db_session, "T3")
    db_session.add_all(
        [
            _row(p2.id, h="c" * 64),  # other printer
            _row(p1.id, h="c" * 64, status="completed"),  # not live
            _row(p1.id, h="c" * 64, file_path=""),  # no bytes on disk
        ]
    )
    await db_session.commit()
    assert await _find_live_hash_twin(db_session, p1.id, exclude_id=-1, content_hash="c" * 64, plate_index=None) is None
