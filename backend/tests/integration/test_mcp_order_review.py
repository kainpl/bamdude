"""Review regressions on the existing isolated Workshop fixtures."""

from dataclasses import replace
from datetime import datetime, timezone
from unittest.mock import Mock

import pytest
from sqlalchemy import delete, select

from backend.app.mcp_server.tools.orders import OrderWorkInput
from backend.app.models.archive_part import PrintArchivePart
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.printer_queue import PrinterQueue
from backend.app.models.product import Product, ProductPart
from backend.app.models.project_line import ProjectLine
from backend.app.services import order_fulfilment, order_metrics
from backend.app.services.order_diagnostics import order_work
from backend.tests.integration.test_mcp_orders import archive, grant, progress, shop  # noqa: F401


@pytest.mark.parametrize("checked", [True, False])
@pytest.mark.parametrize("pooled", [False, True])
async def test_quality_gap_is_scoped_to_possible_part_recipients(db_session, shop, checked, pooled):
    good, _ = await archive(db_session, shop, status="completed", quantities=(7, 2), defects=(1, 0))
    good.outcome_checked_at = datetime.now(timezone.utc).replace(tzinfo=None)
    if pooled:
        product_id = shop["a"].product_id
    else:
        product = Product(name="Independent product")
        db_session.add(product)
        await db_session.flush()
        product_id = product.id
        db_session.add(
            ProductPart(product_id=product_id, kind="printed", name="Other", name_key="other", qty_per_unit=1)
        )
    line = ProjectLine(project_id=shop["order"], product_id=product_id, quantity=3)
    db_session.add(line)
    await db_session.flush()
    gap, _ = await archive(db_session, {**shop, "line": line.id}, status="completed", quantities=(0, 0))
    if pooled:
        gap.project_line_id = None  # the real shared plate may feed either line
    else:
        gap.library_file_id = None  # explicit other home, no foreign pooled work
    gap.outcome_checked_at = datetime.now(timezone.utc).replace(tzinfo=None) if checked else None
    await db_session.execute(delete(PrintArchivePart).where(PrintArchivePart.archive_id == gap.id))
    await db_session.commit()
    result = await progress(db_session, shop, line_id=shop["line"], include_parts=True)
    part = next(p for p in result.data["lines"][0]["parts"] if p["id"] == shop["a"].id)
    field = "checked_good_from_archives" if checked else "unchecked_net_from_archives"
    assert part[field] == (None if pooled else (6 if checked else 0))
    assert part["usable_according_to_ui"] == 6
    assert "completed_archives_without_part_rows" in result.coverage.missing


@pytest.mark.parametrize("state,missing", [("running", False), ("queued", True), ("all", True)])
async def test_printer_scope_coverage_matches_requested_stream(db_session, shop, state, missing):
    access = replace(grant(), printer_ids=frozenset({shop["printer"].id}))
    result = await order_work(db_session, access, OrderWorkInput(state=state), "scope")
    assert ("unassigned_auto_queue_hidden_by_printer_scope" in result.coverage.missing) is missing


async def test_progress_reuses_one_attribution_for_fulfilment_and_matches_workshop(db_session, shop, monkeypatch):
    await archive(db_session, shop, status="completed", quantities=(7, 2), defects=(1, 0))
    traced = Mock(wraps=order_metrics.attribute)
    monkeypatch.setattr(order_metrics, "attribute", traced)
    monkeypatch.setattr(order_fulfilment, "attribute", traced)
    result = await progress(db_session, shop, include_parts=True)
    assert traced.call_count == 1
    ctx = await order_metrics.load_order_context(db_session, shop["order"])
    standalone = await order_fulfilment.state(db_session, ctx.project, ctx=ctx)
    assert result.data["summary"]["can_complete_by_workshop_rules"] == standalone.can_complete
    assert result.data["lines"][0]["stock_receipts"]["held"] == standalone.lines[0].held


async def test_printer_queue_link_uses_the_monitor_target_query(db_session, shop):
    queue = await db_session.scalar(select(PrinterQueue).where(PrinterQueue.printer_id == shop["printer"].id))
    db_session.add(PrintQueueItem(queue_id=queue.id, status="pending", position=1))
    await db_session.commit()
    result = await order_work(db_session, grant(), OrderWorkInput(), "link")
    assert result.data["items"][0]["ui_link"] == f"/queue?monitorPrinter={shop['printer'].id}"
