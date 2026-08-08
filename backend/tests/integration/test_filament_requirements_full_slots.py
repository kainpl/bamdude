"""``full_slots`` widens the answer for the slice modal only (#2712).

One endpoint answers two different questions:

* **print-time AMS matching** asks "what does this plate consume", and must get
  the used-only list — otherwise a job demands spools it never touches;
* **the slice modal** asks "what slots exist", because the list it builds is
  positional all the way down to the CLI's ``filament_N.json`` parts. A source
  declaring four filaments but painting with slot 4 alone used to yield a single
  row, so the user's pick bound to slot 1 and slot 4 sliced with whatever the
  source had baked in. Wrong material, no warning.

So the widening is opt-in, and these tests pin both answers — the bug returns
just as surely by widening the print path as by narrowing the modal's.
"""

from __future__ import annotations

import io
import json
import zipfile

import pytest
from httpx import AsyncClient

from backend.app.core.config import settings as app_settings
from backend.app.models.library import LibraryFile

# Four declared slots; the plate prints with slot 4 alone. The reported shape.
_PROJECT_SETTINGS = json.dumps(
    {
        "filament_type": ["PLA", "PLA", "PETG", "PETG"],
        "filament_colour": ["#FF0000", "#00FF00", "#0000FF", "#FFFFFF"],
    }
)
_SLICE_INFO = """<config>
  <plate>
    <metadata key="index" value="1"/>
    <filament id="4" type="PETG" color="#FFFFFF" used_g="12.5" used_m="4.0" tray_info_idx="GFG99"/>
  </plate>
</config>"""


def _three_mf() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("3D/3dmodel.model", "<model/>")
        zf.writestr("Metadata/project_settings.config", _PROJECT_SETTINGS)
        zf.writestr("Metadata/slice_info.config", _SLICE_INFO)
    return buf.getvalue()


@pytest.fixture
async def sliced_library_file(db_session, tmp_path):
    storage = tmp_path / "library" / "files"
    storage.mkdir(parents=True, exist_ok=True)
    path = storage / "Painted.3mf"
    path.write_bytes(_three_mf())

    original_base_dir = app_settings.base_dir
    app_settings.base_dir = tmp_path

    row = LibraryFile(
        filename="Painted.3mf",
        file_path=str(path.relative_to(tmp_path)),
        file_type="3mf",
        file_size=path.stat().st_size,
    )
    db_session.add(row)
    await db_session.commit()
    await db_session.refresh(row)

    yield row.id

    app_settings.base_dir = original_base_dir


async def _slots(client: AsyncClient, file_id: int, *, full: bool) -> list[dict]:
    url = f"/api/v1/library/files/{file_id}/filament-requirements?plate_id=1"
    if full:
        url += "&full_slots=true"
    resp = await client.get(url)
    assert resp.status_code == 200, resp.text
    return resp.json()["filaments"]


class TestTheModalGetsEverySlot:
    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_full_slots_returns_one_row_per_project_slot(self, async_client: AsyncClient, sliced_library_file):
        slots = await _slots(async_client, sliced_library_file, full=True)

        assert [f["slot_id"] for f in slots] == [1, 2, 3, 4]
        assert [f["used_in_plate"] for f in slots] == [False, False, False, True]

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_the_used_row_keeps_what_slice_info_measured(self, async_client: AsyncClient, sliced_library_file):
        slots = await _slots(async_client, sliced_library_file, full=True)

        assert slots[3]["used_grams"] == 12.5
        assert slots[3]["tray_info_idx"] == "GFG99"


class TestThePrintPathKeepsTheNarrowList:
    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_without_the_flag_only_the_consumed_slot_comes_back(
        self, async_client: AsyncClient, sliced_library_file
    ):
        """AMS matching must ask for exactly the spools the job needs. Widening
        here would have the operator loading three spools for nothing."""
        slots = await _slots(async_client, sliced_library_file, full=False)

        assert [f["slot_id"] for f in slots] == [4]

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_the_flag_defaults_to_off(self, async_client: AsyncClient, sliced_library_file):
        """Every existing caller keeps today's answer without being edited."""
        resp = await async_client.get(f"/api/v1/library/files/{sliced_library_file}/filament-requirements?plate_id=1")
        assert [f["slot_id"] for f in resp.json()["filaments"]] == [4]
