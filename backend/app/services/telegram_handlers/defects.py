"""Telegram completion assessments remain drafts until a full snapshot commits."""

from __future__ import annotations

import asyncio
import secrets
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from aiogram import F, Router
from aiogram.filters import Filter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, ForceReply, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import select

from backend.app.i18n import escape_md, get_language, t
from backend.app.models.archive import PrintArchive
from backend.app.models.archive_part import PrintArchivePart
from backend.app.models.print_completion_receipt import PrintCompletionReceipt
from backend.app.models.printer import Printer
from backend.app.services.archive_defects import DefectsWrite
from backend.app.services.archive_parts import load_rows
from backend.app.services.plate_answers import (
    InvalidPlateAssessment,
    completion_assessment_snapshot,
    record_completion_assessment,
)
from backend.app.services.plate_hold import StalePlateAnswer
from backend.app.services.telegram_handlers.common import NS, chat_allows_printer, has_perm

if TYPE_CHECKING:
    from backend.app.models.telegram_chat import TelegramChat

router = Router()
MAX_BUTTONS = 5
_TTL = 30 * 60
_MAX_DRAFTS = 128
_MAX_DRAFTS_PER_OWNER = 8
Owner = tuple[int, int, int]


@dataclass
class _Draft:
    token: str
    archive_id: int
    printer_id: int | None
    owner: Owner | None
    rows: list[PrintArchivePart]
    values: dict[int, int]
    flat: int
    quantity: int
    cursor: int
    revision: int
    expires_at: float
    source_message_ids: set[int]
    snapshot: tuple
    reply_prompt_ids: set[int] = field(default_factory=set)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


_drafts: dict[str, _Draft] = {}
_reply_prompts: dict[tuple[Owner, int], tuple[str, int]] = {}


def _drop(draft: _Draft) -> None:
    """Remove every address of one draft; never touch a different operator's."""
    _drafts.pop(draft.token, None)
    for key, value in list(_reply_prompts.items()):
        if value[0] == draft.token:
            _reply_prompts.pop(key, None)


def _prune() -> None:
    now = time.monotonic()
    for draft in list(_drafts.values()):
        if draft.expires_at <= now:
            _drop(draft)


def clear_completion_drafts(provider_ids: set[int] | None = None) -> None:
    """Forget drafts for stopped bots (or every draft during process shutdown)."""
    for draft in list(_drafts.values()):
        if provider_ids is None or (draft.owner is not None and draft.owner[0] in provider_ids):
            _drop(draft)


def _owner(
    message: Message | None, tg_chat: TelegramChat | None, callback: CallbackQuery | None = None
) -> Owner | None:
    chat_id = getattr(getattr(message, "chat", None), "id", None)
    user_id = getattr(getattr(callback, "from_user", None), "id", None)
    if user_id is None:
        user_id = getattr(getattr(message, "from_user", None), "id", None)
    provider_id = getattr(tg_chat, "provider_id", None)
    if not isinstance(provider_id, int):
        # Production updates always have a bound TelegramChat.  The sentinel
        # keeps service-originated/test invocations owner-bound too, without
        # pretending that two configured bots share an identity.
        provider_id = 0
    return (provider_id, chat_id, user_id) if isinstance(chat_id, int) and isinstance(user_id, int) else None


def _message_id(message: Message | None) -> int | None:
    value = getattr(message, "message_id", None)
    return value if isinstance(value, int) else None


def _new(archive: PrintArchive, rows: list[PrintArchivePart], owner: Owner | None) -> _Draft | None:
    _prune()
    if owner is not None:
        existing = next((d for d in _drafts.values() if d.owner == owner and d.archive_id == archive.id), None)
        if existing is not None:
            return existing
        if sum(d.owner == owner for d in _drafts.values()) >= _MAX_DRAFTS_PER_OWNER:
            return None
    if len(_drafts) >= _MAX_DRAFTS:
        return None
    now = time.monotonic()
    token = secrets.token_urlsafe(6)
    draft = _Draft(
        token,
        archive.id,
        archive.printer_id,
        owner,
        rows,
        {row.id: int(row.defective or 0) for row in rows},
        int(archive.defective_count or 0),
        int(archive.quantity or 0),
        0,
        0,
        now + _TTL,
        set(),
        completion_assessment_snapshot(archive, rows),
    )
    _drafts[token] = draft
    return draft


def _leased_to_another_operator(archive_id: int, source_message_id: int | None, owner: Owner | None) -> bool:
    """One completion card in a group has one live operator lease."""
    if source_message_id is None:
        return False
    return any(
        draft.archive_id == archive_id
        and source_message_id in draft.source_message_ids
        and draft.owner is not None
        and draft.owner != owner
        for draft in _drafts.values()
    )


def _get(
    token: str,
    owner: Owner | None,
    *,
    source_message_id: int | None = None,
    revision: int | None = None,
) -> _Draft | None:
    draft = _drafts.get(token)
    if draft is None:
        return None
    if draft.expires_at <= time.monotonic():
        _drop(draft)
        return None
    if (
        (draft.owner is not None and draft.owner != owner)
        or (revision is not None and draft.revision != revision)
        or (
            source_message_id is not None
            and draft.source_message_ids
            and source_message_id not in draft.source_message_ids
        )
    ):
        return None
    return draft


class CompletionReply(Filter):
    """Match only replies to a live, addressed completion prompt.

    This replaces the global current FSM state: an unrelated numeric wizard in
    the same chat never sees a completion reply, and opening B cannot steal A.
    """

    async def __call__(self, message: Message, tg_chat: TelegramChat | None = None) -> bool:
        prompt_id = _message_id(getattr(message, "reply_to_message", None))
        owner = _owner(message, tg_chat)
        return isinstance(prompt_id, int) and owner is not None and (owner, prompt_id) in _reply_prompts


async def _load(db, archive_id: int) -> tuple[PrintArchive | None, list[PrintArchivePart]]:
    archive = await db.get(PrintArchive, archive_id)
    return (
        (archive, await load_rows(db, archive_id)) if archive is not None and archive.deleted_at is None else (None, [])
    )


async def _context(lang: str, archive: PrintArchive) -> str:
    from backend.app.core.database import async_session

    async with async_session() as db:
        printer = await db.get(Printer, archive.printer_id) if archive.printer_id is not None else None
    return escape_md(
        t(
            lang,
            NS,
            "defects.context",
            printer=printer.name if printer else f"#{archive.printer_id or '–'}",
            print_name=archive.print_name or archive.filename,
            archive_id=archive.id,
        )
    )


async def _prompt(lang: str, archive: PrintArchive, row: PrintArchivePart | None) -> str:
    question = (
        t(lang, NS, "defects.prompt_part", name=row.name, quantity=row.quantity)
        if row
        else t(
            lang, NS, "defects.prompt_flat", name=archive.print_name or archive.filename, quantity=archive.quantity or 0
        )
    )
    return f"{await _context(lang, archive)}\n{escape_md(question)}"


def _keyboard(draft: _Draft, row: PrintArchivePart | None, archive: PrintArchive, lang: str) -> InlineKeyboardMarkup:
    row_id = row.id if row else 0
    quantity = int(row.quantity if row else draft.quantity)
    numbers = [
        InlineKeyboardButton(text=str(n), callback_data=f"defv:{draft.token}:{draft.revision}:{row_id}:{n}")
        for n in range(min(quantity, MAX_BUTTONS) + 1)
    ]
    extra = []
    if quantity > MAX_BUTTONS:
        extra.append(
            InlineKeyboardButton(
                text=t(lang, NS, "defects.btn_other"), callback_data=f"defo:{draft.token}:{draft.revision}:{row_id}"
            )
        )
    extra.append(
        InlineKeyboardButton(
            text=f"✅ {t(lang, NS, 'defects.btn_none_rest')}",
            callback_data=f"defn:{draft.token}:{draft.revision}:{row_id}",
        )
    )
    return InlineKeyboardMarkup(
        inline_keyboard=[
            numbers,
            extra,
            [
                InlineKeyboardButton(
                    text=f"✖ {t(lang, NS, 'defects.btn_cancel')}", callback_data=f"defc:{draft.token}:{draft.revision}"
                )
            ],
        ]
    )


async def _ask(message: Message, lang: str, archive: PrintArchive, draft: _Draft) -> None:
    row = draft.rows[draft.cursor] if draft.rows else None
    prompt = await message.answer(await _prompt(lang, archive, row), reply_markup=_keyboard(draft, row, archive, lang))
    if isinstance(getattr(prompt, "message_id", None), int):
        draft.source_message_ids.add(prompt.message_id)


async def _replace_prompt(message: Message, text: str) -> None:
    """Edit a completion card without treating a photo as a text message."""
    if isinstance(getattr(message, "photo", None), (list, tuple)) and getattr(message, "photo", None):
        await message.edit_caption(caption=text)
    else:
        await message.edit_text(text)


async def _allow(
    callback: CallbackQuery, tg_chat: TelegramChat | None, archive: PrintArchive | None, lang: str
) -> bool:
    if archive is None:
        await callback.answer(t(lang, NS, "defects.gone"), show_alert=True)
        return False
    if not has_perm(tg_chat, "printers:clear_plate"):
        await callback.answer(t(lang, NS, "auth.no_permission"), show_alert=True)
        return False
    if not chat_allows_printer(tg_chat, archive.printer_id):
        await callback.answer(t(lang, NS, "auth.not_in_scope"), show_alert=True)
        return False
    return True


def _actor(tg_chat: TelegramChat | None) -> int | None:
    return tg_chat.user_id if tg_chat else None


async def _finish(message: Message, lang: str, draft: _Draft, tg_chat: TelegramChat | None) -> None:
    from backend.app.core.database import async_session

    stale = False
    archive = None
    result = None
    async with async_session() as db:
        # Do not preview in this session: record_completion_assessment must be
        # the first authoritative DB read so SQLite obtains BEGIN IMMEDIATE
        # before a competing writer can create a stale snapshot.
        write = DefectsWrite(parts=tuple(draft.values.items())) if draft.rows else DefectsWrite(flat=draft.flat)
        try:
            result = await record_completion_assessment(
                db, draft.archive_id, write, actor_id=_actor(tg_chat), expected_snapshot=draft.snapshot
            )
            await db.commit()
            archive = await db.get(PrintArchive, draft.archive_id)
        except (InvalidPlateAssessment, StalePlateAnswer):
            await db.rollback()
            stale = True
    _drop(draft)
    if stale or archive is None or result is None:
        await _replace_prompt(message, escape_md(t(lang, NS, "defects.stale_prompt")))
        return
    tail = (
        ""
        if not result.ledger_refused
        else "\n" + escape_md(t(lang, NS, "defects.ledger_refused", count=len(result.ledger_refused)))
    )
    await _replace_prompt(
        message,
        f"{await _context(lang, archive)}\n{escape_md(t(lang, NS, 'defects.done', defective=result.defective_count, quantity=archive.quantity or 0))}{tail}",
    )


async def start_defects_prompt(message: Message, archive_id: int, tg_chat: TelegramChat | None = None) -> None:
    """Compatibility entry point for service-originated prompts without a user id."""
    from backend.app.core.database import async_session

    lang = await get_language()
    async with async_session() as db:
        archive, rows = await _load(db, archive_id)
    if archive is None or archive.status != "completed" or not archive.quantity:
        return
    if not has_perm(tg_chat, "printers:clear_plate") or not chat_allows_printer(tg_chat, archive.printer_id):
        return
    draft = _new(archive, rows, None)
    if draft is not None:
        await _ask(message, lang, archive, draft)


@router.callback_query(F.data.startswith("action:defects:"))
async def cb_defects_start(callback: CallbackQuery, state: FSMContext, tg_chat: TelegramChat | None = None) -> None:
    from backend.app.core.database import async_session

    lang = await get_language()
    try:
        archive_id = int(callback.data.split(":")[2])
    except (IndexError, ValueError):
        await callback.answer(t(lang, NS, "defects.stale_prompt"), show_alert=True)
        return
    async with async_session() as db:
        archive, rows = await _load(db, archive_id)
        receipt = await db.scalar(select(PrintCompletionReceipt).where(PrintCompletionReceipt.archive_id == archive_id))
    if not await _allow(callback, tg_chat, archive, lang):
        return
    if receipt is not None and receipt.assessment is not None:
        assessment = receipt.assessment
        await callback.answer()
        await callback.message.answer(
            f"{await _context(lang, archive)}\n"
            f"{escape_md(t(lang, NS, 'defects.done', defective=assessment.get('defective_count', 0), quantity=archive.quantity or 0))}"
        )
        return
    await state.clear()
    owner = _owner(callback.message, tg_chat, callback)
    source_message_id = _message_id(callback.message)
    if _leased_to_another_operator(archive.id, source_message_id, owner):
        await callback.answer(t(lang, NS, "defects.busy"), show_alert=True)
        return
    await callback.answer()
    draft = _new(archive, rows, owner)
    if draft is None:
        await callback.message.answer(escape_md(t(lang, NS, "defects.too_many")))
        return
    if source_message_id is not None:
        draft.source_message_ids.add(source_message_id)
    await _ask(callback.message, lang, archive, draft)


async def _session(callback: CallbackQuery, tg_chat: TelegramChat | None, kind: str) -> tuple[str, _Draft] | None:
    lang = await get_language()
    parts = callback.data.split(":")
    try:
        revision = int(parts[2])
    except (IndexError, ValueError):
        revision = None
    draft = (
        _get(
            parts[1],
            _owner(callback.message, tg_chat, callback),
            source_message_id=_message_id(callback.message),
            revision=revision,
        )
        if len(parts) >= (3 if kind == "defc" else 4) and parts[0] == kind and revision is not None
        else None
    )
    if draft is None:
        await callback.answer(t(lang, NS, "defects.stale_prompt"), show_alert=True)
        return None
    if not has_perm(tg_chat, "printers:clear_plate"):
        await callback.answer(t(lang, NS, "auth.no_permission"), show_alert=True)
        return None
    if not chat_allows_printer(tg_chat, draft.printer_id):
        await callback.answer(t(lang, NS, "auth.not_in_scope"), show_alert=True)
        return None
    return lang, draft


@router.callback_query(F.data.startswith("defv:"))
async def cb_defects_value(callback: CallbackQuery, state: FSMContext, tg_chat: TelegramChat | None = None) -> None:
    found = await _session(callback, tg_chat, "defv")
    if found is None:
        return
    lang, draft = found
    try:
        _, _, _, raw_row, raw_value = callback.data.split(":")
        row_id, value = int(raw_row), int(raw_value)
    except ValueError:
        await callback.answer(t(lang, NS, "defects.stale_prompt"), show_alert=True)
        return
    async with draft.lock:
        row = next((item for item in draft.rows if item.id == row_id), None)
        maximum = row.quantity if row is not None else draft.quantity
        if (row is None and (draft.rows or row_id != 0)) or not 0 <= value <= maximum:
            await callback.answer(t(lang, NS, "defects.invalid", max=maximum), show_alert=True)
            return
        if row is not None:
            draft.values[row.id] = value
            cursor = draft.rows.index(row) + 1
        else:
            draft.flat = value
            cursor = 1
        draft.cursor, draft.revision, draft.expires_at = cursor, draft.revision + 1, time.monotonic() + _TTL
        await state.clear()
        if draft.cursor < len(draft.rows):
            from backend.app.core.database import async_session

            async with async_session() as db:
                archive, _ = await _load(db, draft.archive_id)
            if archive is not None:
                await _replace_prompt(
                    callback.message,
                    escape_md(
                        t(lang, NS, "defects.recorded_part", name=row.name, defective=value, quantity=row.quantity)
                    ),
                )
                await _ask(callback.message, lang, archive, draft)
        else:
            await _finish(callback.message, lang, draft, tg_chat)
    await callback.answer()


@router.callback_query(F.data.startswith("defn:"))
async def cb_defects_none(callback: CallbackQuery, state: FSMContext, tg_chat: TelegramChat | None = None) -> None:
    found = await _session(callback, tg_chat, "defn")
    if found is None:
        return
    lang, draft = found
    try:
        row_id = int(callback.data.split(":")[3])
    except ValueError:
        await callback.answer(t(lang, NS, "defects.stale_prompt"), show_alert=True)
        return
    async with draft.lock:
        index = next((i for i, row in enumerate(draft.rows) if row.id == row_id), None)
        if not draft.rows and row_id == 0:
            draft.flat = 0
        elif index is None:
            await callback.answer(t(lang, NS, "defects.stale_prompt"), show_alert=True)
            return
        else:
            for row in draft.rows[index:]:
                draft.values[row.id] = 0
        draft.revision += 1
        await state.clear()
        await _finish(callback.message, lang, draft, tg_chat)
    await callback.answer()


@router.callback_query(F.data.startswith("defo:"))
async def cb_defects_other(callback: CallbackQuery, state: FSMContext, tg_chat: TelegramChat | None = None) -> None:
    found = await _session(callback, tg_chat, "defo")
    if found is None:
        return
    lang, draft = found
    try:
        row_id = int(callback.data.split(":")[3])
    except ValueError:
        await callback.answer(t(lang, NS, "defects.stale_prompt"), show_alert=True)
        return
    row = next((item for item in draft.rows if item.id == row_id), None)
    if row is None and (draft.rows or row_id != 0):
        await callback.answer(t(lang, NS, "defects.stale_prompt"), show_alert=True)
        return
    maximum = row.quantity if row is not None else draft.quantity
    async with draft.lock:
        prompt = await callback.message.answer(
            escape_md(t(lang, NS, "defects.enter_count", max=maximum)),
            reply_markup=ForceReply(force_reply=True, input_field_placeholder=t(lang, NS, "defects.reply_placeholder")),
        )
        draft.revision += 1
        draft.expires_at = time.monotonic() + _TTL
        owner = _owner(callback.message, tg_chat, callback)
        prompt_id = _message_id(prompt)
        if owner is not None and prompt_id is not None:
            draft.reply_prompt_ids.add(prompt_id)
            _reply_prompts[(owner, prompt_id)] = (draft.token, draft.revision)
        # Keep the old state clear: completion replies are routed by the
        # addressed prompt registry, never by the chat's current FSM wizard.
        await state.clear()
    await callback.answer()


@router.message(CompletionReply())
async def msg_defects_count(message: Message, state: FSMContext, tg_chat: TelegramChat | None = None) -> None:
    lang = await get_language()
    owner = _owner(message, tg_chat)
    reply_id = _message_id(getattr(message, "reply_to_message", None))
    address = _reply_prompts.get((owner, reply_id)) if owner is not None and reply_id is not None else None
    draft = _get(address[0], owner, revision=address[1]) if address is not None else None
    row_id = None
    if draft is not None:
        # A reply prompt is opened for the currently rendered row; its revision
        # makes a late answer stale if the inline flow advanced meanwhile.
        row_id = draft.rows[draft.cursor].id if draft.rows and draft.cursor < len(draft.rows) else 0
    row = next((item for item in (draft.rows if draft else []) if item.id == row_id), None)
    maximum = row.quantity if row is not None else (draft.quantity if draft else 0)
    text = (message.text or "").strip()
    if draft is None:
        await state.clear()
        await message.answer(escape_md(t(lang, NS, "defects.stale_prompt")))
        return
    if (draft.rows and row is None) or not text.isdecimal() or int(text) > maximum:
        await message.answer(escape_md(t(lang, NS, "defects.invalid", max=maximum)))
        return
    async with draft.lock:
        if row is not None:
            draft.values[row.id] = int(text)
            cursor = draft.rows.index(row) + 1
        else:
            draft.flat = int(text)
            cursor = 1
        draft.cursor, draft.revision, draft.expires_at = cursor, draft.revision + 1, time.monotonic() + _TTL
        if owner is not None and reply_id is not None:
            _reply_prompts.pop((owner, reply_id), None)
        await state.clear()
        if draft.cursor < len(draft.rows):
            from backend.app.core.database import async_session

            async with async_session() as db:
                archive, _ = await _load(db, draft.archive_id)
            if archive is not None:
                await _ask(message, lang, archive, draft)
                return
        await _finish(message, lang, draft, tg_chat)


@router.callback_query(F.data.startswith("defc:"))
async def cb_defects_cancel(callback: CallbackQuery, state: FSMContext, tg_chat: TelegramChat | None = None) -> None:
    found = await _session(callback, tg_chat, "defc")
    if found is None:
        return
    lang, draft = found
    async with draft.lock:
        _drop(draft)
        await state.clear()
    await _replace_prompt(callback.message, escape_md(t(lang, NS, "defects.cancelled")))
    await callback.answer()


@router.callback_query(F.data.startswith("defects:"))
@router.callback_query(F.data.startswith("defects_none:"))
@router.callback_query(F.data.startswith("defects_other:"))
async def cb_legacy_defects(callback: CallbackQuery) -> None:
    """Old raw archive-id callbacks cannot mutate after a restart."""
    await callback.answer(t(await get_language(), NS, "defects.stale_prompt"), show_alert=True)
