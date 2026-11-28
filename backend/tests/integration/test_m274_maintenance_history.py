"""m274: the maintenance history's links become SET NULL and its rows get the names they had (N09).

The fill writes only what is still empty (R1-01): a rerun changes nothing, and a printer
renamed later keeps its old name in the history. A performer is named only if it existed
at the reset (R1-02): a user created after it — on SQLite possibly with a deleted user's
number — is not. The same checks run on PostgreSQL (``test_m274_maintenance_history_postgres``).
"""

from __future__ import annotations

import pytest
from sqlalchemy import text

from backend.app.migrations import m274_maintenance_history_outlives as m274
from backend.app.models.maintenance import MaintenanceType, PrinterMaintenance
from backend.app.models.notification import NotificationProvider
from backend.app.models.telegram_chat import TelegramChat
from backend.app.models.user import User

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]

_OLD = {
    "sqlite": (
        "CREATE TABLE maintenance_history (id INTEGER NOT NULL PRIMARY KEY, "
        "printer_maintenance_id INTEGER NOT NULL REFERENCES printer_maintenance(id) ON DELETE CASCADE, "
        "performed_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP, hours_at_maintenance FLOAT NOT NULL, notes TEXT, "
        "performed_by_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL, "
        "performed_by_chat_id INTEGER REFERENCES telegram_chats(id) ON DELETE SET NULL)"
    ),
    "postgresql": (
        "CREATE TABLE maintenance_history (id SERIAL PRIMARY KEY, "
        "printer_maintenance_id INTEGER NOT NULL REFERENCES printer_maintenance(id) ON DELETE CASCADE, "
        "performed_at TIMESTAMP NOT NULL DEFAULT now(), hours_at_maintenance FLOAT NOT NULL, notes TEXT, "
        "performed_by_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL, "
        "performed_by_chat_id INTEGER REFERENCES telegram_chats(id) ON DELETE SET NULL)"
    ),
}


async def _link_rule(conn) -> str | None:
    if conn.dialect.name == "sqlite":
        rows = (await conn.execute(text("PRAGMA foreign_key_list(maintenance_history)"))).fetchall()
        return {r[3]: r[6] for r in rows}.get("printer_maintenance_id")
    rule = (
        await conn.execute(
            text(
                "SELECT con.confdeltype::text FROM pg_constraint con JOIN pg_attribute att "
                "ON att.attrelid = con.conrelid AND att.attnum = ANY(con.conkey) "
                "WHERE con.conrelid = 'maintenance_history'::regclass AND con.contype = 'f' "
                "AND att.attname = 'printer_maintenance_id'"
            )
        )
    ).scalar()
    return {"n": "SET NULL", "c": "CASCADE"}.get(rule, rule)


async def assert_m274_fills_once(engine, db, printer_factory, *, with_fk: bool = True) -> None:
    """The shared scenario: an old-form table with two resets, migrated, then migrated again."""
    printer = await printer_factory(name="Farm-7")
    printer_id = printer.id
    kind = MaintenanceType(name="Clean Nozzle", is_system=True, default_interval_hours=100.0, printer_models='["*"]')
    db.add(kind)
    await db.commit()
    item = PrinterMaintenance(printer_id=printer_id, maintenance_type_id=kind.id)
    user = User(username="m274-operator")
    provider = NotificationProvider(name="bot", provider_type="telegram", enabled=True, config='{"bot_token": "1:x"}')
    db.add_all([item, user, provider])
    await db.flush()
    chat = TelegramChat(chat_id=4242, provider_id=provider.id, is_active=True, label="Farm chat")
    db.add(chat)
    await db.commit()
    item_id, user_id, username, chat_id = item.id, user.id, user.username, chat.id

    async with engine.begin() as conn:
        dialect = conn.dialect.name
        await conn.execute(text("DROP TABLE maintenance_history"))
        ddl = _OLD[dialect]
        if not with_fk:
            ddl = ddl.replace(" REFERENCES printer_maintenance(id) ON DELETE CASCADE", "")
        await conn.execute(text(ddl))
        await conn.execute(
            text(
                "INSERT INTO maintenance_history (id, printer_maintenance_id, performed_at, hours_at_maintenance, "
                "performed_by_user_id) VALUES "
                # after the user was created: named
                "(1, :item, CURRENT_TIMESTAMP, 10, :user), "
                # before it: a later user with the number of a deleted one, not named
                "(2, :item, '2000-01-01 00:00:00', 5, :user)"
            ),
            {"item": item_id, "user": user_id},
        )
        await conn.execute(
            text(
                "INSERT INTO maintenance_history (id, printer_maintenance_id, performed_at, hours_at_maintenance, "
                "performed_by_chat_id) VALUES "
                # the same rule for a Telegram chat: named after it was created, not before
                "(3, :item, CURRENT_TIMESTAMP, 11, :chat), (4, :item, '2000-01-01 00:00:00', 6, :chat)"
            ),
            {"item": item_id, "chat": chat_id},
        )

        await m274.upgrade(conn)

        rows = (
            await conn.execute(
                text(
                    "SELECT id, printer_id, printer_name, maintenance_type_name, performed_by_name "
                    "FROM maintenance_history ORDER BY id"
                )
            )
        ).fetchall()
        assert [tuple(r) for r in rows] == [
            (1, printer_id, "Farm-7", "Clean Nozzle", username),
            (2, printer_id, "Farm-7", "Clean Nozzle", None),
            (3, printer_id, "Farm-7", "Clean Nozzle", "Farm chat"),
            (4, printer_id, "Farm-7", "Clean Nozzle", None),
        ]
        if with_fk or dialect == "sqlite":
            assert await _link_rule(conn) == "SET NULL"
        nullable = (
            (
                await conn.execute(
                    text(
                        "SELECT is_nullable FROM information_schema.columns WHERE table_name = 'maintenance_history' "
                        "AND column_name = 'printer_maintenance_id'"
                    )
                )
            ).scalar()
            if dialect == "postgresql"
            else not next(
                r[3]
                for r in (await conn.execute(text("PRAGMA table_info(maintenance_history)")))
                if r[1] == "printer_maintenance_id"
            )
        )
        assert nullable in (True, "YES")

        # A rename after the reset, then a second run (DEBUG replay): nothing changes.
        await conn.execute(text("UPDATE printers SET name = 'Renamed' WHERE id = :p"), {"p": printer_id})
        await m274.replay(conn)
        again = (
            await conn.execute(text("SELECT printer_name, performed_by_name FROM maintenance_history ORDER BY id"))
        ).fetchall()
        assert [tuple(r) for r in again] == [
            ("Farm-7", username),
            ("Farm-7", None),
            ("Farm-7", "Farm chat"),
            ("Farm-7", None),
        ]


async def test_m274_on_sqlite(test_engine, db_session, printer_factory):
    await assert_m274_fills_once(test_engine, db_session, printer_factory)


async def test_m274_on_a_fresh_install_changes_nothing(test_engine):
    """A table built from today's model is already in the new form."""
    async with test_engine.begin() as conn:
        before = (await conn.execute(text("SELECT sql FROM sqlite_master WHERE name = 'maintenance_history'"))).scalar()
        await m274.upgrade(conn)
        after = (await conn.execute(text("SELECT sql FROM sqlite_master WHERE name = 'maintenance_history'"))).scalar()
    assert before == after
