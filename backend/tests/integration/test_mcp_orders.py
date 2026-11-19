"""MCP order reads share UI attribution and cannot credit, reserve or repair."""

from dataclasses import replace
from datetime import datetime, timezone

import httpx
import pytest
from sqlalchemy import event, select

from backend.app.core.auth import generate_api_key
from backend.app.mcp_server.contracts import DiagnosticAccess
from backend.app.mcp_server.tools import orders
from backend.app.models.api_key import APIKey
from backend.app.models.auto_queue import AutoQueueItem
from backend.app.models.group import Group
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.printer_queue import PrinterQueue
from backend.app.models.project import Project
from backend.app.models.project_line import ProjectLine
from backend.app.models.user import User
from backend.app.services import order_metrics, part_stock
from backend.app.services.farm_diagnostics.access import DiagnosticError
from backend.app.services.order_diagnostics import order_progress, order_work, search_orders
from backend.app.services.queue_sources import current_epoch
from backend.tests.auth_helpers import bind_test_key
from backend.tests.integration.test_mcp_base_transport import key, live_app, runtime  # noqa: F401
from backend.tests.integration.test_mcp_farm_diagnostics import data, error, rpc
from backend.tests.integration.test_workshop_stock_plan_regressions import archive, shop  # noqa: F401

pytestmark = pytest.mark.integration


def grant(**overrides):
    return DiagnosticAccess(
        key_id=1,
        owner_id=1,
        permissions=frozenset(),
        is_admin=True,
        printer_ids=None,
        logs_allowed=False,
        epoch=current_epoch(),
        **overrides,
    )


async def progress(db, s, **args):
    return await order_progress(db, grant(), orders.OrderProgressInput(order_id=s["order"], **args), "test")


async def test_quality_recounts_failed_attempts_and_ui_parity_without_business_writes(
    committing_client, db_session, shop
):
    s = shop
    checked, _ = await archive(db_session, s, status="completed", quantities=(12, 4), defects=(3, 1))
    checked.outcome_checked_at = datetime.now(timezone.utc).replace(tzinfo=None)
    await archive(db_session, s, status="completed", quantities=(6, 2))
    await archive(db_session, s, status="failed", quantities=(99, 99))
    await archive(db_session, s, status="printing", quantities=(3, 1))
    line = await db_session.get(ProjectLine, s["line"])
    line.part_count_adjustments = {str(s["a"].id): -1}
    await db_session.commit()
    # Read-only means no business INSERT/UPDATE/DELETE, including accidental
    # autoflush in the existing loaders. Transport key.last_used is separate.
    writes = []

    def observe(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().split(None, 1)[0].upper() in {"INSERT", "UPDATE", "DELETE"}:
            writes.append(statement)

    engine = db_session.bind.sync_engine
    event.listen(engine, "before_cursor_execute", observe)
    try:
        result = await progress(db_session, s, include_parts=True)
        assert not db_session.dirty
    finally:
        event.remove(engine, "before_cursor_execute", observe)
    assert writes == []
    row = result.data["lines"][0]
    a, b = row["parts"]
    assert a["checked_good_from_archives"] == 9
    assert a["unchecked_net_from_archives"] == 6
    assert a["recount_adjustment"] == -1
    assert a["usable_according_to_ui"] == 14
    assert b["checked_good_from_archives"] == 3
    assert b["unchecked_net_from_archives"] == 2
    assert row["stock_receipts"]["received"] == 0
    assert result.data["quality"]["recorded_defective_parts"] == 4
    assert result.data["quality"]["unchecked_completed_archives"] == 1
    ui = (await committing_client.get(f"/api/v1/projects/{s['order']}")).json()
    assert row["printed_according_to_ui"] == ui["lines"][0]["units_printed"]
    assert result.data["summary"]["ui_remaining"] == ui["figures"]["remaining"]


async def test_pooled_quality_is_not_redistributed_or_counted_twice(db_session, shop):
    s = shop
    second = ProjectLine(project_id=s["order"], product_id=s["a"].product_id, quantity=10)
    db_session.add(second)
    await db_session.flush()
    # An unfiled plate is distributed by the real allocator. Once the first
    # line is full, checked parts go to the next, never back to the first.
    one, _ = await archive(db_session, s, status="completed", quantities=(30, 10))
    two, _ = await archive(db_session, s, status="completed", quantities=(6, 2))
    one.project_line_id = two.project_line_id = None
    two.outcome_checked_at = datetime.now(timezone.utc).replace(tzinfo=None)
    await db_session.commit()
    result = await progress(db_session, s, include_parts=True)
    assert len(result.data["lines"]) == 2
    first, last = result.data["lines"]
    assert first["parts"][0]["unchecked_net_from_archives"] == 30
    assert first["parts"][0]["checked_good_from_archives"] == 0
    assert last["parts"][0]["checked_good_from_archives"] == 6
    ctx = await order_metrics.load_order_context(db_session, s["order"])
    awards = []
    f, _ = order_metrics.attribute(ctx, part_attributions=awards)
    assert sum(a.quantity for a in awards) == 48
    assert sum(x.parts[0].usable for x in f.values()) == 36


async def test_search_code_literal_name_and_bound_cursors(db_session, shop):
    s = shop
    for name in ["Target literal_%", "Target second", "Target third"]:
        db_session.add(Project(name=name))
    await db_session.commit()
    args = orders.SearchOrdersInput(query="Target", limit=1)
    first = await search_orders(db_session, grant(), args, "one")
    assert len(first.data["orders"]) == 1 and first.next_cursor
    db_session.add(Project(name="Target later insert"))
    await db_session.commit()
    found = first.data["orders"][:]
    cursor = first.next_cursor
    while cursor:
        page = await search_orders(db_session, grant(), args.model_copy(update={"cursor": cursor}), "more")
        found += page.data["orders"]
        cursor = page.next_cursor
    assert len(found) == 3
    literal = await search_orders(db_session, grant(), orders.SearchOrdersInput(query="_%"), "literal")
    assert [o["name"] for o in literal.data["orders"]] == ["Target literal_%"]
    code = await search_orders(db_session, grant(), orders.SearchOrdersInput(query=f"OR-{s['order']:04}"), "code")
    assert code.data["orders"][0]["id"] == s["order"]
    with pytest.raises(DiagnosticError, match="invalid_cursor"):
        await search_orders(
            db_session, replace(grant(), owner_id=2), args.model_copy(update={"cursor": first.next_cursor}), "bad"
        )


async def test_work_deduplicates_handoff_and_keeps_missing_links_unknown(db_session, shop):
    s = shop
    queue = await db_session.scalar(select(PrinterQueue).where(PrinterQueue.printer_id == s["printer"].id))
    running, _ = await archive(db_session, s, status="printing", quantities=(3, 1))
    running.printer_id = s["printer"].id
    item = PrintQueueItem(
        queue_id=queue.id,
        archive_id=running.id,
        status="printing",
        position=1,
        project_id=s["order"],
        project_line_id=s["line"],
    )
    db_session.add(item)
    await db_session.flush()
    handed = AutoQueueItem(project_id=s["order"], status="pending", assigned_to_item_id=item.id)
    awaiting = AutoQueueItem(project_id=s["order"], status="pending", target_model="P1S")
    unknown = PrintQueueItem(queue_id=queue.id, status="pending", position=2)
    db_session.add_all([handed, awaiting, unknown])
    await db_session.commit()
    args = orders.OrderWorkInput(limit=1)
    found = []
    cursor = None
    while True:
        page = await order_work(db_session, grant(), args.model_copy(update={"cursor": cursor}), "work")
        found += page.data["items"]
        cursor = page.next_cursor
        if not cursor:
            break
    assert len(found) == 3
    assert {x["source"] for x in found} == {"printer_queue", "auto_queue"}
    assert next(x for x in found if x["id"] == item.id and x["source"] == "printer_queue")["line_id"] == s["line"]
    unassigned = next(x for x in found if x["source"] == "printer_queue" and x["id"] == unknown.id)
    assert unassigned["order"] is None and unassigned["line_id"] is None
    assert unassigned["physical_execution_proven"] is False
    restricted = replace(grant(), printer_ids=frozenset({s["printer"].id}))
    scoped = await order_work(db_session, restricted, orders.OrderWorkInput(), "scope")
    assert all(x["source"] != "auto_queue" for x in scoped.data["items"])
    assert not scoped.coverage.complete


async def test_progress_line_pagination_and_unknown_order(db_session, shop):
    s = shop
    db_session.add(ProjectLine(project_id=s["order"], product_id=s["a"].product_id, quantity=2))
    await db_session.commit()
    first = await progress(db_session, s, limit=1)
    assert len(first.data["lines"]) == 1 and first.next_cursor
    second = await progress(db_session, s, limit=1, cursor=first.next_cursor)
    assert len(second.data["lines"]) == 1 and not second.next_cursor
    assert first.data["summary"] == second.data["summary"]
    with pytest.raises(DiagnosticError, match="not_found"):
        await order_progress(db_session, grant(), orders.OrderProgressInput(order_id=99999), "missing")


async def test_missing_history_stays_unknown_and_other_orders_do_not_credit_this_one(db_session, shop):
    s = shop
    empty, _ = await archive(db_session, s, status="completed", quantities=(0, 0))
    from sqlalchemy import delete

    from backend.app.models.archive_part import PrintArchivePart

    await db_session.execute(delete(PrintArchivePart).where(PrintArchivePart.archive_id == empty.id))
    other = Project(name="Other production order")
    db_session.add(other)
    await db_session.flush()
    foreign, _ = await archive(db_session, s, status="completed", quantities=(99, 99))
    foreign.project_id = other.id
    foreign.project_line_id = None
    await db_session.commit()
    result = await progress(db_session, s, include_parts=True)
    assert not result.coverage.complete
    assert "completed_archives_without_part_rows" in result.coverage.missing
    assert result.data["lines"][0]["parts"][0]["unchecked_net_from_archives"] is None
    assert result.data["quality"]["recorded_defective_parts"] is None
    assert result.data["summary"]["ui_covered"] == 0


async def test_stock_allocations_and_receipts_are_separate_and_reads_do_not_reserve(
    committing_client, db_session, shop
):
    s = shop
    for part, count in ((s["a"].id, 9), (s["b"].id, 3)):
        await part_stock.move(db_session, part_id=part, delta=count, reason="manual", note="Fixture stock")
    await db_session.commit()
    first = await progress(db_session, s, include_parts=True)
    assert first.data["lines"][0]["from_part_kits"] == 0
    assert await part_stock.balances(db_session, s["a"].product_id) == {s["a"].id: 9, s["b"].id: 3}
    take = await committing_client.post(f"/api/v1/projects/{s['order']}/take-stock", json={})
    assert take.status_code == 200, take.text
    allocated = await progress(db_session, s, include_parts=True)
    assert allocated.data["lines"][0]["from_part_kits"] == 3
    assert allocated.data["lines"][0]["parts"][0]["allocated_from_stock"] == 9
    await archive(db_session, s, status="completed", quantities=(6, 2))
    line = await db_session.get(ProjectLine, s["line"])
    line.received = 2
    await db_session.commit()
    result = await progress(db_session, s, include_parts=True)
    row = result.data["lines"][0]
    assert row["printed_according_to_ui"] == 2
    assert row["stock_receipts"]["received"] == 2
    assert row["covered_according_to_ui"] == 5
    again = await progress(db_session, s, include_parts=True)
    assert again.data == result.data


async def test_real_key_owner_permissions_hide_order_tools_and_revocation_is_immediate(
    async_client, runtime, db_session
):  # noqa: F811
    group = Group(name="Order MCP reader", permissions=["printers:read", "orders:read"])
    owner = User(username="order-mcp-reader", role="user", is_active=True, groups=[group])
    db_session.add_all([group, owner])
    await db_session.flush()
    raw, hashed, prefix = generate_api_key()
    owned_key = APIKey(
        name="Order reader key",
        key_hash=hashed,
        key_prefix=prefix,
        can_access_mcp=True,
        can_read_status=True,
        can_queue=False,
        can_control_printer=False,
    )
    await bind_test_key(db_session, owned_key, owner)
    db_session.add(owned_key)
    await db_session.commit()
    async with live_app() as url, httpx.AsyncClient() as http:
        caps = data(await rpc(http, url, raw, "bamdude_get_capabilities"))["data"]
        assert "bamdude_search_orders" in caps["tool_names"]
        assert "bamdude_get_order_progress" not in caps["tool_names"]
        assert "bamdude_get_order_work" not in caps["tool_names"]
        assert error(await rpc(http, url, raw, "bamdude_get_order_progress", {"order_id": 1})) == "forbidden"
        group.permissions = ["printers:read"]
        await db_session.commit()
        caps = data(await rpc(http, url, raw, "bamdude_get_capabilities"))["data"]
        assert "bamdude_search_orders" not in caps["tool_names"]
        assert error(await rpc(http, url, raw, "bamdude_search_orders")) == "forbidden"


async def test_transport_visibility_permissions_and_existing_diagnostics(async_client, runtime):  # noqa: F811
    created = await key(async_client)
    async with live_app() as url, httpx.AsyncClient() as http:
        capabilities = data(await rpc(http, url, created["key"], "bamdude_get_capabilities"))["data"]
        assert capabilities["orders_contract"] == 1
        assert "bamdude_search_orders" in capabilities["tool_names"]
        assert data(await rpc(http, url, created["key"], "bamdude_search_orders"))["data"]["orders"] == []
        assert "printers" in data(await rpc(http, url, created["key"], "bamdude_get_farm_overview"))["data"]
    scoped_key = await key(async_client, scope=[])
    async with live_app() as url, httpx.AsyncClient() as http:
        capabilities = data(await rpc(http, url, scoped_key["key"], "bamdude_get_capabilities"))["data"]
        assert "bamdude_get_order_progress" not in capabilities["tool_names"]
        assert (
            error(await rpc(http, url, scoped_key["key"], "bamdude_get_order_progress", {"order_id": 1})) == "forbidden"
        )
    reader = replace(grant(), is_admin=False, permissions=frozenset({"printers:read"}))
    assert not orders.visible(reader)
    for tool in orders.TOOLS:
        with pytest.raises(DiagnosticError, match="forbidden"):
            args = tool.input_model.model_validate({"order_id": 1} if tool.name == "bamdude_get_order_progress" else {})
            await orders.read(tool.name, reader, args, "denied")
