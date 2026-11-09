"""A stable connection's ordinary completion needs no reconnect to free claims."""

import pytest
from sqlalchemy import select

from backend.app import main
from backend.app.models.archive import PrintArchive
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.printer_queue import PrinterQueue
from backend.app.services.print_execution import decide_start, publish_start
from backend.app.services.print_observation import PrintObservation
from backend.tests.integration.swap_terminal_support import an_execution, make_rig  # noqa: F401


@pytest.mark.asyncio
async def test_only_current_execution_completes_and_books_without_sweep(make_rig, monkeypatch):
    from unittest.mock import AsyncMock

    from backend.app.services import usage_tracker

    rig = await make_rig(require_plate_clear=False, swap_mode=False)
    old_id, old_item_id = await an_execution(rig, fact=False, name="a")
    async with rig.db() as db:
        old = await db.get(PrintArchive, old_id)
        old.subtask_id = "81"
        await db.commit()
        decision = await decide_start(
            db,
            rig.printer_id,
            {
                "subtask_id": "82",
                "_print_observation": PrintObservation("different_client_run_id", "81", "82", "RUNNING", 1),
            },
        )
        new = PrintArchive(
            printer_id=rig.printer_id,
            queue_id=rig.queue_id,
            filename="b.3mf",
            print_name="b",
            file_path="",
            file_size=0,
            status="printing",
            subtask_id="82",
            extra_data={},
        )
        await publish_start(db, new, decision)
        new_id = new.id
        main._bind_print_file_analysis_context(rig.printer_id, new, origin="external")
    library = AsyncMock()
    monkeypatch.setattr(main, "_bump_library_file_usage", library)
    await main.on_print_complete(
        rig.printer_id, {"status": "completed", "filename": "b.3mf", "subtask_name": "b", "subtask_id": "82"}
    )
    # A new process has no terminal dedup or binding. Its durable marker still
    # vetoes A before any legacy printer-wide completion effect.
    rig.manager._print_run_bindings = {}
    rig.manager._print_run_finishing_bindings = {}
    rig.manager._print_terminal_generations = {}
    rig.manager._print_effect_generations = {}
    main.ws_manager.send_print_complete.reset_mock()
    # Replaying an old terminal must not book the new archive a second time.
    await main.on_print_complete(
        rig.printer_id, {"status": "completed", "filename": "a.gcode.3mf", "subtask_name": "a", "subtask_id": "81"}
    )
    main.ws_manager.send_print_complete.assert_not_called()
    if rig.tasks:
        import asyncio

        await asyncio.gather(*rig.tasks)
    async with rig.db() as db:
        old, new = await db.get(PrintArchive, old_id), await db.get(PrintArchive, new_id)
        item = await db.get(PrintQueueItem, old_item_id)
        assert old.status == item.status == "cancelled"
        assert item.error_code == "noncurrent_execution_claim"
        assert new.status == "completed"
        active = list((await db.scalars(select(PrintQueueItem).where(PrintQueueItem.status == "printing"))).all())
        assert active == []
        assert (await db.get(PrinterQueue, rig.queue_id)).is_paused
    assert usage_tracker.on_print_complete.await_count == 1
    library.assert_awaited_once()
    rig.macro.assert_not_called()
