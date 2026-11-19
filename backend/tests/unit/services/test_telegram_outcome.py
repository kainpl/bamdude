"""«Без браку» and the reaction shortcut: a mark that somebody looked, never a verdict."""

import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.app.models.archive import PrintArchive
from backend.app.models.group import Group
from backend.app.models.notification import PROVIDER_EVENT_DEFAULTS, NotificationProvider
from backend.app.models.telegram_chat import ALL_NOTIFY_EVENTS, DEFAULT_NOTIFY_EVENTS, TelegramChat
from backend.app.schemas.notification import NotificationProviderCreate
from backend.app.services import telegram_bot as tb, telegram_outcome_messages as registry
from backend.app.services.notification_events import EVENT_CATALOG, default_inbox_events
from backend.app.services.telegram_handlers import defects

pytestmark = pytest.mark.unit

MOD = "backend.app.services.telegram_handlers.defects"
DATA = Path(__file__).resolve().parents[3] / "app" / "data"
BOT_ID, CHAT_ID, MESSAGE_ID = 123456, 4242, 77


@pytest.fixture(autouse=True)
def clean_memory():
    registry.clear()
    defects._drafts.clear()
    defects._reply_prompts.clear()
    saved = dict(tb._bot_ids)
    yield
    registry.clear()
    defects._drafts.clear()
    defects._reply_prompts.clear()
    tb._bot_ids.clear()
    tb._bot_ids.update(saved)


@pytest.fixture
def patched_session(test_engine):
    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    with (
        patch("backend.app.core.database.async_session", maker),
        patch(f"{MOD}.get_language", AsyncMock(return_value="en")),
        patch("backend.app.i18n.get_language", AsyncMock(return_value="en")),
    ):
        yield maker


async def _print(db, **extra) -> PrintArchive:
    fields = {
        "printer_id": 5,
        "filename": "plate.3mf",
        "print_name": "Plate",
        "file_path": "x/plate.3mf",
        "file_size": 1,
        "status": "completed",
        "quantity": 4,
        "completed_at": datetime.now(timezone.utc),
    }
    archive = PrintArchive(**{**fields, **extra})
    db.add(archive)
    await db.commit()
    await db.refresh(archive)
    return archive


async def _chat(db, permissions=("printers:clear_plate",), *, active: bool = True) -> TelegramChat:
    group = Group(name=f"Ops-{len(permissions)}-{active}", permissions=list(permissions))
    provider = NotificationProvider(name="Bot", provider_type="telegram", enabled=True, config='{"bot_token":"1:A"}')
    db.add_all([group, provider])
    await db.flush()
    chat = TelegramChat(chat_id=CHAT_ID, provider_id=provider.id, group_id=group.id, is_active=active)
    db.add(chat)
    await db.commit()
    chat = (
        await db.execute(
            select(TelegramChat).where(TelegramChat.id == chat.id).execution_options(populate_existing=True)
        )
    ).scalar_one()
    tb._bot_ids[BOT_ID] = provider.id
    return chat


async def _mark(db, archive_id: int):
    return (
        await db.execute(
            select(PrintArchive.outcome_checked_at, PrintArchive.defective_count).where(PrintArchive.id == archive_id)
        )
    ).one()


def _callback(data: str, markup=None):
    callback = MagicMock()
    callback.data = data
    callback.from_user = SimpleNamespace(id=7, full_name="Olha", username="olha")
    callback.answer = AsyncMock()
    callback.message = MagicMock()
    callback.message.chat = MagicMock(id=CHAT_ID)
    callback.message.message_id = MESSAGE_ID
    callback.message.photo = None
    callback.message.md_text = "*Print Complete*\nPlate"
    callback.message.reply_markup = markup
    callback.message.edit_text = AsyncMock()
    callback.message.edit_caption = AsyncMock()
    callback.message.edit_reply_markup = AsyncMock()
    return callback


def _card_markup(archive: PrintArchive) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Clear", callback_data=f"action:clear_plate:5:{archive.id}")],
            [
                InlineKeyboardButton(text="Defects", callback_data=f"action:defects:{archive.id}"),
                InlineKeyboardButton(
                    text="No defects", callback_data=f"defok:{archive.id}:{archive.recovery_identity[:8]}"
                ),
            ],
        ]
    )


def _reaction(new=("👍",), old=(), *, user=True, message_id=MESSAGE_ID):
    bot = MagicMock()
    bot.id = BOT_ID
    bot.send_message = AsyncMock(return_value=MagicMock(message_id=500))
    return SimpleNamespace(
        bot=bot,
        chat=SimpleNamespace(id=CHAT_ID),
        message_id=message_id,
        user=SimpleNamespace(id=7, is_bot=False, full_name="Olha", username="olha") if user else None,
        old_reaction=[SimpleNamespace(emoji=e) for e in old],
        new_reaction=[SimpleNamespace(emoji=e) for e in new],
    )


# --- the catalog ------------------------------------------------------------------------------------------


def test_the_reminder_is_catalogued_off_by_default_everywhere():
    meta = EVENT_CATALOG["print_outcome_unchecked"]
    assert (meta.severity, meta.group) == ("info", "print")
    assert "print_outcome_unchecked" not in default_inbox_events()
    assert PROVIDER_EVENT_DEFAULTS["on_print_outcome_unchecked"] is False
    assert NotificationProviderCreate(name="n", provider_type="ntfy", config={}).on_print_outcome_unchecked is False
    assert "print_outcome_unchecked" in ALL_NOTIFY_EVENTS
    assert "print_outcome_unchecked" not in DEFAULT_NOTIFY_EVENTS


def test_both_templates_name_the_print_the_printer_the_end_and_the_link():
    for language in ("en", "uk"):
        body = json.loads((DATA / f"notification_templates_{language}.json").read_text(encoding="utf-8"))[
            "print_outcome_unchecked"
        ]["body_template"]
        for variable in ("{printer}", "{filename}", "{finished_at}", "{archive_url}"):
            assert variable in body, (language, variable)


def test_both_bot_vocabularies_have_the_new_lines():
    for language in ("en", "uk"):
        lines = json.loads((DATA / f"telegram_ui_{language}.json").read_text(encoding="utf-8"))["defects"]
        for key in ("btn_no_defects", "checked_by", "checked", "checked_already", "not_completed"):
            assert lines.get(key), (language, key)
        assert "{who}" in lines["checked_by"]


# --- the message registry ---------------------------------------------------------------------------------


def test_the_registry_is_bounded_per_bot_and_keyed_on_the_bot():
    for message_id in range(registry.MAX_PER_BOT + 10):
        registry.remember(BOT_ID, CHAT_ID, message_id, message_id, f"identity-{message_id}")
    assert registry.lookup(BOT_ID, CHAT_ID, 0) is None, "the oldest went"
    newest = registry.MAX_PER_BOT + 9
    assert registry.lookup(BOT_ID, CHAT_ID, newest) == (newest, f"identity-{newest}")
    assert len(registry._messages[BOT_ID]) == registry.MAX_PER_BOT
    # The same chat id under another bot is another chat.
    assert registry.lookup(BOT_ID + 1, CHAT_ID, newest) is None
    # A print without an identity is not remembered at all.
    registry.remember(BOT_ID, CHAT_ID, 9999, 1, None)
    assert registry.lookup(BOT_ID, CHAT_ID, 9999) is None


def test_an_old_entry_ages_out(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(registry.time, "monotonic", lambda: clock[0])
    registry.remember(BOT_ID, CHAT_ID, 1, 11, "a")
    clock[0] += registry.MAX_AGE_SECONDS + 1
    assert registry.lookup(BOT_ID, CHAT_ID, 1) is None


# --- the buttons ------------------------------------------------------------------------------------------


def _buttons(markup) -> list[str]:
    return [button.callback_data for row in markup.inline_keyboard for button in row]


async def test_the_completion_message_offers_both_answers(patched_session, db_session):
    from backend.app.services.notification_service import notification_service

    archive = await _print(db_session)
    await _chat(db_session)
    with patch(
        "backend.app.services.printer_manager.printer_manager.is_awaiting_plate_clear", MagicMock(return_value=False)
    ):
        extra = await notification_service._with_archive_identity("print_complete", {"archive_id": archive.id})
        markup = await notification_service._build_telegram_actions("print_complete", 5, CHAT_ID, extra)

    assert extra["archive_identity"] == archive.recovery_identity
    assert _buttons(markup) == [f"action:defects:{archive.id}", f"defok:{archive.id}:{archive.recovery_identity[:8]}"]


async def test_the_reminder_offers_both_answers_and_only_to_a_chat_that_may_answer(patched_session, db_session):
    from backend.app.services.notification_service import notification_service

    archive = await _print(db_session, printer_id=None)
    extra = {"archive_id": archive.id, "archive_identity": archive.recovery_identity}
    chat = await _chat(db_session)

    markup = await notification_service._build_telegram_actions("print_outcome_unchecked", None, CHAT_ID, extra)
    assert _buttons(markup) == [f"action:defects:{archive.id}", f"defok:{archive.id}:{archive.recovery_identity[:8]}"]

    group = await db_session.get(Group, chat.group_id)
    group.permissions = ["printers:read"]
    await db_session.commit()
    assert await notification_service._build_telegram_actions("print_outcome_unchecked", None, CHAT_ID, extra) is None


async def test_a_sent_completion_message_is_remembered_for_reactions(patched_session, db_session):
    from backend.app.services.notification_service import notification_service

    archive = await _print(db_session)
    chat = await _chat(db_session)
    bot = MagicMock(token="1:A")
    bot.id = BOT_ID

    async def send(provider_id, chat_id, text, **kwargs):
        kwargs["on_sent"](bot, MagicMock(message_id=MESSAGE_ID))
        return True

    with (
        patch("backend.app.services.telegram_bot.get_bot", MagicMock(return_value=bot)),
        patch("backend.app.services.telegram_bot.send_message", send),
        patch(
            "backend.app.services.printer_manager.printer_manager.is_awaiting_plate_clear",
            MagicMock(return_value=False),
        ),
    ):
        ok, _detail = await notification_service._send_telegram(
            {"bot_token": "1:A"},
            "*Print Complete*\nPlate",
            chat_id=str(CHAT_ID),
            event_type="print_complete",
            printer_id=5,
            extra_data={"archive_id": archive.id},
            provider_id=chat.provider_id,
        )

    assert ok
    assert registry.lookup(BOT_ID, CHAT_ID, MESSAGE_ID) == (archive.id, archive.recovery_identity)


# --- «Без браку» ------------------------------------------------------------------------------------------


async def test_no_defects_marks_the_print_and_says_who(patched_session, db_session):
    archive = await _print(db_session, defective_count=1)
    chat = await _chat(db_session)
    callback = _callback(f"defok:{archive.id}:{archive.recovery_identity[:8]}", _card_markup(archive))

    await defects.cb_no_defects(callback, tg_chat=chat)

    marked, defective = await _mark(db_session, archive.id)
    assert marked is not None and defective == 1, "checked; the defect count is not a verdict to overwrite"
    callback.answer.assert_awaited_once_with("Marked as checked, no defects")
    text = callback.message.edit_text.await_args.args[0]
    assert text.startswith("*Print Complete*\nPlate") and "Olha" in text
    kept = callback.message.edit_text.await_args.kwargs["reply_markup"]
    assert _buttons(kept) == [f"action:clear_plate:5:{archive.id}"], "the plate answer stays on the card"


async def test_a_second_press_changes_nothing(patched_session, db_session):
    archive = await _print(db_session)
    chat = await _chat(db_session)
    data = f"defok:{archive.id}:{archive.recovery_identity[:8]}"
    await defects.cb_no_defects(_callback(data), tg_chat=chat)
    first, _ = await _mark(db_session, archive.id)

    again = _callback(data, _card_markup(archive))
    await defects.cb_no_defects(again, tg_chat=chat)

    assert (await _mark(db_session, archive.id))[0] == first
    again.answer.assert_awaited_once_with("This print's result was already checked")
    again.message.edit_text.assert_not_awaited()


async def test_a_chat_without_the_right_is_refused(patched_session, db_session):
    archive = await _print(db_session)
    chat = await _chat(db_session, permissions=("printers:read",))
    callback = _callback(f"defok:{archive.id}:{archive.recovery_identity[:8]}")

    await defects.cb_no_defects(callback, tg_chat=chat)

    assert (await _mark(db_session, archive.id))[0] is None
    assert callback.answer.await_args.kwargs == {"show_alert": True}


async def test_a_chat_scoped_to_other_printers_is_refused(patched_session, db_session):
    archive = await _print(db_session)
    chat = await _chat(db_session)
    with patch(f"{MOD}.chat_allows_printer", MagicMock(return_value=False)):
        await defects.cb_no_defects(_callback(f"defok:{archive.id}:"), tg_chat=chat)
    assert (await _mark(db_session, archive.id))[0] is None


async def test_a_button_of_a_print_whose_id_was_reused_marks_nothing(patched_session, db_session):
    archive = await _print(db_session)
    chat = await _chat(db_session)
    callback = _callback(f"defok:{archive.id}:deadbeef")

    await defects.cb_no_defects(callback, tg_chat=chat)

    assert (await _mark(db_session, archive.id))[0] is None
    callback.answer.assert_awaited_once_with("This print no longer exists", show_alert=True)


async def test_a_failed_print_is_not_marked(patched_session, db_session):
    archive = await _print(db_session, status="failed")
    chat = await _chat(db_session)
    callback = _callback(f"defok:{archive.id}:{archive.recovery_identity[:8]}")

    await defects.cb_no_defects(callback, tg_chat=chat)

    assert (await _mark(db_session, archive.id))[0] is None
    assert callback.answer.await_args.kwargs == {"show_alert": True}


# --- reactions --------------------------------------------------------------------------------------------


async def test_a_thumbs_up_marks_the_print(patched_session, db_session):
    archive = await _print(db_session)
    await _chat(db_session)
    registry.remember(BOT_ID, CHAT_ID, MESSAGE_ID, archive.id, archive.recovery_identity)
    event = _reaction()

    await defects.on_outcome_reaction(event)

    assert (await _mark(db_session, archive.id))[0] is not None
    sent = event.bot.send_message.await_args.kwargs
    assert sent["chat_id"] == CHAT_ID and sent["reply_to_message_id"] == MESSAGE_ID and "Olha" in sent["text"]


async def test_a_thumbs_down_opens_the_defects_prompt_and_records_nothing(patched_session, db_session):
    archive = await _print(db_session)
    chat = await _chat(db_session)
    registry.remember(BOT_ID, CHAT_ID, MESSAGE_ID, archive.id, archive.recovery_identity)
    event = _reaction(new=("👎",))

    await defects.on_outcome_reaction(event)

    marked, defective = await _mark(db_session, archive.id)
    assert marked is None and defective == 0, "a 👎 is a question, never a verdict"
    (draft,) = defects._drafts.values()
    assert draft.archive_id == archive.id and draft.owner == (chat.provider_id, CHAT_ID, 7)
    markup = event.bot.send_message.await_args.kwargs["reply_markup"]
    assert any(data.startswith("defv:") for data in _buttons(markup))


@pytest.mark.parametrize(
    "event_kwargs",
    [
        {"new": (), "old": ("👍",)},  # the reaction was removed
        {"new": ("👍",), "old": ("👍",)},  # nothing new
        {"new": ("🔥",)},  # not one of the two
        {"user": False},  # an anonymous admin: nobody to ask the right of
        {"message_id": 999},  # a message this process does not remember
    ],
)
async def test_a_reaction_that_means_nothing_does_nothing(patched_session, db_session, event_kwargs):
    archive = await _print(db_session)
    await _chat(db_session)
    registry.remember(BOT_ID, CHAT_ID, MESSAGE_ID, archive.id, archive.recovery_identity)
    event = _reaction(**event_kwargs)

    await defects.on_outcome_reaction(event)

    assert (await _mark(db_session, archive.id))[0] is None
    event.bot.send_message.assert_not_awaited()
    assert not defects._drafts


@pytest.mark.parametrize("case", ["no_right", "inactive", "unknown_chat", "reused_id"])
async def test_a_reaction_without_authority_or_identity_does_nothing(patched_session, db_session, case):
    archive = await _print(db_session)
    identity = archive.recovery_identity
    if case == "no_right":
        await _chat(db_session, permissions=("printers:read",))
    elif case == "inactive":
        await _chat(db_session, active=False)
    elif case == "reused_id":
        await _chat(db_session)
        identity = "00000000-0000-0000-0000-000000000000"
    registry.remember(BOT_ID, CHAT_ID, MESSAGE_ID, archive.id, identity)
    event = _reaction()

    await defects.on_outcome_reaction(event)

    assert (await _mark(db_session, archive.id))[0] is None
    event.bot.send_message.assert_not_awaited()


def test_the_poller_asks_telegram_for_reactions():
    """aiogram derives ``allowed_updates`` from the registered handlers; Telegram sends no reaction
    update that was not asked for. The handler must sit in the router the dispatcher includes."""
    assert defects.router.message_reaction.handlers
    assert "message_reaction" in defects.router.resolve_used_update_types()
