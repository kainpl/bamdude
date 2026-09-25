"""MakerWorld lives in ``services/model_providers/makerworld/`` — there is no second front door.

``services/makerworld.py`` and ``services/makerworld_meta.py`` were removed when
MakerWorld became the first model provider (upstream #2845). The migration m056
imported them; the owner ruled (2026-09-25) that editing its import lines beats
keeping two re-export shims alive for it.
"""

import ast
import importlib.util
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.app.models.library import LibraryFile
from backend.app.models.library_file_makerworld_meta import LibraryFileMakerworldMeta
from backend.app.services.model_providers.makerworld.errors import MakerWorldUnavailableError

APP = Path(__file__).resolve().parents[2] / "app"
OLD_PATHS = {"backend.app.services.makerworld", "backend.app.services.makerworld_meta"}


def test_the_old_makerworld_modules_are_gone():
    for name in OLD_PATHS:
        assert importlib.util.find_spec(name) is None, name


def test_nothing_imports_the_old_makerworld_paths():
    offenders = []
    for path in APP.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.ImportFrom) and node.module in OLD_PATHS:
                offenders.append(f"{path.relative_to(APP)}:{node.lineno} {node.module}")
            elif isinstance(node, ast.Import):
                offenders.extend(
                    f"{path.relative_to(APP)}:{node.lineno} {a.name}" for a in node.names if a.name in OLD_PATHS
                )
    assert not offenders, offenders


@pytest.mark.asyncio
async def test_m056_backfill_still_runs_through_the_provider_package(test_engine, db_session):
    """The edited import lines are exercised for real: an unreachable MakerWorld
    is caught as ``MakerWorldError`` and the row is skipped, as before."""
    from backend.app.migrations.m056_library_file_makerworld_meta import seed

    db_session.add(
        LibraryFile(
            filename="old.3mf",
            file_path="library/files/old.3mf",
            file_type="3mf",
            file_size=1,
            source_type="makerworld",
            source_url="https://makerworld.com/models/7#profileId-8",
        )
    )
    await db_session.commit()
    factory = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    unreachable = AsyncMock(side_effect=MakerWorldUnavailableError("offline"))

    with patch(
        "backend.app.services.model_providers.makerworld.service.MakerWorldService.get_design",
        unreachable,
    ):
        await seed(factory)

    unreachable.assert_awaited_once_with(7)
    assert (await db_session.execute(select(LibraryFileMakerworldMeta))).all() == []
