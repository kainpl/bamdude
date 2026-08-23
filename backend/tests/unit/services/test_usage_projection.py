"""Live usage projection: display-only math from journal + G-code cumulative."""

from unittest.mock import MagicMock, patch

import pytest

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
