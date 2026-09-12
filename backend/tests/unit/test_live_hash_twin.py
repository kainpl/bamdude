"""One predicate for "this printer already has a live row for these bytes" (spec §3.4),
shared by on_print_start's restart-recovery download and the adoption path."""

from __future__ import annotations

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
    p = await _printer(db_session, "T1")
    twin = _row(p.id, h="a" * 64, plate=2)
    other_plate = _row(p.id, h="a" * 64, plate=1)
    mine = _row(p.id, h="a" * 64, plate=2, file_path="")
    db_session.add_all([twin, other_plate, mine])
    await db_session.commit()
    found = await _find_live_hash_twin(db_session, p.id, exclude_id=mine.id, content_hash="a" * 64, plate_index=2)
    assert found is not None and found.id == twin.id
    assert (
        await _find_live_hash_twin(db_session, p.id, exclude_id=mine.id, content_hash="b" * 64, plate_index=2) is None
    )


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
