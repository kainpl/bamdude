"""An external folder's scan judges a 3MF by its contents too (upstream #2993).

Every other door into the library records ``has_sliced_gcode`` when it parses a
3MF (m137). The external-folder scan parsed the file for its thumbnail and
metadata and never asked, so a sliced ``Foo.3mf`` on a NAS was filed as a
source project — no Print button — while the same bytes uploaded were not.

Rows the scan already wrote are put right by the next scan of their folder:
m137 cannot reach them (it ran before they existed), and a migration is the
wrong place to walk a mount that may be slow or absent.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models.library import LibraryFile, LibraryFolder
from backend.app.models.library_scan import LibraryScanJob
from backend.app.services.library_scan import run_scan
from backend.tests.fixtures.filament_routing_cases import write_routing_3mf

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]

_PLATE = {1: [{"id": 1, "type": "PLA", "color": "#FFFFFF", "used_g": "1"}]}


@pytest.fixture
async def mount(db_session: AsyncSession, tmp_path):
    root = tmp_path / "nas"
    root.mkdir()
    folder = LibraryFolder(name="NAS", is_external=True, external_path=str(root), external_show_hidden=False)
    db_session.add(folder)
    await db_session.commit()
    await db_session.refresh(folder)
    return root, folder


async def _scan(db: AsyncSession, folder: LibraryFolder) -> None:
    job = LibraryScanJob(folder_id=folder.id, status="queued")
    db.add(job)
    await db.commit()
    await run_scan(job.id)
    await db.refresh(job)
    assert job.status == "finished", job.error


async def _row(db: AsyncSession, filename: str) -> LibraryFile:
    """Read fresh: the scan wrote from its own sessions."""
    stmt = select(LibraryFile).where(LibraryFile.filename == filename).execution_options(populate_existing=True)
    return (await db.execute(stmt)).scalar_one()


async def test_a_sliced_3mf_named_as_source_is_filed_as_sliced(db_session, mount):
    root, folder = mount
    write_routing_3mf(root / "lamp.3mf", _PLATE)
    write_routing_3mf(root / "model.3mf", _PLATE, gcode_plates=[])

    await _scan(db_session, folder)

    lamp, model = await _row(db_session, "lamp.3mf"), await _row(db_session, "model.3mf")
    assert lamp.file_metadata["has_sliced_gcode"] is True
    assert "gcode" in lamp.file_tags, "the Print button reads this tag"
    assert model.file_metadata["has_sliced_gcode"] is False
    assert "gcode" not in model.file_tags


async def test_a_row_written_without_the_answer_gets_it_on_the_next_scan(db_session, mount):
    root, folder = mount
    write_routing_3mf(root / "lamp.3mf", _PLATE)
    await _scan(db_session, folder)

    # What an earlier version of the scan left behind: no flag, source-project tags.
    lamp = await _row(db_session, "lamp.3mf")
    lamp.file_metadata = {k: v for k, v in (lamp.file_metadata or {}).items() if k != "has_sliced_gcode"}
    lamp.file_tags = ["3mf", "project"]
    await db_session.commit()

    await _scan(db_session, folder)

    lamp = await _row(db_session, "lamp.3mf")
    assert lamp.file_metadata["has_sliced_gcode"] is True
    assert "gcode" in lamp.file_tags and "project" not in lamp.file_tags


async def test_a_file_that_was_re_sliced_in_place_is_re_judged(db_session, mount):
    """The bytes moved, so the old answer is about a file that no longer exists."""
    root, folder = mount
    target = root / "part.3mf"
    write_routing_3mf(target, _PLATE, gcode_plates=[])
    await _scan(db_session, folder)
    assert (await _row(db_session, "part.3mf")).file_metadata["has_sliced_gcode"] is False

    write_routing_3mf(target, {**_PLATE, 2: _PLATE[1]})  # now sliced — and a different size
    await _scan(db_session, folder)

    part = await _row(db_session, "part.3mf")
    assert part.file_metadata["has_sliced_gcode"] is True
    assert "gcode" in part.file_tags
