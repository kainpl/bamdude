"""The calibration session's link to its job is written in the insert's own transaction (G03 B, A7 / A24 F5;
acceptance TA32 F5).

``enqueue_calibration_print`` inserts the job and links its session to it under the queue's lane, one commit: the
statement log shows the session's UPDATE between the job's INSERT and that commit, with no commit between them, and a
delete of the job right after the commit meets no later link write.
"""

from __future__ import annotations

import re

import pytest
from sqlalchemy import delete, event

from backend.app.core import database
from backend.app.models.calibration_session import CalibrationSession
from backend.app.models.print_queue import PrintQueueItem
from backend.tests.integration.test_light_writers_take_the_lane import a_queue, calibration_insert

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


async def test_the_session_link_is_written_inside_the_insert_s_transaction(db_session, printer_factory, monkeypatch):
    queue_id = await a_queue(db_session, printer_factory)
    session = CalibrationSession(
        printer_id=queue_id,
        cali_mode="flow",
        method="manual",
        nozzle_diameter=0.4,
        nozzle_volume_type="normal",
        filaments_json="[]",
        status="printing",
    )
    db_session.add(session)
    await db_session.commit()
    statements: list[str] = []

    def seen(_conn, _cursor, statement, *_args):
        statements.append(statement)

    def committed(_conn):
        statements.append("COMMIT")

    engine = db_session.bind.sync_engine
    event.listen(engine, "before_cursor_execute", seen)
    event.listen(engine, "commit", committed)
    try:
        from backend.app.services import background_dispatch

        monkeypatch.setattr(background_dispatch, "async_session", database.async_session)
        item_id = await background_dispatch.enqueue_calibration_print(
            printer_id=queue_id,
            asset_path="calib.3mf",
            cali_mode="flow",
            user_id=None,
            ams_id=0,
            slot_id=0,
            tray_id=0,
            calibration_session_id=session.id,
        )
        after_the_commit = len(statements)
        async with database.async_session() as db:
            assert (await db.get(CalibrationSession, session.id)).print_queue_item_id == item_id
            await db.execute(delete(PrintQueueItem).where(PrintQueueItem.id == item_id))
            await db.commit()
    finally:
        event.remove(engine, "before_cursor_execute", seen)
        event.remove(engine, "commit", committed)

    insert = next(i for i, s in enumerate(statements) if re.match(r"\s*INSERT INTO print_queue\b", s))
    link = next(i for i, s in enumerate(statements) if re.match(r"\s*UPDATE calibration_session\b", s))
    commit = next(i for i, s in enumerate(statements) if i > insert and s == "COMMIT")
    assert insert < link < commit, "the link is part of the insert's transaction"
    assert not any(re.match(r"\s*UPDATE calibration_session\b", s) for s in statements[after_the_commit:]), (
        "no link write after the insert committed"
    )
