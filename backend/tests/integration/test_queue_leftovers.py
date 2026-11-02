"""A finished queue row that nothing will ever clean up is retired, with its router row.

Live completion keeps a completed row while our table swap is held: the row is the
hold's record (swap-source-lifecycle R2-5), and only a later successful swap of the
same run cleans it. Once another print has started on that printer the hold can
never settle -- the owed macro is only ever sent while its execution still owns
the queue -- so the row is a skipped ordinary cleanup, and its assigned AutoQueue
partner hangs with it. Found on a farm 2026-11-01: seven such pairs, ten days old.
"""

from datetime import datetime, timedelta

import pytest
from sqlalchemy import select

import backend.app.models.printer_location  # noqa: F401  (Printer's string relationship)
from backend.app.models.archive import PrintArchive
from backend.app.models.auto_queue import AutoQueueItem
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.printer import Printer
from backend.app.models.printer_queue import PrinterQueue
from backend.app.models.queue_source import QueueSource
from backend.app.services import queue_leftovers

pytestmark = pytest.mark.integration

T0 = datetime(2026, 10, 22, 17, 20)
HOLD = {"swap_hold": {"reason": "swap_hold_run_unproven", "cycle": "not_started"}}


async def held_pair(db, printer_factory, archive_factory, *, later=True, stage=None, source=False):
    """A swap printer whose completed run was held, with its router row; optionally a later run."""
    printer = await printer_factory(swap_mode_enabled=True)
    queue = PrinterQueue(id=printer.id, printer_id=printer.id, status="printing")
    db.add(queue)
    await db.commit()
    extra = dict(HOLD)
    if stage:
        extra["bamdude_terminal_effects"] = {"swap_change_table": {"stage": stage, "owner": "live"}}
    archive = await archive_factory(
        printer.id, status="completed", started_at=T0, completed_at=T0 + timedelta(hours=4), extra_data=extra
    )
    source_id = None
    if source:
        blob = QueueSource(sha256="0" * 64, size_bytes=1, relative_path="objects/00/x.3mf", format="3mf")
        db.add(blob)
        await db.flush()
        source_id = blob.id
    row = PrintQueueItem(
        queue_id=queue.id,
        status="completed",
        archive_id=archive.id,
        position=1,
        completed_at=T0 + timedelta(hours=4),
        waiting_reason="Inspect the plate, then resume the queue.",
        queue_source_id=source_id,
    )
    db.add(row)
    await db.flush()
    auto = AutoQueueItem(status="assigned", assigned_to_item_id=row.id, target_model="P1S", position=1)
    db.add(auto)
    await db.flush()
    row.source_auto_item_id = auto.id
    if later:
        await archive_factory(printer.id, status="printing", started_at=T0 + timedelta(hours=4, minutes=5))
    await db.commit()
    return printer, queue, archive, row.id, auto.id


async def gone(db, model, row_id) -> bool:
    db.expire_all()
    return await db.get(model, row_id) is None


async def test_superseded_held_row_and_its_router_row_are_retired(db_session, printer_factory, archive_factory):
    printer, queue, archive, row_id, auto_id = await held_pair(db_session, printer_factory, archive_factory)
    archive_id = archive.id

    found = await queue_leftovers.find(db_session)
    assert [(f.kind, f.item_id, f.keep_reason) for f in found] == [("finished_row", row_id, None)]
    assert found[0].hold_reason == "swap_hold_run_unproven"

    result = await queue_leftovers.retire(item_ids=[row_id])

    assert result.retired_items == [row_id] and result.skipped == []
    assert await gone(db_session, PrintQueueItem, row_id)
    assert await gone(db_session, AutoQueueItem, auto_id)
    kept = await db_session.get(PrintArchive, archive_id)
    assert kept is not None and kept.extra_data["swap_hold"]["reason"] == "swap_hold_run_unproven"


async def test_a_row_with_no_later_print_on_its_printer_stays(db_session, printer_factory, archive_factory):
    *_, row_id, auto_id = await held_pair(db_session, printer_factory, archive_factory, later=False)

    assert await queue_leftovers.find(db_session) == []
    result = await queue_leftovers.retire(item_ids=[row_id])

    assert result.retired_items == [] and [(s.item_id, s.reason) for s in result.skipped] == [
        (row_id, "not_superseded")
    ]
    assert not await gone(db_session, PrintQueueItem, row_id)
    assert not await gone(db_session, AutoQueueItem, auto_id)


@pytest.mark.parametrize("guard", ["swap_owed", "plate_gate", "current_item", "archive_copy_missing"])
async def test_a_row_that_can_still_be_answered_is_kept_with_its_reason(
    db_session, printer_factory, archive_factory, guard
):
    printer, queue, archive, row_id, auto_id = await held_pair(
        db_session,
        printer_factory,
        archive_factory,
        stage="owed" if guard == "swap_owed" else None,
        source=guard == "archive_copy_missing",
    )
    if guard == "plate_gate":
        (await db_session.get(Printer, printer.id)).awaiting_plate_clear_archive_id = archive.id
    if guard == "current_item":
        (await db_session.get(PrinterQueue, queue.id)).current_item_id = row_id
    await db_session.commit()

    found = await queue_leftovers.find(db_session)
    assert [(f.item_id, f.keep_reason) for f in found] == [(row_id, guard)]
    result = await queue_leftovers.retire(item_ids=[row_id])

    assert result.retired_items == [] and [(s.item_id, s.reason) for s in result.skipped] == [(row_id, guard)]
    assert not await gone(db_session, PrintQueueItem, row_id)
    assert not await gone(db_session, AutoQueueItem, auto_id)


@pytest.mark.parametrize("status", ["failed", "cancelled", "skipped", "pending", "printing"])
async def test_rows_kept_for_retry_or_still_live_are_never_leftovers(
    db_session, printer_factory, archive_factory, status
):
    *_, row_id, _ = await held_pair(db_session, printer_factory, archive_factory)
    (await db_session.get(PrintQueueItem, row_id)).status = status
    await db_session.commit()

    assert await queue_leftovers.find(db_session) == []
    assert (await queue_leftovers.retire(item_ids=[row_id])).retired_items == []
    assert not await gone(db_session, PrintQueueItem, row_id)


async def test_an_orphan_assignment_is_retired_but_one_on_a_cancelled_row_is_not(
    db_session, printer_factory, archive_factory
):
    printer = await printer_factory()
    queue = PrinterQueue(id=printer.id, printer_id=printer.id)
    db_session.add(queue)
    await db_session.flush()
    cancelled = PrintQueueItem(queue_id=queue.id, status="cancelled", position=1)
    db_session.add(cancelled)
    await db_session.flush()
    orphan = AutoQueueItem(status="assigned", assigned_to_item_id=None, target_model="P1S")
    dangling = AutoQueueItem(status="assigned", assigned_to_item_id=987_654, target_model="P1S")
    retry = AutoQueueItem(status="assigned", assigned_to_item_id=cancelled.id, target_model="P1S")
    pending = AutoQueueItem(status="pending", assigned_to_item_id=None, target_model="P1S")
    db_session.add_all([orphan, dangling, retry, pending])
    await db_session.commit()

    found = await queue_leftovers.find(db_session)
    assert sorted(f.auto_item_id for f in found if f.kind == "orphan_assignment") == sorted([orphan.id, dangling.id])

    result = await queue_leftovers.retire(auto_item_ids=[orphan.id, dangling.id, retry.id])

    assert sorted(result.retired_autos) == sorted([orphan.id, dangling.id])
    assert [(s.auto_item_id, s.reason) for s in result.skipped] == [(retry.id, "not_orphan")]
    pending_id, retry_id = pending.id, retry.id
    assert not await gone(db_session, AutoQueueItem, pending_id)
    assert not await gone(db_session, AutoQueueItem, retry_id)


async def test_scoped_find_sees_only_the_named_printers(db_session, printer_factory, archive_factory):
    first, *_ = await held_pair(db_session, printer_factory, archive_factory)
    second, *_rest, second_row, _ = await held_pair(db_session, printer_factory, archive_factory)

    found = await queue_leftovers.find(db_session, printer_ids={second.id})

    assert [f.item_id for f in found] == [second_row]


async def test_the_automatic_pass_retires_what_the_manual_one_would(db_session, printer_factory, archive_factory):
    printer, _, _, row_id, auto_id = await held_pair(db_session, printer_factory, archive_factory)
    *_, kept_id, _ = await held_pair(db_session, printer_factory, archive_factory, stage="owed")

    result = await queue_leftovers.retire_for_printer(printer.id)

    assert result.retired_items == [row_id]
    assert await gone(db_session, AutoQueueItem, auto_id)
    assert not await gone(db_session, PrintQueueItem, kept_id)


async def test_the_connect_edge_sweep_retires_superseded_rows(db_session, printer_factory, archive_factory):
    from backend.app.services import print_reconciliation

    printer, _, _, row_id, auto_id = await held_pair(db_session, printer_factory, archive_factory)

    await print_reconciliation.reconcile_printer_prints(printer.id, "IDLE", "")

    assert await gone(db_session, PrintQueueItem, row_id)
    assert await gone(db_session, AutoQueueItem, auto_id)


async def test_a_completion_retires_what_it_superseded(db_session, printer_factory, archive_factory, monkeypatch):
    """The print that just finished started after the held one ended: the wrapper hands the printer to the pass."""
    import backend.app.main as main_module

    printer, _, _, row_id, auto_id = await held_pair(db_session, printer_factory, archive_factory)

    async def nothing(*args, **kwargs):
        return None

    monkeypatch.setattr(main_module, "_on_print_complete_impl", nothing)
    monkeypatch.setattr(main_module, "_release_unseeded_journal", nothing)

    await main_module.on_print_complete(printer.id, {"status": "completed"})

    assert await gone(db_session, PrintQueueItem, row_id)
    assert await gone(db_session, AutoQueueItem, auto_id)


async def test_api_lists_and_retires_under_queue_rights(async_client, db_session, printer_factory, archive_factory):
    printer, _, _, row_id, auto_id = await held_pair(db_session, printer_factory, archive_factory)

    listed = await async_client.get("/api/v1/queue/leftovers")
    assert listed.status_code == 200, listed.text
    body = listed.json()
    assert [(i["kind"], i["item_id"], i["retirable"]) for i in body["items"]] == [("finished_row", row_id, True)]
    assert body["items"][0]["printer_name"] == printer.name

    retired = await async_client.post("/api/v1/queue/leftovers/retire", json={"item_ids": [row_id]})
    assert retired.status_code == 200, retired.text
    assert retired.json()["retired_item_ids"] == [row_id]

    again = await async_client.post("/api/v1/queue/leftovers/retire", json={"item_ids": [row_id]})
    assert again.status_code == 200, again.text
    assert again.json()["retired_item_ids"] == []
    assert [(s["item_id"], s["reason"]) for s in again.json()["skipped"]] == [(row_id, "gone")]
    assert await gone(db_session, AutoQueueItem, auto_id)


async def test_api_requires_delete_all_to_retire(async_client, db_session, printer_factory, archive_factory):
    from backend.tests.integration.test_workshop_library_rights import _jwt, _user

    *_, row_id, _ = await held_pair(db_session, printer_factory, archive_factory)
    reader = await _user(db_session, "leftover_reader", ["queue:read", "queue:read_all", "queue:delete_own"])

    listed = await async_client.get("/api/v1/queue/leftovers", headers=_jwt(reader.username))
    assert listed.status_code == 200, listed.text
    refused = await async_client.post(
        "/api/v1/queue/leftovers/retire", json={"item_ids": [row_id]}, headers=_jwt(reader.username)
    )
    assert refused.status_code == 403, refused.text
    assert not await gone(db_session, PrintQueueItem, row_id)


async def test_a_printer_restricted_key_sees_and_retires_only_its_printers(
    async_client, db_session, printer_factory, archive_factory
):
    from backend.app.models.user import User
    from backend.tests.integration.test_workshop_library_rights import _key

    mine, *_, my_row, _ = await held_pair(db_session, printer_factory, archive_factory)
    other, *_, other_row, _ = await held_pair(db_session, printer_factory, archive_factory)
    owner = await db_session.scalar(select(User).where(User.username == "test_admin"))
    raw = await _key(db_session, owner, can_read_status=True, can_queue=True, printer_ids=[mine.id])
    headers = {"Authorization": f"Bearer {raw}"}

    listed = await async_client.get("/api/v1/queue/leftovers", headers=headers)
    assert listed.status_code == 200, listed.text
    assert [i["item_id"] for i in listed.json()["items"]] == [my_row]

    refused = await async_client.post("/api/v1/queue/leftovers/retire", json={"item_ids": [other_row]}, headers=headers)
    assert refused.status_code == 403, refused.text
    assert not await gone(db_session, PrintQueueItem, other_row)


async def test_orphans_are_retired_by_the_farm_but_not_by_a_printer_restricted_key(async_client, db_session):
    from backend.app.models.user import User
    from backend.tests.integration.test_workshop_library_rights import _key

    first = AutoQueueItem(status="assigned", assigned_to_item_id=None, target_model="P1S")
    second = AutoQueueItem(status="assigned", assigned_to_item_id=None, target_model="P1S")
    db_session.add_all([first, second])
    await db_session.commit()
    owner = await db_session.scalar(select(User).where(User.username == "test_admin"))
    raw = await _key(db_session, owner, can_read_status=True, can_queue=True, printer_ids=[])
    headers = {"Authorization": f"Bearer {raw}"}

    listed = await async_client.get("/api/v1/queue/leftovers", headers=headers)
    assert listed.status_code == 200 and listed.json()["items"] == [], listed.text
    scoped = await async_client.post("/api/v1/queue/leftovers/retire", json={"orphan_ids": [first.id]}, headers=headers)
    assert scoped.status_code == 200, scoped.text
    assert scoped.json()["retired_orphan_ids"] == []
    assert [(s["auto_item_id"], s["reason"]) for s in scoped.json()["skipped"]] == [(first.id, "out_of_scope")]

    farm = await async_client.post("/api/v1/queue/leftovers/retire", json={"orphan_ids": [first.id, second.id]})
    assert farm.status_code == 200, farm.text
    assert sorted(farm.json()["retired_orphan_ids"]) == sorted([first.id, second.id])
