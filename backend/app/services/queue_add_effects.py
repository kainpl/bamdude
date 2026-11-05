"""What a freshly committed printer-queue add announces: the MQTT relay event and the "job added" notification.

One helper for every door that adds work to a printer's queue in a fresh commit (the REST queue route and the
slicer connector, OrcaBamDude spec r2 §8.3 step 10). It runs once, after the commit that created the jobs -- never
for a replay or a read of a receipt -- and a failure in either effect never changes what was accepted. It is
best-effort: a crash between the commit and these calls loses the announcement, and nothing re-sends it.
"""

from __future__ import annotations

import logging

from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.printer_queue import PrinterQueue

logger = logging.getLogger(__name__)


async def announce_printer_queue_add(db: AsyncSession, item: PrintQueueItem, queue: PrinterQueue) -> None:
    """``item`` is the first created row, loaded with its archive and library file; ``queue`` with its printer."""
    from backend.app.services.notification_service import notification_service

    try:
        from backend.app.services.mqtt_relay import mqtt_relay

        await mqtt_relay.on_queue_job_added(
            job_id=item.id,
            filename=item.archive.filename if item.archive else "",
            printer_id=item.printer_id,
            printer_name=queue.printer.name if queue.printer else None,
        )
    except Exception:
        logger.debug("MQTT relay announcement of queue job %s failed", item.id, exc_info=True)

    try:
        job_name = (
            item.archive.filename
            if item.archive
            else item.library_file.filename
            if item.library_file
            else f"Job #{item.id}"
        )
        job_name = job_name.replace(".gcode.3mf", "").replace(".3mf", "")
        target = queue.printer.name if queue.printer else f"Queue #{queue.id}"
        await notification_service.on_queue_job_added(
            job_name=job_name,
            target=target,
            db=db,
            printer_id=item.printer_id,
            printer_name=target,
        )
    except Exception:
        logger.debug("Notification of queue job %s failed", item.id, exc_info=True)
