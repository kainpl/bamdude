"""Batch read projection over already-authorized queue rows; no execution authority."""

from sqlalchemy import select

from backend.app.models.auto_queue import AutoQueueItem
from backend.app.models.auto_queue_return import AutoQueueReturn, AutoQueueUnsentReturn
from backend.app.models.printer import Printer
from backend.app.services.auto_queue_unsent_return import REFUSAL_PREFIX


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
    unsent_column = AutoQueueUnsentReturn.auto_identity if auto else AutoQueueUnsentReturn.old_job_identity
    unsent_rows = await db.execute(
        select(AutoQueueUnsentReturn, Printer.name, AutoQueueItem.id)
        .outerjoin(
            Printer,
            (Printer.id == AutoQueueUnsentReturn.printer_id)
            & (Printer.connector_identity == AutoQueueUnsentReturn.printer_identity),
        )
        .outerjoin(AutoQueueItem, AutoQueueItem.recovery_identity == AutoQueueUnsentReturn.auto_identity)
        .where(unsent_column.in_(identities))
    )
    unsent = {receipt.id: (receipt, printer_name, auto_item_id) for receipt, printer_name, auto_item_id in unsent_rows}
    for item in items:
        candidates = by_identity.get(item.recovery_identity, [])
        if auto:
            current = [r for r in candidates if r.returned_at is None and r.old_job_id == item.assigned_to_item_id]
        else:
            current = [r for r in candidates if r.returned_at is None and r.execution_archive_id == item.archive_id]
        item._return_refusal_code = current[-1].last_refusal_code if current else None
        if auto:
            proof = item.unsent_return_proof
            key = proof.get("receipt_identity") if isinstance(proof, dict) else None
            receipt, printer_name, _ = (
                unsent.get(key, (None, None, None)) if isinstance(key, str) else (None, None, None)
            )
            item._unsent_return_printer = (
                printer_name
                if receipt is not None
                and receipt.auto_identity == item.recovery_identity
                and receipt.ordinal == item.unsent_return_count
                else None
            )
        else:
            if item.status == "failed" and (item.waiting_reason or "").startswith(REFUSAL_PREFIX):
                item._return_refusal_code = item.waiting_reason.removeprefix(REFUSAL_PREFIX)
            key = item.returned_receipt_identity
            receipt, _, auto_item_id = unsent.get(key, (None, None, None))
            if receipt is not None and receipt.old_job_identity == item.recovery_identity:
                item._return_kind = "unsent"
                item._return_ordinal = receipt.ordinal
                item._returned_auto_item_id = auto_item_id
            else:
                published = next((r for r in candidates if r.id == key), None)
                item._return_kind = "failed_print" if published is not None else None
                item._return_ordinal = published.ordinal if published is not None else None
