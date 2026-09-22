"""Live usage projection: display-only math from journal + G-code cumulative."""

import asyncio
import logging
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from backend.app.core.config import settings as app_settings
from backend.app.models.print_usage_event import EVENT_RUNOUT, EVENT_SPOOL_LOADED, KIND_PAUSE, PrintUsageEvent
from backend.app.services.usage_projection import compute_usage_projection


async def _printer(db_session):
    from backend.app.models.printer import Printer

    p = Printer(name="PJ1", ip_address="10.0.0.4", serial_number="SN-PJ1", access_code="1")
    db_session.add(p)
    await db_session.commit()
    await db_session.refresh(p)
    return p


async def _archive(db_session, printer, tmp_path):
    from backend.app.models.archive import PrintArchive

    (tmp_path / "archives").mkdir(exist_ok=True)
    (tmp_path / "archives" / "p.3mf").write_bytes(b"stub")
    a = PrintArchive(
        printer_id=printer.id,
        filename="p.3mf",
        file_path="archives/p.3mf",
        file_size=4,
        print_name="proj_print",
        status="printing",
    )
    db_session.add(a)
    await db_session.commit()
    await db_session.refresh(a)
    return a


def _pm(state="RUNNING", layer=100, total=200, mapping=None):
    s = MagicMock()
    s.state = state
    s.layer_num = layer
    s.total_layers = total
    s.raw_data = {"mapping": mapping} if mapping is not None else {}
    pm = MagicMock()
    pm.get_status.return_value = s

    # Keep projection-math tests in-process; production uses the bounded child
    # worker and has separate lifecycle coverage.
    def analysis_runner(path, plate_id):
        from pathlib import Path

        from backend.app.services.print_file_analysis import PrintFileAnalysis
        from backend.app.utils import threemf_tools

        file_path = Path(path)
        return PrintFileAnalysis(
            filament_usage=threemf_tools.extract_filament_usage_from_3mf(file_path, plate_id) or [],
            layer_usage=threemf_tools.extract_layer_filament_usage_from_3mf(file_path, plate_id),
            filament_properties=threemf_tools.extract_filament_properties_from_3mf(file_path) or {},
        )

    pm._print_file_analysis_runner = analysis_runner
    return pm


def _patched(usage, layer_usage=None):
    return (
        patch("backend.app.utils.threemf_tools.extract_filament_usage_from_3mf", return_value=usage),
        patch("backend.app.utils.threemf_tools.extract_layer_filament_usage_from_3mf", return_value=layer_usage),
        patch("backend.app.utils.threemf_tools.extract_filament_properties_from_3mf", return_value={}),
    )


@pytest.mark.asyncio
async def test_idle_printer_is_inactive(db_session):
    pm = _pm(state="IDLE")
    assert await compute_usage_projection(db_session, 1, printer_manager=pm) == {"active": False}


def test_projection_diagnostics_emit_a_rate_limited_summary(monkeypatch, caplog):
    from backend.app.services import usage_projection

    caplog.set_level(logging.INFO)
    diagnostics = usage_projection._ProjectionDiagnostics(started_at=0.0, summary_started_at=0.0, last_summary_at=0.0)
    monkeypatch.setattr(usage_projection, "_projection_diagnostics", diagnostics)
    monkeypatch.setattr(usage_projection, "_SUMMARY_INTERVAL_SECONDS", 10.0)
    monkeypatch.setattr(usage_projection.time, "monotonic", lambda: 11.0)

    usage_projection._record_projection("ready")

    assert "[USAGE PROJECTION] summary window_seconds=11.0 requests=1" in caplog.text
    assert usage_projection.get_usage_projection_diagnostics()["requests"] == 0

    monkeypatch.setattr(usage_projection.time, "monotonic", lambda: 12.0)
    usage_projection._record_projection("inactive")
    assert usage_projection.get_usage_projection_diagnostics() == {
        "window_seconds": 1.0,
        "requests": 1,
        "inactive": 1,
        "waiting_source": 0,
        "waiting_analysis": 0,
        "ready": 0,
    }


@pytest.mark.asyncio
async def test_linear_projection_halfway(db_session, tmp_path, monkeypatch):
    monkeypatch.setattr(app_settings, "base_dir", tmp_path)
    printer = await _printer(db_session)
    await _archive(db_session, printer, tmp_path)
    p1, p2, p3 = _patched([{"slot_id": 1, "used_g": 300.0, "type": "PLA", "color": "#FF0000"}])
    with p1, p2, p3:
        result = await compute_usage_projection(db_session, printer.id, printer_manager=_pm(layer=100, total=200))
    assert result["active"] is True
    assert result["slots"][0]["consumed_g"] == pytest.approx(150.0)
    assert result["slots"][0]["estimate_g"] == 300.0
    assert "segments" not in result["slots"][0]


@pytest.mark.asyncio
async def test_projection_never_exceeds_the_estimate(db_session, tmp_path, monkeypatch):
    monkeypatch.setattr(app_settings, "base_dir", tmp_path)
    printer = await _printer(db_session)
    await _archive(db_session, printer, tmp_path)
    p1, p2, p3 = _patched([{"slot_id": 1, "used_g": 300.0, "type": "PLA", "color": ""}])
    with p1, p2, p3:
        result = await compute_usage_projection(db_session, printer.id, printer_manager=_pm(layer=250, total=200))
    assert result["slots"][0]["consumed_g"] == pytest.approx(300.0)


@pytest.mark.asyncio
async def test_segments_follow_journal_boundaries(db_session, tmp_path, monkeypatch):
    """A same-slot runout at layer 80: origin's segment freezes at the runout,
    the replacement's grows with the print — attribution the UI can show."""
    monkeypatch.setattr(app_settings, "base_dir", tmp_path)
    printer = await _printer(db_session)
    archive = await _archive(db_session, printer, tmp_path)
    for event, kind, layer, spool_id in (
        (EVENT_RUNOUT, KIND_PAUSE, 80, 7),
        (EVENT_SPOOL_LOADED, None, 80, 9),
    ):
        db_session.add(
            PrintUsageEvent(
                printer_id=printer.id,
                archive_id=archive.id,
                layer_num=layer,
                event=event,
                kind=kind,
                global_tray_id=0,
                spool_id=spool_id,
            )
        )
    await db_session.commit()

    p1, p2, p3 = _patched([{"slot_id": 1, "used_g": 300.0, "type": "PLA", "color": ""}])
    with p1, p2, p3:
        result = await compute_usage_projection(
            db_session, printer.id, printer_manager=_pm(layer=100, total=200, mapping=[0])
        )
    segments = result["slots"][0]["segments"]
    # Linear: 300 g / 200 layers = 1.5 g per layer.
    assert [(s["spool_id"], s["consumed_g"]) for s in segments] == [(7, 120.0), (9, 30.0)]


@pytest.mark.asyncio
async def test_running_print_with_no_3mf_projects_nothing_but_stays_active(db_session, tmp_path, monkeypatch):
    from backend.app.models.archive import PrintArchive

    monkeypatch.setattr(app_settings, "base_dir", tmp_path)
    printer = await _printer(db_session)
    a = PrintArchive(
        printer_id=printer.id, filename="x.3mf", file_path="", file_size=0, print_name="x", status="printing"
    )
    db_session.add(a)
    await db_session.commit()

    result = await compute_usage_projection(db_session, printer.id, printer_manager=_pm())
    assert result["active"] is True
    assert result["slots"] == []


@pytest.mark.asyncio
async def test_projection_drops_a_snapshot_that_finished_while_analysis_waited(db_session, tmp_path, monkeypatch):
    """A slow cold parse must not publish the preceding print after completion."""
    from backend.app.models.archive import PrintArchive
    from backend.app.services import print_file_analysis
    from backend.app.services.print_file_analysis import PrintFileAnalysis

    monkeypatch.setattr(app_settings, "base_dir", tmp_path)
    printer = await _printer(db_session)
    archive = await _archive(db_session, printer, tmp_path)

    async def finish_while_waiting(*args, **kwargs):
        await db_session.execute(update(PrintArchive).where(PrintArchive.id == archive.id).values(status="completed"))
        await db_session.commit()
        return PrintFileAnalysis([], None, {})

    monkeypatch.setattr(print_file_analysis, "get_print_file_analysis", finish_while_waiting)

    assert await compute_usage_projection(db_session, printer.id, printer_manager=_pm()) == {"active": False}


@pytest.mark.asyncio
async def test_projection_releases_its_read_transaction_before_waiting_for_analysis(db_session, tmp_path, monkeypatch):
    """A long shared parse may wait, but it must not retain this request's transaction."""
    from backend.app.services import print_file_analysis
    from backend.app.services.print_file_analysis import PrintFileAnalysis

    monkeypatch.setattr(app_settings, "base_dir", tmp_path)
    printer = await _printer(db_session)
    await _archive(db_session, printer, tmp_path)
    observed_transaction = None

    async def inspect_wait(*args, **kwargs):
        nonlocal observed_transaction
        observed_transaction = db_session.in_transaction()
        return PrintFileAnalysis([], None, {})

    monkeypatch.setattr(print_file_analysis, "get_print_file_analysis", inspect_wait)

    result = await compute_usage_projection(db_session, printer.id, printer_manager=_pm())

    assert observed_transaction is False
    assert result["active"] is True


@pytest.mark.asyncio
async def test_projection_wait_does_not_block_a_real_file_sqlite_writer(tmp_path, monkeypatch):
    """Separate SQLite connections can write while the shared parser is held.

    The normal test engine deliberately uses one in-memory StaticPool, so a
    second session cannot demonstrate lock ownership there.  This file-backed
    engine is intentionally local to the test and exercises the real SQLite
    transaction boundary without borrowing application globals.
    """
    from backend.app.core.database import Base, import_all_models
    from backend.app.models.printer import Printer
    from backend.app.services import print_file_analysis
    from backend.app.services.print_file_analysis import PrintFileAnalysis

    monkeypatch.setattr(app_settings, "base_dir", tmp_path)
    engine = create_async_engine(f"sqlite+aiosqlite:///{(tmp_path / 'projection-contention.sqlite').as_posix()}")
    sessions = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    entered = asyncio.Event()
    release = asyncio.Event()

    async def held_analysis(*args, **kwargs):
        entered.set()
        await release.wait()
        return PrintFileAnalysis([], None, {})

    monkeypatch.setattr(print_file_analysis, "get_print_file_analysis", held_analysis)
    import_all_models()
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with sessions() as seed:
            printer = await _printer(seed)
            await _archive(seed, printer, tmp_path)
            printer_id = printer.id

        async with sessions() as reader:
            projection = asyncio.create_task(compute_usage_projection(reader, printer_id, printer_manager=_pm()))
            await asyncio.wait_for(entered.wait(), timeout=1)
            assert reader.in_transaction() is False

            async with sessions() as writer:
                await asyncio.wait_for(
                    writer.execute(update(Printer).where(Printer.id == printer_id).values(name="writer-progressed")),
                    timeout=1,
                )
                await asyncio.wait_for(writer.commit(), timeout=1)

            release.set()
            assert (await projection)["active"] is True
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_runout_without_replacement_shows_no_segments(db_session, tmp_path, monkeypatch):
    """Resumed without replacing: both boundary segments are the same reel —
    that is not a split and the projection must not claim one (live report,
    2026-08-23: the widget said 'split across spools' on an unchanged spool)."""
    monkeypatch.setattr(app_settings, "base_dir", tmp_path)
    printer = await _printer(db_session)
    archive = await _archive(db_session, printer, tmp_path)
    db_session.add(
        PrintUsageEvent(
            printer_id=printer.id,
            archive_id=archive.id,
            layer_num=80,
            event=EVENT_RUNOUT,
            kind=KIND_PAUSE,
            global_tray_id=0,
            spool_id=7,
        )
    )
    await db_session.commit()

    p1, p2, p3 = _patched([{"slot_id": 1, "used_g": 300.0, "type": "PLA", "color": ""}])
    with p1, p2, p3:
        result = await compute_usage_projection(
            db_session, printer.id, printer_manager=_pm(layer=100, total=200, mapping=[0])
        )
    assert "segments" not in result["slots"][0]
    assert result["slots"][0]["consumed_g"] == pytest.approx(150.0)
