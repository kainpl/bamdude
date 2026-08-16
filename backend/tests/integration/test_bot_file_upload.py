"""The bot accepts a sent file, puts it in the library, and offers to print it.

Requested as "кинув боту 3mf, обрав принтер, надрукував". Everything after the
first step already worked; the bot accepted no documents at all.

⚠️ The checks that matter here are the ones that happen **before** anything is
downloaded — permission and size — because getting those wrong costs the
operator a wait and then a silence.
"""

from __future__ import annotations

import zipfile
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.app.models.library import LibraryFile, LibraryFolder
from backend.app.services.telegram_handlers import library_upload_scene as scene

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


def _3mf_bytes(*, sliced: bool) -> bytes:
    buf = BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("Metadata/slice_info.config", "<config/>")
        zf.writestr("Metadata/plate_1.gcode" if sliced else "Metadata/plate_1.png", "x")
    return buf.getvalue()


class _Chat:
    """A telegram chat with (or without) the upload permission."""

    def __init__(self, allowed: bool):
        self._allowed = allowed

    def has_permission(self, perm: str) -> bool:
        return self._allowed


def _message(filename: str, size: int) -> MagicMock:
    m = MagicMock()
    m.document = SimpleNamespace(file_id="F1", file_name=filename, file_size=size)
    m.answer = AsyncMock()
    return m


def _callback(data: str, content: bytes | None = None) -> MagicMock:
    cb = MagicMock()
    cb.data = data
    cb.answer = AsyncMock()
    cb.message = MagicMock()
    cb.message.edit_text = AsyncMock()

    async def _download(_file_id, destination):
        if content is None:
            raise RuntimeError("file is too big or unavailable")
        destination.write(content)

    cb.bot = MagicMock()
    cb.bot.download = AsyncMock(side_effect=_download)
    return cb


class _State:
    def __init__(self, data=None):
        self._data = dict(data or {})
        self.cleared = False

    async def set_state(self, _s):
        pass

    async def update_data(self, **kw):
        self._data.update(kw)

    async def get_data(self):
        return dict(self._data)

    async def clear(self):
        self.cleared = True


@pytest.fixture(autouse=True)
def _point_at_the_test_db(test_engine, tmp_path, monkeypatch):
    from backend.app.core.config import settings as app_settings

    # The library lives under ``archive_dir/library``; both are derived, so
    # redirecting these two is enough to keep the test off the real tree.
    monkeypatch.setattr(app_settings, "base_dir", tmp_path)
    monkeypatch.setattr(app_settings, "archive_dir", tmp_path / "archive")
    (tmp_path / "archive").mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(
        "backend.app.core.database.async_session",
        async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False),
    )


class TestWhatHappensBeforeTheDownload:
    async def test_a_chat_without_permission_is_refused(self, db_session):
        msg = _message("part.gcode.3mf", 1024)
        state = _State()

        await scene.on_document(msg, state, tg_chat=_Chat(False))

        msg.answer.assert_awaited_once()
        assert await db_session.scalar(select(func.count(LibraryFolder.id))) == 0, (
            "a refused upload still created the Telegram folder"
        )

    async def test_a_file_over_the_telegram_limit_is_refused_by_name_and_number(self, db_session):
        """⚠️ Refused from ``file_size`` on the message, before a byte moves.
        Telegram caps a bot download at 20 MB; discovering that at the end of a
        transfer would be a wait followed by a shrug."""
        msg = _message("huge.gcode.3mf", scene.MAX_DOWNLOAD_BYTES + 1)

        await scene.on_document(msg, _State(), tg_chat=_Chat(True))

        (said,), _ = msg.answer.call_args
        # Backslashes stripped: the text is MarkdownV2, so ``escape_md`` has
        # already turned the dots into ``\.`` on the way out.
        plain = said.replace("\\", "")
        assert "huge.gcode.3mf" in plain, plain
        assert "20 MB" in plain, f"the limit was not stated: {plain}"

    async def test_an_accepted_file_asks_where_to_put_it(self, db_session):
        msg = _message("part.gcode.3mf", 4096)
        state = _State()

        await scene.on_document(msg, state, tg_chat=_Chat(True))

        assert state._data["file_id"] == "F1"
        kb = msg.answer.call_args.kwargs["reply_markup"]
        labels = [b.text for row in kb.inline_keyboard for b in row]
        assert any(scene.DEFAULT_FOLDER_NAME in x for x in labels), labels

    async def test_the_default_folder_is_created_once(self, db_session):
        for _ in range(2):
            await scene.on_document(_message("a.gcode.3mf", 10), _State(), tg_chat=_Chat(True))

        db_session.expire_all()
        assert (
            await db_session.scalar(
                select(func.count(LibraryFolder.id)).where(LibraryFolder.name == scene.DEFAULT_FOLDER_NAME)
            )
            == 1
        )


class TestStoringIt:
    async def _store(self, db_session, filename, content):
        await scene.on_document(_message(filename, len(content)), _State(), tg_chat=_Chat(True))
        db_session.expire_all()
        folder_id = await db_session.scalar(
            select(LibraryFolder.id).where(LibraryFolder.name == scene.DEFAULT_FOLDER_NAME)
        )
        cb = _callback(f"libup:folder:{folder_id}", content)
        await scene.cb_folder_chosen(cb, _State({"file_id": "F1", "file_name": filename}), tg_chat=_Chat(True))
        db_session.expire_all()
        return cb

    async def test_a_sliced_file_is_saved_and_offered_for_printing(self, db_session):
        cb = await self._store(db_session, "ready.gcode.3mf", _3mf_bytes(sliced=True))

        row = await db_session.scalar(select(LibraryFile).where(LibraryFile.filename == "ready.gcode.3mf"))
        assert row is not None and row.file_size > 0
        assert "gcode" in (row.file_tags or [])

        kb = cb.message.edit_text.call_args.kwargs.get("reply_markup")
        assert kb is not None, "a printable file was saved without a Print button"
        assert kb.inline_keyboard[0][0].callback_data == f"lib:file:{row.id}", (
            "the Print button must hand off to the existing library picker"
        )

    async def test_a_model_is_saved_but_not_offered_for_printing(self, db_session):
        """⚠️ Decided by what is INSIDE the container. This one is named like a
        sliced file and holds no G-code; the printer would answer thirty
        seconds later that it cannot parse it."""
        cb = await self._store(db_session, "model.gcode.3mf", _3mf_bytes(sliced=False))

        row = await db_session.scalar(select(LibraryFile).where(LibraryFile.filename == "model.gcode.3mf"))
        assert row is not None, "the model should still be saved"
        assert "gcode" not in (row.file_tags or [])
        assert cb.message.edit_text.call_args.kwargs.get("reply_markup") is None

    async def test_a_failed_download_says_so_and_saves_nothing(self, db_session):
        await scene.on_document(_message("x.gcode.3mf", 10), _State(), tg_chat=_Chat(True))
        db_session.expire_all()
        fid = await db_session.scalar(select(LibraryFolder.id).where(LibraryFolder.name == scene.DEFAULT_FOLDER_NAME))
        cb = _callback(f"libup:folder:{fid}", content=None)

        await scene.cb_folder_chosen(cb, _State({"file_id": "F1", "file_name": "x.gcode.3mf"}), tg_chat=_Chat(True))

        db_session.expire_all()
        assert await db_session.scalar(select(func.count(LibraryFile.id))) == 0

    async def test_a_corrupt_file_is_rejected_in_the_uploaders_own_words(self, db_session):
        """⚠️ The HTTP validator's message is shown verbatim — it was written
        for a human, and a second vocabulary for the same rejection would only
        be a worse one."""
        await scene.on_document(_message("bad.gcode.3mf", 10), _State(), tg_chat=_Chat(True))
        db_session.expire_all()
        fid = await db_session.scalar(select(LibraryFolder.id).where(LibraryFolder.name == scene.DEFAULT_FOLDER_NAME))
        cb = _callback(f"libup:folder:{fid}", b"this is not a zip")

        await scene.cb_folder_chosen(cb, _State({"file_id": "F1", "file_name": "bad.gcode.3mf"}), tg_chat=_Chat(True))

        db_session.expire_all()
        assert await db_session.scalar(select(func.count(LibraryFile.id))) == 0
        (said,), _ = cb.message.edit_text.call_args
        assert said.strip(), "the rejection was silent"

    async def test_an_expired_wizard_is_not_reported_as_a_failure(self, db_session):
        cb = _callback("libup:folder:1", b"")
        state = _State({})  # restart wiped the FSM data

        await scene.cb_folder_chosen(cb, state, tg_chat=_Chat(True))

        cb.bot.download.assert_not_awaited()
        assert state.cleared
