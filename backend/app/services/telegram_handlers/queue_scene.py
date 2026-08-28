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
    scene_expired,
)
from backend.app.services.telegram_handlers.pagination import build_page_nav

if TYPE_CHECKING:
    from backend.app.models.telegram_chat import TelegramChat

router = Router()

PAGE_SIZE = 8


class QueueAddState(StatesGroup):
    selecting_file = State()
    selecting_target = State()
    selecting_location = State()
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


# A Telegram keyboard is a poor place to scroll a farm's whole location tree,
# and a place that holds no printer of the chosen model is never a useful
# answer. Cap what we draw and say what was left out — a silently truncated
# list reads as "that's all there is".
MAX_LOCATION_BUTTONS = 24


async def _locations_for_model(model: str) -> list[tuple[int, str]]:
    """Places worth offering for ``model``, as ``(id, path)``, shallowest first.

    A location qualifies when its **subtree** holds a printer of that model —
    the same subtree rule routing itself applies, so aiming at a workshop
    reaches the printers on its shelves.

    ⚠️ Archived printers don't count (they are retired), but a printer in
    Maintenance Mode does: that is temporary, and hiding its workshop would stop
    the operator queueing work for when it comes back. This is deliberately a
    weaker filter than ``printers_for_item`` uses at routing time — the question
    here is "is this place ever right", not "can it run right now".
    """
    from sqlalchemy import func as sa_func, select

    from backend.app.core.database import async_session
    from backend.app.models.printer import Printer
    from backend.app.services.printer_location_service import load_tree, path_of, subtree_ids

    async with async_session() as db:
        tree = await load_tree(db)
        if not tree:
            return []
        occupied = set(
            (
                await db.execute(
                    select(Printer.location_id)
                    .where(sa_func.lower(Printer.model) == model.lower())
                    .where(Printer.archived.is_(False))
                    .where(Printer.location_id.is_not(None))
                )
            )
            .scalars()
            .all()
        )

    if not occupied:
        return []
    offered = [loc_id for loc_id in tree if subtree_ids(tree, loc_id) & occupied]
    return sorted(((loc_id, path_of(tree, loc_id)) for loc_id in offered), key=lambda pair: pair[1])


@router.callback_query(F.data.startswith("qadd:model:"))
async def cb_qadd_select_model(callback: CallbackQuery, state: FSMContext, tg_chat: TelegramChat | None = None) -> None:
    """Model-based assignment selected — then, if it can matter, where."""
    lang = await get_language()
    model = callback.data.split(":")[2]
    await callback.answer()

    await state.update_data(
        printer_id=None,
        target_model=model,
        target_label=f"Any {model}",
        target_location_id=None,
        target_location_label=None,
    )

    locations = await _locations_for_model(model)
    if not locations:
        # Nothing to choose between: no locations on this farm, or none of them
        # holds a printer of this model. Asking anyway would be a step whose
        # every answer is the same.
        await state.set_state(QueueAddState.confirming)
        await _show_confirm(callback, state, lang)
        return

    await state.set_state(QueueAddState.selecting_location)
    await _show_location_list(callback, state, lang, locations)


async def _show_location_list(
    callback: CallbackQuery, state: FSMContext, lang: str, locations: list[tuple[int, str]]
) -> None:
    data = await state.get_data()
    lines = [
        f"📄 *{escape_md(data.get('file_name', '?'))}*\n",
        f"🎯 {escape_md(data.get('target_label', '?'))}\n",
        escape_md(t(lang, NS, "queue_add.select_location")),
    ]

    shown = locations[:MAX_LOCATION_BUTTONS]
    hidden = len(locations) - len(shown)
    if hidden:
        lines.append(escape_md(t(lang, NS, "queue_add.locations_hidden", count=hidden)))

    btns = [
        [
            InlineKeyboardButton(
                text=f"🌍 {t(lang, NS, 'queue_add.btn_anywhere')}",
                callback_data="qadd:loc:any",
            )
        ]
    ]
    for loc_id, path in shown:
        btns.append([InlineKeyboardButton(text=f"📍 {path}", callback_data=f"qadd:loc:{loc_id}")])

    btns.append([InlineKeyboardButton(text=f"◀️ {t(lang, NS, 'printers.btn_back')}", callback_data="qadd:start")])

    await callback.message.edit_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(inline_keyboard=btns))


@router.callback_query(F.data.startswith("qadd:loc:"))
async def cb_qadd_select_location(
    callback: CallbackQuery, state: FSMContext, tg_chat: TelegramChat | None = None
) -> None:
    """Location filter chosen (or explicitly declined)."""
    lang = await get_language()
    raw = callback.data.split(":")[2]
    await callback.answer()

    if raw == "any":
        await state.update_data(target_location_id=None, target_location_label=None)
    else:
        location_id = int(raw)
        label = None
        data = await state.get_data()
        model = data.get("target_model")
        if model:
            label = next((path for lid, path in await _locations_for_model(model) if lid == location_id), None)
        await state.update_data(target_location_id=location_id, target_location_label=label)

    await state.set_state(QueueAddState.confirming)
    await _show_confirm(callback, state, lang)


async def _show_confirm(callback: CallbackQuery, state: FSMContext, lang: str) -> None:
    data = await state.get_data()
    file_name = data.get("file_name", "?")
    target_label = data.get("target_label", "?")
    location_label = data.get("target_location_label")

    text = (
        f"\U0001f4cb *{escape_md(t(lang, NS, 'queue_add.confirm_title'))}*\n\n"
        f"\U0001f4c4 {escape_md(t(lang, NS, 'queue.file'))}: *{escape_md(file_name)}*\n"
        f"\U0001f3af {escape_md(target_label)}"
    )
    if location_label:
        text += f"\n\U0001f4cd {escape_md(t(lang, NS, 'queue_add.location'))}: {escape_md(location_label)}"

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
    target_location_id: int | None = None,
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
                target_location_id=target_location_id,
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
        await _add_to_auto_queue(callback, lang, file_id, target_model, tg_chat, data.get("target_location_id"))
        return

    # Neither a printer nor a model: nothing was ever chosen here, which after a
    # restart is what an intact keyboard over a dead wizard looks like.
    if not printer_id and not target_model:
        await scene_expired(callback, lang)
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
