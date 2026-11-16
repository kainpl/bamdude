"""The nozzle a filament prints from is read for the plate that was asked (upstream 19212d066).

A multi-plate 3MF carries one filament list per plate, and on a dual-nozzle
machine the same slot may print from the left hotend on one plate and from the
right one on another. Both ``filament-requirements`` answers read the carriage
without naming the plate, so a slot took it from whichever plate came last in
the file: the print dialog showed the badge of the wrong nozzle and filtered
the trays offered for that slot by it.
"""

from __future__ import annotations

import io
import json
import zipfile

import pytest
from httpx import AsyncClient

from backend.app.core.config import settings as app_settings
from backend.app.models.library import LibraryFile

# As BambuStudio ships the H2 machines: the slicer's first extruder (group 0)
# is MQTT extruder 1 (left), its second (group 1) MQTT extruder 0 (right).
_PROJECT_SETTINGS = json.dumps(
    {
        "physical_extruder_map": ["1", "0"],
        "filament_type": ["PLA", "PETG"],
        "filament_colour": ["#FF0000", "#00FF00"],
    }
)
# Slot 1 prints from group 0 on plate 1 and from group 1 on plate 2; slot 2 the
# other way round.
_SLICE_INFO = """<config>
  <plate>
    <metadata key="index" value="1"/>
    <filament id="1" type="PLA" color="#FF0000" used_g="10" used_m="3" group_id="0"/>
    <filament id="2" type="PETG" color="#00FF00" used_g="5" used_m="2" group_id="1"/>
  </plate>
  <plate>
    <metadata key="index" value="2"/>
    <filament id="1" type="PLA" color="#FF0000" used_g="7" used_m="2" group_id="1"/>
    <filament id="2" type="PETG" color="#00FF00" used_g="4" used_m="1" group_id="0"/>
  </plate>
</config>"""

LEFT, RIGHT = 1, 0


def _three_mf() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("3D/3dmodel.model", "<model/>")
        zf.writestr("Metadata/project_settings.config", _PROJECT_SETTINGS)
        zf.writestr("Metadata/slice_info.config", _SLICE_INFO)
    return buf.getvalue()


@pytest.fixture
def storage(tmp_path):
    original = app_settings.base_dir
    app_settings.base_dir = tmp_path
    yield tmp_path
    app_settings.base_dir = original


@pytest.fixture
async def library_file(db_session, storage) -> int:
    path = storage / "library" / "files" / "TwoPlates.3mf"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_three_mf())
    row = LibraryFile(
        filename="TwoPlates.3mf",
        file_path=str(path.relative_to(storage)),
        file_type="3mf",
        file_size=path.stat().st_size,
    )
    db_session.add(row)
    await db_session.commit()
    await db_session.refresh(row)
    return row.id


@pytest.fixture
def archive(storage, printer_factory, archive_factory):
    async def create(**kwargs) -> int:
        path = storage / "archives" / "test" / "TwoPlates.gcode.3mf"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(_three_mf())
        printer = await printer_factory()
        row = await archive_factory(printer.id, file_path=str(path.relative_to(storage)), **kwargs)
        return row.id

    return create


async def _nozzles(client: AsyncClient, url: str) -> dict[int, int | None]:
    resp = await client.get(url)
    assert resp.status_code == 200, resp.text
    return {f["slot_id"]: f.get("nozzle_id") for f in resp.json()["filaments"]}


@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.parametrize(("plate", "expected"), [(1, {1: LEFT, 2: RIGHT}), (2, {1: RIGHT, 2: LEFT})])
async def test_a_library_file_answers_with_the_nozzles_of_the_plate_asked(
    async_client: AsyncClient, library_file, plate, expected
):
    url = f"/api/v1/library/files/{library_file}/filament-requirements?plate_id={plate}"

    assert await _nozzles(async_client, url) == expected


@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.parametrize(("plate", "expected"), [(1, {1: LEFT, 2: RIGHT}), (2, {1: RIGHT, 2: LEFT})])
async def test_an_archive_answers_with_the_nozzles_of_the_plate_asked(
    async_client: AsyncClient, archive, plate, expected
):
    url = f"/api/v1/archives/{await archive()}/filament-requirements?plate_id={plate}"

    assert await _nozzles(async_client, url) == expected


@pytest.mark.asyncio
@pytest.mark.integration
async def test_an_archive_asked_without_a_plate_answers_for_the_plate_it_printed(async_client: AsyncClient, archive):
    """The archive is one print of one plate — ``plate_index`` says which."""
    url = f"/api/v1/archives/{await archive(plate_index=1)}/filament-requirements"

    assert await _nozzles(async_client, url) == {1: LEFT, 2: RIGHT}
