"""Completion assessment drafts: Telegram taps must not mutate archive facts early."""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.app.models.archive import PrintArchive
from backend.app.models.archive_part import PrintArchivePart
from backend.app.models.print_completion_receipt import PrintCompletionReceipt

pytestmark = pytest.mark.unit

MOD = "backend.app.services.telegram_handlers.defects"


class _State:
    def __init__(self) -> None:
        self.data: dict = {}
        self.state = None

    async def clear(self):
        self.data = {}
        self.state = None

    async def set_state(self, value):
        self.state = value

    async def update_data(self, **values):
        self.data.update(values)

    async def get_data(self):
        return dict(self.data)


@pytest.fixture
def patched_session(test_engine):
    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    with patch("backend.app.core.database.async_session", maker):
        yield maker


async def _print(db, parts: dict[str, int], *, quantity: int | None = None) -> PrintArchive:
    archive = PrintArchive(
        printer_id=5,
        filename="plate.3mf",
        print_name="Plate",
        file_path="x/plate.3mf",
        file_size=1,
        status="completed",
        quantity=sum(parts.values()) if quantity is None else quantity,
        completed_at=datetime.now(timezone.utc),
    )
    db.add(archive)
    await db.flush()
    db.add_all(
        PrintArchivePart(archive_id=archive.id, name=name, name_key=name, quantity=quantity)
        for name, quantity in parts.items()
    )
    await db.commit()
    return archive


def _callback(data: str, *, user_id: int = 7):
    callback = MagicMock()
    callback.data = data
    callback.from_user = MagicMock(id=user_id)
    callback.answer = AsyncMock()
    callback.message = MagicMock()
    callback.message.chat = MagicMock(id=4242)
    callback.message.answer = AsyncMock(return_value=MagicMock(message_id=99))
    callback.message.edit_text = AsyncMock()
    return callback


def _markup(callback):
    return callback.message.answer.await_args.kwargs["reply_markup"]


def _button(markup, prefix: str) -> str:
    return next(
        button.callback_data
        for row in markup.inline_keyboard
        for button in row
        if button.callback_data.startswith(prefix)
    )


def _allowed():
    return (
        patch(f"{MOD}.has_perm", MagicMock(return_value=True)),
        patch(f"{MOD}.chat_allows_printer", MagicMock(return_value=True)),
        patch(f"{MOD}.get_language", AsyncMock(return_value="en")),
    )


async def test_draft_does_not_write_until_all_parts_are_confirmed(patched_session, db_session):
    from backend.app.services.telegram_handlers.defects import cb_defects_start, cb_defects_value

    archive = await _print(db_session, {"lid": 2, "base": 3})
    archive_id = archive.id
    start = _callback(f"action:defects:{archive.id}")
    p1, p2, p3 = _allowed()
    with p1, p2, p3:
        await cb_defects_start(start, _State())
    first = _button(_markup(start), "defv:")

    value = _callback(first)
    p1, p2, p3 = _allowed()
    with p1, p2, p3:
        await cb_defects_value(value, _State())

    db_session.expire_all()
    assert (await db_session.get(PrintArchive, archive_id)).defective_count == 0
    assert (
        await db_session.scalar(select(PrintCompletionReceipt).where(PrintCompletionReceipt.archive_id == archive_id))
        is None
    )

    second = _button(_markup(value), "defv:")
    finish = _callback(second)
    p1, p2, p3 = _allowed()
    with p1, p2, p3:
        await cb_defects_value(finish, _State())

    db_session.expire_all()
    receipt = await db_session.scalar(
        select(PrintCompletionReceipt).where(PrintCompletionReceipt.archive_id == archive_id)
    )
    assert (await db_session.get(PrintArchive, archive_id)).defective_count == 0
    assert receipt is not None and receipt.assessment["defective_count"] == 0


async def test_flat_print_uses_the_same_draft_then_commits_its_final_count(patched_session, db_session):
    from backend.app.services.telegram_handlers.defects import cb_defects_start, cb_defects_value

    archive = await _print(db_session, {}, quantity=3)
    archive_id = archive.id
    start = _callback(f"action:defects:{archive.id}")
    p1, p2, p3 = _allowed()
    with p1, p2, p3:
        await cb_defects_start(start, _State())
    value = _callback(_button(_markup(start), "defv:"))
    p1, p2, p3 = _allowed()
    with p1, p2, p3:
        await cb_defects_value(value, _State())

    db_session.expire_all()
    saved = await db_session.get(PrintArchive, archive_id)
    receipt = await db_session.scalar(
        select(PrintCompletionReceipt).where(PrintCompletionReceipt.archive_id == archive_id)
    )
    assert saved.defective_count == 0
    assert receipt is not None and receipt.assessment["defective_count"] == 0


async def test_a_draft_belongs_to_the_operator_who_opened_it(patched_session, db_session):
    from backend.app.services.telegram_handlers.defects import cb_defects_start, cb_defects_value

    archive = await _print(db_session, {"lid": 2})
    start = _callback(f"action:defects:{archive.id}", user_id=7)
    p1, p2, p3 = _allowed()
    with p1, p2, p3:
        await cb_defects_start(start, _State())
    stolen = _callback(_button(_markup(start), "defv:"), user_id=8)
    p1, p2, p3 = _allowed()
    with p1, p2, p3:
        await cb_defects_value(stolen, _State())

    assert stolen.answer.await_args.kwargs["show_alert"] is True
    assert (
        await db_session.scalar(select(PrintCompletionReceipt).where(PrintCompletionReceipt.archive_id == archive.id))
        is None
    )


async def test_a_permission_revoked_after_opening_refuses_the_next_draft_step(patched_session, db_session):
    from backend.app.services.telegram_handlers.defects import cb_defects_start, cb_defects_value

    archive = await _print(db_session, {"lid": 2})
    start = _callback(f"action:defects:{archive.id}")
    p1, p2, p3 = _allowed()
    with p1, p2, p3:
        await cb_defects_start(start, _State())
    denied = _callback(_button(_markup(start), "defv:"))
    with (
        patch(f"{MOD}.has_perm", MagicMock(return_value=False)),
        patch(f"{MOD}.chat_allows_printer", MagicMock(return_value=True)),
        patch(f"{MOD}.get_language", AsyncMock(return_value="en")),
    ):
        await cb_defects_value(denied, _State())

    assert denied.answer.await_args.kwargs["show_alert"] is True
    assert (
        await db_session.scalar(select(PrintCompletionReceipt).where(PrintCompletionReceipt.archive_id == archive.id))
        is None
    )


async def test_a_draft_does_not_cross_telegram_bots_with_the_same_chat_id(patched_session, db_session):
    from backend.app.services.telegram_handlers.defects import cb_defects_start, cb_defects_value

    archive = await _print(db_session, {"lid": 2})
    first_bot = MagicMock(provider_id=11)
    second_bot = MagicMock(provider_id=12)
    start = _callback(f"action:defects:{archive.id}", user_id=7)
    p1, p2, p3 = _allowed()
    with p1, p2, p3:
        await cb_defects_start(start, _State(), first_bot)
    foreign_bot = _callback(_button(_markup(start), "defv:"), user_id=7)
    p1, p2, p3 = _allowed()
    with p1, p2, p3:
        await cb_defects_value(foreign_bot, _State(), second_bot)

    assert foreign_bot.answer.await_args.kwargs["show_alert"] is True
    assert (
        await db_session.scalar(select(PrintCompletionReceipt).where(PrintCompletionReceipt.archive_id == archive.id))
        is None
    )


async def test_stale_button_cannot_destroy_the_current_draft(patched_session, db_session):
    from backend.app.services.telegram_handlers.defects import cb_defects_none, cb_defects_start, cb_defects_value

    archive = await _print(db_session, {"lid": 2, "base": 3})
    archive_id = archive.id
    start = _callback(f"action:defects:{archive.id}")
    p1, p2, p3 = _allowed()
    with p1, p2, p3:
        await cb_defects_start(start, _State())
    old_none = _button(_markup(start), "defn:")
    first = _button(_markup(start), "defv:")

    advance = _callback(first)
    p1, p2, p3 = _allowed()
    with p1, p2, p3:
        await cb_defects_value(advance, _State())
    stale = _callback(old_none)
    p1, p2, p3 = _allowed()
    with p1, p2, p3:
        await cb_defects_none(stale, _State())
    assert stale.answer.await_args.kwargs["show_alert"] is True

    finish = _callback(_button(_markup(advance), "defv:"))
    p1, p2, p3 = _allowed()
    with p1, p2, p3:
        await cb_defects_value(finish, _State())

    receipt = await db_session.scalar(
        select(PrintCompletionReceipt).where(PrintCompletionReceipt.archive_id == archive_id)
    )
    assert receipt is not None and receipt.assessment["defective_count"] == 0


async def test_old_raw_archive_callback_is_consumed_without_a_write(patched_session, db_session):
    from backend.app.services.telegram_handlers.defects import cb_legacy_defects

    archive = await _print(db_session, {"lid": 2})
    callback = _callback(f"defects:{archive.id}:1:2")
    with patch(f"{MOD}.get_language", AsyncMock(return_value="en")):
        await cb_legacy_defects(callback)

    assert callback.answer.await_args.kwargs["show_alert"] is True
    assert (await db_session.get(PrintArchive, archive.id)).defective_count == 0


async def test_force_reply_is_bound_to_the_draft_prompt(patched_session, db_session):
    from backend.app.services.telegram_handlers.defects import cb_defects_other, cb_defects_start, msg_defects_count

    archive = await _print(db_session, {"lid": 6})
    start = _callback(f"action:defects:{archive.id}")
    p1, p2, p3 = _allowed()
    with p1, p2, p3:
        await cb_defects_start(start, _State())
    other = _callback(_button(_markup(start), "defo:"))
    state = _State()
    p1, p2, p3 = _allowed()
    with p1, p2, p3:
        await cb_defects_other(other, state)

    message = MagicMock()
    message.text = "4"
    message.chat = MagicMock(id=4242)
    message.reply_to_message = MagicMock(message_id=98)
    message.answer = AsyncMock()
    with patch(f"{MOD}.get_language", AsyncMock(return_value="en")):
        await msg_defects_count(message, state)

    assert message.answer.awaited
    assert (await db_session.get(PrintArchive, archive.id)).defective_count == 0


async def test_force_reply_from_the_owner_finishes_the_draft(patched_session, db_session):
    from backend.app.services.telegram_handlers.defects import cb_defects_other, cb_defects_start, msg_defects_count

    archive = await _print(db_session, {"lid": 6})
    archive_id = archive.id
    start = _callback(f"action:defects:{archive.id}", user_id=7)
    p1, p2, p3 = _allowed()
    with p1, p2, p3:
        await cb_defects_start(start, _State())
    other = _callback(_button(_markup(start), "defo:"), user_id=7)
    state = _State()
    p1, p2, p3 = _allowed()
    with p1, p2, p3:
        await cb_defects_other(other, state)

    message = MagicMock()
    message.text = "4"
    message.chat = MagicMock(id=4242)
    message.from_user = MagicMock(id=7)
    message.reply_to_message = MagicMock(message_id=99)
    message.answer = AsyncMock()
    message.edit_text = AsyncMock()
    with (
        patch(f"{MOD}.get_language", AsyncMock(return_value="en")),
        patch(f"{MOD}.has_perm", MagicMock(return_value=True)),
        patch(f"{MOD}.chat_allows_printer", MagicMock(return_value=True)),
    ):
        await msg_defects_count(message, state)

    db_session.expire_all()
    receipt = await db_session.scalar(
        select(PrintCompletionReceipt).where(PrintCompletionReceipt.archive_id == archive_id)
    )
    assert receipt is not None and receipt.assessment["defective_count"] == 4


async def _ops_chat(db) -> None:
    from backend.app.models.group import Group
    from backend.app.models.notification import NotificationProvider
    from backend.app.models.telegram_chat import TelegramChat

    group = Group(name="Ops", permissions=["printers:clear_plate"])
    provider = NotificationProvider(name="Bot", provider_type="telegram", enabled=True, config='{"bot_token":"1:A"}')
    db.add_all([group, provider])
    await db.flush()
    db.add(TelegramChat(chat_id=4242, provider_id=provider.id, group_id=group.id, is_active=True))
    await db.commit()


def _buttons(markup) -> list[str]:
    return [button.callback_data for row in markup.inline_keyboard for button in row]


async def test_completion_notification_names_only_the_announced_archive(patched_session, db_session):
    from backend.app.services.notification_service import notification_service

    older = await _print(db_session, {"lid": 2})
    newer = await _print(db_session, {"base": 1})
    await _ops_chat(db_session)
    with (
        patch("backend.app.i18n.get_language", AsyncMock(return_value="en")),
        patch(
            "backend.app.services.printer_manager.printer_manager.is_awaiting_plate_clear",
            MagicMock(return_value=False),
        ),
    ):
        markup = await notification_service._build_telegram_actions("print_complete", 5, 4242, {"archive_id": older.id})

    assert f"action:defects:{older.id}" in _buttons(markup)
    assert f"action:defects:{newer.id}" not in _buttons(markup)
