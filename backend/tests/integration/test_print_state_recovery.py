"""Production reader/preview/writer; all printer observations are synthetic."""

from copy import deepcopy
from datetime import timedelta
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.print_state_recovery_action import PrintStateRecoveryAction
from backend.app.services import print_state_recovery_preview as tokens, print_state_recovery_store as store
from backend.tests.unit.services.test_terminal_queue_repair import make_pair, telemetry  # noqa: F401

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


async def test_info_shows_terminal_row_still_owned_by_printing_header(
    db_session, printer_factory, archive_factory, telemetry
):
    p, q, a, item, _ = await prepare(db_session, printer_factory, archive_factory)
    item.status = "completed"
    a.extra_data = {"swap_macro_events_pending": ["swap_mode_change_table"]}
    await db_session.commit()
    page = await store.list_issues(db_session, None)
    issue = next(row for row in page["issues"] if row["kind"] == "item" and row["target_id"] == item.id)
    assert issue["code"] == "header_without_active_item"
    assert issue["actions"] == []
    assert "effects_pending" in issue["blocked_reasons"]
    await db_session.refresh(q)
    await db_session.refresh(a)
    assert q.current_item_id == item.id
    assert a.extra_data["swap_macro_events_pending"] == ["swap_mode_change_table"]


async def prepare(db, printer_factory, archive_factory):
    rows = await make_pair(db, printer_factory, archive_factory)
    rows[2].extra_data = {"historical": {"kept": True}}
    rows[1].pending_count = 1
    await db.commit()
    return rows


@pytest.mark.parametrize("action", ["align_terminal_item", "delete_stale_item", "fill_estimated_end_and_align"])
async def test_exact_change_with_durable_audit_and_idempotent_response(
    db_session, test_engine, printer_factory, archive_factory, telemetry, action
):
    p, q, a, item, pending = await prepare(db_session, printer_factory, archive_factory)
    old_extra = deepcopy(a.extra_data)
    if action == "fill_estimated_end_and_align":
        a.completed_at = None
        a.print_time_seconds = 3600
        await db_session.commit()
    preview = await store.make_preview(db_session, p.id, "item", item.id, action, "user:1", confirmed_completed=True)
    payload = tokens.verify(preview["preview_token"])
    factory = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    result = await store.apply_preview(factory, payload, "user:1", "Operator")
    repeated = await store.apply_preview(factory, payload, "user:1", "Operator")
    assert repeated == result
    await db_session.refresh(q)
    await db_session.refresh(a)
    await db_session.refresh(pending)
    assert q.is_paused and q.status == "paused" and q.current_item_id is None
    assert q.pending_count == 1 and pending.status == "pending"
    assert await db_session.scalar(select(func.count()).select_from(PrintStateRecoveryAction)) == 1
    if action == "delete_stale_item":
        assert await db_session.scalar(select(PrintQueueItem.id).where(PrintQueueItem.id == item.id)) is None
    else:
        await db_session.refresh(item)
        assert item.status == "completed"
        assert item.completed_at is not None
    if action == "fill_estimated_end_and_align":
        assert a.extra_data["operator_recovery_end"]["action_id"] == result["id"]
        assert a.extra_data["historical"] == old_extra["historical"]
        assert "extra_data" not in preview["after"]["archive"]
    else:
        assert a.extra_data == old_extra


@pytest.mark.parametrize("change", ["start", "duration", "effect", "identity", "header", "reference"])
async def test_preview_refuses_stale_rows_without_partial_writes(
    db_session, test_engine, printer_factory, archive_factory, telemetry, change
):
    p, q, a, item, _ = await prepare(db_session, printer_factory, archive_factory)
    preview = await store.make_preview(db_session, p.id, "item", item.id, "delete_stale_item", "user:1")
    if change == "start":
        item.started_at += timedelta(seconds=1)
    elif change == "duration":
        a.print_time_seconds = 50
    elif change == "effect":
        a.extra_data = {"swap_macro_events_pending": ["swap_mode_change_table"]}
    elif change == "identity":
        item.recovery_identity = "different-row"
    elif change == "header":
        q.is_paused = True
    else:
        from backend.app.models.print_completion_receipt import PrintCompletionReceipt

        db_session.add(PrintCompletionReceipt(archive_id=a.id, rearmed_queue_item_id=item.id))
    await db_session.commit()
    factory = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    with pytest.raises(ValueError, match="recovery_changed"):
        await store.apply_preview(factory, tokens.verify(preview["preview_token"]), "user:1", "Operator")
    await db_session.refresh(item)
    assert item.status == "printing"
    assert await db_session.scalar(select(func.count()).select_from(PrintStateRecoveryAction)) == 0


async def test_audit_failure_rolls_back_the_repair(
    db_session, test_engine, printer_factory, archive_factory, telemetry, monkeypatch
):
    p, q, a, item, _ = await prepare(db_session, printer_factory, archive_factory)
    preview = await store.make_preview(db_session, p.id, "item", item.id, "align_terminal_item", "user:1")
    from sqlalchemy import event

    def fail(*_args):
        raise RuntimeError("audit unavailable")

    event.listen(PrintStateRecoveryAction, "before_insert", fail)
    try:
        with pytest.raises(RuntimeError, match="audit unavailable"):
            await store.apply_preview(
                async_sessionmaker(test_engine, expire_on_commit=False),
                tokens.verify(preview["preview_token"]),
                "user:1",
                "Operator",
            )
    finally:
        event.remove(PrintStateRecoveryAction, "before_insert", fail)
    await db_session.refresh(item)
    await db_session.refresh(q)
    assert item.status == "printing" and q.status == "printing"


async def test_late_live_change_after_flush_rolls_back(
    db_session, test_engine, printer_factory, archive_factory, telemetry, monkeypatch
):
    p, q, a, item, _ = await prepare(db_session, printer_factory, archive_factory)
    preview = await store.make_preview(db_session, p.id, "item", item.id, "align_terminal_item", "user:1")
    from sqlalchemy import event

    def move(*_args):
        telemetry.connection_generation += 1

    event.listen(PrintStateRecoveryAction, "after_insert", move)
    try:
        with pytest.raises(ValueError, match="recovery_changed"):
            await store.apply_preview(
                async_sessionmaker(test_engine, expire_on_commit=False),
                tokens.verify(preview["preview_token"]),
                "user:1",
                "Operator",
            )
    finally:
        event.remove(PrintStateRecoveryAction, "after_insert", move)
    await db_session.refresh(item)
    assert item.status == "printing"


async def test_list_is_scoped_and_never_writes(db_session, printer_factory, archive_factory, telemetry):
    p, q, a, item, _ = await prepare(db_session, printer_factory, archive_factory)
    assert (await store.list_issues(db_session, frozenset()))["issues"] == []
    page = await store.list_issues(db_session, frozenset({p.id}))
    row = next(row for row in page["issues"] if row["item_id"] == item.id)
    assert row["actions"] == ["align_terminal_item", "delete_stale_item"]
    assert "extra_data" not in row
    assert not db_session.new and not db_session.dirty and not db_session.deleted


async def test_no_replay_or_physical_effects(
    db_session, test_engine, printer_factory, archive_factory, telemetry, monkeypatch
):
    from backend.app.services import print_reconciliation

    forbidden = AsyncMock(side_effect=AssertionError("completion replay"))
    monkeypatch.setattr(print_reconciliation, "_reconcile_complete_archive", forbidden)
    p, _, _, item, _ = await prepare(db_session, printer_factory, archive_factory)
    preview = await store.make_preview(db_session, p.id, "item", item.id, "align_terminal_item", "user:1")
    await store.apply_preview(
        async_sessionmaker(test_engine, expire_on_commit=False),
        tokens.verify(preview["preview_token"]),
        "user:1",
        "Operator",
    )
    forbidden.assert_not_awaited()


async def test_preview_expiry_tamper_restore_and_actor(
    db_session, printer_factory, archive_factory, telemetry, monkeypatch
):
    p, _, _, item, _ = await prepare(db_session, printer_factory, archive_factory)
    response = await store.make_preview(db_session, p.id, "item", item.id, "align_terminal_item", "user:1")
    token = response["preview_token"]
    with pytest.raises(ValueError):
        tokens.verify(token + "a")
    payload = tokens.verify(token)
    monkeypatch.setattr(tokens, "clock", lambda: payload["expires"] + 1)
    with pytest.raises(ValueError, match="expired"):
        tokens.verify(token)


async def test_http_preview_apply_history_and_key_scope(
    async_client, db_session, printer_factory, archive_factory, telemetry
):
    p, q, a, item, _ = await prepare(db_session, printer_factory, archive_factory)
    body = {"kind": "item", "target_id": item.id, "action": "align_terminal_item"}
    base = f"/api/v1/system/printers/{p.id}/print-state"
    response = await async_client.get("/api/v1/system/print-state-issues")
    assert response.status_code == 200, response.text
    assert any(row["item_id"] == item.id for row in response.json()["issues"])
    response = await async_client.post(base + "/preview", json=body)
    assert response.status_code == 200, response.text
    preview = response.json()
    response = await async_client.post(
        base + "/apply", json={"preview_token": preview["preview_token"], "confirmed": True}
    )
    assert response.status_code == 200, response.text
    assert (await async_client.get(base + "/history")).json()["actions"][0]["id"] == response.json()["id"]
    made = await async_client.post(
        "/api/v1/api-keys/", json={"name": "restricted", "printer_ids": [], "can_control_printer": True}
    )
    assert made.status_code == 200
    headers = {"X-API-Key": made.json()["key"]}
    async_client.headers.pop("Authorization", None)
    scoped = await async_client.get("/api/v1/system/print-state-issues", headers=headers)
    assert scoped.status_code == 200 and scoped.json()["issues"] == []
    assert (await async_client.get(base + "/history", headers=headers)).status_code == 403
    assert (await async_client.post(base + "/preview", json=body, headers=headers)).status_code == 403


async def test_readonly_key_has_no_actions_and_cannot_write(
    async_client, db_session, printer_factory, archive_factory, telemetry
):
    p, _, _, item, _ = await prepare(db_session, printer_factory, archive_factory)
    made = await async_client.post(
        "/api/v1/api-keys/",
        json={"name": "read", "can_queue": False, "can_control_printer": False, "can_manage_archives": False},
    )
    assert made.status_code == 200
    async_client.headers.pop("Authorization", None)
    headers = {"X-API-Key": made.json()["key"]}
    response = await async_client.get("/api/v1/system/print-state-issues", headers=headers)
    assert response.status_code == 200, response.text
    assert all(not row["actions"] for row in response.json()["issues"])
    response = await async_client.post(
        f"/api/v1/system/printers/{p.id}/print-state/preview",
        json={"kind": "item", "target_id": item.id, "action": "delete_stale_item"},
        headers=headers,
    )
    assert response.status_code == 403, response.text


@pytest.mark.parametrize("action", ["align_terminal_item", "delete_stale_item"])
async def test_an_active_successor_is_untouched(
    db_session, test_engine, printer_factory, archive_factory, telemetry, monkeypatch, action
):
    from backend.app.services.print_run_binding import PrintRunBinding
    from backend.app.services.printer_manager import printer_manager

    p, q, old_archive, old_item, pending = await prepare(db_session, printer_factory, archive_factory)
    started = old_archive.completed_at + timedelta(minutes=5)
    successor = await archive_factory(p.id, queue_id=q.id, status="printing", started_at=started, completed_at=None)
    current = PrintQueueItem(queue_id=q.id, archive_id=successor.id, status="printing", started_at=started)
    db_session.add(current)
    await db_session.flush()
    q.current_item_id = current.id
    q.is_paused = True
    await db_session.commit()
    telemetry.state = "RUNNING"
    telemetry.subtask_id = "222"
    monkeypatch.setattr(
        printer_manager,
        "_print_run_bindings",
        {
            p.id: PrintRunBinding(
                p.id,
                successor.id,
                queue_item_id=current.id,
                claim_started_at=started,
                observed_subtask_id="222",
                client_generation=1,
            )
        },
    )

    def snapshot(row):
        return {c.name: deepcopy(getattr(row, c.name)) for c in row.__table__.columns}

    for row in (p, q, successor, current, pending):
        await db_session.refresh(row)
    before = [snapshot(row) for row in (p, q, successor, current, pending)]
    response = await store.make_preview(db_session, p.id, "item", old_item.id, action, "user:1")
    assert response["after"]["queue"] == {}
    await store.apply_preview(
        async_sessionmaker(test_engine, expire_on_commit=False),
        tokens.verify(response["preview_token"]),
        "user:1",
        "Operator",
    )
    for row, expected in zip((p, q, successor, current, pending), before, strict=True):
        await db_session.refresh(row)
        assert snapshot(row) == expected


async def test_item_pagination_does_not_lose_large_printer_or_next_printer(db_session, printer_factory, telemetry):
    from backend.app.models.printer_queue import PrinterQueue

    first, second = await printer_factory(name="large"), await printer_factory(name="next")
    qa, qb = PrinterQueue(printer_id=first.id, status="printing"), PrinterQueue(printer_id=second.id, status="printing")
    db_session.add_all([qa, qb])
    await db_session.flush()
    db_session.add_all([PrintQueueItem(queue_id=qa.id, status="printing") for _ in range(105)])
    db_session.add(PrintQueueItem(queue_id=qb.id, status="printing"))
    await db_session.commit()
    seen, cursor = set(), None
    for _ in range(5):
        page = await store.list_issues(db_session, None, cursor=cursor)
        assert len(page["issues"]) <= 100
        for row in page["issues"]:
            key = row["printer_id"], row["kind"], row["target_id"]
            assert key not in seen
            seen.add(key)
        cursor = page["next_cursor"]
        if not cursor:
            break
    assert len([key for key in seen if key[1] == "item"]) == 106
    assert any(key[0] == second.id for key in seen)


@pytest.mark.parametrize("value", [1, 0, "true", False, None])
async def test_apply_requires_a_literal_boolean_confirmation(value):
    from pydantic import ValidationError

    from backend.app.schemas.print_state_recovery import RecoveryApplyRequest

    with pytest.raises(ValidationError):
        RecoveryApplyRequest(preview_token="a" * 32, confirmed=value)
    assert RecoveryApplyRequest(preview_token="a" * 32, confirmed=True).confirmed is True


async def prepare_stuck_print(db, printer_factory, archive_factory, monkeypatch, *, with_context=True):
    from backend.app.services.print_run_binding import bind_print_run
    from backend.app.services.printer_manager import printer_manager

    p, q, a, item, pending = await prepare(db, printer_factory, archive_factory)
    a.status, a.completed_at, a.dispatch_phase = "printing", None, "accepted"
    p.awaiting_plate_clear = True
    p.awaiting_plate_clear_archive_id = a.id
    p.awaiting_plate_clear_token = "existing-gate"
    await db.commit()

    monkeypatch.setattr(store, "_observations", {})
    tick = [100.0]
    monkeypatch.setattr(store, "clock", lambda: tick[0])
    # This singleton uses per-printer run bindings; give each case independent state.
    monkeypatch.setattr(printer_manager, "_print_run_bindings", {})
    if with_context:
        bind_print_run(
            printer_manager,
            printer_id=p.id,
            archive_id=a.id,
            queue_item_id=item.id,
            claim_started_at=item.started_at,
            archive_identity=a.recovery_identity,
            client_generation=1,
            observed_subtask_id="123",
        )
    return (p, q, a, item, pending), tick


@pytest.mark.parametrize(
    ("outcome", "status", "with_context"),
    [
        ("interrupted", "failed", True),
        ("unknown", "cancelled", True),
        ("unknown", "cancelled", False),
    ],
)
async def test_close_stopped_print_after_explicit_confirmation(
    db_session, test_engine, printer_factory, archive_factory, telemetry, monkeypatch, outcome, status, with_context
):
    from backend.app.services.print_run_binding import current_print_run
    from backend.app.services.print_state_recovery import CLOSE_PRINT_ACTION
    from backend.app.services.printer_manager import printer_manager

    (p, q, a, item, pending), tick = await prepare_stuck_print(
        db_session, printer_factory, archive_factory, monkeypatch, with_context=with_context
    )
    key = (p.id, "item", item.id)

    async def preview(**kwargs):
        return await store.make_preview(db_session, *key, CLOSE_PRINT_ACTION, "user:1", **kwargs)

    # Neither one idle sighting nor an omitted operator assertion is enough.
    with pytest.raises(ValueError, match="recovery_not_allowed"):
        await preview(confirmed_idle=True, operator_outcome=outcome)
    tick[0] += 11
    page = await store.list_issues(db_session, None)
    issue = next(row for row in page["issues"] if row["kind"] == "item" and row["item_id"] == item.id)
    assert issue["actions"] == [CLOSE_PRINT_ACTION], issue
    with pytest.raises(ValueError, match="recovery_not_allowed"):
        await preview(operator_outcome=outcome)
    with pytest.raises(ValueError, match="recovery_not_allowed"):
        await preview(confirmed_idle=True)
    answer = await preview(confirmed_idle=True, operator_outcome=outcome, note="Checked printer")
    assert answer["after"]["archive"]["status"] == status
    assert ("runtime" in answer["after"]) == with_context
    payload = tokens.verify(answer["preview_token"])
    factory = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    result = await store.apply_preview(factory, payload, "user:1", "Operator")
    assert result["context_released"] is True
    assert await store.apply_preview(factory, payload, "user:1", "Operator") == {
        key: value for key, value in result.items() if key != "context_released"
    }
    assert result["operator_outcome"] == outcome
    assert result["note"] == "Checked printer"
    assert result["members"][0]["after_status"] == status
    assert await db_session.scalar(select(func.count()).select_from(PrintStateRecoveryAction)) == 1
    for row in (p, q, a, item, pending):
        await db_session.refresh(row)
    assert a.status == item.status == status
    assert a.completed_at == item.completed_at
    assert a.extra_data["historical"] == {"kept": True}
    assert a.extra_data["operator_interrupted_print"]["action_id"] == result["id"]
    assert bool(a.extra_data.get("recovered_outcome_uncertain")) == (outcome == "unknown")
    assert p.awaiting_plate_clear and p.awaiting_plate_clear_token == "existing-gate"
    assert q.is_paused and q.status == "paused" and q.current_item_id is None
    assert pending.status == "pending" and q.pending_count == 1
    assert current_print_run(printer_manager, p.id) is None
    from backend.app.models.print_completion_receipt import PrintCompletionReceipt
    from backend.app.models.spool_usage_history import SpoolUsageHistory

    for model in (PrintCompletionReceipt, SpoolUsageHistory):
        assert await db_session.scalar(select(func.count()).select_from(model)) == 0


@pytest.mark.parametrize("change", ["running", "stale", "new_binding", "completion_receipt"])
async def test_stopped_print_preview_refuses_changed_identity_or_evidence(
    db_session, test_engine, printer_factory, archive_factory, telemetry, monkeypatch, change
):
    from backend.app.models.print_completion_receipt import PrintCompletionReceipt
    from backend.app.services.print_run_binding import bind_print_run, current_print_run
    from backend.app.services.print_state_recovery import CLOSE_PRINT_ACTION
    from backend.app.services.printer_manager import printer_manager

    (p, q, a, item, _), tick = await prepare_stuck_print(db_session, printer_factory, archive_factory, monkeypatch)
    await store.list_issues(db_session, None)
    tick[0] += 11
    preview = await store.make_preview(
        db_session,
        p.id,
        "item",
        item.id,
        CLOSE_PRINT_ACTION,
        "user:1",
        confirmed_idle=True,
        operator_outcome="interrupted",
    )
    if change == "running":
        telemetry.state = "RUNNING"
    elif change == "stale":
        monkeypatch.setattr(printer_manager, "peek_status", lambda _: (telemetry, 10.0, True))
    elif change == "new_binding":
        bind_print_run(printer_manager, printer_id=p.id, archive_id=a.id + 1)
    else:
        db_session.add(PrintCompletionReceipt(archive_id=a.id))
        await db_session.commit()
    held = current_print_run(printer_manager, p.id)
    factory = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    with pytest.raises(ValueError, match="recovery_changed"):
        await store.apply_preview(factory, tokens.verify(preview["preview_token"]), "user:1", "Operator")
    for row in (q, a, item):
        await db_session.refresh(row)
    assert q.current_item_id == item.id and not q.is_paused
    assert a.status == item.status == "printing"
    assert current_print_run(printer_manager, p.id) is held
    assert await db_session.scalar(select(func.count()).select_from(PrintStateRecoveryAction)) == 0
