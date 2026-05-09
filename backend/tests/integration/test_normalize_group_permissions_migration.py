"""Integration test for m046_normalize_group_permissions.seed().

Runs against an in-memory SQLite engine with a fresh ``Group`` schema,
inserts groups whose ``permissions`` JSON list carries pre-rename keys,
runs the migration's ``seed()`` callable, then asserts the lists were
rewritten in place.
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.app.migrations import m046_normalize_group_permissions
from backend.app.models.group import Group


@pytest_asyncio.fixture
async def session_factory(test_engine):
    """Reuse the project-wide ``test_engine`` fixture (conftest.py loads the
    full ORM registry — 47+ models, cross-table FKs, the works) so the
    Group mapper can resolve all its relationship references.

    Wrapping the engine in our own ``async_sessionmaker`` because m046's
    ``seed()`` takes a callable factory, not an engine."""
    factory = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    yield factory


@pytest.mark.asyncio
async def test_seed_rewrites_legacy_filament_keys(session_factory):
    """Group created with pre-rename ``filaments:*`` keys gets ``inventory:*``
    after migration."""
    async with session_factory() as db:
        db.add(
            Group(
                name="LegacyOps",
                description="Pre-rename custom group",
                permissions=[
                    "printers:read",
                    "filaments:read",
                    "filaments:create",
                    "filaments:update",
                    "filaments:delete",
                ],
                is_system=False,
            )
        )
        await db.commit()

    await m046_normalize_group_permissions.seed(session_factory)

    async with session_factory() as db:
        from sqlalchemy import select

        group = (await db.execute(select(Group).where(Group.name == "LegacyOps"))).scalar_one()
        assert group.permissions == [
            "printers:read",
            "inventory:read",
            "inventory:create",
            "inventory:update",
            "inventory:delete",
        ]


@pytest.mark.asyncio
async def test_seed_rewrites_legacy_github_keys(session_factory):
    async with session_factory() as db:
        db.add(
            Group(
                name="BackupAdmin",
                permissions=["settings:read", "github:backup", "github:restore"],
                is_system=False,
            )
        )
        await db.commit()

    await m046_normalize_group_permissions.seed(session_factory)

    async with session_factory() as db:
        from sqlalchemy import select

        group = (await db.execute(select(Group).where(Group.name == "BackupAdmin"))).scalar_one()
        assert group.permissions == ["settings:read", "git:backup", "git:restore"]


@pytest.mark.asyncio
async def test_seed_drops_unknown_keys(session_factory):
    """Keys not in ``ALL_PERMISSIONS`` and not in the rename map get dropped
    silently — the alternative is to leave the group permanently
    un-saveable through the validator in ``routes/groups.py``."""
    async with session_factory() as db:
        db.add(
            Group(
                name="DefunctOps",
                permissions=["printers:read", "some:vanished_perm", "queue:read"],
                is_system=False,
            )
        )
        await db.commit()

    await m046_normalize_group_permissions.seed(session_factory)

    async with session_factory() as db:
        from sqlalchemy import select

        group = (await db.execute(select(Group).where(Group.name == "DefunctOps"))).scalar_one()
        assert group.permissions == ["printers:read", "queue:read"]


@pytest.mark.asyncio
async def test_seed_dedupes_old_and_new_key(session_factory):
    """A group that somehow ended up with both old and new key (e.g. backup
    restored over a partially-migrated DB) collapses to a single entry."""
    async with session_factory() as db:
        db.add(
            Group(
                name="DupesGroup",
                permissions=["inventory:read", "filaments:read", "git:backup", "github:backup"],
                is_system=False,
            )
        )
        await db.commit()

    await m046_normalize_group_permissions.seed(session_factory)

    async with session_factory() as db:
        from sqlalchemy import select

        group = (await db.execute(select(Group).where(Group.name == "DupesGroup"))).scalar_one()
        assert group.permissions == ["inventory:read", "git:backup"]


@pytest.mark.asyncio
async def test_seed_idempotent_on_clean_groups(session_factory):
    """Re-running on already-current keys is a no-op — required for
    upgrade-then-restart-then-upgrade to be safe."""
    clean_perms = ["printers:read", "inventory:read", "git:backup", "queue:read"]
    async with session_factory() as db:
        db.add(Group(name="CleanGroup", permissions=list(clean_perms), is_system=False))
        await db.commit()

    await m046_normalize_group_permissions.seed(session_factory)
    await m046_normalize_group_permissions.seed(session_factory)  # second run

    async with session_factory() as db:
        from sqlalchemy import select

        group = (await db.execute(select(Group).where(Group.name == "CleanGroup"))).scalar_one()
        assert group.permissions == clean_perms


@pytest.mark.asyncio
async def test_seed_handles_empty_groups_table(session_factory):
    """Fresh-install case: no groups yet (system-group seed runs after
    migrations). Must not crash."""
    await m046_normalize_group_permissions.seed(session_factory)
    # No assertion needed — just must not raise.
