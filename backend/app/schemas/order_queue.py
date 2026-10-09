"""``GET /projects/{id}/queue`` — the order's live work in both queue tiers (spec workshop-order-queue)."""

from pydantic import BaseModel

from backend.app.schemas.auto_queue import AutoQueueItemResponse
from backend.app.schemas.print_queue import PrintQueueItemResponse


class OrderQueuePrinting(BaseModel):
    """An archive of the order that is printing now — what ``prints_in_progress`` counts."""

    auto_eject: bool = False
    archive_id: int
    printer_id: int | None
    printer_name: str | None
    name: str
    project_line_id: int | None


class OrderQueueOut(BaseModel):
    printing: list[OrderQueuePrinting]
    pending: list[PrintQueueItemResponse]
    awaiting: list[AutoQueueItemResponse]
