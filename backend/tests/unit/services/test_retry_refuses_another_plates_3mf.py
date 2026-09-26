"""A retry never attaches another plate's 3MF (upstream a4cfbd42 part 3, #3063).

When the print-start guard (#1204) refuses a downloaded 3MF because it holds
another plate than the one running, the row is left without a file — and the
four retry triggers come back for it with the SAME stale name that fetched the
contradicted file. Recovery checked that a candidate is a readable 3MF, never
which plate it holds, so it put back exactly what the guard had discarded.

Upstream simply does not schedule a retry after a plate rejection. We check
the plate in the one place every trigger goes through instead: a candidate
whose sliced plates do not include the archive's own plate is discarded. A
retry that later finds the right plate's file still attaches it.
"""

from __future__ import annotations

import zipfile
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from backend.app.services.archive import sliced_plate_indices_in_3mf


def _sliced_3mf(path: Path, plates: list[int]) -> Path:
    body = "".join(f'<plate><metadata key="index" value="{n}" /></plate>' for n in plates)
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("Metadata/slice_info.config", f"<config>{body}</config>")
    return path


class TestSlicedPlateIndices:
    def test_one_plate(self, tmp_path):
        assert sliced_plate_indices_in_3mf(_sliced_3mf(tmp_path / "a.3mf", [4])) == {4}

    def test_every_plate_of_a_slice_all(self, tmp_path):
        assert sliced_plate_indices_in_3mf(_sliced_3mf(tmp_path / "a.3mf", [1, 2, 3])) == {1, 2, 3}

    def test_nothing_readable_is_an_empty_answer(self, tmp_path):
        bad = tmp_path / "bad.3mf"
        bad.write_bytes(b"not a zip")
        assert sliced_plate_indices_in_3mf(bad) == set()
        empty = tmp_path / "empty.3mf"
        with zipfile.ZipFile(empty, "w") as zf:
            zf.writestr("3D/3dmodel.model", "<model/>")
        assert sliced_plate_indices_in_3mf(empty) == set()


async def _retry(db_session, test_engine, printer_factory, monkeypatch, tmp_path, *, archive_plate, file_plates):
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from backend.app.models.archive import PrintArchive
    from backend.app.services import archive_download_retry as retry_module

    printer = await printer_factory()
    archive = PrintArchive(
        printer_id=printer.id,
        filename="Lamp.gcode.3mf",
        file_path="",
        file_size=0,
        print_name="Lamp",
        status="printing",
        plate_index=archive_plate,
    )
    db_session.add(archive)
    await db_session.commit()
    archive_id = archive.id

    candidate = _sliced_3mf(tmp_path / "Lamp.gcode.3mf", file_plates)

    async def downloaded(*_args, **_kwargs):
        return candidate, "Lamp.gcode.3mf"

    attach = AsyncMock(return_value=True)
    monkeypatch.setattr(retry_module, "async_session", async_sessionmaker(test_engine, class_=AsyncSession))
    monkeypatch.setattr(retry_module, "try_download_3mf", downloaded)
    monkeypatch.setattr(retry_module.ArchiveService, "attach_3mf_to_archive", attach)
    monkeypatch.setattr(retry_module.ws_manager, "send_archive_updated", AsyncMock())
    monkeypatch.setattr("backend.app.services.archive.load_objects_from_archive_into_state", lambda *_a: None)

    status = await retry_module.archive_download_retry.retry_archive(archive_id)
    db_session.expire_all()
    row = await db_session.get(PrintArchive, archive_id)
    return status, attach, row, candidate


@pytest.mark.asyncio
async def test_another_plates_file_is_not_attached(db_session, test_engine, printer_factory, monkeypatch, tmp_path):
    status, attach, row, candidate = await _retry(
        db_session, test_engine, printer_factory, monkeypatch, tmp_path, archive_plate=1, file_plates=[4]
    )
    assert status == "failed"
    attach.assert_not_awaited()
    assert row.file_path == ""
    assert row.extra_data["no_3mf_available"] is True
    assert not candidate.exists(), "the refused download is cleaned up"


@pytest.mark.asyncio
async def test_the_running_plates_file_still_attaches(db_session, test_engine, printer_factory, monkeypatch, tmp_path):
    status, attach, _row, _candidate = await _retry(
        db_session, test_engine, printer_factory, monkeypatch, tmp_path, archive_plate=4, file_plates=[4]
    )
    assert status == "recovered"
    attach.assert_awaited_once()


@pytest.mark.asyncio
async def test_a_slice_all_that_holds_the_plate_attaches(
    db_session, test_engine, printer_factory, monkeypatch, tmp_path
):
    status, attach, _row, _candidate = await _retry(
        db_session, test_engine, printer_factory, monkeypatch, tmp_path, archive_plate=2, file_plates=[1, 2, 3]
    )
    assert status == "recovered"
    attach.assert_awaited_once()


@pytest.mark.asyncio
async def test_an_unknown_plate_on_either_side_is_not_a_contradiction(
    db_session, test_engine, printer_factory, monkeypatch, tmp_path
):
    """No plate on the row (an old archive) or none readable in the file:
    nothing contradicts, so the file attaches as it always did."""
    status, attach, _row, _candidate = await _retry(
        db_session, test_engine, printer_factory, monkeypatch, tmp_path, archive_plate=None, file_plates=[4]
    )
    assert status == "recovered"
    attach.assert_awaited_once()
