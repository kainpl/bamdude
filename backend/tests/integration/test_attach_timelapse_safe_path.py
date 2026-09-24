"""``ArchiveService.attach_timelapse`` path-traversal containment (GHSA-r2qv).

``filename`` reaches ``attach_timelapse`` from a printer's FTP directory listing
(the printer is part of the trust surface — a compromised printer can return
``..`` names) or the ``?filename=`` query param. It must never write outside the
archive directory.
"""

import pytest


@pytest.mark.asyncio
@pytest.mark.integration
async def test_attach_timelapse_rejects_traversal_and_absolute(
    db_session, printer_factory, archive_factory, monkeypatch, tmp_path
):
    from backend.app.core.config import settings
    from backend.app.services.archive import ArchiveService

    monkeypatch.setattr(settings, "base_dir", tmp_path)
    printer = await printer_factory()
    archive = await archive_factory(printer.id, file_path="archives/p/print.gcode.3mf")
    (tmp_path / "archives" / "p").mkdir(parents=True, exist_ok=True)

    svc = ArchiveService(db_session)

    # ``..`` traversal → refused, nothing written at the escape target.
    assert await svc.attach_timelapse(archive.id, b"x", "../../../evil.mp4") is False
    assert not (tmp_path / "evil.mp4").exists()

    # Absolute path → refused.
    assert await svc.attach_timelapse(archive.id, b"x", "/tmp/evil.mp4") is False

    # Legitimate filename → written INSIDE the archive dir (guards over-strictness).
    assert await svc.attach_timelapse(archive.id, b"data", "timelapse.mp4") is True
    assert (tmp_path / "archives" / "p" / "timelapse.mp4").read_bytes() == b"data"


def _data_dir(monkeypatch, tmp_path):
    """A DATA_DIR one level down, so "outside it" is somewhere a test can look."""
    from backend.app.core.config import settings

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    monkeypatch.setattr(settings, "base_dir", data_dir)
    monkeypatch.setattr(settings, "archive_dir", data_dir / "archive")
    return data_dir


@pytest.mark.asyncio
@pytest.mark.integration
async def test_a_print_without_a_3mf_keeps_its_timelapse_inside_the_data_dir(
    db_session, printer_factory, archive_factory, monkeypatch, tmp_path
):
    """Upstream #2843. A slicer-sent print on an H2 / P2S keeps its file on the
    printer's internal storage, so its archive has ``file_path == ""`` — and
    ``(base_dir / "").parent`` is the PARENT of the data directory. In Docker
    that is /app: the write failed with EACCES and the scan retried and
    discarded the video; where the parent was writable the file landed beside
    the install and the attach failed anyway on ``relative_to``."""
    from backend.app.services.archive import ArchiveService

    data_dir = _data_dir(monkeypatch, tmp_path)
    printer = await printer_factory()
    archive = await archive_factory(printer.id, file_path="")

    assert await ArchiveService(db_session).attach_timelapse(archive.id, b"video", "timelapse.mp4") is True

    assert not (tmp_path / "timelapse.mp4").exists()
    await db_session.refresh(archive)
    assert (data_dir / archive.timelapse_path).read_bytes() == b"video"
    # The same folder the archive's photos use (utils/archive_paths).
    assert (data_dir / archive.timelapse_path).parent == data_dir / "archive" / "no_source" / str(archive.id)


@pytest.mark.asyncio
@pytest.mark.integration
async def test_a_rejected_timelapse_name_leaves_no_folder_behind(
    db_session, printer_factory, archive_factory, monkeypatch, tmp_path
):
    from backend.app.services.archive import ArchiveService

    data_dir = _data_dir(monkeypatch, tmp_path)
    printer = await printer_factory()
    archive = await archive_factory(printer.id, file_path="")

    assert await ArchiveService(db_session).attach_timelapse(archive.id, b"x", "../../evil.mp4") is False

    assert not (data_dir / "archive" / "no_source" / str(archive.id)).exists()


@pytest.mark.asyncio
@pytest.mark.integration
async def test_a_print_without_a_3mf_takes_a_design_file(
    async_client, db_session, printer_factory, archive_factory, monkeypatch, tmp_path
):
    """Same derivation, same fault, in the Fusion 360 upload."""
    data_dir = _data_dir(monkeypatch, tmp_path)
    printer = await printer_factory()
    archive = await archive_factory(printer.id, file_path="")

    response = await async_client.post(
        f"/api/v1/archives/{archive.id}/f3d",
        files={"file": ("design.f3d", b"fusion", "application/octet-stream")},
    )

    assert response.status_code == 200, response.text
    stored = response.json()["f3d_path"]
    assert (data_dir / stored).read_bytes() == b"fusion"
    assert (data_dir / stored).is_relative_to(data_dir / "archive" / "no_source" / str(archive.id))
