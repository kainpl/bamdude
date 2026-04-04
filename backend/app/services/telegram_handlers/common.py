"""Shared helpers for Telegram bot handlers."""

from __future__ import annotations

from typing import TYPE_CHECKING

from backend.app.i18n import t, get_language, escape_md
from backend.app.services.printer_manager import printer_manager

if TYPE_CHECKING:
    from backend.app.models.telegram_chat import TelegramChat

NS = "telegram_ui"

STATE_EMOJIS = {
    "IDLE": "\U0001f7e2",
    "RUNNING": "\U0001f535",
    "PAUSE": "\U0001f7e1",
    "FINISH": "\u2705",
    "FAILED": "\U0001f534",
}


def state_emoji(state: str | None) -> str:
    return STATE_EMOJIS.get(state or "", "\u26aa")


def has_perm(tg_chat: TelegramChat | None, perm: str) -> bool:
    """Check permission, allowing all if no tg_chat (auth disabled)."""
    if tg_chat is None:
        return True
    return tg_chat.has_permission(perm)


def format_time(lang: str, minutes: int | None) -> str:
    if not minutes:
        return "–"
    hours = minutes // 60
    mins = minutes % 60
    if hours > 0:
        return t(lang, NS, "printers.time_hm", h=hours, m=mins)
    return t(lang, NS, "printers.time_m", m=mins)


async def get_printers_data() -> list[dict]:
    """Get all printers with their status from printer_manager."""
    from backend.app.core.database import async_session
    from backend.app.models.printer import Printer
    from sqlalchemy import select

    async with async_session() as db:
        result = await db.execute(select(Printer).where(Printer.is_active == True))  # noqa: E712
        printers = list(result.scalars().all())

    data = []
    for p in printers:
        status = printer_manager.get_status(p.id)
        temps = status.temperatures if status else {}
        data.append({
            "id": p.id,
            "name": p.name,
            "model": p.model,
            "connected": status.connected if status else False,
            "state": status.state if status else None,
            "progress": status.progress if status else 0,
            "current_file": (status.subtask_name or status.current_print) if status else None,
            "nozzle_temp": temps.get("nozzle"),
            "bed_temp": temps.get("bed"),
            "remaining_time": status.remaining_time if status else None,
            "plate_cleared": printer_manager.is_plate_cleared(p.id),
            "speed_level": status.speed_level if status else 2,
        })

    return data


async def get_total_hours(printer_id: int) -> float:
    """Get total print hours for a printer."""
    from backend.app.core.database import async_session
    from backend.app.models.printer import Printer
    from sqlalchemy import select

    async with async_session() as db:
        result = await db.execute(
            select(Printer.runtime_seconds, Printer.print_hours_offset).where(Printer.id == printer_id)
        )
        row = result.one_or_none()
        if not row:
            return 0.0
        return (row[0] or 0) / 3600.0 + (row[1] or 0.0)


async def get_next_queue_item(printer_id: int) -> str | None:
    """Get the name of the next pending queue item for this printer."""
    from backend.app.core.database import async_session
    from backend.app.models.print_queue import PrintQueueItem
    from sqlalchemy import select

    async with async_session() as db:
        result = await db.execute(
            select(PrintQueueItem)
            .where(PrintQueueItem.status == "pending", PrintQueueItem.printer_id == printer_id)
            .order_by(PrintQueueItem.position)
            .limit(1)
        )
        item = result.scalar_one_or_none()
        if item:
            return item.file_name or f"Job #{item.id}"
    return None


async def get_maintenance_counts(printer_id: int) -> tuple[int, int]:
    """Get (due_count, warning_count) for a printer."""
    try:
        from backend.app.core.database import async_session
        from backend.app.api.routes.maintenance import _get_printer_maintenance_internal, ensure_default_types

        async with async_session() as db:
            await ensure_default_types(db)
            overview = await _get_printer_maintenance_internal(printer_id, db, commit=False)
            if overview:
                return overview.due_count, overview.warning_count
    except Exception:
        pass
    return 0, 0
