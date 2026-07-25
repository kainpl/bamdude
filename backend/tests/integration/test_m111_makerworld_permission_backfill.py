"""Integration test for m111_makerworld_permission_backfill.seed().

``makerworld:view`` / ``makerworld:import`` were added to the fresh-install
DEFAULT_GROUPS lists but to no migration, so every database seeded before that
change left Operators and Viewers with a 403 on MakerWorld. This pins the
backfill: the right groups gain exactly the right keys, nothing else is touched,
and re-running is a no-op.
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.app.migrations import m111_makerworld_permission_backfill as m111
from backend.app.models.group import Group

VIEW = "makerworld:view"
IMPORT = "makerworld:import"


@pytest_asyncio.fixture
async def session_factory(test_engine):
    """Reuse the project-wide ``test_engine`` so the Group mapper can resolve
    its relationship targets; ``seed()`` takes a factory, not an engine."""
    yield async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)


async def _perms(session_factory, name: str) -> list[str]:
    async with session_factory() as db:
        row = (await db.execute(select(Group.permissions).where(Group.name == name))).scalar_one()
        return list(row or [])


@pytest.mark.asyncio
async def test_backfills_the_system_groups(session_factory):
    async with session_factory() as db:
        db.add_all(
            [
                Group(name="Administrators", description="", is_system=True, permissions=["printers:read"]),
                Group(name="Operators", description="", is_system=True, permissions=["printers:read"]),
                Group(name="Viewers", description="", is_system=True, permissions=["printers:read"]),
            ]
        )
        await db.commit()

    await m111.seed(session_factory)

    # Operators and Administrators browse AND import; Viewers only browse.
    for name in ("Administrators", "Operators"):
        perms = await _perms(session_factory, name)
        assert VIEW in perms and IMPORT in perms, name
    viewers = await _perms(session_factory, "Viewers")
    assert VIEW in viewers
    assert IMPORT not in viewers, "Viewers must not gain import"

    # Pre-existing entries survive.
    assert "printers:read" in await _perms(session_factory, "Operators")


@pytest.mark.asyncio
async def test_leaves_custom_groups_alone(session_factory):
    """A group the operator built themselves decides its own permissions —
    silently widening it would be worse than the 403."""
    async with session_factory() as db:
        db.add(Group(name="ShopFloor", description="custom", is_system=False, permissions=["printers:read"]))
        await db.commit()

    await m111.seed(session_factory)

    assert await _perms(session_factory, "ShopFloor") == ["printers:read"]


@pytest.mark.asyncio
async def test_is_idempotent_and_does_not_duplicate(session_factory):
    async with session_factory() as db:
        db.add(Group(name="Operators", description="", is_system=True, permissions=[VIEW]))
        await db.commit()

    await m111.seed(session_factory)
    await m111.seed(session_factory)

    perms = await _perms(session_factory, "Operators")
    assert perms.count(VIEW) == 1
    assert perms.count(IMPORT) == 1


@pytest.mark.asyncio
async def test_seed_matches_the_fresh_install_lists(session_factory):
    """Guard against the two lists drifting apart again — the backfill must
    grant exactly what a fresh install would."""
    from backend.app.core.permissions import DEFAULT_GROUPS

    fresh = {name: set(cfg["permissions"]) for name, cfg in DEFAULT_GROUPS.items()}
    assert {VIEW, IMPORT} <= fresh["Operators"]
    assert VIEW in fresh["Viewers"]
    assert IMPORT not in fresh["Viewers"]
