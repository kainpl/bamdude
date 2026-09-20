"""Defects from Telegram: one tap per part, «other…» takes a typed number.

Real rows in the test database (the handlers open their own session), a fake
CallbackQuery / Message, permission and scope patched the way the plate-answer
tests do it.
"""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.app.i18n import escape_md, t
from backend.app.models.archive import PrintArchive
from backend.app.models.archive_part import PrintArchivePart

pytestmark = pytest.mark.unit

MOD = "backend.app.services.telegram_handlers.defects"
NS = "telegram_ui"


class _FakeState:
    """The FSM storage, small enough to assert against.

    A real ``FSMContext`` needs a storage and a key; what the handlers use of it
    is four calls and one dict, and the tests care whether the state was CLEARED
    — a stale «other…» turning the operator's next message into a defect count
    is the failure this stands in for.
    """

    def __init__(self) -> None:
        self.state = None
        self.data: dict = {}
        self.clears = 0

    async def set_state(self, state) -> None:
        self.state = state

    async def update_data(self, **kwargs) -> None:
        self.data.update(kwargs)

    async def get_data(self) -> dict:
        return dict(self.data)

    async def clear(self) -> None:
        self.state = None
        self.data = {}
        self.clears += 1


@pytest.fixture
def patched_session(test_engine):
    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    with patch("backend.app.core.database.async_session", maker):
        yield maker


async def _print(db_session, parts: dict[str, int] | None, *, quantity: int | None = None) -> PrintArchive:
    archive = PrintArchive(
        printer_id=5,
        filename="p.3mf",
        print_name="Plate",
        file_path="x/p.3mf",
        file_size=1,
        status="completed",
        quantity=quantity if quantity is not None else sum((parts or {}).values()),
        completed_at=datetime.now(timezone.utc),
    )
    db_session.add(archive)
    await db_session.flush()
    for name, qty in (parts or {}).items():
        db_session.add(PrintArchivePart(archive_id=archive.id, name=name, name_key=name.lower(), quantity=qty))
    await db_session.commit()
    await db_session.refresh(archive)
    return archive


def _callback(data: str) -> MagicMock:
    cb = MagicMock()
    cb.data = data
    cb.answer = AsyncMock()
    cb.message = MagicMock()
    cb.message.answer = AsyncMock()
    cb.message.edit_text = AsyncMock()
    return cb


def _allowed():
    return (
        patch(f"{MOD}.has_perm", MagicMock(return_value=True)),
        patch(f"{MOD}.chat_allows_printer", MagicMock(return_value=True)),
        patch(f"{MOD}.get_language", AsyncMock(return_value="en")),
    )


async def _rows(db_session, archive):
    return {
        r.name_key: r
        for r in (
            await db_session.execute(select(PrintArchivePart).where(PrintArchivePart.archive_id == archive.id))
        ).scalars()
    }


async def test_a_tap_writes_one_part_and_asks_the_next(patched_session, db_session):
    from backend.app.services.telegram_handlers.defects import cb_defects_set

    archive = await _print(db_session, {"lid": 2, "base": 4})
    rows = await _rows(db_session, archive)
    # Ids into plain ints BEFORE expire_all() below — an expired attribute read
    # outside an ``await`` is a MissingGreenlet, not a useful failure.
    archive_id, lid_id = archive.id, rows["lid"].id
    cb = _callback(f"defects:{archive_id}:{lid_id}:1")
    state = _FakeState()
    await state.set_state("other")
    await state.update_data(archive_id=archive_id, row_id=lid_id, maximum=2)
    p1, p2, p3 = _allowed()
    with p1, p2, p3:
        await cb_defects_set(cb, state)

    db_session.expire_all()
    assert (await db_session.get(PrintArchivePart, lid_id)).defective == 1
    assert (await db_session.get(PrintArchive, archive_id)).defective_count == 1
    # The next part's prompt went out as a NEW message; the answered one was edited to say what was recorded.
    cb.message.edit_text.assert_awaited()
    cb.message.answer.assert_awaited()
    assert "base" in cb.message.answer.await_args.args[0]
    # The tap IS the answer: an «other…» left waiting must not survive it and
    # swallow the operator's next ordinary message.
    assert state.state is None and state.clears == 1


async def test_the_last_tap_says_the_total(patched_session, db_session):
    from backend.app.services.telegram_handlers.defects import cb_defects_set

    archive = await _print(db_session, {"lid": 2})
    rows = await _rows(db_session, archive)
    cb = _callback(f"defects:{archive.id}:{rows['lid'].id}:2")
    p1, p2, p3 = _allowed()
    with p1, p2, p3:
        await cb_defects_set(cb, _FakeState())

    cb.message.answer.assert_not_awaited()
    assert "2" in cb.message.edit_text.await_args.args[0]


async def test_no_defects_done_zeroes_this_and_the_rest(patched_session, db_session):
    from backend.app.services.telegram_handlers.defects import cb_defects_none

    archive = await _print(db_session, {"lid": 2, "base": 4, "cap": 1})
    rows = await _rows(db_session, archive)
    for row in rows.values():
        row.defective = 1
    await db_session.commit()
    ids = {name: row.id for name, row in rows.items()}
    cb = _callback(f"defects_none:{archive.id}:{ids['base']}")
    state = _FakeState()
    await state.set_state("other")
    p1, p2, p3 = _allowed()
    with p1, p2, p3:
        await cb_defects_none(cb, state)

    db_session.expire_all()
    assert {k: (await db_session.get(PrintArchivePart, row_id)).defective for k, row_id in ids.items()} == {
        "lid": 1,
        "base": 0,
        "cap": 0,
    }
    assert state.state is None and state.clears == 1, "«done» ends the prompt, waiting state included"


async def test_a_flat_print_is_one_question(patched_session, db_session):
    from backend.app.services.telegram_handlers.defects import cb_defects_set

    archive = await _print(db_session, None, quantity=3)
    archive_id = archive.id
    cb = _callback(f"defects:{archive_id}:0:2")
    p1, p2, p3 = _allowed()
    with p1, p2, p3:
        await cb_defects_set(cb, _FakeState())

    db_session.expire_all()
    assert (await db_session.get(PrintArchive, archive_id)).defective_count == 2


def _message(text: str) -> MagicMock:
    message = MagicMock()
    message.text = text
    message.answer = AsyncMock()
    return message


async def test_other_takes_a_typed_number(patched_session, db_session):
    from backend.app.services.telegram_handlers.defects import cb_defects_other, msg_defects_count

    archive = await _print(db_session, {"lid": 4})
    rows = await _rows(db_session, archive)
    archive_id, lid_id = archive.id, rows["lid"].id
    state = _FakeState()
    cb = _callback(f"defects_other:{archive_id}:{lid_id}")
    p1, p2, p3 = _allowed()
    with p1, p2, p3:
        await cb_defects_other(cb, state)
    assert state.state is not None
    assert state.data == {"archive_id": archive_id, "row_id": lid_id, "maximum": 4}

    message = _message("3")
    p1, p2, p3 = _allowed()
    with p1, p2, p3:
        await msg_defects_count(message, state)

    db_session.expire_all()
    assert (await db_session.get(PrintArchivePart, lid_id)).defective == 3
    assert state.clears == 1


async def test_a_number_above_the_row_is_refused_and_the_question_stays(patched_session, db_session):
    """The prompt promised "0 to {max}" — an over-count is a typo, not a clamp."""
    from backend.app.services.telegram_handlers.defects import msg_defects_count

    archive = await _print(db_session, {"lid": 2})
    rows = await _rows(db_session, archive)
    lid_id = rows["lid"].id
    state = _FakeState()
    await state.set_state("waiting")
    await state.update_data(archive_id=archive.id, row_id=lid_id, maximum=2)

    message = _message("7")
    p1, p2, p3 = _allowed()
    with p1, p2, p3:
        await msg_defects_count(message, state)

    db_session.expire_all()
    assert (await db_session.get(PrintArchivePart, lid_id)).defective == 0, "nothing was written"
    assert message.answer.await_args.args[0] == escape_md(t("en", NS, "defects.invalid", max=2))
    assert state.clears == 0, "the question stays open — the next message is still this row's answer"


async def test_a_number_that_is_not_a_plain_count_is_refused(patched_session, db_session):
    """``isdigit`` says "²" is a digit; ``int()`` disagrees, loudly."""
    from backend.app.services.telegram_handlers.defects import msg_defects_count

    archive = await _print(db_session, {"lid": 2})
    rows = await _rows(db_session, archive)
    state = _FakeState()
    await state.set_state("waiting")
    await state.update_data(archive_id=archive.id, row_id=rows["lid"].id, maximum=2)

    message = _message("²")
    p1, p2, p3 = _allowed()
    with p1, p2, p3:
        await msg_defects_count(message, state)

    message.answer.assert_awaited()
    assert state.clears == 0


async def test_the_fsm_path_says_why_it_refused(patched_session, db_session):
    """A silent no-op leaves the operator waiting for a confirmation that never comes."""
    from backend.app.services.telegram_handlers.defects import msg_defects_count

    archive = await _print(db_session, {"lid": 2})
    rows = await _rows(db_session, archive)
    lid_id = rows["lid"].id
    state = _FakeState()
    await state.set_state("waiting")
    await state.update_data(archive_id=archive.id, row_id=lid_id, maximum=2)

    message = _message("1")
    with (
        patch(f"{MOD}.has_perm", MagicMock(return_value=False)),
        patch(f"{MOD}.chat_allows_printer", MagicMock(return_value=True)),
        patch(f"{MOD}.get_language", AsyncMock(return_value="en")),
    ):
        await msg_defects_count(message, state)

    db_session.expire_all()
    assert (await db_session.get(PrintArchivePart, lid_id)).defective == 0
    assert message.answer.await_args.args[0] == escape_md(t("en", NS, "auth.no_permission"))
    assert state.clears == 1


async def test_the_ledger_credits_the_chats_user(patched_session, db_session):
    """``actor_id`` travels to the writer, or the shelf correction lands unsigned."""
    from backend.app.services import archive_defects
    from backend.app.services.telegram_handlers.defects import cb_defects_set

    archive = await _print(db_session, {"lid": 2})
    rows = await _rows(db_session, archive)
    cb = _callback(f"defects:{archive.id}:{rows['lid'].id}:1")
    spy = AsyncMock(side_effect=archive_defects.record_defects)
    chat = MagicMock()
    chat.user_id = 7
    p1, p2, p3 = _allowed()
    with p1, p2, p3, patch(f"{MOD}.record_defects", spy):
        await cb_defects_set(cb, _FakeState(), chat)

    assert spy.await_args.kwargs["actor_id"] == 7


async def test_permission_and_scope_refuse(patched_session, db_session):
    from backend.app.services.telegram_handlers.defects import cb_defects_set

    archive = await _print(db_session, {"lid": 2})
    rows = await _rows(db_session, archive)
    lid_id = rows["lid"].id
    cb = _callback(f"defects:{archive.id}:{lid_id}:1")
    with (
        patch(f"{MOD}.has_perm", MagicMock(return_value=False)),
        patch(f"{MOD}.get_language", AsyncMock(return_value="en")),
    ):
        await cb_defects_set(cb, _FakeState())
    db_session.expire_all()
    assert (await db_session.get(PrintArchivePart, lid_id)).defective == 0
    assert cb.answer.await_args.args[0] == t("en", NS, "auth.no_permission")


async def test_a_gone_print_answers_gone(patched_session):
    from backend.app.services.telegram_handlers.defects import cb_defects_set

    cb = _callback("defects:999999:0:1")
    p1, p2, p3 = _allowed()
    with p1, p2, p3:
        await cb_defects_set(cb, _FakeState())
    assert cb.answer.await_args.args[0] == t("en", NS, "defects.gone")


def _button_data(markup):
    return [b.callback_data for row in (markup.inline_keyboard if markup else []) for b in row]


async def _ops_chat(db_session) -> None:
    from backend.app.models.group import Group
    from backend.app.models.telegram_chat import TelegramChat

    group = Group(name="Ops", permissions=["printers:clear_plate"])
    db_session.add(group)
    await db_session.flush()
    db_session.add(TelegramChat(chat_id=4242, group_id=group.id, is_active=True))
    await db_session.commit()


def _keyboard_env():
    return (
        patch("backend.app.i18n.get_language", AsyncMock(return_value="en")),
        patch(
            "backend.app.services.printer_manager.printer_manager.is_awaiting_plate_clear",
            MagicMock(return_value=False),
        ),
    )


async def test_the_completion_message_offers_the_defects_button(patched_session, db_session):
    """The button is `print_complete`'s alone — a failed plate has nothing good to grade."""
    from backend.app.services.notification_service import notification_service

    archive = await _print(db_session, {"lid": 2})
    await _ops_chat(db_session)
    extra = {"archive_id": archive.id}

    p1, p2 = _keyboard_env()
    with p1, p2:
        complete = await notification_service._build_telegram_actions("print_complete", 5, 4242, extra)
        failed = await notification_service._build_telegram_actions("print_failed", 5, 4242, extra)

    assert f"action:defects:{archive.id}" in _button_data(complete)
    assert not any(data.startswith("action:defects:") for data in _button_data(failed))


async def test_the_button_names_the_archive_it_was_given_and_no_other(patched_session, db_session):
    """The id comes from the notification, never from a query.

    A printer-wide "newest completed archive" lookup got the PREVIOUS plate
    whenever the announced print could not be identified — and every tap then
    wrote defects, and a free-stock ledger correction, against the wrong print.
    """
    from backend.app.services.notification_service import notification_service

    older = await _print(db_session, {"lid": 2})
    newer = await _print(db_session, {"base": 1})
    await _ops_chat(db_session)
    older_id, newer_id = older.id, newer.id

    p1, p2 = _keyboard_env()
    with p1, p2:
        markup = await notification_service._build_telegram_actions("print_complete", 5, 4242, {"archive_id": older_id})

    assert f"action:defects:{older_id}" in _button_data(markup)
    assert f"action:defects:{newer_id}" not in _button_data(markup), "not the newest — the one announced"


async def test_a_completion_with_no_archive_gets_no_defects_button(patched_session, db_session):
    """``main.py``'s no-archive path ("Could not find archive for print complete")
    still sends the completion notification. A print we could not attach to an
    archive must not be offered a grading button at all — there is nothing to
    grade it against, and the old guess reached for the previous plate."""
    from backend.app.services.notification_service import notification_service

    await _print(db_session, {"lid": 2})
    await _ops_chat(db_session)

    p1, p2 = _keyboard_env()
    with p1, p2:
        no_extra = await notification_service._build_telegram_actions("print_complete", 5, 4242)
        empty_extra = await notification_service._build_telegram_actions("print_complete", 5, 4242, {})

    assert not any(data.startswith("action:defects:") for data in _button_data(no_extra))
    assert not any(data.startswith("action:defects:") for data in _button_data(empty_extra))


async def test_the_done_message_says_the_shelf_could_not_follow(patched_session, db_session):
    """A refusal is reported where it happens.

    The spec's rule is "callers log the refused parts and keep the defects; the
    shelf is corrected by hand" — and half of it was missing: nothing told the
    operator there was a hand correction to make. The count is read off the last
    write, which is the standing list: ``adjust_unfiled_print`` recomputes the
    whole archive against the shelf on every tap, so a part it refused once is
    refused again until somebody fixes it.
    """
    from backend.app.models.product import Product, ProductPart, ProductPlate
    from backend.app.services.part_stock import credit_unfiled_print, move
    from backend.app.services.telegram_handlers.defects import cb_defects_set

    product = Product(name="Widget")
    db_session.add(product)
    await db_session.flush()
    db_session.add_all(
        [
            ProductPart(product_id=product.id, kind="printed", name="lid", name_key="lid", qty_per_unit=1),
            ProductPlate(product_id=product.id, library_file_id=77, plate_index=0),
        ]
    )
    archive = PrintArchive(
        printer_id=5,
        filename="p.3mf",
        print_name="Plate",
        file_path="x/p.3mf",
        file_size=1,
        status="completed",
        library_file_id=77,
        plate_index=1,
        quantity=4,
        completed_at=datetime.now(timezone.utc),
    )
    db_session.add(archive)
    await db_session.flush()
    row = PrintArchivePart(archive_id=archive.id, name="lid", name_key="lid", quantity=4)
    db_session.add(row)
    await db_session.flush()
    await credit_unfiled_print(db_session, archive)
    # Sold before anybody graded the plate: the shelf has nothing left to give back.
    lid = (await db_session.execute(select(ProductPart).where(ProductPart.name_key == "lid"))).scalar_one()
    await move(db_session, part_id=lid.id, delta=-4, reason="manual", note="sold")
    await db_session.commit()
    archive_id, row_id = archive.id, row.id

    cb = _callback(f"defects:{archive_id}:{row_id}:1")
    p1, p2, p3 = _allowed()
    with p1, p2, p3:
        await cb_defects_set(cb, _FakeState())

    said = cb.message.edit_text.await_args.args[0]
    assert escape_md(t("en", NS, "defects.ledger_refused", count=1)) in said
    db_session.expire_all()
    assert (await db_session.get(PrintArchive, archive_id)).defective_count == 1, "the defects are kept regardless"


async def test_a_done_message_with_nothing_refused_carries_no_extra_line(patched_session, db_session):
    from backend.app.services.telegram_handlers.defects import cb_defects_set

    archive = await _print(db_session, {"lid": 2})
    rows = await _rows(db_session, archive)
    cb = _callback(f"defects:{archive.id}:{rows['lid'].id}:1")
    p1, p2, p3 = _allowed()
    with p1, p2, p3:
        await cb_defects_set(cb, _FakeState())

    said = cb.message.edit_text.await_args.args[0]
    assert "\n" not in said
    assert cb.message.edit_text.await_count == 1, "one edit for the last part, not a confirmation then a total"
