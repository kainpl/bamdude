"""Independent real SQLite connections, not StaticPool transaction overlap."""

import asyncio
from datetime import datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import create_async_engine

from backend.app.core.credential_config import initialize_credential_config_anchor
from backend.app.core.database import Base, import_all_models
from backend.app.models.git_backup import GitBackupConfig, GitBackupLog
from backend.app.models.user_notification import UserNotification
from backend.app.services import git_backup
from backend.tests.integration.test_g01_e_backup_approval import backup  # noqa: F401


@pytest.fixture
async def test_engine(tmp_path):
    import_all_models()
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'approval.sqlite'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await initialize_credential_config_anchor(conn)
    yield engine
    await engine.dispose()


async def exercise_concurrent_due(backup):
    service, factory, (cid, _, _) = backup
    async with factory() as db:
        (await db.get(GitBackupConfig, cid)).approved_by_identity = None
        await db.commit()
    other = git_backup.GitBackupService()
    results = await asyncio.gather(service.run_backup(cid, "scheduled"), other.run_backup(cid, "scheduled"))
    assert sum(r.get("reason_code") == "approval_required" for r in results) == 1
    async with factory() as db:
        assert await db.scalar(select(func.count()).select_from(GitBackupLog)) == 1
        assert await db.scalar(select(func.count()).select_from(UserNotification)) == 1
        config = await db.get(GitBackupConfig, cid)
        episode = config.admission_episode_id
        config.next_scheduled_run = datetime.utcnow() - timedelta(seconds=1)
        await db.commit()
    # The shared ZIP/Git guard refuses the overlapping call before admission.
    # Retry that occurrence after the owner exits: it still gets its own
    # attempt, within the same durable notification episode.
    results = await asyncio.gather(service.run_backup(cid, "manual"), other.run_backup(cid, "scheduled"))
    assert sum(r.get("message") == "A backup is already running" for r in results) == 1
    async with factory() as db:
        assert await db.scalar(select(func.count()).select_from(GitBackupLog)) == 2
    index = next(i for i, r in enumerate(results) if r.get("log_id") is None)
    retried = await (service if index == 0 else other).run_backup(cid, "manual" if index == 0 else "scheduled")
    assert retried["reason_code"] == "approval_required"
    async with factory() as db:
        assert await db.scalar(select(func.count()).select_from(GitBackupLog)) == 3
        assert await db.scalar(select(func.count()).select_from(UserNotification)) == 1
        assert (await db.get(GitBackupConfig, cid)).admission_episode_id == episode


async def test_concurrent_due_and_manual_on_separate_sqlite_connections(backup):
    await exercise_concurrent_due(backup)


async def exercise_admission_races_below_guard(backup):
    """Pin DB claim/episode serialisation independently of the public guard."""
    service, factory, (cid, _, _) = backup
    async with factory() as db:
        (await db.get(GitBackupConfig, cid)).approved_by_identity = None
        await db.commit()
    other = git_backup.GitBackupService()
    # Enter two admitted bodies directly: neither can be refused by the
    # process-wide ZIP/Git guard before it reaches the database claim.
    results = await asyncio.gather(
        service._run_backup_owned(cid, "scheduled"), other._run_backup_owned(cid, "scheduled")
    )
    assert sum(r.get("reason_code") == "approval_required" for r in results) == 1
    assert sum(r.get("log_id") is not None for r in results) == 1
    async with factory() as db:
        assert await db.scalar(select(func.count()).select_from(GitBackupLog)) == 1
        assert await db.scalar(select(func.count()).select_from(UserNotification)) == 1
        config = await db.get(GitBackupConfig, cid)
        episode = config.admission_episode_id
        assert episode is not None
        config.next_scheduled_run = datetime.utcnow() - timedelta(seconds=1)
        await db.commit()
    # Both calls must enter admission, producing distinct attempts but the
    # same durable episode and no duplicate notification.
    results = await asyncio.gather(service._run_backup_owned(cid, "manual"), other._run_backup_owned(cid, "scheduled"))
    assert all(r.get("reason_code") == "approval_required" for r in results)
    assert len({r["log_id"] for r in results}) == 2
    async with factory() as db:
        assert await db.scalar(select(func.count()).select_from(GitBackupLog)) == 3
        assert await db.scalar(select(func.count()).select_from(UserNotification)) == 1
        assert (await db.get(GitBackupConfig, cid)).admission_episode_id == episode
    service._collect_backup_data.assert_not_awaited()
    service._push_to_provider.assert_not_awaited()


async def test_admission_races_below_guard_on_separate_sqlite_connections(backup):
    await exercise_admission_races_below_guard(backup)
