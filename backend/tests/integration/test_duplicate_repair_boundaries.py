"""Addressed recovery boundaries, adapted from the independent r1 counterexamples."""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.app.models.print_queue import PrintQueueItem
from backend.app.services import print_state_recovery_preview as tokens, print_state_recovery_store as store
from backend.app.services.print_state_recovery import RUNTIME_OWNERS, classify
from backend.tests.integration.test_print_state_recovery import prepare, telemetry  # noqa: F401

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


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


async def test_f1_archive_kind_release_bypasses_header_effect_veto(
    db_session,
    test_engine,
    printer_factory,
    archive_factory,
    telemetry,  # noqa: F811
):
    """The queue still holds a completed run whose archive owes a held swap: the header refuses release
    (effects_pending). The newly widened kind=archive addresses ANOTHER clean archive of the same printer and
    releases the same header with the same old action."""
    p, q, a, item, _pending = await prepare(db_session, printer_factory, archive_factory)
    item.status = "completed"
    a.extra_data = {
        "swap_macro_events_pending": ["swap_mode_change_table"],
        "bamdude_terminal_effects": {"swap_change_table": {"stage": "owed", "owner": "sweep"}},
        "swap_hold": {"reason": "swap_hold_swap_off", "cycle": "started"},
    }
    started = datetime.now(timezone.utc) - timedelta(days=3)
    clean = await archive_factory(
        p.id, queue_id=q.id, status="completed", started_at=started, completed_at=started + timedelta(hours=1)
    )
    await db_session.commit()

    facts, _, _ = await store.read_facts(db_session, [p.id], [(p.id, "header", q.id), (p.id, "archive", clean.id)])
    header = classify(facts[(p.id, "header", q.id)])
    assert header["actions"] == [] and "effects_pending" in header["blocked_reasons"], header
    with pytest.raises(ValueError):
        await store.make_preview(db_session, p.id, "header", q.id, "release_orphan_header", "user:1")

    with pytest.raises(ValueError):
        await store.make_preview(db_session, p.id, "archive", clean.id, "release_orphan_header", "user:1")
    await db_session.refresh(q)
    assert q.current_item_id == item.id


@pytest.mark.parametrize(
    "action",
    [
        "align_terminal_item",
        "fill_estimated_end_and_align",
        "release_orphan_header",
        "delete_stale_item",
        "close_interrupted_attempt",
        "clear_terminal_runtime_context",
        "cancel_terminal_claims",
    ],
)
async def test_f1_http_accepts_kind_archive_for_old_actions(
    async_client,
    db_session,
    printer_factory,
    archive_factory,
    telemetry,  # noqa: F811
    action,
):
    p, q, a, item, _pending = await prepare(db_session, printer_factory, archive_factory)
    item.status = "completed"
    a.extra_data = {"swap_macro_events_pending": ["swap_mode_change_table"]}
    started = datetime.now(timezone.utc) - timedelta(days=3)
    clean = await archive_factory(
        p.id, queue_id=q.id, status="completed", started_at=started, completed_at=started + timedelta(hours=1)
    )
    await db_session.commit()
    base = f"/api/v1/system/printers/{p.id}/print-state"
    header = await async_client.post(
        base + "/preview", json={"kind": "header", "target_id": q.id, "action": "release_orphan_header"}
    )
    assert header.status_code == 409, header.text
    response = await async_client.post(
        base + "/preview", json={"kind": "archive", "target_id": clean.id, "action": action}
    )
    assert response.status_code == 409, response.text
    await db_session.refresh(q)
    assert q.current_item_id == item.id


async def test_f3_claim_origin_is_never_projected(
    db_session,
    printer_factory,
    archive_factory,
    telemetry,
    monkeypatch,  # noqa: F811
):
    p, q, a, item, _ = await prepare(db_session, printer_factory, archive_factory)
    db_session.add(PrintQueueItem(queue_id=q.id, archive_id=a.id, status="printing", origin="external"))
    await db_session.commit()
    monkeypatch.setattr(store, "runtime_snapshot", lambda _pid: quiet())
    facts, _, _ = await store.read_facts(db_session, [p.id], [(p.id, "header", q.id)])
    public = store.public_issue((p.id, "header", q.id), facts[(p.id, "header", q.id)])
    assert len(public["claims"]) == 2
    assert any(claim["origin"] == "external" for claim in public["claims"]), public["claims"]


async def test_f2_superseded_owed_archive_is_listed_and_settlement_offered(
    db_session,
    printer_factory,
    archive_factory,
    telemetry,
    monkeypatch,  # noqa: F811
):
    """An archive closed by the sweep weeks ago with its swap held (stage stays owed), followed by later prints:
    the Info list scans archive history for it and offers a settlement that pauses today's idle queue."""
    p, q, a, item, pending = await prepare(db_session, printer_factory, archive_factory)
    item.status = "completed"
    q.status, q.current_item_id = "idle", None
    old = datetime.now(timezone.utc) - timedelta(days=30)
    a.started_at, a.completed_at = old, old + timedelta(hours=1)
    a.extra_data = {
        "swap_macro_events_pending": ["swap_mode_change_table"],
        "bamdude_terminal_effects": {"swap_change_table": {"stage": "owed", "owner": "sweep"}},
        "swap_hold": {"reason": "swap_hold_swap_off", "cycle": "started"},
    }
    recent = datetime.now(timezone.utc) - timedelta(days=1)
    for n in range(3):  # later, ordinary prints on the same printer
        await archive_factory(
            p.id,
            queue_id=q.id,
            status="completed",
            started_at=recent + timedelta(hours=n),
            completed_at=recent + timedelta(hours=n, minutes=30),
        )
    await db_session.commit()
    t = [10.0]
    monkeypatch.setattr(store, "clock", lambda: t[0])
    monkeypatch.setattr(store, "runtime_snapshot", lambda _pid: quiet())
    store._observations.clear()
    await store.list_issues(db_session, None)
    t[0] += 11
    page = await store.list_issues(db_session, None)
    assert not any(i["kind"] == "archive" and i["target_id"] == a.id for i in page["issues"])


async def test_verify_ac36_settled_carrier_is_never_sent_by_any_reader(db_session, printer_factory):
    """POSITIVE verification (expected to pass on a correct build): the shape in which the sweep WOULD send the
    owed change_table, then the carrier settled by the single writer -- no reader may claim or send it."""
    from unittest.mock import AsyncMock, patch
    from uuid import uuid4

    from backend.app.models.archive import PrintArchive
    from backend.app.services import swap_terminal
    from backend.app.services.print_reconciliation import _resolve_pending_swaps, _stranded_swaps
    from backend.tests.integration.test_reconcile_owes_a_swap import _close, _global_session_on, _swap_orphan, _the_run

    printer, queue, archive = await _swap_orphan(db_session, printer_factory)
    await _the_run(db_session, queue, archive)
    await _close(db_session, archive)
    await db_session.refresh(archive)
    assert swap_terminal.claim_stage(archive.extra_data) == "owed"
    with _global_session_on(db_session):
        resume, settle = await _stranded_swaps(printer.id, exclude=set())
    assert resume == [archive.id]  # control: before settlement the sweep would send it

    await db_session.refresh(archive)
    swap_terminal.settle_by_operator(
        archive, action_identity=str(uuid4()), actor="user:1", at=datetime.now(timezone.utc)
    )
    await db_session.commit()
    macro = type("M", (), {"name": "change", "gcode": "G28"})()
    with (
        patch("backend.app.services.macro_executor.find_swap_macro", new_callable=AsyncMock, return_value=macro),
        patch(
            "backend.app.services.printer_manager.printer_manager.execute_macro_and_wait",
            new_callable=AsyncMock,
            return_value=(True, "ok"),
        ) as mock_exec,
        _global_session_on(db_session),
    ):
        assert await _stranded_swaps(printer.id, exclude=set()) == ([], [])
        await _resolve_pending_swaps(printer.id, [archive.id])
        from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

        maker = async_sessionmaker(db_session.bind, class_=AsyncSession, expire_on_commit=False)
        for allow_absent in (True, False):
            outcome = await swap_terminal.claim_change_table(
                maker, archive.id, queue_id=queue.id, owner="live", allow_absent=allow_absent
            )
            assert outcome == swap_terminal.CLAIM_TAKEN
    mock_exec.assert_not_awaited()
    archive_id = archive.id
    db_session.expire_all()
    fresh = await db_session.get(PrintArchive, archive_id)
    assert swap_terminal.claim_stage(fresh.extra_data) == swap_terminal.SETTLED_BY_OPERATOR
