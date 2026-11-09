"""The incident shape, through signed previews and the existing audit writer."""

from copy import deepcopy

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.app.models.print_queue import PrintQueueItem
from backend.app.services import print_state_recovery_preview as tokens, print_state_recovery_store as store
from backend.app.services.print_execution import StaleClaimRepeat, require_repeatable
from backend.app.services.print_state_recovery import RUNTIME_OWNERS
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


async def test_settlement_then_whole_group_cancel_and_replay(
    db_session, test_engine, printer_factory, archive_factory, telemetry, monkeypatch
):
    db = db_session
    p, q, a, item, pending = await prepare(db, printer_factory, archive_factory)
    a.extra_data = {
        "swap_start_effect": "fired",
        "swap_hold": {"reason": "swap_off"},
        "bamdude_terminal_effects": {"swap_change_table": {"stage": "owed", "owner": "sweep"}},
        "swap_macro_events_pending": ["swap_mode_change_table"],
    }
    q.status = "idle"  # The actual damaged shape, not a convenient printing header.
    other = PrintQueueItem(queue_id=q.id, archive_id=a.id, status="printing", origin="external")
    db.add(other)
    await db.commit()
    t = [10.0]
    monkeypatch.setattr(store, "clock", lambda: t[0])
    monkeypatch.setattr(store, "runtime_snapshot", lambda _pid: quiet())
    store._observations.clear()
    await store.read_facts(db, [p.id], [(p.id, "archive", a.id), (p.id, "header", q.id)])
    t[0] += 11
    old_archive_status = a.status
    facts, _, _ = await store.read_facts(db, [p.id], [(p.id, "archive", a.id)])
    from backend.app.services.print_state_recovery import classify

    issue = classify(facts[(p.id, "archive", a.id)])
    assert "settle_swap_by_operator" in issue["actions"], issue
    response = await store.make_preview(
        db, p.id, "archive", a.id, "settle_swap_by_operator", "user:1", confirmed_idle=True, confirmed_manual_swap=True
    )
    factory = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    payload = tokens.verify(response["preview_token"])
    result = await store.apply_preview(factory, payload, "user:1", "Operator")
    assert await store.apply_preview(factory, payload, "user:1", "Operator") == result
    await db.refresh(a)
    assert a.status == old_archive_status
    assert a.extra_data["bamdude_terminal_effects"]["swap_change_table"]["stage"] == "settled_by_operator"
    assert a.extra_data["swap_hold"] == {"reason": "swap_off"}
    preserved = deepcopy(a.extra_data)
    preview = await store.make_preview(
        db, p.id, "header", q.id, "cancel_terminal_claims", "user:1", confirmed_idle=True
    )
    assert {m["id"] for m in preview["members"]} == {item.id, other.id}
    result = await store.apply_preview(factory, tokens.verify(preview["preview_token"]), "user:1", "Operator")
    assert await store.apply_preview(factory, tokens.verify(preview["preview_token"]), "user:1", "Operator") == result
    for row in (item, other, pending, q, a):
        await db.refresh(row)
    assert item.status == other.status == "cancelled"
    assert item.error_code == other.error_code == "operator_cancelled_stale_claim"
    assert pending.status == "pending"
    assert q.status == "paused" and q.is_paused and q.current_item_id is None
    assert a.status == old_archive_status and a.extra_data == preserved
    assert len(result["members"]) == 2
    with pytest.raises(StaleClaimRepeat):
        await require_repeatable(db, item)


async def test_group_confirmation_is_strict_bool():
    from pydantic import ValidationError

    from backend.app.schemas.print_state_recovery import RecoveryPreviewRequest

    with pytest.raises(ValidationError):
        RecoveryPreviewRequest(kind="header", target_id=1, action="cancel_terminal_claims", confirmed_idle="true")


async def group_case(db, printer_factory, archive_factory, monkeypatch):
    p, q, a, item, pending = await prepare(db, printer_factory, archive_factory)
    q.status = "idle"
    db.add(PrintQueueItem(queue_id=q.id, archive_id=a.id, status="printing"))
    await db.commit()
    now = [10.0]
    monkeypatch.setattr(store, "clock", lambda: now[0])
    monkeypatch.setattr(store, "runtime_snapshot", lambda _pid: quiet())
    store._observations.clear()
    await store.read_facts(db, [p.id], [(p.id, "header", q.id)])
    now[0] += 11
    return p, q, a, item, pending


@pytest.mark.parametrize("change", ["member_identity", "append", "effect", "foreign_header", "owner", "live", "audit"])
async def test_group_apply_refuses_changes_and_rolls_back_whole_set(
    db_session, test_engine, printer_factory, archive_factory, telemetry, monkeypatch, change
):
    from sqlalchemy import event, select

    from backend.app.models.print_state_recovery_action import PrintStateRecoveryAction

    p, q, a, item, pending = await group_case(db_session, printer_factory, archive_factory, monkeypatch)
    preview = await store.make_preview(
        db_session, p.id, "header", q.id, "cancel_terminal_claims", "user:1", confirmed_idle=True
    )
    if change == "member_identity":
        from uuid import uuid4

        item.recovery_identity = str(uuid4())
    elif change == "append":
        db_session.add(PrintQueueItem(queue_id=q.id, archive_id=a.id, status="printing"))
    elif change == "effect":
        a.extra_data = {"cleanup_pending": True}
    elif change == "foreign_header":
        q.current_item_id = pending.id
    elif change == "owner":

        def active(_pid):
            result = quiet()
            result["owners"]["dispatch_work"] = True
            return result

        monkeypatch.setattr(store, "runtime_snapshot", active)
    elif change == "live":
        telemetry.state = "RUNNING"
    await db_session.commit()

    def audit_failure(*_args):
        raise RuntimeError("audit unavailable")

    if change == "audit":
        event.listen(PrintStateRecoveryAction, "before_insert", audit_failure)
    try:
        with pytest.raises((ValueError, RuntimeError)):
            await store.apply_preview(
                async_sessionmaker(test_engine, expire_on_commit=False),
                tokens.verify(preview["preview_token"]),
                "user:1",
                "Operator",
            )
    finally:
        if change == "audit":
            event.remove(PrintStateRecoveryAction, "before_insert", audit_failure)
    await db_session.refresh(item)
    assert item.status == "printing"
    assert not list((await db_session.scalars(select(PrintStateRecoveryAction))).all())


@pytest.mark.parametrize("action", ["cancel_terminal_claims", "settle_swap_by_operator"])
async def test_new_http_actions_require_assertions_and_do_not_grant_readonly_key_write(
    async_client, db_session, printer_factory, archive_factory, telemetry, monkeypatch, action
):
    p, q, a, item, _ = await group_case(db_session, printer_factory, archive_factory, monkeypatch)
    kind, target = "header", q.id
    if action == "settle_swap_by_operator":
        a.extra_data = {
            "bamdude_terminal_effects": {"swap_change_table": {"stage": "owed"}},
            "swap_macro_events_pending": ["swap_mode_change_table"],
        }
        await db_session.commit()
        kind, target = "archive", a.id
    body = {"kind": kind, "target_id": target, "action": action}
    base = f"/api/v1/system/printers/{p.id}/print-state"
    response = await async_client.post(base + "/preview", json=body)
    assert response.status_code == 409, response.text
    response = await async_client.post(base + "/preview", json={**body, "confirmed_idle": "true"})
    assert response.status_code == 422
    if action == "settle_swap_by_operator":
        response = await async_client.post(base + "/preview", json={**body, "confirmed_idle": True})
        assert response.status_code == 409, response.text
    body.update(confirmed_idle=True, confirmed_manual_swap=True)
    response = await async_client.post(base + "/preview", json=body)
    assert response.status_code == 200, response.text
    made = await async_client.post(
        "/api/v1/api-keys/",
        json={"name": "read-only", "can_queue": False, "can_control_printer": False, "can_manage_archives": False},
    )
    assert made.status_code == 200, made.text
    async_client.headers.pop("Authorization", None)
    response = await async_client.post(base + "/preview", json=body, headers={"X-API-Key": made.json()["key"]})
    assert response.status_code == 403, response.text
    await db_session.refresh(item)
    assert item.status == "printing"


@pytest.mark.parametrize("code", ["operator_cancelled_stale_claim", "noncurrent_execution_claim"])
@pytest.mark.parametrize("door", ["retry", "bulk_retry", "clone", "batch_one", "batch_all"])
async def test_every_repeat_door_rejects_stale_execution_before_writes(
    async_client, db_session, printer_factory, archive_factory, code, door
):
    from sqlalchemy import func, select

    p, q, a, item, pending = await prepare(db_session, printer_factory, archive_factory)
    item.status, item.error_code, item.batch_id = "cancelled", code, "stale-batch"
    pending.batch_id = item.batch_id
    await db_session.commit()
    before = await db_session.scalar(select(func.count()).select_from(PrintQueueItem))
    if door == "bulk_retry":
        response = await async_client.post(
            "/api/v1/queue/bulk-retry", json={"queue_id": q.id, "item_ids": [item.id], "cancelled_item_ids": [item.id]}
        )
    elif door.startswith("batch"):
        response = await async_client.post(
            f"/api/v1/queue/batch/stale-batch/clone?scope={'one' if door == 'batch_one' else 'batch'}"
        )
    else:
        response = await async_client.post(f"/api/v1/queue/{item.id}/{door}")
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "stale_execution_claim", response.text
    await db_session.refresh(item)
    await db_session.refresh(pending)
    assert item.status == "cancelled" and pending.status == "pending"
    assert await db_session.scalar(select(func.count()).select_from(PrintQueueItem)) == before


@pytest.mark.parametrize("count", [100, 101])
async def test_group_limit_is_complete_and_independent_of_issue_page(
    db_session, printer_factory, archive_factory, telemetry, monkeypatch, count
):
    from backend.app.services.print_state_recovery import classify

    p, q, a, item, _ = await group_case(db_session, printer_factory, archive_factory, monkeypatch)
    q.current_item_id = None
    db_session.add_all(PrintQueueItem(queue_id=q.id, archive_id=a.id, status="printing") for _ in range(count - 2))
    await db_session.commit()
    rows, _, _ = await store.read_facts(db_session, [p.id], [(p.id, "header", q.id)])
    f = rows[(p.id, "header", q.id)]
    assert len(f.members) == count
    issue = classify(f)
    if count == 100:
        assert issue["actions"] == ["cancel_terminal_claims"], issue
        preview = await store.make_preview(
            db_session, p.id, "header", q.id, "cancel_terminal_claims", "user:1", confirmed_idle=True
        )
        assert len(preview["members"]) == 100
        assert len(preview["before"]["claims"]) == len(preview["after"]["claims"]) == 100
        public = store.public_issue((p.id, "header", q.id), f)
        assert public["claims_total"] == 100 and public["claims_complete"]
        assert len(public["claims"]) == 3
        assert all("source_snapshot" not in m for m in public["claims"])
    else:
        assert issue["actions"] == [] and "group_overflow" in issue["blocked_reasons"]
        with pytest.raises(ValueError):
            await store.make_preview(
                db_session, p.id, "header", q.id, "cancel_terminal_claims", "user:1", confirmed_idle=True
            )


async def test_group_http_rechecks_key_after_route_authorization(
    async_client, db_session, test_engine, printer_factory, archive_factory, telemetry, monkeypatch
):
    from sqlalchemy import select

    from backend.app.models.api_key import APIKey

    p, q, a, item, _ = await group_case(db_session, printer_factory, archive_factory, monkeypatch)
    made = await async_client.post("/api/v1/api-keys/", json={"name": "repair", "can_control_printer": True})
    assert made.status_code == 200, made.text
    async_client.headers.pop("Authorization", None)
    headers = {"X-API-Key": made.json()["key"]}
    base = f"/api/v1/system/printers/{p.id}/print-state"
    response = await async_client.post(
        base + "/preview",
        headers=headers,
        json={"kind": "header", "target_id": q.id, "action": "cancel_terminal_claims", "confirmed_idle": True},
    )
    assert response.status_code == 200, response.text
    original = store.apply_preview

    async def revoked(*args, **kwargs):
        async with async_sessionmaker(test_engine, expire_on_commit=False)() as db:
            key = await db.scalar(select(APIKey).where(APIKey.name == "repair"))
            key.can_control_printer = False
            await db.commit()
        return await original(*args, **kwargs)

    monkeypatch.setattr(store, "apply_preview", revoked)
    applied = await async_client.post(
        base + "/apply", headers=headers, json={"preview_token": response.json()["preview_token"], "confirmed": True}
    )
    assert applied.status_code == 403, applied.text
    await db_session.refresh(item)
    assert item.status == "printing"


@pytest.mark.parametrize("action", ["cancel_terminal_claims", "settle_swap_by_operator"])
async def test_mcp_explains_new_actions_without_supplying_human_assertions(
    db_session, printer_factory, archive_factory, telemetry, monkeypatch, action
):
    from unittest.mock import Mock

    from backend.app.services.farm_diagnostics.contracts import RecoveryInput
    from backend.app.services.farm_diagnostics.recovery import explain_recovery
    from backend.app.services.farm_diagnostics.timeline import RECOVERY_ACTIONS
    from backend.tests.integration.test_mcp_recovery_explain import actor

    p, q, a, item, _ = await prepare(db_session, printer_factory, archive_factory)
    if action == "settle_swap_by_operator":
        a.extra_data = {
            "swap_start_effect": "fired",
            "bamdude_terminal_effects": {"swap_change_table": {"stage": "owed", "owner": "sweep"}},
            "swap_macro_events_pending": ["swap_mode_change_table"],
        }
    else:
        db_session.add(PrintQueueItem(queue_id=q.id, archive_id=a.id, status="printing"))
    await db_session.commit()
    t = [10.0]
    monkeypatch.setattr(store, "clock", lambda: t[0])
    monkeypatch.setattr(store, "runtime_snapshot", lambda pid: quiet())
    store._observations.clear()
    kind, target = ("archive", a.id) if action == "settle_swap_by_operator" else ("header", q.id)
    await store.read_facts(db_session, [p.id], [(p.id, kind, target)])
    t[0] += 11
    blocked = Mock(side_effect=AssertionError("MCP cannot construct an asserted mutation plan"))
    monkeypatch.setattr(store, "plan_action", blocked)
    args = RecoveryInput(printer_id=p.id, kind=kind, target_id=target, action=action)
    result = await explain_recovery(db_session, actor(), args, "new-action")
    assert action in RECOVERY_ACTIONS
    assert result.data["blockers"] == ["requires_operator_confirmation"]
    assert result.data["after"] is None and not result.data["can_execute_via_mcp"]
    assert result.data["requires_idle_confirmation"]
    assert result.data["requires_manual_swap_confirmation"] == (action == "settle_swap_by_operator")
    assert "preview_token" not in str(result)
    blocked.assert_not_called()
