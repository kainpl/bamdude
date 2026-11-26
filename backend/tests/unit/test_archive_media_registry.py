"""The registry of archive videos: bound to the archive's identity, gone with the archive (G10 C, m237)."""

import ast
from datetime import datetime
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from backend.app.models.archive import PrintArchive
from backend.app.models.archive_media_asset import ArchiveMediaAsset
from backend.app.services import archive_media

APP = Path(__file__).resolve().parents[2] / "app"


async def _archive(db, **over) -> PrintArchive:
    row = PrintArchive(filename="a.3mf", file_path="", file_size=1, status="completed", **over)
    db.add(row)
    await db.flush()
    return row


def _asset(archive: PrintArchive, *, role="primary", revision=1, identity=None, path="v.mp4") -> ArchiveMediaAsset:
    return ArchiveMediaAsset(
        asset_uuid=str(uuid4()),
        archive_id=archive.id,
        archive_identity=identity or archive.recovery_identity,
        role=role,
        revision=revision,
        relative_path=path,
        origin="legacy",
        created_at=datetime(2026, 1, 1),
    )


async def _count(db) -> int:
    return (await db.execute(select(func.count()).select_from(ArchiveMediaAsset))).scalar_one()


@pytest.mark.asyncio
async def test_an_archive_reads_its_own_rows(db_session):
    archive = await _archive(db_session)
    other = await _archive(db_session)
    db_session.add_all(
        [
            _asset(archive, role="variant", revision=2, path="b.mp4"),
            _asset(archive, role="primary", revision=1, path="a.mp4"),
            _asset(other, path="other.mp4"),
        ]
    )
    await db_session.flush()

    primary = await archive_media.primary_of(db_session, archive.id, archive.recovery_identity)
    assert primary.relative_path == "a.mp4"
    assert [
        a.relative_path for a in await archive_media.assets_of(db_session, archive.id, archive.recovery_identity)
    ] == [
        "a.mp4",
        "b.mp4",
    ]


@pytest.mark.asyncio
async def test_a_row_left_under_a_reused_id_answers_nothing(db_session):
    """SQLite hands a deleted archive's id to the next one: the identity is what binds a row."""
    archive = await _archive(db_session)
    db_session.add(_asset(archive, identity="an-earlier-archive-of-this-id"))
    await db_session.flush()

    assert await archive_media.primary_of(db_session, archive.id, archive.recovery_identity) is None
    assert await archive_media.assets_of(db_session, archive.id, archive.recovery_identity) == []
    assert await archive_media.primary_of(db_session, archive.id, None) is None


@pytest.mark.asyncio
async def test_forgetting_archives_removes_their_rows_whatever_identity_they_carry(db_session):
    doomed, kept = await _archive(db_session), await _archive(db_session)
    db_session.add_all(
        [
            _asset(doomed),
            _asset(doomed, role="variant", revision=2, path="b.mp4"),
            _asset(doomed, role="retired", revision=3, identity="an-earlier-archive-of-this-id", path="c.mp4"),
            _asset(kept),
        ]
    )
    await db_session.flush()

    assert await archive_media.forget_archives(db_session, [doomed.id, doomed.id, None]) == 3
    assert await _count(db_session) == 1
    assert (await archive_media.primary_of(db_session, kept.id, kept.recovery_identity)) is not None
    assert await archive_media.forget_archives(db_session, []) == 0


@pytest.mark.asyncio
async def test_a_hard_deleted_archive_takes_its_registry_rows_with_it(db_session):
    from backend.app.services.archive import ArchiveService

    doomed, kept = await _archive(db_session), await _archive(db_session)
    db_session.add_all([_asset(doomed), _asset(kept)])
    await db_session.commit()
    doomed_id = doomed.id

    assert await ArchiveService(db_session).delete_archive(doomed_id) is True

    rows = (await db_session.execute(select(ArchiveMediaAsset.archive_id))).scalars().all()
    assert rows == [kept.id]


def _sources():
    for path in APP.rglob("*.py"):
        if "migrations" not in path.parts:
            yield path, path.read_text(encoding="utf-8")


WRITER = APP / "services" / "archive_media.py"
MODEL = APP / "models" / "archive_media_asset.py"


def test_only_the_writer_holds_the_class_or_names_the_table():
    offenders = []
    for path, source in _sources():
        if path in {WRITER, MODEL}:
            continue
        tree = ast.parse(source)
        names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
        strings = {
            node.value for node in ast.walk(tree) if isinstance(node, ast.Constant) and isinstance(node.value, str)
        }
        if "ArchiveMediaAsset" in names or any("archive_media_assets" in s for s in strings):
            offenders.append(str(path.relative_to(APP)))
    assert offenders == []


def _functions_without_a_function_ancestor(tree: ast.AST) -> set[int]:
    """``id``s of the functions not nested in another function (methods of a module-level class count).

    One walk that stops descending at every function: the same set the per-function walks of every
    function found, without walking each nested body again under every function that encloses it.
    """
    found: set[int] = set()
    stack = [tree]
    while stack:
        for child in ast.iter_child_nodes(stack.pop()):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                found.add(id(child))
            else:
                stack.append(child)
    return found


def test_every_door_that_deletes_archive_rows_tells_the_registry():
    """A statement that deletes ``print_archives`` rows sits in a function that calls ``forget_archives``."""
    missing = []
    for path, source in _sources():
        tree = ast.parse(source)
        functions = [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
        top_level = _functions_without_a_function_ancestor(tree)
        for function in functions:
            if id(function) not in top_level:
                continue  # a helper inside a function is covered by the function that holds it
            text = ast.unparse(function)
            deletes_rows = any(
                call in text for call in ("delete(PrintArchive)", "self.db.delete(archive)", "db.delete(archive)")
            )
            if deletes_rows and "forget_archives" not in text:
                missing.append(f"{path.relative_to(APP)}:{function.name}")
    assert missing == []


@pytest.mark.asyncio
async def test_a_refused_sweep_keeps_the_archive_and_its_media_without_raw_fallback(db_session, monkeypatch):
    """C removed the unsafe raw fallback: a refused guarded root owns nothing."""
    from datetime import timedelta, timezone
    from unittest.mock import AsyncMock

    from backend.app.services.archive_purge import ArchivePurgeService

    long_ago = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=100)
    restored = await _archive(db_session, deleted_at=long_ago)
    gone = await _archive(db_session, deleted_at=long_ago)
    live = await _archive(db_session)
    db_session.add_all([_asset(restored), _asset(gone), _asset(live)])
    await db_session.commit()
    restored_id, gone_id, live_id = restored.id, gone.id, live.id

    service = ArchivePurgeService()
    monkeypatch.setattr(service, "_read_retention", AsyncMock(return_value=1))
    monkeypatch.setattr(service, "hard_delete_now", AsyncMock(return_value=False))
    assert await service._sweep(db_session) == 0
    assert service.hard_delete_now.await_count == 2

    archives = sorted((await db_session.execute(select(PrintArchive.id))).scalars().all())
    assets = sorted((await db_session.execute(select(ArchiveMediaAsset.archive_id))).scalars().all())
    assert archives == sorted([restored_id, gone_id, live_id])
    assert assets == archives
