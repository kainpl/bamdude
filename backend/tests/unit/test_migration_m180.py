"""m180 binds every Telegram chat to the bot that registered it — ``telegram_chats.provider_id``.

The chats of an install are the running bot's, and the running bot is the
oldest ENABLED telegram provider; with none enabled, the oldest telegram row
of any state (it was the bot when the chats wrote to it); with no telegram
provider at all the chats are orphans and go. Idempotent, like every head
migration under ``DEBUG=true``.
"""

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from backend.app.migrations import m180_telegram_chat_provider as m180

_PROVIDERS = (
    "CREATE TABLE notification_providers (id INTEGER PRIMARY KEY, name VARCHAR(100), "
    "provider_type VARCHAR(50), enabled BOOLEAN, config TEXT)"
)
# The shape every install carries before m180: chat_id unique through a
# SEPARATE index (create_all and the long-lived files alike), not inline.
_CHATS = "CREATE TABLE telegram_chats (id INTEGER PRIMARY KEY, chat_id BIGINT NOT NULL, label VARCHAR(100))"
_OLD_UNIQUE = "CREATE UNIQUE INDEX ix_telegram_chats_chat_id ON telegram_chats (chat_id)"


async def _schema(conn):
    await conn.exec_driver_sql(_PROVIDERS)
    await conn.exec_driver_sql(_CHATS)
    await conn.exec_driver_sql(_OLD_UNIQUE)


async def _indexes(conn) -> dict[str, str]:
    rows = await conn.execute(
        text("SELECT name, sql FROM sqlite_master WHERE type='index' AND tbl_name='telegram_chats'")
    )
    return {r[0]: (r[1] or "") for r in rows.fetchall()}


async def _provider(conn, pid: int, kind: str, enabled: bool) -> None:
    await conn.execute(
        text(
            "INSERT INTO notification_providers (id, name, provider_type, enabled, config) VALUES (:i, :n, :t, :e, '{}')"
        ),
        {"i": pid, "n": f"p{pid}", "t": kind, "e": 1 if enabled else 0},
    )


async def _chat(conn, chat_id: int) -> None:
    await conn.execute(text("INSERT INTO telegram_chats (chat_id, label) VALUES (:c, 'x')"), {"c": chat_id})


async def _bindings(conn) -> dict[int, int | None]:
    rows = await conn.execute(text("SELECT chat_id, provider_id FROM telegram_chats ORDER BY chat_id"))
    return {r[0]: r[1] for r in rows.fetchall()}


@pytest.mark.asyncio
async def test_chats_go_to_the_oldest_enabled_telegram_provider_and_the_run_is_idempotent(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/a.db")
    async with engine.begin() as conn:
        await conn.exec_driver_sql(_PROVIDERS)
        await conn.exec_driver_sql(_CHATS)
        await _provider(conn, 1, "telegram", enabled=False)  # older, but off
        await _provider(conn, 2, "ntfy", enabled=True)  # not a bot
        await _provider(conn, 3, "telegram", enabled=True)  # the bot
        await _provider(conn, 4, "telegram", enabled=True)  # younger
        await _chat(conn, 10)
        await _chat(conn, 11)

        await m180.upgrade(conn)
        assert await _bindings(conn) == {10: 3, 11: 3}

        # A second run (DEBUG=true re-runs the head) changes nothing and keeps the index.
        await m180.upgrade(conn)
        assert await _bindings(conn) == {10: 3, 11: 3}
        indexes = {
            r[0] for r in (await conn.execute(text("SELECT name FROM sqlite_master WHERE type='index'"))).fetchall()
        }
        assert "ix_telegram_chats_provider_id" in indexes
    await engine.dispose()


@pytest.mark.asyncio
async def test_with_no_enabled_bot_the_oldest_telegram_row_takes_the_chats(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/b.db")
    async with engine.begin() as conn:
        await conn.exec_driver_sql(_PROVIDERS)
        await conn.exec_driver_sql(_CHATS)
        await _provider(conn, 5, "telegram", enabled=False)
        await _provider(conn, 6, "telegram", enabled=False)
        await _chat(conn, 20)

        await m180.upgrade(conn)
        assert await _bindings(conn) == {20: 5}

        # The backfill itself reports what it did; run again it has nothing left to bind.
        assert await m180.backfill(conn) == (0, 0)
    await engine.dispose()


@pytest.mark.asyncio
async def test_chats_of_no_bot_at_all_are_removed(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/c.db")
    async with engine.begin() as conn:
        await conn.exec_driver_sql(_PROVIDERS)
        await conn.exec_driver_sql(_CHATS)
        await _provider(conn, 7, "ntfy", enabled=True)
        await _chat(conn, 30)
        await _chat(conn, 31)

        await m180.upgrade(conn)
        assert await _bindings(conn) == {}
    await engine.dispose()


@pytest.mark.asyncio
async def test_an_already_bound_chat_is_left_alone(tmp_path):
    """Only NULL bindings are filled — a chat re-bound by the middleware keeps its bot."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/d.db")
    async with engine.begin() as conn:
        await conn.exec_driver_sql(_PROVIDERS)
        await conn.exec_driver_sql(_CHATS)
        await _provider(conn, 1, "telegram", enabled=True)
        await _provider(conn, 2, "telegram", enabled=True)
        await m180.upgrade(conn)
        await conn.execute(text("INSERT INTO telegram_chats (chat_id, label, provider_id) VALUES (40, 'x', 2)"))
        await _chat(conn, 41)

        await m180.upgrade(conn)
        assert await _bindings(conn) == {40: 2, 41: 1}
    await engine.dispose()


@pytest.mark.asyncio
async def test_a_row_whose_enabled_is_null_never_beats_an_enabled_one(tmp_path):
    """``enabled`` is nullable; a NULL must sort behind a real True on both dialects.

    Spelled as ``ORDER BY enabled DESC`` it would come FIRST on PostgreSQL
    (NULLS FIRST under DESC) and last on SQLite — the CASE the backfill uses
    puts it in the ELSE branch on both.
    """
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/e.db")
    async with engine.begin() as conn:
        await conn.exec_driver_sql(_PROVIDERS)
        await conn.exec_driver_sql(_CHATS)
        await conn.execute(
            text(
                "INSERT INTO notification_providers (id, name, provider_type, enabled, config) "
                "VALUES (1, 'p1', 'telegram', NULL, '{}')"
            )
        )
        await _provider(conn, 2, "telegram", enabled=True)
        await _chat(conn, 50)

        await m180.upgrade(conn)
        assert await _bindings(conn) == {50: 2}
    await engine.dispose()


# ---------------------------------------------------------------------------
# A chat is (bot, chat_id); one token, one provider.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_unique_key_moves_from_chat_id_to_the_pair_and_survives_a_rerun(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/f.db")
    async with engine.begin() as conn:
        await _schema(conn)
        await _provider(conn, 1, "telegram", enabled=True)
        await _provider(conn, 2, "telegram", enabled=True)

        await m180.upgrade(conn)
        await m180.upgrade(conn)

        indexes = await _indexes(conn)
        assert "UNIQUE" in indexes["ix_telegram_chats_provider_chat"]
        assert "UNIQUE" not in indexes["ix_telegram_chats_chat_id"], "chat_id alone is no longer the key"

        insert = "INSERT INTO telegram_chats (chat_id, label, provider_id) VALUES (:c, 'x', :p)"
        await conn.execute(text(insert), {"c": 5, "p": 1})
        await conn.execute(text(insert), {"c": 5, "p": 2})  # the same person, another bot
        with pytest.raises(Exception, match="UNIQUE"):
            await conn.execute(text(insert), {"c": 5, "p": 1})
    await engine.dispose()


async def _provider_with_token(conn, pid: int, token, enabled: bool = True) -> None:
    config = token if isinstance(token, str) and token.startswith("{") else f'{{"bot_token": "{token}"}}'
    await conn.execute(
        text(
            "INSERT INTO notification_providers (id, name, provider_type, enabled, config) VALUES (:i, :n, 'telegram', :e, :c)"
        ),
        {"i": pid, "n": f"p{pid}", "e": 1 if enabled else 0, "c": config},
    )


@pytest.mark.asyncio
async def test_a_duplicated_token_stays_with_the_oldest_row_and_the_younger_one_is_switched_off(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/g.db")
    async with engine.begin() as conn:
        await _schema(conn)
        await _provider_with_token(conn, 1, "111:AAsame")
        await _provider_with_token(conn, 2, "222:AAother")
        await _provider_with_token(conn, 3, "111:AAsame")  # the duplicate
        # The column arrives with the migration; add it here and seed the
        # bindings a duplicate row would hold (a chat added from its card).
        await conn.execute(text("ALTER TABLE telegram_chats ADD COLUMN provider_id INTEGER"))
        await conn.execute(text("INSERT INTO telegram_chats (chat_id, label, provider_id) VALUES (10, 'x', 3)"))
        await conn.execute(text("INSERT INTO telegram_chats (chat_id, label, provider_id) VALUES (11, 'x', 2)"))

        outcome = await m180.dedupe_tokens(conn)

        assert outcome == [(1, 3, 1)], "keeper 1, duplicate 3 switched off, one chat moved"
        enabled = {
            r[0]: r[1] for r in (await conn.execute(text("SELECT id, enabled FROM notification_providers"))).fetchall()
        }
        assert enabled == {1: 1, 2: 1, 3: 0}
        assert await _bindings(conn) == {10: 1, 11: 2}

        # Run again: nothing left to do.
        assert await m180.dedupe_tokens(conn) == []
    await engine.dispose()


@pytest.mark.asyncio
async def test_a_config_that_is_not_json_or_has_no_token_takes_no_part_in_the_dedupe(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/h.db")
    async with engine.begin() as conn:
        await _schema(conn)
        await _provider_with_token(conn, 1, "111:AAsame")
        await _provider_with_token(conn, 2, "{not json")
        await _provider_with_token(conn, 3, '{"chat_id": "42"}')
        await _provider_with_token(conn, 4, "111:AAsame")
        await conn.execute(text("ALTER TABLE telegram_chats ADD COLUMN provider_id INTEGER"))

        assert await m180.dedupe_tokens(conn) == [(1, 4, 0)]
    await engine.dispose()
