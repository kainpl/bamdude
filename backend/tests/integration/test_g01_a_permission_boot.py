"""A recorded permission migration cannot restore an owner's later revocation."""

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from backend.app import migrations
from backend.app.core.config import settings
from backend.app.models.group import Group

pytestmark = pytest.mark.integration


async def test_custom_permission_stays_revoked_across_two_upgrade_checks(test_engine, db_session, monkeypatch):
    monkeypatch.setattr(settings, "debug", False)
    custom = Group(name="G01 custom floor", is_system=False, permissions=["printers:control"])
    db_session.add(custom)
    await db_session.commit()
    await migrations._ensure_migrations_table(test_engine)
    # The real baseline is pending, everything else is already applied. Run
    # its existing one-time grant through the real production migration runner.
    for migration in migrations._discover_migrations():
        if migration["version"] != 1:
            await migrations._record_migration(test_engine, migration["version"], migration["name"])
    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    await migrations._run_pending(test_engine, factory)
    await db_session.refresh(custom)
    assert "printers:clear_plate" in custom.permissions
    custom.permissions = ["printers:control"]
    await db_session.commit()

    await migrations._run_pending(test_engine, factory)
    await migrations._run_pending(test_engine, factory)
    db_session.expire_all()
    assert (await db_session.scalar(select(Group).where(Group.name == "G01 custom floor"))).permissions == [
        "printers:control"
    ]
