"""part_renders.publish: a new immutable directory, a CAS on the full key, an unknown commit (spec §9.3)."""

import contextlib
import hashlib
import io
import json

import pytest
from PIL import Image
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.app.models.plate_render import PlateRender, PlateRenderObject
from backend.app.services import part_renders
from backend.app.services.part_render_types import AttemptResult
from backend.tests.unit.test_part_renders_queue import GEN, linked

pytestmark = pytest.mark.usefixtures("live_generation")


@pytest.fixture
def live_generation():
    part_renders.set_live_generation(GEN)
    yield
    part_renders.set_live_generation(None)


@pytest.fixture
def factory(test_engine):
    return async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)


def _png(size: int) -> bytes:
    out = io.BytesIO()
    Image.new("RGBA", (size, size), (10, 200, 10, 255)).save(out, format="PNG")
    return out.getvalue()


def attempt_files(tmp_path, *, tamper: bool = False) -> AttemptResult:
    files = tmp_path / "staging" / "files"
    files.mkdir(parents=True)
    lg, sm = _png(512), _png(128)
    (files / "101.lg.png").write_bytes(lg)
    (files / "101.sm.png").write_bytes(sm if not tamper else _png(64))
    manifest = {
        "renderer": 2,
        "reason": None,
        "objects": [
            {
                "identify_id": 101,
                "method": "toolpath",
                "reason": None,
                "width": 512,
                "height": 512,
                "tools": [0, 2],
                "files": {
                    "lg": {"bytes": len(lg), "sha256": hashlib.sha256(lg).hexdigest()},
                    "sm": {"bytes": len(sm), "sha256": hashlib.sha256(sm).hexdigest()},
                },
            },
            {
                "identify_id": 202,
                "method": "missing",
                "reason": "no_markers",
                "width": None,
                "height": None,
                "tools": [],
                "files": {},
            },
        ],
    }
    (files / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return AttemptResult("done", "c" * 32, 5, {"outcome": "ok", "reason": None}, files, "v24.17.0")


async def pending_task(db_session):
    file = await linked(db_session)
    await part_renders.ensure_for_files(db_session, [file.id])
    await db_session.commit()
    return await part_renders.next_task(db_session, part_renders.utcnow())


async def objects_of(db_session, render_id: int) -> dict[int, str]:
    rows = (
        await db_session.execute(select(PlateRenderObject).where(PlateRenderObject.render_id == render_id))
    ).scalars()
    return {row.identify_id: row.method for row in rows}


async def test_publish_moves_a_complete_directory_and_writes_the_instance_rows(db_session, factory, tmp_path):
    task = await pending_task(db_session)
    root = tmp_path / "part-renders"
    outcome = await part_renders.publish(factory, root, task, attempt_files(tmp_path), GEN, reason=None)
    assert outcome == "published"
    db_session.expire_all()
    row = await db_session.get(PlateRender, task.render_id)
    assert (row.status, row.reason, row.runtime_version, row.attempts) == ("ready", None, "v24.17.0", 0)
    directory = part_renders.result_path(
        root, task.file_sha256, task.renderer_version, task.plate_index, row.result_dir
    )
    assert sorted(p.name for p in directory.iterdir()) == ["101.lg.png", "101.sm.png", "manifest.json"]
    assert row.manifest_sha256 == hashlib.sha256((directory / "manifest.json").read_bytes()).hexdigest()
    assert await objects_of(db_session, task.render_id) == {101: "toolpath", 202: "missing"}
    assert not list(directory.parent.glob("*.part"))


async def test_a_cas_that_matches_nothing_deletes_the_new_directory(db_session, factory, tmp_path):
    task = await pending_task(db_session)
    await db_session.execute(
        PlateRender.__table__.update().where(PlateRender.id == task.render_id).values(status="failed")
    )
    await db_session.commit()
    root = tmp_path / "part-renders"
    assert await part_renders.publish(factory, root, task, attempt_files(tmp_path), GEN, reason=None) == "stale"
    assert not any(p.is_dir() for p in root.rglob("*") if len(p.name) == 32)


async def test_a_restored_row_of_another_hash_under_the_same_id_is_not_written(db_session, factory, tmp_path):
    """Spec §15: after a restore the id may name another file's plate -- the full key refuses it."""
    task = await pending_task(db_session)
    await db_session.execute(
        PlateRender.__table__.update().where(PlateRender.id == task.render_id).values(file_sha256="f" * 64)
    )
    await db_session.commit()
    root = tmp_path / "part-renders"
    assert await part_renders.publish(factory, root, task, attempt_files(tmp_path), GEN, reason=None) == "stale"
    db_session.expire_all()
    assert (await db_session.get(PlateRender, task.render_id)).result_dir is None


async def test_a_stale_generation_publishes_nothing(db_session, factory, tmp_path):
    task = await pending_task(db_session)
    root = tmp_path / "part-renders"
    assert await part_renders.publish(factory, root, task, attempt_files(tmp_path), "x" * 32, reason=None) == "stale"
    db_session.expire_all()
    assert (await db_session.get(PlateRender, task.render_id)).status == "pending"


async def test_an_unknown_commit_deletes_nothing(db_session, factory, tmp_path):
    task = await pending_task(db_session)

    @contextlib.asynccontextmanager
    async def dropping():
        async with factory() as db:

            async def commit():
                raise ConnectionResetError("the link dropped inside COMMIT")

            db.commit = commit
            yield db

    root = tmp_path / "part-renders"
    assert await part_renders.publish(dropping, root, task, attempt_files(tmp_path), GEN, reason=None) == "unknown"
    # the reconciliation decides on the next start, from what the database says (spec §9.3)
    assert [p for p in root.rglob("*") if p.is_dir() and len(p.name) == 32]


async def test_publishing_over_a_result_deletes_the_old_directory_after_the_commit(db_session, factory, tmp_path):
    task = await pending_task(db_session)
    root = tmp_path / "part-renders"
    await part_renders.publish(factory, root, task, attempt_files(tmp_path), GEN, reason=None)
    db_session.expire_all()
    old = (await db_session.get(PlateRender, task.render_id)).result_dir
    await db_session.execute(
        PlateRender.__table__.update().where(PlateRender.id == task.render_id).values(status="pending")
    )
    await db_session.commit()
    task = await part_renders.next_task(db_session, part_renders.utcnow())
    second = attempt_files(tmp_path / "again")
    assert await part_renders.publish(factory, root, task, second, GEN, reason=None) == "published"
    db_session.expire_all()
    new = (await db_session.get(PlateRender, task.render_id)).result_dir
    assert new != old
    assert not part_renders.result_path(root, task.file_sha256, 2, task.plate_index, old).exists()


async def test_a_fallback_publication_carries_the_rows_reason(db_session, factory, tmp_path):
    task = await pending_task(db_session)
    root = tmp_path / "part-renders"
    await part_renders.publish(factory, root, task, attempt_files(tmp_path), GEN, reason="memory_limit")
    db_session.expire_all()
    assert (await db_session.get(PlateRender, task.render_id)).reason == "memory_limit"


async def test_a_png_that_does_not_answer_its_manifest_is_refused(db_session, factory, tmp_path):
    task = await pending_task(db_session)
    root = tmp_path / "part-renders"
    with pytest.raises(part_renders.InvalidResult):
        await part_renders.publish(factory, root, task, attempt_files(tmp_path, tamper=True), GEN, reason=None)
    assert not root.exists() or not any(root.rglob("*.png"))


async def test_an_instance_id_beyond_u32_is_an_invalid_result(db_session, factory, tmp_path):
    """Final review C1, main's backstop: the instance row is BIGINT, a slicer's id is u32."""
    task = await pending_task(db_session)
    result = attempt_files(tmp_path)
    manifest = json.loads((result.files / "manifest.json").read_text(encoding="utf-8"))
    manifest["objects"][1]["identify_id"] = 2**64
    (result.files / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(part_renders.InvalidResult):
        await part_renders.publish(factory, tmp_path / "part-renders", task, result, GEN, reason=None)


async def test_a_database_failure_before_the_commit_is_failed_and_leaves_no_directory(
    db_session, factory, tmp_path, monkeypatch
):
    """Final review C1(b): a statement that fails before the commit committed nothing -- the new directory
    goes, and the caller hears "failed", not "unknown"."""
    task = await pending_task(db_session)

    def refused(*args, **kwargs):
        raise RuntimeError("the instance rows were refused")

    monkeypatch.setattr(part_renders, "insert", refused)
    root = tmp_path / "part-renders"
    assert await part_renders.publish(factory, root, task, attempt_files(tmp_path), GEN, reason=None) == "failed"
    assert not [p for p in root.rglob("*") if p.is_dir() and len(p.name) == 32]
    db_session.expire_all()
    assert (await db_session.get(PlateRender, task.render_id)).status == "pending"


async def test_a_disk_that_refuses_the_copy_is_failed_and_leaves_no_partial_directory(
    db_session, factory, tmp_path, monkeypatch
):
    """Final review I2: ENOSPC / EACCES while the new directory is written -- no .part is left behind."""
    import errno

    task = await pending_task(db_session)

    def full(src, dst, length=0):
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(part_renders.shutil, "copyfileobj", full)
    root = tmp_path / "part-renders"
    assert await part_renders.publish(factory, root, task, attempt_files(tmp_path), GEN, reason=None) == "failed"
    assert not list(root.rglob("*.part"))


async def test_defer_moves_the_next_attempt_without_counting_it(db_session, factory):
    task = await pending_task(db_session)
    now = part_renders.utcnow()
    assert await part_renders.defer(factory, task, "publish_failed", GEN, now)
    db_session.expire_all()
    row = await db_session.get(PlateRender, task.render_id)
    assert (row.status, row.attempts, row.last_error) == ("pending", 0, "publish_failed")
    assert row.next_attempt_at > now
