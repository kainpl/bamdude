"""Read-only recovery policy parity and addressed browser handoff."""

from dataclasses import replace
from unittest.mock import Mock

import httpx
import pytest
from sqlalchemy import event, select, update

from backend.app.mcp_server.contracts import DiagnosticAccess
from backend.app.models.api_key import APIKey
from backend.app.models.group import Group
from backend.app.models.user import User
from backend.app.services import print_state_recovery_preview as tokens, print_state_recovery_store as store
from backend.app.services.farm_diagnostics.access import DiagnosticError
from backend.app.services.farm_diagnostics.contracts import IssuesInput, RecoveryInput
from backend.app.services.print_state_recovery import plan_action
from backend.app.services.queue_sources import current_epoch
from backend.tests.integration.test_mcp_base_transport import key, live_app, runtime  # noqa: F401
from backend.tests.integration.test_mcp_farm_diagnostics import data, error, rpc
from backend.tests.integration.test_print_state_recovery import prepare
from backend.tests.integration.test_printer_runtime_stall import runtime as print_runtime  # noqa: F401
from backend.tests.unit.services.test_terminal_queue_repair import telemetry as recovery_telemetry  # noqa: F401

READ = frozenset({"system:read", "printers:read", "queue:read_all", "archives:read_all"})


def actor(scope=None):
    return DiagnosticAccess(1, 1, READ, False, scope, False, current_epoch())


def addressed(p, item, action="align_terminal_item", **kwargs):
    return RecoveryInput(printer_id=p.id, kind="item", target_id=item.id, action=action, **kwargs)


async def explain(db, access, args):
    from backend.app.services.farm_diagnostics.recovery import explain_recovery

    return await explain_recovery(db, access, args, "explain-test")


@pytest.mark.parametrize("modern", [False, True])
async def test_recovery_read_only_key_uses_real_policy_without_preview_or_effects(
    async_client,
    runtime,
    db_session,
    test_engine,
    printer_factory,
    archive_factory,
    recovery_telemetry,
    monkeypatch,
    modern,
):  # noqa: F811
    p, q, a, item, _ = await prepare(db_session, printer_factory, archive_factory)
    a.extra_data = {"private": "SECRET_NEVER_OUTPUT"}
    await db_session.commit()
    created = await key(async_client, scope=[p.id])
    group = Group(name="recovery read only", permissions=sorted(READ))
    user = User(username="recovery reader", is_active=True, role="user", groups=[group])
    db_session.add_all([group, user])
    await db_session.flush()
    await db_session.execute(update(APIKey).where(APIKey.id == created["id"]).values(user_id=user.id))
    await db_session.commit()
    blocked = Mock(side_effect=AssertionError("recovery mutation or signing"))
    for module, name in (
        (store, "make_preview"),
        (store, "apply_preview"),
        (tokens, "sign"),
        (store, "_pin_runtime_objects"),
    ):
        monkeypatch.setattr(module, name, blocked)
    writes = []

    def observed(conn, cursor, sql, params, context, many):
        if sql.lstrip().upper().startswith(("UPDATE", "INSERT", "DELETE")) and "api_keys" not in sql:
            writes.append(sql)

    event.listen(test_engine.sync_engine, "before_cursor_execute", observed)
    try:
        async with live_app() as url, httpx.AsyncClient() as http:
            listing = data(
                await rpc(http, url, created["key"], "bamdude_list_state_issues", {"printer_id": p.id}, modern=modern)
            )
            result = data(
                await rpc(
                    http,
                    url,
                    created["key"],
                    "bamdude_explain_recovery",
                    addressed(p, item).model_dump(),
                    modern=modern,
                )
            )
        assert listing["data"]["can_execute_via_mcp"] is False
        assert result["data"]["after"]["item"]["status"] == "completed"
        assert result["data"]["requires_operator_confirmation"] and not result["data"]["can_execute_via_mcp"]
        assert result["data"]["ui_path"] == f"/system?printer_id={p.id}&kind=item&target_id={item.id}#print-state"
        assert "SECRET_NEVER_OUTPUT" not in str(result) and "preview_token" not in str(result)
        assert not writes
        blocked.assert_not_called()
    finally:
        event.remove(test_engine.sync_engine, "before_cursor_execute", observed)
    await db_session.refresh(item)
    assert item.status == "printing" and q.current_item_id == item.id


async def test_recovery_plan_matches_same_facts_and_dependencies_only_block_delete(
    db_session, printer_factory, archive_factory, recovery_telemetry, monkeypatch
):
    p, q, a, item, _ = await prepare(db_session, printer_factory, archive_factory)
    monkeypatch.setattr(store, "unknown_inbound_refs", lambda: True)
    key_tuple = (p.id, "item", item.id)
    facts = (await store.read_facts(db_session, [p.id], [key_tuple]))[0][key_tuple]
    pure = store.public_plan(plan_action(facts, "align_terminal_item"))
    result = await explain(db_session, actor(), addressed(p, item))
    assert result.data["after"] == pure
    assert result.data["blockers"] == []
    assert "delete_has_dependencies" in result.data["issue"]["blocked_reasons"]
    refused = await explain(db_session, actor(), addressed(p, item, "delete_stale_item"))
    assert refused.data["after"] is None and "delete_has_dependencies" in refused.data["blockers"]


async def test_recovery_estimate_requires_human_confirmation_without_forming_a_plan(
    db_session, printer_factory, archive_factory, recovery_telemetry, monkeypatch
):
    p, q, a, item, _ = await prepare(db_session, printer_factory, archive_factory)
    a.completed_at = None
    await db_session.commit()
    blocked = Mock(side_effect=AssertionError("must not synthesize human completion"))
    monkeypatch.setattr(store, "plan_action", blocked)
    result = await explain(db_session, actor(), addressed(p, item, "fill_estimated_end_and_align"))
    assert result.data["requires_completed_confirmation"]
    assert result.data["time_provenance"]["requested_estimate_basis"] == "archive_start_plus_slicer_duration"
    assert result.data["blockers"] == ["requires_operator_confirmation"] and result.data["after"] is None
    blocked.assert_not_called()
    assert a.completed_at is None


async def test_recovery_h2s_blockers_do_not_promise_restart_or_completion(
    db_session, printer_factory, archive_factory, recovery_telemetry, monkeypatch
):
    p, q, a, item, _ = await prepare(db_session, printer_factory, archive_factory)
    a.started_at = None
    a.completed_at = None
    a.extra_data = {"swap_macro_events_pending": ["swap_mode_change_table"]}
    monkeypatch.setattr(store, "unknown_inbound_refs", lambda: True)
    await db_session.commit()
    result = await explain(db_session, actor(), addressed(p, item))
    assert {"effects_pending", "missing_run_times", "delete_has_dependencies"} <= set(result.data["blockers"])
    assert result.data["after"] is None and result.data["archive_preserved"]


@pytest.mark.parametrize("scope", [frozenset(), frozenset({999999})])
async def test_recovery_hidden_and_missing_target_are_indistinguishable(
    db_session, printer_factory, archive_factory, recovery_telemetry, scope
):
    p, q, a, item, _ = await prepare(db_session, printer_factory, archive_factory)
    with pytest.raises(DiagnosticError, match="not_found"):
        await explain(db_session, actor(scope), addressed(p, item))
    with pytest.raises(DiagnosticError, match="not_found"):
        await explain(db_session, actor(), RecoveryInput(printer_id=p.id, kind="item", target_id=999999))


async def test_recovery_changed_facts_are_not_published(
    db_session, printer_factory, archive_factory, recovery_telemetry, monkeypatch
):
    p, q, a, item, _ = await prepare(db_session, printer_factory, archive_factory)
    original = store.read_facts
    calls = 0

    async def changed(*args, **kwargs):
        nonlocal calls
        result = await original(*args, **kwargs)
        calls += 1
        if calls == 1:
            item.status = "completed"
            await db_session.commit()
        return result

    monkeypatch.setattr(store, "read_facts", changed)
    with pytest.raises(DiagnosticError, match="state_changed"):
        await explain(db_session, actor(), addressed(p, item))


async def test_recovery_list_signed_bounded_cursor_and_scope(
    db_session, printer_factory, archive_factory, recovery_telemetry
):
    from backend.app.services.farm_diagnostics.recovery import list_state_issues

    rows = [await prepare(db_session, printer_factory, archive_factory) for _ in range(3)]
    access = actor(frozenset({rows[-1][0].id}))
    small = await list_state_issues(db_session, access, IssuesInput(limit=1), "list")
    assert all(row["printer_id"] == rows[-1][0].id for row in small.data["issues"])
    empty = await list_state_issues(db_session, actor(frozenset()), IssuesInput(limit=1), "list")
    assert empty.data["issues"] == [] and empty.next_cursor is None
    first = await list_state_issues(db_session, actor(), IssuesInput(limit=1), "list")
    assert first.next_cursor and first.coverage.truncated
    with pytest.raises(DiagnosticError, match="invalid_cursor"):
        await list_state_issues(
            db_session, replace(actor(), key_id=2), IssuesInput(limit=1, cursor=first.next_cursor), "list"
        )
    seen, page = [], first
    for _ in range(20):
        seen += [(r["printer_id"], r["kind"], r["target_id"]) for r in page.data["issues"]]
        if not page.next_cursor:
            break
        page = await list_state_issues(db_session, actor(), IssuesInput(limit=1, cursor=page.next_cursor), "list")
    else:
        pytest.fail("cursor did not finish")
    assert len(seen) == len(set(seen)) and len(seen) >= 3


async def test_recovery_addressed_http_read_rechecks_target_and_never_previews(
    async_client, db_session, printer_factory, archive_factory, recovery_telemetry
):
    p, q, a, item, _ = await prepare(db_session, printer_factory, archive_factory)
    path = f"/api/v1/system/print-state-issues?printer_id={p.id}&kind=item&target_id={item.id}"
    response = await async_client.get(path)
    assert response.status_code == 200
    assert [r["target_id"] for r in response.json()["issues"]] == [item.id]
    item.status = "completed"
    q.status = "paused"
    q.current_item_id = None
    await db_session.commit()
    assert (await async_client.get(path)).json()["issues"] == []
    assert (await async_client.get("/api/v1/system/print-state-issues?kind=item&target_id=1")).status_code == 422


@pytest.mark.parametrize("removed", ["system:read", "queue:read_all", "archives:read_all"])
async def test_recovery_requires_every_read_gate(
    db_session, printer_factory, archive_factory, recovery_telemetry, removed
):
    p, q, a, item, _ = await prepare(db_session, printer_factory, archive_factory)
    with pytest.raises(DiagnosticError, match="forbidden"):
        await explain(db_session, replace(actor(), permissions=READ - {removed}), addressed(p, item))


async def test_recovery_runtime_plan_names_only_the_normal_pause_and_context_fields(
    db_session, test_engine, printer_factory, archive_factory, recovery_telemetry, print_runtime, monkeypatch
):
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from backend.app import main
    from backend.app.services import layer_timelapse
    from backend.app.services.print_run_binding import current_print_run
    from backend.tests.integration.test_printer_runtime_stall import a_dispatched_run_the_sweep_closes

    monkeypatch.setattr(main, "async_session", async_sessionmaker(test_engine, expire_on_commit=False))
    p, q, a, item, pending, run = await a_dispatched_run_the_sweep_closes(
        db_session, printer_factory, archive_factory, recovery_telemetry
    )
    request = RecoveryInput(printer_id=p.id, kind="runtime", target_id=a.id, action="clear_terminal_runtime_context")
    result = await explain(db_session, actor(), request)
    assert result.data["after"]["queue"] == {"is_paused": True}
    assert set(result.data["after"]["runtime"]) == {"archive_id", "admission", "macro_selection", "swap_config"}
    assert not q.is_paused and current_print_run(print_runtime, p.id) is run
    monkeypatch.setattr(layer_timelapse, "get_session", lambda _: object())
    blocked = await explain(db_session, actor(), request)
    assert "timelapse_open" in blocked.data["blockers"] and blocked.data["after"] is None


@pytest.mark.parametrize("kind", ["episode", "repair"])
async def test_recovery_informational_kinds_have_no_action_or_token(
    db_session, printer_factory, archive_factory, recovery_telemetry, monkeypatch, kind
):
    from types import SimpleNamespace
    from uuid import uuid4

    from backend.app.models.print_state_recovery_action import PrintStateRecoveryAction
    from backend.app.services import dispatch_stall_tracker

    p, q, a, item, _ = await prepare(db_session, printer_factory, archive_factory)
    if kind == "episode":
        episode = SimpleNamespace(
            stalled=True,
            reached=True,
            item_id=item.id,
            detail="PRIVATE_EPISODE_DETAIL",
            blocker_archive_id=a.id,
            last_seen=10,
            count=4,
            span=61,
            quiet=0,
        )
        monkeypatch.setattr(dispatch_stall_tracker, "snapshot", lambda _: (episode,))
        target = item.id
    else:
        q.is_paused = True
        db_session.add(
            PrintStateRecoveryAction(
                id=str(uuid4()),
                actor="test",
                actor_label="test",
                printer_id=p.id,
                queue_id=q.id,
                archive_id=a.id,
                action="clear_terminal_runtime_context",
                input_hash="a" * 64,
                result={"phase": "intent", "incarnation": "old", "before": {"queue_was_paused": False}},
            )
        )
        await db_session.commit()
        target = 0
    result = await explain(db_session, actor(), RecoveryInput(printer_id=p.id, kind=kind, target_id=target))
    assert result.data["after"] is None and result.data["policy_actions"] == []
    assert result.data["issue"]["kind"] == kind and not result.data["can_execute_via_mcp"]
    assert "PRIVATE_EPISODE_DETAIL" not in result.model_dump_json()


@pytest.mark.parametrize("extra", [{"confirmed_completed": True}, {"preview_token": "fake"}])
def test_recovery_input_never_accepts_confirmation_or_preview(extra):
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        RecoveryInput.model_validate({"printer_id": 1, "kind": "item", "target_id": 1, **extra})


async def test_recovery_page_precedes_fact_hydration_with_constant_sql_count(
    db_session, test_engine, printer_factory, archive_factory, recovery_telemetry, monkeypatch
):
    from backend.app.services.farm_diagnostics.recovery import list_state_issues

    for _ in range(2):
        await prepare(db_session, printer_factory, archive_factory)
    original, queries, sizes = store.read_facts, [], []

    async def bounded(db, pids, targets, **kwargs):
        sizes.append((len(pids), len(targets)))
        return await original(db, pids, targets, **kwargs)

    def sql(conn, cursor, statement, params, context, many):
        if statement.lstrip().upper().startswith("SELECT"):
            queries.append(statement)

    monkeypatch.setattr(store, "read_facts", bounded)
    event.listen(test_engine.sync_engine, "before_cursor_execute", sql)
    try:
        await list_state_issues(db_session, actor(), IssuesInput(limit=1), "small")
        count = len(queries)
        for _ in range(6):
            await prepare(db_session, printer_factory, archive_factory)
        queries.clear()
        await list_state_issues(db_session, actor(), IssuesInput(limit=1), "larger")
        assert len(queries) == count
        assert all(pids <= 1 and targets <= 1 for pids, targets in sizes)
        candidate = next(sql for sql in queries if "UNION ALL" in sql)
        assert "LIMIT" in candidate
        grouped = candidate[: candidate.index("GROUP BY")]
        assert "queue_id IN (SELECT printer_queues.id" in grouped
    finally:
        event.remove(test_engine.sync_engine, "before_cursor_execute", sql)
