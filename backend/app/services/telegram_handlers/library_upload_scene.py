"""Send the bot a file: it lands in the library, and prints from there.

The request was "кинув боту 3mf, обрав принтер, надрукував". Everything after
"кинув" already existed — :mod:`library_scene` browses, picks a printer, prints
or queues. The bot simply accepted no documents at all.

So this module owns exactly the missing link: receive, ask where to put it,
store it, and hand off. The hand-off is the existing ``lib:file:{id}``
callback, so the printer picker and dispatch are reused rather than repeated.

⚠️ **The folder is asked BEFORE the download.** Telegram keeps ``file_id``
valid, so nothing is lost by waiting — and an abandoned dialog then costs no
transfer at all instead of twenty megabytes fetched for nothing.
"""

from __future__ import annotations

from io import BytesIO
from typing import TYPE_CHECKING

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from backend.app.i18n import escape_md, get_language, t
from backend.app.services.telegram_handlers.common import NS, has_perm, scene_expired

if TYPE_CHECKING:
    from backend.app.models.telegram_chat import TelegramChat

router = Router()

# Telegram's Bot API lets a bot download at most 20 MB. A local Bot API server
# would raise it; BamDude configures none, so this is the real ceiling.
# ``document.file_size`` arrives with the message, which is what lets us refuse
# by name and number instead of failing minutes into a transfer.
MAX_DOWNLOAD_BYTES = 20 * 1024 * 1024

# Where uploads land unless the operator picks otherwise. Created on first use.
DEFAULT_FOLDER_NAME = "Telegram"

FOLDER_PAGE = 6


class LibraryUploadState(StatesGroup):
    choosing_folder = State()


async def _default_folder_id() -> int:
    """The ``Telegram`` folder, created the first time a file arrives."""
    from sqlalchemy import select

    from backend.app.core.database import async_session
    from backend.app.models.library import LibraryFolder

    async with async_session() as db:
        existing = (
            await db.execute(select(LibraryFolder).where(LibraryFolder.name == DEFAULT_FOLDER_NAME))
        ).scalar_one_or_none()
        if existing:
            return existing.id
        folder = LibraryFolder(name=DEFAULT_FOLDER_NAME)
        db.add(folder)
        await db.commit()
        await db.refresh(folder)
        return folder.id


async def _folder_choices(default_id: int) -> list[tuple[int | None, str]]:
    """``(id, name)`` for the picker — the default first, then the rest."""
    from sqlalchemy import select

    from backend.app.core.database import async_session
    from backend.app.models.library import LibraryFolder

    async with async_session() as db:
        rows = (await db.execute(select(LibraryFolder.id, LibraryFolder.name).order_by(LibraryFolder.name))).all()

    others = [(fid, name) for fid, name in rows if fid != default_id][: FOLDER_PAGE - 1]
    return [(default_id, DEFAULT_FOLDER_NAME), *others]


@router.message(F.document)
async def on_document(message: Message, state: FSMContext, tg_chat: TelegramChat | None = None) -> None:
    """A file arrived. Check what we can before spending a download on it."""
    lang = await get_language()
    doc = message.document

    # Receiving a file is a write to the library. Gated like every other bot
    # action — by the chat's permissions, not by who happened to send it.
    if not has_perm(tg_chat, "library:upload"):
        await message.answer(escape_md(t(lang, NS, "auth.no_permission")))
        return

    if (doc.file_size or 0) > MAX_DOWNLOAD_BYTES:
        await message.answer(
            escape_md(
                t(
                    lang,
                    NS,
                    "library.upload.too_big",
                    name=doc.file_name or "?",
                    size=round((doc.file_size or 0) / 1048576, 1),
                    limit=MAX_DOWNLOAD_BYTES // 1048576,
                )
            )
        )
        return

    default_id = await _default_folder_id()
    await state.set_state(LibraryUploadState.choosing_folder)
    await state.update_data(file_id=doc.file_id, file_name=doc.file_name or "upload.bin")

    buttons = [
        [InlineKeyboardButton(text=f"\U0001f4c1 {name}", callback_data=f"libup:folder:{fid}")]
        for fid, name in await _folder_choices(default_id)
    ]
    buttons.append([InlineKeyboardButton(text=f"❌ {t(lang, NS, 'library.btn_cancel')}", callback_data="libup:cancel")])

    await message.answer(
        f"\U0001f4e5 *{escape_md(doc.file_name or '?')}*\n\n{escape_md(t(lang, NS, 'library.upload.choose_folder'))}",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
    )


@router.callback_query(F.data == "libup:cancel", LibraryUploadState.choosing_folder)
async def cb_cancel(callback: CallbackQuery, state: FSMContext, tg_chat: TelegramChat | None = None) -> None:
    lang = await get_language()
    await state.clear()
    await callback.answer()
    await callback.message.edit_text(escape_md(t(lang, NS, "library.upload.cancelled")))


@router.callback_query(F.data.startswith("libup:folder:"), LibraryUploadState.choosing_folder)
async def cb_folder_chosen(callback: CallbackQuery, state: FSMContext, tg_chat: TelegramChat | None = None) -> None:
    """Folder picked — now fetch the bytes and store them."""
    from fastapi import HTTPException
    from sqlalchemy import select

    from backend.app.api.routes.library import store_library_upload
    from backend.app.core.database import async_session
    from backend.app.models.library import LibraryFolder

    lang = await get_language()
    data = await state.get_data()
    file_id, file_name = data.get("file_id"), data.get("file_name")
    if not file_id:
        # The wizard died with a restart; say so rather than "failed", which
        # reads as a refusal. Same helper the other scenes use.
        await scene_expired(callback, lang)
        await state.clear()
        return

    folder_id = int(callback.data.split(":")[2])
    await callback.answer()
    await callback.message.edit_text(escape_md(t(lang, NS, "library.upload.saving", name=file_name)))

    buf = BytesIO()
    try:
        await callback.bot.download(file_id, destination=buf)
    except Exception as e:  # noqa: BLE001 — any transport failure reads the same to the operator
        await callback.message.edit_text(escape_md(t(lang, NS, "library.upload.download_failed", error=str(e))))
        await state.clear()
        return

    async with async_session() as db:
        folder = (await db.execute(select(LibraryFolder).where(LibraryFolder.id == folder_id))).scalar_one_or_none()
        try:
            library_file, duplicate_of = await store_library_upload(
                db,
                filename=file_name,
                content=buf.getvalue(),
                target_folder=folder,
            )
        except HTTPException as e:
            # ⚠️ Shown verbatim. These messages were written for a human —
            # "the file is not a valid 3MF archive" and the like — and a second
            # vocabulary for the same rejections would only be a worse one.
            await callback.message.edit_text(escape_md(str(e.detail)))
            await state.clear()
            return
        except Exception:
            await callback.message.edit_text(escape_md(t(lang, NS, "library.upload.failed")))
            await state.clear()
            return

        # ⚠️ Printability comes from the TAG, which since 2026-08-16 is decided
        # by what is inside the container rather than by its name. A model
        # called ``something.gcode.3mf`` gets no Print button here, because the
        # printer would answer thirty seconds later that it cannot parse it.
        printable = "gcode" in (library_file.file_tags or [])
        file_pk, stored_name = library_file.id, library_file.filename

    await state.clear()

    lines = [f"✅ *{escape_md(stored_name)}*", escape_md(t(lang, NS, "library.upload.saved"))]
    if duplicate_of:
        lines.append(escape_md(t(lang, NS, "library.upload.duplicate")))

    buttons: list[list[InlineKeyboardButton]] = []
    if printable:
        buttons.append(
            [
                InlineKeyboardButton(
                    text=f"\U0001f5a8 {t(lang, NS, 'library.upload.btn_print')}", callback_data=f"lib:file:{file_pk}"
                )
            ]
        )
    else:
        # Said plainly rather than by omission: the operator sent a model on
        # purpose and should learn why no Print button appeared.
        lines.append(escape_md(t(lang, NS, "library.upload.not_sliced")))

    await callback.message.edit_text(
        "\n".join(lines),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons) if buttons else None,
    )
