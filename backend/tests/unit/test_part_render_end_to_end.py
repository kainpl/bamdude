"""Spec §16, E3's exit: a linked file goes through the queue to ready on the real worker."""

import asyncio
import hashlib
import logging
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.app.core.config import settings
from backend.app.models.library import LibraryFile
from backend.app.models.plate_render import PlateRender, PlateRenderObject
from backend.app.models.product import Product, ProductPlate
from backend.app.part_render_probe import pixel_counts
from backend.app.services import part_renders, render_runtime
from backend.app.services.local_worker_broker import LocalWorkerBroker
from backend.app.services.part_render_runtime import PartRenderRuntime
from backend.tests.fixtures.part_render_3mf import two_objects_3mf
from backend.tests.unit.services.test_part_render_node import needs_node
from backend.tests.unit.test_part_render_scheduler import make_scheduler, stop_parked

ROOT = Path(__file__).resolve().parents[3]


async def _linked_plate(db, tmp_path: Path, monkeypatch) -> str:
    monkeypatch.setattr(settings, "base_dir", tmp_path)
    monkeypatch.setattr(settings, "library_dir", tmp_path / "library")
    (tmp_path / "library").mkdir()
    source = two_objects_3mf(tmp_path / "library" / "lamp.gcode.3mf")
    data = source.read_bytes()
    sha = hashlib.sha256(data).hexdigest()
    file = LibraryFile(
        filename="lamp.gcode.3mf",
        file_path="library/lamp.gcode.3mf",
        file_type="gcode",
        file_size=len(data),
        file_hash=sha,
    )
    product = Product(name="Lamp")
    db.add_all([file, product])
    await db.flush()
    db.add(ProductPlate(product_id=product.id, library_file_id=file.id, plate_index=1))
    await db.commit()
    return sha


async def _until_settled(db, sha: str) -> PlateRender:
    for _ in range(1200):
        db.expire_all()
        row = (await db.execute(select(PlateRender).where(PlateRender.file_sha256 == sha))).scalar_one_or_none()
        if row is not None and row.status != "pending":
            return row
        await asyncio.sleep(0.1)
    raise AssertionError("the plate never left pending")


async def _run(tmp_path, test_engine, db_session, sha):
    broker = LocalWorkerBroker(tmp_path / ".cache" / "preview-service")
    await broker.start()
    runtime = PartRenderRuntime(tmp_path, broker, app_dir=ROOT)
    await runtime.start()
    factory = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    scheduler = make_scheduler(runtime, factory, tmp_path / "part-renders")
    try:
        await scheduler.start()  # the backfill queues the linked plate
        row = await _until_settled(db_session, sha)
        # stopped while parked, never inside a query: the test's own session shares that one connection
        await stop_parked(scheduler)
        return row
    finally:
        await scheduler.stop("shutdown")  # one stop per scheduler: a no-op after stop_parked
        await runtime.stop()
        await broker.stop()


async def _instances(db, row) -> dict[int, str]:
    found = (await db.execute(select(PlateRenderObject).where(PlateRenderObject.render_id == row.id))).scalars()
    return {o.identify_id: o.method for o in found}


@needs_node
async def test_a_linked_file_goes_to_ready_through_the_real_worker(
    tmp_path, test_engine, db_session, monkeypatch, caplog
):
    caplog.set_level(logging.INFO)
    sha = await _linked_plate(db_session, tmp_path, monkeypatch)
    row = await _run(tmp_path, test_engine, db_session, sha)
    assert (row.status, row.reason) == ("ready", None)
    assert await _instances(db_session, row) == {101: "toolpath", 202: "toolpath"}
    directory = part_renders.result_path(tmp_path / "part-renders", sha, row.renderer_version, 1, row.result_dir)
    assert pixel_counts(directory / "101.lg.png")["green"] > 0  # 101 is printed with T2 (#00AE42)
    assert pixel_counts(directory / "202.lg.png")["green"] == 0  # 202 is not: ownership by pixels
    messages = [r.getMessage() for r in caplog.records]
    assert any("started" in m and "Part render attempt=" in m for m in messages)
    assert any("outcome=done result=ok" in m for m in messages)


async def test_without_an_official_node_a_linked_file_is_ready_by_the_fallback(
    tmp_path, test_engine, db_session, monkeypatch
):
    def unsupported(app_dir):
        raise render_runtime.UnsupportedPlatform("no official Node.js build")

    monkeypatch.setattr(render_runtime, "locate", unsupported)
    sha = await _linked_plate(db_session, tmp_path, monkeypatch)
    row = await _run(tmp_path, test_engine, db_session, sha)
    assert (row.status, row.reason) == ("ready", "no_runtime")
    assert await _instances(db_session, row) == {101: "top_mask", 202: "top_mask"}
