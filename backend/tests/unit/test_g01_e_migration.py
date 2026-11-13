"""Frozen E migration upgrades a legacy database without guessing approval."""

from datetime import datetime

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine

from backend.app.migrations import m224_git_backup_approval as migration


async def exercise_legacy_migration(engine):
    async with engine.begin() as conn:
        await conn.execute(text("DROP TABLE IF EXISTS git_backup_logs"))
        await conn.execute(text("DROP TABLE IF EXISTS git_backup_config"))
        await conn.execute(
            text(
                "CREATE TABLE git_backup_config (id INTEGER PRIMARY KEY, last_backup_status VARCHAR(20), last_backup_at TIMESTAMP)"
            )
        )
        await conn.execute(
            text(
                "CREATE TABLE git_backup_logs (id INTEGER PRIMARY KEY, config_id INTEGER, status VARCHAR(20), completed_at TIMESTAMP, trigger VARCHAR(20))"
            )
        )
        await conn.execute(
            text("INSERT INTO git_backup_config VALUES (1, 'success', :t), (2, 'failed', :t), (3, 'failed', :t)"),
            {"t": datetime(2026, 11, 1, 12)},
        )
        await conn.execute(
            text(
                "INSERT INTO git_backup_logs VALUES (10, 2, 'success', :t, 'manual'), (11, 2, 'failed', :later, 'scheduled'), "
                "(12, 2, 'success', :later, 'restore'), (13, 3, 'success', :later, 'restore')"
            ),
            {"t": datetime(2026, 10, 31, 11), "later": datetime(2026, 11, 1, 12)},
        )
        await migration.upgrade(conn)
        await migration.upgrade(conn)
        rows = (
            await conn.execute(
                text(
                    "SELECT approved_by_identity, admission_episode_id, last_successful_backup_at FROM git_backup_config ORDER BY id"
                )
            )
        ).all()
        assert rows[0][0:2] == rows[1][0:2] == (None, None)
        assert str(rows[0][2]).startswith("2026-11-01 12:00:00")
        assert str(rows[1][2]).startswith("2026-10-31 11:00:00")
        assert rows[2][2] is None
        await conn.execute(
            text("INSERT INTO git_backup_logs (id, config_id, status, scheduled_for) VALUES (1, 1, 'blocked', :t)"),
            {"t": datetime(2026, 11, 13)},
        )
        async with conn.begin_nested() as savepoint:
            try:
                await conn.execute(
                    text(
                        "INSERT INTO git_backup_logs (id, config_id, status, scheduled_for) VALUES (2, 1, 'blocked', :t)"
                    ),
                    {"t": datetime(2026, 11, 13)},
                )
            except IntegrityError:
                await savepoint.rollback()
            else:
                raise AssertionError("A repeated due occurrence must be rejected")


async def test_legacy_sqlite_approval_migration(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'legacy.sqlite'}")
    try:
        await exercise_legacy_migration(engine)
    finally:
        await engine.dispose()


async def test_fresh_model_and_migration_share_one_occurrence_index(tmp_path):
    from sqlalchemy import inspect

    from backend.app.core.database import Base
    from backend.app.models.git_backup import GitBackupConfig, GitBackupLog

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'fresh.sqlite'}")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(
                lambda c: Base.metadata.create_all(c, tables=[GitBackupConfig.__table__, GitBackupLog.__table__])
            )
            await migration.upgrade(conn)
            indexes = await conn.run_sync(lambda c: inspect(c).get_indexes("git_backup_logs"))
            assert [(i["name"], bool(i["unique"])) for i in indexes] == [("uq_git_backup_occurrence", True)]
            assert await conn.run_sync(lambda c: inspect(c).get_unique_constraints("git_backup_logs")) == []
    finally:
        await engine.dispose()
