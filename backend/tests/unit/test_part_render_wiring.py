"""Part render is wired where the spec says: lifespan order, restore, the sync hooks, health, budgets."""

import inspect
from pathlib import Path

from sqlalchemy import select

from backend.app.models.library import LibraryFile
from backend.app.models.plate_render import PlateRender
from backend.app.models.product import Product

APP = Path(__file__).resolve().parents[2] / "app"


def test_the_lifespan_starts_part_render_after_the_library_worker_and_stops_it_first():
    text = (APP / "main.py").read_text(encoding="utf-8")
    assert text.index("await start_library_file_runtime(") < text.index("await start_part_render(")
    shutdown = text.index("    # Shutdown")
    assert shutdown < text.index("await stop_part_render()") < text.index("await stop_preview_runtime()", shutdown)


def test_a_restore_asks_the_render_writer_first_and_before_the_database_is_replaced():
    from backend.app.api.routes import settings as settings_routes

    source = inspect.getsource(settings_routes._restore_backup)
    gate = source.index("await _quiesce_part_render()")
    # first of the quiesce steps: a refusal leaves every other service running
    assert gate < source.index("virtual_printer_manager.configure") < source.index("files.apply")


async def test_the_restore_gate_refuses_an_attempt_whose_end_is_not_proven(monkeypatch):
    """Consilium E3-R4: no proof, no database replacement -- 409 before anything is touched."""
    import pytest
    from fastapi import HTTPException

    from backend.app.api.routes import settings as settings_routes
    from backend.app.services import part_render_scheduler

    async def unproven(reason):
        return False

    monkeypatch.setattr(part_render_scheduler, "stop_part_render_scheduler", unproven)
    with pytest.raises(HTTPException) as refused:
        await settings_routes._quiesce_part_render()
    assert refused.value.status_code == 409


async def test_the_restore_gate_lets_a_proven_stop_through(monkeypatch):
    from backend.app.api.routes import settings as settings_routes
    from backend.app.services import part_render_scheduler

    async def proven(reason):
        return True

    monkeypatch.setattr(part_render_scheduler, "stop_part_render_scheduler", proven)
    await settings_routes._quiesce_part_render()


async def test_the_sync_queues_the_plates_where_it_refreshes_the_facets(db_session):
    from backend.app.services import product_sync

    file = LibraryFile(
        filename="p.gcode.3mf",
        file_path="library/p.gcode.3mf",
        file_type="gcode",
        file_size=1,
        file_hash="a" * 64,
        file_metadata={},
    )
    product = Product(name="Lamp")
    db_session.add_all([file, product])
    await db_session.commit()
    await product_sync.sync_product_for_file(db_session, library_file_id=file.id, product_ids=[product.id])
    await db_session.commit()
    rows = (await db_session.execute(select(PlateRender))).scalars().all()
    assert [(r.file_sha256, r.plate_index, r.priority) for r in rows] == [("a" * 64, 0, 1)]
    await product_sync.purge_file_product_links(db_session, [file.id])
    await db_session.commit()
    assert len((await db_session.execute(select(PlateRender))).scalars().all()) == 1  # GC's, after its grace


async def test_system_info_reports_the_part_render_worker(async_client):
    from unittest.mock import MagicMock, patch

    with patch("backend.app.api.routes.system.psutil") as mock_psutil:  # as test_system_api does
        mock_psutil.disk_usage.return_value = MagicMock(total=1, used=0, free=1, percent=0.0)
        mock_psutil.virtual_memory.return_value = MagicMock(total=1, available=1, used=0, percent=0.0)
        mock_psutil.boot_time.return_value = 1700000000.0
        mock_psutil.Process.return_value.create_time.return_value = 1700000000.0
        mock_psutil.cpu_count.return_value = 1
        mock_psutil.cpu_percent.return_value = 0.0
        rsp = await async_client.get("/api/v1/system/info")
    assert rsp.status_code == 200
    assert rsp.json()["part_render_worker"]["reason"] == "not_started"  # ASGI tests run no lifespan


def test_every_worker_bucket_fits_the_broker_store_together():
    from backend.app.services import (
        analysis_transport,
        library_file_runtime,
        local_worker_broker,
        part_render_protocol,
        preview_protocol,
    )

    total = (
        preview_protocol.BUCKET_BYTES
        + analysis_transport.BUCKET_BYTES
        + library_file_runtime.BUCKET_BYTES
        + part_render_protocol.BUCKET_BYTES
    )
    assert 'max_file_store="3072MB"' in inspect.getsource(local_worker_broker)
    assert total <= 3072 * 1024**2
