"""m275 — H3C / P3C queue rows saved before stage A go to review; nothing is guessed (N01 stage B).

A pending row on an H3C / P3C printer whose ``ams_mapping`` still names 128-138:
an automatic one loses its stale plan, every other one is marked
``review_required`` (pinned, an old row without an intent, a calibration, raw
G-code). Other printers, other states and other values are untouched, and a
second run changes nothing. The PostgreSQL arm is
``tests/integration/test_m275_postgres.py``.
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.app.migrations.m275_rack_queue_rows_review import seed
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.printer import Printer
from backend.app.models.printer_queue import PrinterQueue

AUTO = json.dumps({"version": 2, "mode": "auto", "feed_policy": "auto"})
PINNED = json.dumps({"version": 2, "mode": "pinned", "feed_policy": "auto", "physical_pins": {"1": {"source_id": 130}}})


async def _setup(sm) -> dict[str, int]:
    async with sm() as db:
        for pid, model, serial in ((1, "H3C", "M274A"), (2, "P3C", "M274B"), (3, "X1C", "M274C")):
            db.add(
                Printer(
                    id=pid, name=f"p{pid}", serial_number=serial, ip_address="127.0.0.1", access_code="0", model=model
                )
            )
            db.add(PrinterQueue(id=pid, printer_id=pid))
        await db.flush()
        rows = {
            "auto": PrintQueueItem(
                queue_id=1, position=1, status="pending", ams_mapping="[130, -1]", filament_routing=AUTO
            ),
            "pinned": PrintQueueItem(
                queue_id=1, position=2, status="pending", ams_mapping="[130]", filament_routing=PINNED
            ),
            "legacy": PrintQueueItem(queue_id=1, position=3, status="pending", ams_mapping="[128]"),
            "calibration": PrintQueueItem(
                queue_id=1,
                position=4,
                status="pending",
                ams_mapping="[133]",
                is_calibration=True,
                filament_routing=AUTO,
            ),
            "p3c": PrintQueueItem(queue_id=2, position=1, status="pending", ams_mapping="[138]", filament_routing=AUTO),
            "printing": PrintQueueItem(
                queue_id=1, position=5, status="printing", ams_mapping="[130]", filament_routing=AUTO
            ),
            "new_numbers": PrintQueueItem(
                queue_id=1, position=6, status="pending", ams_mapping="[30]", filament_routing=AUTO
            ),
            "x1c": PrintQueueItem(queue_id=3, position=1, status="pending", ams_mapping="[130]", filament_routing=AUTO),
        }
        db.add_all(rows.values())
        await db.commit()
        return {name: row.id for name, row in rows.items()}


async def _rows(sm, ids):
    async with sm() as db:
        found = (await db.execute(select(PrintQueueItem).where(PrintQueueItem.id.in_(ids.values())))).scalars().all()
        by_id = {r.id: r for r in found}
        return {name: (by_id[i].ams_mapping, by_id[i].filament_routing) for name, i in ids.items()}


@pytest.mark.asyncio
async def test_old_rack_rows_are_cleared_or_sent_to_review(test_engine):
    sm = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    ids = await _setup(sm)
    before = await _rows(sm, ids)
    await seed(sm)
    after = await _rows(sm, ids)

    assert after["auto"] == (None, AUTO)  # its plan cleared, its intent kept
    assert after["p3c"] == (None, AUTO)
    for name in ("pinned", "legacy", "calibration"):
        mapping, routing = after[name]
        assert mapping == before[name][0]  # the operator's numbers are kept, never translated
        assert json.loads(routing)["review_required"] is True
    assert json.loads(after["pinned"][1])["physical_pins"] == {"1": {"source_id": 130}}
    assert json.loads(after["legacy"][1]) == {"review_required": True}
    for name in ("printing", "new_numbers", "x1c"):
        assert after[name] == before[name]


@pytest.mark.asyncio
async def test_a_second_run_changes_nothing(test_engine):
    sm = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    ids = await _setup(sm)
    await seed(sm)
    once = await _rows(sm, ids)
    await seed(sm)
    assert await _rows(sm, ids) == once
