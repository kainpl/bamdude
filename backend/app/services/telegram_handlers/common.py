"""Shared helpers for Telegram bot handlers."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from backend.app.i18n import get_language, t
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


def refusal_text(exc: Exception, lang: str, fallback_key: str) -> str:
    """The reason to show an operator for a refusal raised by a queue writer.

    The queue's refusals travel as ``{"code", "params", "message"}`` \u2014 the shape
    ``filament_intake.routing_detail`` builds and the one the frontend reacts to \u2014
    and ``message`` is already in the system language, which is the only language
    the bot has (``i18n.get_language`` reads the same setting). So there is
    **nothing to map here and no table of reasons to keep in step**: the sentence
    the HTTP route would have answered with is the sentence the chat gets.

    Both scenes used to answer a flat "failed" for every exception, which read as
    "the printer refused your print" for what was in fact "the file is being
    copied right now, try again" or "the disk is full" (queue-source-spool \u00a76).
    Anything that is not one of those refusals still falls back to that wording \u2014
    an unexpected error is not something to paraphrase at an operator.
    """
    detail = getattr(exc, "detail", None)
    message = detail.get("message") if isinstance(detail, dict) else None
    return message or t(lang, NS, fallback_key)


def has_perm(tg_chat: TelegramChat | None, perm: str) -> bool:
    """Check permission, allowing all if no tg_chat (auth disabled)."""
    if tg_chat is None:
        return True
    return tg_chat.has_permission(perm)


async def deny_out_of_scope(callback, tg_chat: TelegramChat | None, printer_id: int | None) -> bool:
    """Answer-and-refuse when the printer is outside the chat's scope.

    Returns True when the caller must stop (the callback has been answered
    with the refusal). One line at each direct-callback site:
    ``if await deny_out_of_scope(callback, tg_chat, printer_id): return``
    """
    if chat_allows_printer(tg_chat, printer_id):
        return False
    from backend.app.i18n import get_language, t

    lang = await get_language()
    try:
        await callback.answer(t(lang, NS, "auth.not_in_scope"), show_alert=True)
    except Exception:  # noqa: BLE001 — refusing must never crash the handler
        pass
    return True


def chat_allows_printer(tg_chat: TelegramChat | None, printer_id: int | None) -> bool:
    """m157 printer scope, the CONTROL half.

    Listings are already scoped by ``get_printers_data(tg_chat)``, so an
    in-scope operator never sees out-of-scope buttons — this guards the
    direct callbacks those listings can't cover: a keyboard rendered before
    the chat's scope was narrowed, or callback data typed by hand.
    """
    if tg_chat is None:
        return True
    return tg_chat.allows_printer(printer_id)


async def ensure_fresh(printer_id: int) -> bool:
    """Ensure MQTT connection is fresh before sending a command.

    Uses ensure_fresh_connection(printer_id) which fetches Printer from DB internally.
    Returns True if connection is ok or was refreshed, False if failed.
    """
    return await printer_manager.ensure_fresh_connection(printer_id)


def format_time(lang: str, minutes: int | None) -> str:
    if not minutes:
        return "–"
    hours = minutes // 60
    mins = minutes % 60
    if hours > 0:
        return t(lang, NS, "printers.time_hm", h=hours, m=mins)
    return t(lang, NS, "printers.time_m", m=mins)


async def get_printers_data(tg_chat=None) -> list[dict]:
    """Get printers with their status from printer_manager.

    ``tg_chat`` scopes the answer to the chat's ``printer_ids`` (m157) —
    notifications and bot CONTROL follow the same list, so a chat watching
    two machines browses, prints to and commands exactly those two. None
    (no chat in context) = unscoped, e.g. internal callers.
    """
    from sqlalchemy import select

    from backend.app.core.database import async_session
    from backend.app.models.printer import Printer

    async with async_session() as db:
        query = select(Printer).where(Printer.is_active == True).where(Printer.archived.is_(False))  # noqa: E712
        scope = tg_chat.printer_scope() if tg_chat is not None else None  # N62 D4
        if scope is not None:
            query = query.where(Printer.id.in_(scope))
        result = await db.execute(query)
        printers = list(result.scalars().all())

    data = []
    for p in printers:
        status = printer_manager.get_status(p.id)
        temps = status.temperatures if status else {}
        data.append(
            {
                "id": p.id,
                "name": p.name,
                "model": p.model,
                "effective_model": printer_manager.effective_model_for(p.id, p.model),
                "connected": status.connected if status else False,
                "state": status.state if status else None,
                "progress": status.progress if status else 0,
                "current_file": (status.subtask_name or status.current_print) if status else None,
                "nozzle_temp": temps.get("nozzle"),
                "bed_temp": temps.get("bed"),
                "remaining_time": status.remaining_time if status else None,
                "awaiting_plate_clear": printer_manager.is_awaiting_plate_clear(p.id),
                "speed_level": status.speed_level if status else 2,
            }
        )

    return data


async def get_total_hours(printer_id: int) -> float:
    """Get total print hours for a printer."""
    from sqlalchemy import select

    from backend.app.core.database import async_session
    from backend.app.models.printer import Printer

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
    from sqlalchemy import select

    from backend.app.core.database import async_session
    from backend.app.models.print_queue import PrintQueueItem

    async with async_session() as db:
        result = await db.execute(
            select(PrintQueueItem)
            .where(PrintQueueItem.status == "pending", PrintQueueItem.queue_id == printer_id)
            .order_by(PrintQueueItem.position, PrintQueueItem.id)
            .limit(1)
        )
        item = result.scalar_one_or_none()
        if item:
            return (await queue_item_names(db, [item], await get_language()))[item.id]
    return None


async def queue_item_names(db, items, lang: str) -> dict[int, str]:
    """Resolve human names in batches without reading or capturing file bytes."""
    from sqlalchemy import select

    from backend.app.models.archive import PrintArchive
    from backend.app.models.library import LibraryFile
    from backend.app.models.queue_source import QueueSource
    from backend.app.services.filament_intake import source_display_filename
    from backend.app.services.queue_source_descriptor import stored_descriptor
    from backend.app.services.source_io import SourceUnavailable

    source_ids = {item.queue_source_id for item in items if item.queue_source_id is not None}
    legacy = [item for item in items if item.queue_source_id is None]
    archive_ids = {item.archive_id for item in legacy if item.archive_id is not None}
    library_ids = {
        item.library_file_id for item in legacy if item.archive_id is None and item.library_file_id is not None
    }
    sources = (
        {row.id: row for row in (await db.scalars(select(QueueSource).where(QueueSource.id.in_(source_ids)))).all()}
        if source_ids
        else {}
    )
    archives = (
        {row.id: row for row in (await db.scalars(select(PrintArchive).where(PrintArchive.id.in_(archive_ids)))).all()}
        if archive_ids
        else {}
    )
    libraries = (
        {row.id: row for row in (await db.scalars(LibraryFile.active().where(LibraryFile.id.in_(library_ids)))).all()}
        if library_ids
        else {}
    )

    names = {}
    for item in items:
        name = None
        if item.queue_source_id is not None:
            source = sources.get(item.queue_source_id)
            if source is not None:
                try:
                    name = source_display_filename(stored_descriptor(source, item.source_snapshot))
                except SourceUnavailable:
                    pass
        elif item.archive_id is not None:
            archive = archives.get(item.archive_id)
            if archive is not None:
                name = archive.print_name or archive.filename
        elif item.library_file_id is not None:
            library = libraries.get(item.library_file_id)
            if library is not None:
                name = library.filename
        names[item.id] = (
            name.strip() if isinstance(name, str) and name.strip() else t(lang, NS, "queue.job_fallback", id=item.id)
        )
    return names


async def get_maintenance_counts(printer_id: int) -> tuple[int, int]:
    """Get (due_count, warning_count) for a printer."""
    try:
        from backend.app.api.routes.maintenance import _get_printer_maintenance_internal, ensure_default_types
        from backend.app.core.database import async_session

        async with async_session() as db:
            await ensure_default_types(db)
            overview = await _get_printer_maintenance_internal(printer_id, db, commit=False)
            if overview:
                return overview.due_count, overview.warning_count
    except Exception:
        pass
    return 0, 0


async def resolve_queue_id(printer_id: int) -> int | None:
    """Look up the printer's queue, or return ``None`` when it is absent.

    ``ensure_printer_queue`` creates and repairs rows with id == printer_id;
    this read does not create or repair a queue while rendering Telegram UI.
    """
    from sqlalchemy import select

    from backend.app.core.database import async_session
    from backend.app.models.printer_queue import PrinterQueue

    async with async_session() as db:
        result = await db.execute(select(PrinterQueue.id).where(PrinterQueue.printer_id == printer_id))
        return result.scalar_one_or_none()


async def next_queue_position(db, queue_id: int) -> int:
    """The position a new item should take at the back of ``queue_id``.

    ⚠️ **Scoped to the queue AND to pending items**, matching the web path
    (``services/queue_add.py``). Both bot scenes used to take
    ``max(position)`` across the WHOLE table, so the number they reported —
    "added at position N" — was the size of every queue put together and grew
    forever. The item still went to the back, because the value is monotonic,
    but the position shown to the operator meant nothing and did not match what
    the same queue shows in the browser.
    """
    from sqlalchemy import func, select

    from backend.app.models.print_queue import PrintQueueItem

    result = await db.execute(
        select(func.max(PrintQueueItem.position))
        .where(PrintQueueItem.queue_id == queue_id)
        .where(PrintQueueItem.status == "pending")
    )
    return (result.scalar() or 0) + 1


async def saved_queue_options(db, user, printer_id: int, swap_fact: bool | None) -> dict:
    """Build the row fields from the chat's current linked operator or the system profile."""
    from backend.app.models.printer import Printer
    from backend.app.schemas.calibration_mode import mode_to_bool
    from backend.app.services.print_option_defaults import preference_options
    from backend.app.services.queue_source_capture import swap_request

    printer = await db.get(Printer, printer_id)
    if printer is None:
        return {}
    profile = await preference_options(db, user, printer.model)
    if profile is None:
        return {}
    fields = profile.for_printer_queue()
    execute, events = swap_request(
        swap_fact,
        execute=fields["execute_swap_macros"],
        events=fields["swap_macro_events"],
        printer_swap_mode=printer.swap_mode_enabled,
    )
    fields["execute_swap_macros"] = execute
    fields["swap_macro_events"] = json.dumps(events) if events else None
    fields["selected_macro_ids"] = json.dumps(fields["selected_macro_ids"])
    for field in ("bed_levelling", "flow_cali", "nozzle_offset_cali"):
        fields[f"{field}_mode"] = fields[field]
        fields[field] = mode_to_bool(fields[field])
    return fields


async def scene_expired(callback, lang: str) -> None:
    """Answer a scene step whose wizard is no longer running.

    FSM state lives in aiogram's in-memory storage, so every restart of the
    backend wipes whatever wizards were open. The messages stay on the
    operator's screen with their buttons intact, and pressing one used to read
    the missing data as a bad request and answer "failed" — which says the
    action was refused, not that it was never attempted.

    ⚠️ **Not a fix for losing the state**, deliberately. A wizard is a few
    seconds of interaction and restarts are rare; persisting it would buy little
    and cost a table, a migration and an expiry policy. What was worth fixing is
    that the loss was indistinguishable from a failure.
    """
    from backend.app.i18n import escape_md, t

    await callback.answer(t(lang, NS, "start.scene_expired"), show_alert=True)
    try:
        await callback.message.edit_text(escape_md(t(lang, NS, "start.scene_expired")))
    except Exception:  # noqa: BLE001 — the message may be too old to edit; the alert already landed
        pass
