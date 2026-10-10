"""part_renders.reconcile (spec §8.4) and gc (spec §9.5): rows and files agree, nothing of a live owner goes."""

import hashlib
import json
import logging
from datetime import timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.app.models.plate_render import PlateRender, PlateRenderObject
from backend.app.services import part_images, part_renders
from backend.app.services.part_render_protocol import GC_GRACE_SECONDS
from backend.tests.unit.test_part_renders_queue import linked

SHA = "a" * 64


@pytest.fixture
def factory(test_engine):
    return async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)


def write_dir(root, name: str, *, sha=SHA, version=2, plate=1, files=None) -> tuple:
    directory = part_renders.result_path(root, sha, version, plate, name)
    directory.mkdir(parents=True)
    files = {"101.lg.png": b"lg", "101.sm.png": b"sm"} if files is None else files
    for file_name, body in files.items():
        (directory / file_name).write_bytes(body)
    manifest = {
        "renderer": version,
        "objects": [
            {
                "identify_id": 101,
                "method": "toolpath",
                "files": {
                    "lg": {"bytes": 2, "sha256": hashlib.sha256(b"lg").hexdigest()},
                    "sm": {"bytes": 2, "sha256": hashlib.sha256(b"sm").hexdigest()},
                },
            }
        ],
    }
    raw = json.dumps(manifest).encode()
    (directory / "manifest.json").write_bytes(raw)
    return directory, hashlib.sha256(raw).hexdigest()


async def add_row(db, *, status="ready", result_dir=None, manifest_sha256=None, sha=SHA, version=2, objects=True):
    row = PlateRender(
        file_sha256=sha,
        plate_index=1,
        renderer_version=version,
        status=status,
        phase="render",
        result_dir=result_dir,
        manifest_sha256=manifest_sha256,
    )
    db.add(row)
    await db.flush()
    if objects:
        db.add(PlateRenderObject(render_id=row.id, identify_id=101, method="toolpath", tools=[0]))
    await db.commit()
    return row


async def reload(db, row) -> PlateRender:
    row_id = row.id  # read before expire_all: an expired attribute would be a sync load on an async session
    db.expire_all()
    return await db.get(PlateRender, row_id)


async def instance_count(db, row) -> int:
    return len((await db.execute(select(PlateRenderObject).where(PlateRenderObject.render_id == row.id))).all())


async def test_a_complete_result_is_kept(db_session, factory, tmp_path):
    directory, manifest = write_dir(tmp_path, "1" * 32)
    row = await add_row(db_session, result_dir="1" * 32, manifest_sha256=manifest)
    report = await part_renders.reconcile(factory, tmp_path)
    assert (report.kept, report.cleared, report.removed) == (1, 0, 0)
    assert directory.exists() and (await reload(db_session, row)).status == "ready"


@pytest.mark.parametrize("damage", ["missing_dir", "other_manifest", "missing_png", "short_png"])
@pytest.mark.parametrize("status", ["ready", "pending", "failed"])
async def test_a_broken_result_is_cleared_in_one_transaction(db_session, factory, tmp_path, damage, status):
    directory, manifest = write_dir(tmp_path, "1" * 32)
    if damage == "missing_dir":
        for path in directory.iterdir():
            path.unlink()
        directory.rmdir()
    elif damage == "other_manifest":
        manifest = "0" * 64
    elif damage == "missing_png":
        (directory / "101.sm.png").unlink()
    else:
        (directory / "101.lg.png").write_bytes(b"l")
    row = await add_row(db_session, status=status, result_dir="1" * 32, manifest_sha256=manifest)
    report = await part_renders.reconcile(factory, tmp_path)
    assert report.cleared == 1
    row = await reload(db_session, row)
    assert (row.result_dir, row.manifest_sha256) == (None, None)
    assert row.status == ("pending" if status == "ready" else status)
    assert await instance_count(db_session, row) == 0
    assert not directory.exists()


async def test_a_directory_left_between_rename_and_commit_is_removed(db_session, factory, tmp_path):
    kept, manifest = write_dir(tmp_path, "1" * 32)
    orphan, _ = write_dir(tmp_path, "2" * 32)  # same key, not the row's result_dir
    await add_row(db_session, result_dir="1" * 32, manifest_sha256=manifest)
    report = await part_renders.reconcile(factory, tmp_path)
    assert report.removed == 1 and kept.exists() and not orphan.exists()


async def test_a_directory_without_any_row_and_a_partial_one_are_removed(db_session, factory, tmp_path):
    lone, _ = write_dir(tmp_path, "3" * 32, sha="b" * 64)
    partial = part_renders.result_path(tmp_path, SHA, 2, 1, "4" * 32 + ".part")
    partial.mkdir(parents=True)
    report = await part_renders.reconcile(factory, tmp_path)
    assert report.removed == 2 and not lone.exists() and not partial.exists()


async def test_entries_outside_the_pattern_are_only_reported(db_session, factory, tmp_path, caplog):
    stranger = tmp_path / "not-ours"
    stranger.mkdir()
    caplog.set_level(logging.WARNING, logger=part_renders.__name__)
    report = await part_renders.reconcile(factory, tmp_path)
    assert report.foreign == 1 and stranger.exists()
    assert any("not-ours" in r.getMessage() for r in caplog.records)


async def test_gc_starts_the_grace_when_the_last_linked_file_goes(db_session, factory, tmp_path):
    directory, manifest = write_dir(tmp_path, "1" * 32)
    row = await add_row(db_session, result_dir="1" * 32, manifest_sha256=manifest)
    now = part_renders.utcnow()
    assert await part_renders.gc(factory, tmp_path, now) == 0  # orphaned now, inside its grace
    assert (await reload(db_session, row)).orphaned_at == now and directory.exists()
    later = now + timedelta(seconds=GC_GRACE_SECONDS + 1)
    assert await part_renders.gc(factory, tmp_path, later) == 1
    assert await reload(db_session, row) is None and not directory.exists()


async def test_gc_ends_the_grace_when_the_hash_is_linked_again(db_session, factory, tmp_path):
    row = await add_row(db_session)
    now = part_renders.utcnow()
    await part_renders.gc(factory, tmp_path, now)
    await linked(db_session, sha=SHA)
    later = now + timedelta(seconds=GC_GRACE_SECONDS + 1)
    assert await part_renders.gc(factory, tmp_path, later) == 0
    assert (await reload(db_session, row)).orphaned_at is None


async def test_gc_counts_a_file_in_the_trash_as_linked(db_session, factory, tmp_path):
    row = await add_row(db_session)
    await linked(db_session, sha=SHA, deleted=True)  # restored from the trash, its pictures are still there
    later = part_renders.utcnow() + timedelta(seconds=GC_GRACE_SECONDS + 1)
    await part_renders.gc(factory, tmp_path, part_renders.utcnow())
    assert await part_renders.gc(factory, tmp_path, later) == 0
    assert (await reload(db_session, row)).orphaned_at is None


async def test_a_hash_linked_again_between_the_choice_and_the_delete_keeps_everything(db_session, factory, tmp_path):
    """Consilium E3-R3: the plate, its instances and its directory all stay."""
    directory, manifest = write_dir(tmp_path, "1" * 32)
    row = await add_row(db_session, result_dir="1" * 32, manifest_sha256=manifest)
    now = part_renders.utcnow()
    await part_renders.gc_due(factory, now)
    later = now + timedelta(seconds=GC_GRACE_SECONDS + 1)
    due = await part_renders.gc_due(factory, later)
    assert [r.id for r in due] == [row.id]
    await linked(db_session, sha=SHA)  # linked again after the choice, before the delete
    assert await part_renders.gc_remove(factory, tmp_path, due, later) == 0
    assert await reload(db_session, row) is not None
    assert await instance_count(db_session, row) == 1
    assert directory.exists()


async def test_gc_drops_another_renderer_versions_rows_after_the_grace(db_session, factory, tmp_path):
    await linked(db_session, sha=SHA)
    row = await add_row(db_session, version=1)
    now = part_renders.utcnow()
    await part_renders.gc(factory, tmp_path, now)
    assert await part_renders.gc(factory, tmp_path, now + timedelta(seconds=GC_GRACE_SECONDS + 1)) == 1
    assert await reload(db_session, row) is None


@pytest.fixture
def changed():
    seen: list[set] = []
    unsubscribe = part_images.subscribe(seen.append)
    yield seen
    unsubscribe()


async def test_gc_says_which_plates_went(db_session, factory, changed, tmp_path):
    """Plan E4, task 27: a GC deletion reaches the picture collector with the plate's key, after its commit."""
    row = await add_row(db_session)  # an orphan: no linked file
    now = part_renders.utcnow()
    await part_renders.gc_due(factory, now)  # starts the grace
    later = now + timedelta(seconds=GC_GRACE_SECONDS + 1)
    due = await part_renders.gc_due(factory, later)
    assert await part_renders.gc_remove(factory, tmp_path, due, later) == 1
    assert changed == [{(row.file_sha256, row.plate_index)}]
