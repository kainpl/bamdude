"""Issues as the lists and the document read them (spec workshop-dispatch-notes, rules 12, 14).
Read-only — ``services/stock_issues.py`` writes. Everything comes from the note's snapshot:
the lines, the units, the supplier, the basis and the performer."""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models.stock_issue import StockIssue, StockIssueLine
from backend.app.schemas.listing import (
    DispatchNoteLine,
    DispatchNoteOut,
    DispatchNoteSupplier,
    StockIssueRow,
    StockIssueSummaryLine,
)
from backend.app.schemas.project import LineConfigurationSummaryOut
from backend.app.services.entity_codes import code_for

#: How many lines a list row names before «+N».
SUMMARY_LINES = 3


def _row(issue: StockIssue, count: int, summary: list[StockIssueSummaryLine]) -> StockIssueRow:
    return StockIssueRow(
        id=issue.id,
        code=code_for("dispatch_note", issue.id),
        created_at=issue.created_at,
        project_id=issue.project_id,
        order_code=issue.order_code,
        order_name=issue.order_name,
        customer_id=issue.customer_id,
        customer_name=issue.customer_name,
        units=issue.units,
        lines_count=count,
        summary=summary,
        recipient_name=issue.recipient_name,
        recipient_phone=issue.recipient_phone,
        delivery_method=issue.delivery_method,
        delivery_details=issue.delivery_details,
        waybill=issue.waybill,
        note=issue.note,
        created_by_name=issue.created_by_name,
    )


async def issue_rows(db: AsyncSession, issues: Sequence[StockIssue]) -> list[StockIssueRow]:
    """The rows for ``issues``, in the order given — two statements for a page of any size."""
    ids = [issue.id for issue in issues]
    if not ids:
        return []
    counts = dict(
        (
            await db.execute(
                select(StockIssueLine.issue_id, func.count(StockIssueLine.id))
                .where(StockIssueLine.issue_id.in_(ids))
                .group_by(StockIssueLine.issue_id)
            )
        ).all()
    )
    summary: dict[int, list[StockIssueSummaryLine]] = {issue_id: [] for issue_id in ids}
    for line in (
        await db.execute(
            select(StockIssueLine)
            .where(StockIssueLine.issue_id.in_(ids), StockIssueLine.position <= SUMMARY_LINES)
            .order_by(StockIssueLine.issue_id, StockIssueLine.position)
        )
    ).scalars():
        summary[line.issue_id].append(
            StockIssueSummaryLine(
                product_name=line.product_name,
                part_name=line.part_name,
                quantity=line.quantity,
                configuration=None if line.part_name else LineConfigurationSummaryOut(**(line.configuration or {})),
            )
        )
    return [_row(issue, counts.get(issue.id, 0), summary[issue.id]) for issue in issues]


async def note_out(db: AsyncSession, issue: StockIssue) -> DispatchNoteOut:
    """The whole document of ``issue``."""
    lines = (
        (
            await db.execute(
                select(StockIssueLine).where(StockIssueLine.issue_id == issue.id).order_by(StockIssueLine.position)
            )
        )
        .scalars()
        .all()
    )
    [row] = await issue_rows(db, [issue])
    return DispatchNoteOut(
        **row.model_dump(),
        supplier=DispatchNoteSupplier(**(issue.supplier or {})),
        lines=[
            DispatchNoteLine(
                position=line.position,
                product_id=line.product_id,
                product_name=line.product_name,
                sku=line.sku,
                configuration=LineConfigurationSummaryOut(**(line.configuration or {})),
                part_name=line.part_name,
                quantity=line.quantity,
            )
            for line in lines
        ],
    )
