"""A print adopted mid-flight gets its start time from the slicer estimate and the
remaining time observed when BamDude joined (spec §3.3). Pure rule, no I/O."""

from __future__ import annotations

import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from backend.app.models.archive import PrintArchive
from backend.app.services.archive import ArchiveService, _reconstruct_recovered_start

OBS = datetime(2026, 9, 12, 12, 0, tzinfo=timezone.utc)


def _row(*, remaining, estimate, started_at=None, recovered=True):
    extra = {"recovered_start": {"observed_at": OBS.isoformat(), "remaining_seconds": remaining}} if recovered else {}
    return PrintArchive(
        filename="p.3mf",
        file_path="a/p.3mf",
        file_size=1,
        print_name="p",
        source_content_hash="a" * 64,
        print_time_seconds=estimate,
        started_at=started_at,
        extra_data=extra,
    )


def test_start_is_observed_minus_elapsed():
    a = _row(remaining=1800, estimate=7200)
    assert _reconstruct_recovered_start(a) is True
    assert a.started_at == OBS - timedelta(seconds=5400)
    assert a.extra_data["started_at_reconstructed"] is True


def test_remaining_beyond_estimate_floors_at_observed():
    a = _row(remaining=9000, estimate=7200)
    assert _reconstruct_recovered_start(a) is True
    assert a.started_at == OBS


def test_missing_inputs_leave_it_unknown():
    for row in (
        _row(remaining=None, estimate=7200),
        _row(remaining=1800, estimate=None),
        _row(remaining=1800, estimate=0),
    ):
        assert _reconstruct_recovered_start(row) is False
        assert row.started_at is None


def test_a_known_start_and_a_normal_row_are_left_alone():
    known = _row(remaining=1800, estimate=7200, started_at=OBS)
    assert _reconstruct_recovered_start(known) is False and known.started_at == OBS
    normal = _row(remaining=1800, estimate=7200, recovered=False)
    assert _reconstruct_recovered_start(normal) is False and normal.started_at is None


def _as_utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _single_plate_3mf(path: Path, prediction: int) -> Path:
    """A container the parser can read a ``prediction`` (the slicer estimate) out of.

    Same shape as ``test_attach_3mf_field_parity``'s builder, one plate.
    """
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr(
            "Metadata/slice_info.config",
            '<config><plate><metadata key="index" value="1" />'
            f'<metadata key="prediction" value="{prediction}" />'
            '<metadata key="weight" value="10.0" /></plate></config>',
        )
    return path


async def _attach_to_provisional_row(db_session, tmp_path, monkeypatch, printer, *, started_at):
    """The provisional row an adoption (or ``on_print_start``) leaves behind, then its 3MF."""
    from backend.app.core.config import settings as app_settings

    monkeypatch.setattr(app_settings, "base_dir", tmp_path)
    monkeypatch.setattr(app_settings, "archive_dir", tmp_path / "archive")
    (tmp_path / "archive").mkdir(parents=True, exist_ok=True)

    archive = PrintArchive(
        printer_id=printer.id,
        filename="p.gcode.3mf",
        file_path="",
        file_size=0,
        print_name="p",
        status="printing",
        started_at=started_at,
        extra_data={"recovered_start": {"observed_at": OBS.isoformat(), "remaining_seconds": 1800}},
    )
    db_session.add(archive)
    await db_session.commit()
    await db_session.refresh(archive)

    src = _single_plate_3mf(tmp_path / f"src{archive.id}.gcode.3mf", 7200)
    ok = await ArchiveService(db_session).attach_3mf_to_archive(archive.id, src, "p.gcode.3mf")
    assert ok, "attach failed — the assertions below would be about nothing"
    await db_session.refresh(archive)
    return archive


async def test_the_attach_reconstructs_a_missing_start(db_session, tmp_path, monkeypatch, printer_factory):
    printer = await printer_factory()
    archive = await _attach_to_provisional_row(db_session, tmp_path, monkeypatch, printer, started_at=None)

    assert archive.print_time_seconds == 7200, "the estimate has to arrive with the file"
    # ``started_at`` is a naive column; the row comes back from SQLite without
    # the offset it was written with. The instant is what is asserted.
    assert _as_utc(archive.started_at) == OBS - timedelta(seconds=5400)
    assert (archive.extra_data or {}).get("started_at_reconstructed") is True


async def test_the_attach_never_overwrites_a_known_start(db_session, tmp_path, monkeypatch, printer_factory):
    printer = await printer_factory()
    archive = await _attach_to_provisional_row(db_session, tmp_path, monkeypatch, printer, started_at=OBS)

    assert _as_utc(archive.started_at) == OBS
    assert "started_at_reconstructed" not in (archive.extra_data or {})
