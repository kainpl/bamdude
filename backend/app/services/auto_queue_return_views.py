"""Batch read projection over already-authorized queue rows; no execution authority."""

from sqlalchemy import select

from backend.app.models.auto_queue_return import AutoQueueReturn


async def describe_returns(db, items, *, auto=False):
    identities = {row.recovery_identity for row in items if row.recovery_identity}
    if not identities:
        return
    column = AutoQueueReturn.auto_identity if auto else AutoQueueReturn.old_job_identity
    rows = (
        await db.scalars(
            select(AutoQueueReturn).where(column.in_(identities)).order_by(AutoQueueReturn.ready_at, AutoQueueReturn.id)
        )
    ).all()
    by_identity = {}
    for receipt in rows:
        by_identity.setdefault(receipt.auto_identity if auto else receipt.old_job_identity, []).append(receipt)
    for item in items:
        candidates = by_identity.get(item.recovery_identity, [])
        if auto:
            current = [r for r in candidates if r.returned_at is None and r.old_job_id == item.assigned_to_item_id]
        else:
            current = [r for r in candidates if r.returned_at is None and r.execution_archive_id == item.archive_id]
        item._return_refusal_code = current[-1].last_refusal_code if current else None
        published = next((r for r in candidates if r.id == getattr(item, "returned_receipt_identity", None)), None)
        item._return_ordinal = published.ordinal if published is not None else None
