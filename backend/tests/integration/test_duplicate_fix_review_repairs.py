"""Independent fix review: address diagnostics, repeat and SWAP holds (P2-A/P3)."""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.printer_queue import PrinterQueue
from backend.app.services import print_state_recovery_store as store
from backend.app.services.print_state_recovery import RUNTIME_OWNERS
from backend.tests.integration.test_print_state_recovery import prepare, telemetry  # noqa: F401

pytestmark = [pytest.mark.asyncio]


def quiet():
    return {
        "owners": dict.fromkeys(RUNTIME_OWNERS, False),
        "resolution": False,
        "pending_terminal": False,
        "finishing": 0,
        "layer_timelapse": False,
        "admission": None,
        "current": None,
    }


async def _two_passes(db, monkeypatch):
    t = [10.0]
    monkeypatch.setattr(store, "clock", lambda: t[0])
    monkeypatch.setattr(store, "runtime_snapshot", lambda _pid: quiet())
    store._observations.clear()
    await store.list_issues(db, None)
    t[0] += 11
    return await store.list_issues(db, None)


async def test_ok_owed_group_archive_is_listed_with_settlement(
    db_session,
    printer_factory,
    archive_factory,
    telemetry,
    monkeypatch,  # noqa: F811
):
    """Vlad shape (idle header, two printing claims of one completed owed archive) THROUGH list_issues."""
    p, q, a, item, _pending = await prepare(db_session, printer_factory, archive_factory)
    a.extra_data = {
        "swap_start_effect": "fired",
        "swap_hold": {"reason": "swap_off"},
        "bamdude_terminal_effects": {"swap_change_table": {"stage": "owed", "owner": "sweep"}},
        "swap_macro_events_pending": ["swap_mode_change_table"],
    }
    q.status = "idle"
    db_session.add(PrintQueueItem(queue_id=q.id, archive_id=a.id, status="printing", origin="external"))
    await db_session.commit()
    page = await _two_passes(db_session, monkeypatch)
    hits = [i for i in page["issues"] if i["kind"] == "archive" and i["target_id"] == a.id]
    assert len(hits) == 1, page["issues"]
    assert hits[0]["code"] == "swap_settlement_required" and hits[0]["actions"] == ["settle_swap_by_operator"], hits


async def test_printing_archive_without_claim_is_still_diagnosed(
    db_session,
    printer_factory,
    archive_factory,
    telemetry,
    monkeypatch,  # noqa: F811
):
    """Baseline/11c9424e1 Info diagnostic: an archive left `printing` with no queue claim on a stably idle printer
    is shown as `active_archive_without_current_run`. The archive-kind printing branch must not become dead."""
    p = await printer_factory()
    q = PrinterQueue(printer_id=p.id, status="idle")
    db_session.add(q)
    await db_session.flush()
    started = datetime.now(timezone.utc) - timedelta(hours=3)
    a = await archive_factory(p.id, queue_id=q.id, status="printing", started_at=started, completed_at=None)
    await db_session.commit()
    page = await _two_passes(db_session, monkeypatch)
    hits = [i for i in page["issues"] if i["kind"] == "archive" and i["target_id"] == a.id]
    assert hits and hits[0]["code"] == "active_archive_without_current_run", page["issues"]


async def test_mcp_reason_catalog_knows_the_new_dependency_reason():
    """The MCP explain projects unknown policy reasons to `unknown`; the renamed reason must be catalogued."""
    from backend.app.services.farm_diagnostics.recovery import reasons

    assert reasons(["recovery_has_dependencies"]) == ["recovery_has_dependencies"]


async def _batch_with_stale_sibling(db, printer_factory, archive_factory, code):
    p = await printer_factory()
    q = PrinterQueue(printer_id=p.id, status="idle")
    db.add(q)
    await db.flush()
    start = datetime.now(timezone.utc) - timedelta(hours=2)
    a = await archive_factory(
        p.id, queue_id=q.id, status="completed", started_at=start, completed_at=start + timedelta(hours=1)
    )
    first = PrintQueueItem(queue_id=q.id, status="pending", position=1, batch_id="mixed")  # lower id
    db.add(first)
    await db.flush()
    stale = PrintQueueItem(
        queue_id=q.id, archive_id=a.id, status="cancelled", error_code=code, batch_id="mixed", started_at=start
    )
    db.add(stale)
    await db.commit()
    return a, first, stale


@pytest.mark.parametrize("code", ["operator_cancelled_stale_claim", "noncurrent_execution_claim"])
@pytest.mark.parametrize("door", ["batch_all", "batch_one", "clone_sibling"])
async def test_batch_clone_refusal_names_the_stale_archive(
    async_client, db_session, printer_factory, archive_factory, code, door
):
    """§7.4: every repeat door refuses with a typed reason AND the archive address."""
    a, first, stale = await _batch_with_stale_sibling(db_session, printer_factory, archive_factory, code)
    if door == "clone_sibling":
        response = await async_client.post(f"/api/v1/queue/{first.id}/clone")
    else:
        response = await async_client.post(
            f"/api/v1/queue/batch/mixed/clone?scope={'one' if door == 'batch_one' else 'batch'}"
        )
    assert response.status_code == 409, response.text
    detail = response.json()["detail"]
    assert detail["code"] == "stale_execution_claim", detail
    assert detail.get("params", {}).get("archive") == a.id, detail


async def _owed_completion(db, printer_factory, archive_factory):
    p = await printer_factory()
    q = PrinterQueue(printer_id=p.id, status="printing")
    db.add(q)
    await db.flush()
    start = datetime.now(timezone.utc) - timedelta(hours=2)
    a = await archive_factory(
        p.id, queue_id=q.id, status="completed", started_at=start, completed_at=start + timedelta(hours=1)
    )
    nxt = PrintQueueItem(queue_id=q.id, status="pending", position=1)
    db.add(nxt)
    await db.commit()
    return p, q, a, nxt


async def test_ok_swap_hold_never_touches_a_reissued_archive_or_its_queue(
    db_session, test_engine, printer_factory, archive_factory, monkeypatch
):
    from backend.app import main

    if test_engine.dialect.name != "sqlite":
        pytest.skip("actual SQLite id reissue")
    p, q, a, nxt = await _owed_completion(db_session, printer_factory, archive_factory)
    aid, identity = a.id, a.recovery_identity
    await db_session.delete(a)
    await db_session.commit()
    b = await archive_factory(p.id, queue_id=q.id, status="completed")
    assert b.id == aid and b.recovery_identity != identity
    monkeypatch.setattr(main, "async_session", async_sessionmaker(test_engine, expire_on_commit=False))
    text = await main._hold_for_owed_swap(
        p.id, aid, None, "swap_hold_swap_off", "not_started", owns_printer=True, expected_identity=identity
    )
    await db_session.refresh(b)
    await db_session.refresh(q)
    assert text is None and "swap_hold" not in (b.extra_data or {}) and q.status == "printing", q.status


async def test_swap_hold_still_pauses_the_queue_when_its_archive_was_trashed(
    db_session, test_engine, printer_factory, archive_factory, monkeypatch
):
    """Fail-closed SWAP hold (swap-source-lifecycle §10, CLAUDE.md "anything else HOLDS"): the run still owns the
    printer and its owed change_table did not run. Before the delta the queue was paused even when the archive row
    could not be written; the identity fence now returns before the pause when the SAME archive was trashed."""
    from backend.app import main

    p, q, a, nxt = await _owed_completion(db_session, printer_factory, archive_factory)
    a.deleted_at = datetime.now(timezone.utc)
    await db_session.commit()
    monkeypatch.setattr(main, "async_session", async_sessionmaker(test_engine, expire_on_commit=False))
    await main._hold_for_owed_swap(
        p.id,
        a.id,
        None,
        "swap_hold_swap_off",
        "not_started",
        owns_printer=True,
        expected_identity=a.recovery_identity,
    )
    await db_session.refresh(q)
    assert q.status == "paused", f"owned printer with an unsent owed change_table left {q.status}"


async def test_the_printing_archive_that_vetoes_group_repair_is_named_on_the_page(
    db_session,
    printer_factory,
    archive_factory,
    telemetry,
    monkeypatch,  # noqa: F811
):
    """Consequence: an unclaimed `printing` archive makes the group's repair `ambiguous_owner`
    (`printing_archive_ids`), and it must be the subject of its own card or nothing explains the veto."""
    p, q, a, item, _pending = await prepare(db_session, printer_factory, archive_factory)
    q.status = "idle"
    db_session.add(PrintQueueItem(queue_id=q.id, archive_id=a.id, status="printing", origin="external"))
    stuck = await archive_factory(
        p.id, queue_id=q.id, status="printing", started_at=datetime.now(timezone.utc) - timedelta(days=2)
    )
    a.extra_data = {"historical": True}
    await db_session.commit()
    page = await _two_passes(db_session, monkeypatch)
    group = next(i for i in page["issues"] if i["kind"] == "header" and i["target_id"] == q.id)
    assert group["actions"] == [] and "ambiguous_owner" in group["blocked_reasons"], group
    assert any(i["kind"] == "archive" and i["target_id"] == stuck.id for i in page["issues"]), page["issues"]
