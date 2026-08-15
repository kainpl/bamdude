"""Add to Queue scene - select file → select target → confirm."""

from __future__ import annotations

from typing import TYPE_CHECKING

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup

from backend.app.i18n import escape_md, get_language, t
from backend.app.services.telegram_handlers.common import (
    NS,
    get_printers_data,
    has_perm,
    next_queue_position,
    resolve_queue_id,
)
from backend.app.services.telegram_handlers.pagination import build_page_nav

if TYPE_CHECKING:
    from backend.app.models.telegram_chat import TelegramChat

router = Router()

PAGE_SIZE = 8


class QueueAddState(StatesGroup):
    selecting_file = State()
    selecting_target = State()
    confirming = State()


@router.callback_query(F.data == "qadd:start")
async def cb_queue_add_start(callback: CallbackQuery, state: FSMContext, tg_chat: TelegramChat | None = None) -> None:
    """Entry point for Add to Queue."""
    lang = await get_language()

    if not (has_perm(tg_chat, "library:read") and has_perm(tg_chat, "queue:create")):
        await callback.answer(t(lang, NS, "auth.no_permission"), show_alert=True)
        return

    await callback.answer()
    await state.set_state(QueueAddState.selecting_file)
    await state.update_data(offset=0)
    await _show_file_list(callback, lang, 0)


async def _show_file_list(callback: CallbackQuery, lang: str, offset: int) -> None:
    from backend.app.services.telegram_handlers.library_scene import _get_library_files

    files, total = await _get_library_files(offset, PAGE_SIZE)

    if not files and offset == 0:
        await callback.message.edit_text(
            f"\U0001f4cb *{escape_md(t(lang, NS, 'queue_add.title'))}*\n\n{escape_md(t(lang, NS, 'library.no_files'))}",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text=f"\u25c0\ufe0f {t(lang, NS, 'printers.btn_back')}", callback_data="menu:queue"
                        )
                    ],
                ]
            ),
        )
        return

    lines = [
        f"\U0001f4cb *{escape_md(t(lang, NS, 'queue_add.title'))}*",
        escape_md(t(lang, NS, "queue_add.select_file")),
    ]

    btns = []
    for f in files:
        btns.append(
            [
                InlineKeyboardButton(
                    text=f"\U0001f4c4 {f.filename}",
                    callback_data=f"qadd:file:{f.id}",
                )
            ]
        )

    nav = build_page_nav(total, offset, PAGE_SIZE, "page:qadd:", lang)
    if nav:
        btns.append(nav)

    btns.append(
        [
            InlineKeyboardButton(
                text=f"\u274c {t(lang, NS, 'queue_add.btn_cancel')}",
                callback_data="qadd:cancel",
            )
        ]
    )

    await callback.message.edit_text(
        "\n".join(lines),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=btns),
    )


@router.callback_query(F.data.startswith("page:qadd:"))
async def cb_qadd_page(callback: CallbackQuery, state: FSMContext, tg_chat: TelegramChat | None = None) -> None:
    lang = await get_language()
    offset = int(callback.data.split(":")[2])
    await callback.answer()
    await state.update_data(offset=offset)
    await _show_file_list(callback, lang, offset)


@router.callback_query(F.data.startswith("qadd:file:"))
async def cb_qadd_select_file(callback: CallbackQuery, state: FSMContext, tg_chat: TelegramChat | None = None) -> None:
    """File selected - show target selection."""
    lang = await get_language()
    file_id = int(callback.data.split(":")[2])
    await callback.answer()

    from sqlalchemy import select

    from backend.app.core.database import async_session
    from backend.app.models.library import LibraryFile

    async with async_session() as db:
        result = await db.execute(select(LibraryFile).where(LibraryFile.id == file_id))
        lib_file = result.scalar_one_or_none()

    if not lib_file:
        await callback.message.edit_text(escape_md("File not found"))
        await state.clear()
        return

    # Get sliced_for_model from metadata
    sliced_for_model = None
    if lib_file.file_metadata and isinstance(lib_file.file_metadata, dict):
        sliced_for_model = lib_file.file_metadata.get("sliced_for_model")

    await state.set_state(QueueAddState.selecting_target)
    await state.update_data(file_id=file_id, file_name=lib_file.filename, sliced_for_model=sliced_for_model)

    # Show target options: specific printers + model-based
    printers = await get_printers_data()
    active_printers = [p for p in printers if p["connected"]]

    # Filter by compatible model if known
    if sliced_for_model:
        compatible = [p for p in active_printers if p["model"] and p["model"].upper() == sliced_for_model.upper()]
        if compatible:
            active_printers = compatible

    # Get distinct models from filtered list
    models = sorted({p["model"] for p in active_printers if p["model"]})

    lines = [
        f"\U0001f4c4 *{escape_md(lib_file.filename)}*\n",
        escape_md(t(lang, NS, "queue_add.select_target")),
    ]

    btns = []

    # Model-based assignment
    if models:
        for model in models:
            btns.append(
                [
                    InlineKeyboardButton(
                        text=f"\U0001f3af {t(lang, NS, 'queue_add.btn_any_model')} {model}",
                        callback_data=f"qadd:model:{model}",
                    )
                ]
            )

    # Specific printers
    for p in active_printers:
        btns.append(
            [
                InlineKeyboardButton(
                    text=f"\U0001f5a8 {p['name']}",
                    callback_data=f"qadd:printer:{p['id']}",
                )
            ]
        )

    btns.append(
        [
            InlineKeyboardButton(
                text=f"\u25c0\ufe0f {t(lang, NS, 'printers.btn_back')}",
                callback_data="qadd:start",
            )
        ]
    )

    await callback.message.edit_text(
        "\n".join(lines),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=btns),
    )


@router.callback_query(F.data.startswith("qadd:printer:"))
async def cb_qadd_select_printer(
    callback: CallbackQuery, state: FSMContext, tg_chat: TelegramChat | None = None
) -> None:
    """Specific printer selected."""
    lang = await get_language()
    printer_id = int(callback.data.split(":")[2])
    await callback.answer()

    printers = await get_printers_data()
    printer = next((p for p in printers if p["id"] == printer_id), None)
    printer_name = printer["name"] if printer else f"#{printer_id}"

    await state.set_state(QueueAddState.confirming)
    await state.update_data(printer_id=printer_id, target_model=None, target_label=printer_name)
    await _show_confirm(callback, state, lang)


@router.callback_query(F.data.startswith("qadd:model:"))
async def cb_qadd_select_model(callback: CallbackQuery, state: FSMContext, tg_chat: TelegramChat | None = None) -> None:
    """Model-based assignment selected."""
    lang = await get_language()
    model = callback.data.split(":")[2]
    await callback.answer()

    await state.set_state(QueueAddState.confirming)
    await state.update_data(printer_id=None, target_model=model, target_label=f"Any {model}")
    await _show_confirm(callback, state, lang)


async def _show_confirm(callback: CallbackQuery, state: FSMContext, lang: str) -> None:
    data = await state.get_data()
    file_name = data.get("file_name", "?")
    target_label = data.get("target_label", "?")

    text = (
        f"\U0001f4cb *{escape_md(t(lang, NS, 'queue_add.confirm_title'))}*\n\n"
        f"\U0001f4c4 {escape_md(t(lang, NS, 'queue.file'))}: *{escape_md(file_name)}*\n"
        f"\U0001f3af {escape_md(target_label)}"
    )

    await callback.message.edit_text(
        text,
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text=f"\u2795 {t(lang, NS, 'queue_add.btn_add')}", callback_data="qadd:confirm")],
                [
                    InlineKeyboardButton(
                        text=f"\u274c {t(lang, NS, 'queue_add.btn_cancel')}", callback_data="qadd:cancel"
                    )
                ],
            ]
        ),
    )


async def _add_to_auto_queue(
    callback: CallbackQuery,
    lang: str,
    file_id: int | None,
    target_model: str,
    tg_chat: TelegramChat | None,
) -> None:
    """Put the job on the auto-queue, for the distributor to place.

    Built the same way the web route builds it, and deliberately so: routing
    requirements are read out of the 3MF itself rather than left empty, or an
    item queued from the bot would be matched on its model alone while the same
    file queued from the browser also matches on filament. Two tiers with two
    behaviours for one action is exactly the drift this tier exists to avoid.

    ⚠️ The extractor never raises — a corrupted or truncated 3MF yields empty
    fields — so a file we cannot read still queues, just with model-only
    routing. Refusing there would be worse: the operator picked a target that
    is perfectly valid.
    """
    import json

    from sqlalchemy import func, select

    from backend.app.core.database import async_session
    from backend.app.models.auto_queue import AutoQueueItem
    from backend.app.models.library import LibraryFile

    if not file_id:
        await callback.answer(t(lang, NS, "queue_add.failed"), show_alert=True)
        return

    try:
        async with async_session() as db:
            lib_file = (await db.execute(select(LibraryFile).where(LibraryFile.id == file_id))).scalar_one_or_none()
            if not lib_file:
                await callback.answer(t(lang, NS, "queue_add.failed"), show_alert=True)
                return

            required_types_json = None
            try:
                from pathlib import Path

                from backend.app.core.config import settings as app_settings
                from backend.app.services.auto_queue_threemf import extract_auto_queue_requirements

                if lib_file.file_path:
                    path = Path(lib_file.file_path)
                    if not path.is_absolute():
                        path = app_settings.base_dir / lib_file.file_path
                    reqs = extract_auto_queue_requirements(path)
                    if reqs.required_filament_types:
                        required_types_json = json.dumps(reqs.required_filament_types)
            except Exception:  # noqa: BLE001 — routing on the model alone is still a valid item
                pass

            # ⚠️ One global ordering, unlike the per-printer queues: the
            # auto-queue is a single list the distributor walks.
            max_pos = int(
                (
                    await db.execute(
                        select(func.coalesce(func.max(AutoQueueItem.position), 0)).where(
                            AutoQueueItem.status == "pending"
                        )
                    )
                ).scalar()
                or 0
            )

            item = AutoQueueItem(
                library_file_id=file_id,
                target_model=target_model,
                required_filament_types=required_types_json,
                status="pending",
                position=max_pos + 1,
                created_by_id=tg_chat.user_id if tg_chat else None,
            )
            db.add(item)
            await db.commit()
            pos = item.position

        await callback.answer(f"✅ {t(lang, NS, 'queue_add.added', pos=pos)}")
    except Exception:
        await callback.answer(t(lang, NS, "queue_add.failed"), show_alert=True)

    from backend.app.services.telegram_handlers.queue import render_queue

    await render_queue(callback, tg_chat)


@router.callback_query(F.data == "qadd:confirm")
async def cb_qadd_confirm(callback: CallbackQuery, state: FSMContext, tg_chat: TelegramChat | None = None) -> None:
    """Confirm - create queue item."""
    lang = await get_language()

    data = await state.get_data()
    file_id = data.get("file_id")
    printer_id = data.get("printer_id")
    target_model = data.get("target_model")

    await state.clear()

    from backend.app.core.database import async_session
    from backend.app.models.print_queue import PrintQueueItem

    # "Any printer of model X" is the AUTO-QUEUE, the tier that routes a job to
    # whichever machine can take it. It is a different table and a different
    # question from a per-printer queue, and until now the bot drew the buttons
    # for it and then refused every one of them at this line: the target sets
    # printer_id=None, and the check below read that as "nothing chosen".
    if not printer_id and target_model:
        await _add_to_auto_queue(callback, lang, file_id, target_model, tg_chat)
        return

    try:
        async with async_session() as db:
            if not printer_id:
                await callback.answer(t(lang, NS, "queue_add.failed"), show_alert=True)
                return

            queue_id = await resolve_queue_id(printer_id)
            if queue_id is None:
                await callback.answer(t(lang, NS, "queue_add.failed"), show_alert=True)
                return

            item = PrintQueueItem(
                queue_id=queue_id,
                library_file_id=file_id,
                status="pending",
                position=await next_queue_position(db, queue_id),
                created_by_id=tg_chat.user_id if tg_chat else None,
            )
            db.add(item)
            await db.commit()
            pos = item.position

        await callback.answer(f"\u2705 {t(lang, NS, 'queue_add.added', pos=pos)}")
    except Exception:
        await callback.answer(t(lang, NS, "queue_add.failed"), show_alert=True)

    from backend.app.services.telegram_handlers.queue import render_queue

    await render_queue(callback, tg_chat)


@router.callback_query(F.data == "qadd:cancel")
async def cb_qadd_cancel(callback: CallbackQuery, state: FSMContext, **kwargs) -> None:
    """Cancel add to queue."""
    await state.clear()
    await callback.answer()
    from backend.app.services.telegram_handlers.queue import render_queue

    await render_queue(callback)
