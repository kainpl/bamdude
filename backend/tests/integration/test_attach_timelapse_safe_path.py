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
