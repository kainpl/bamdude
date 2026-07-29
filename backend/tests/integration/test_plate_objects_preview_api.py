"""Integration tests for the read-only plate object preview.

Two endpoints and one query parameter:

1. ``/library/files/{id}/plate-objects?plate=N`` and
   ``/archives/{id}/plate-objects`` — the object list, read live from the 3MF.
   The archive route deliberately takes no ``plate``: it answers for
   ``archive.plate_index``, because an archive records one executed print and
   object ids are numbered per plate.
2. ``?view=top`` on both plate-thumbnail routes — serves ``Metadata/top_N.png``
   with **no** fallback chain. Markers are positioned in top-down pick-PNG
   space; over the ¾ ``plate_N.png`` render they would sit convincingly on the
   wrong parts.

The regression that matters most: a file whose ``exclude_object`` is off still
returns its full object list. Skip capability and object discovery are
independent axes — OrcaSlicer ships ``exclude_object=false`` by default while
still labelling every instance in the gcode.
"""

import io
import json
import zipfile
from pathlib import Path

import pytest
from httpx import AsyncClient

from backend.tests.unit.services.test_plate_object_discovery import _png, make_3mf


def _build_3mf(*, glo: bool, eo: bool, top: bool, gcode_ids=None, plate_index=1) -> bytes:
    """A synthetic sliced 3MF with skip flags and an optional top view."""
    base = make_3mf(slice_ids={941: "part"}, gcode_ids=gcode_ids, plate_index=plate_index)
    src = zipfile.ZipFile(io.BytesIO(base))
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for item in src.namelist():
            z.writestr(item, src.read(item))
        z.writestr(
            "Metadata/project_settings.config",
            json.dumps({"gcode_label_objects": glo, "exclude_object": eo}),
        )
        z.writestr(f"Metadata/plate_{plate_index}.png", b"\x89PNG\r\n\x1a\nthreequarter")
        if top:
            z.writestr(f"Metadata/top_{plate_index}.png", _png([[(0, 0, 0)] * 4 for _ in range(4)]))
    return buf.getvalue()


@pytest.fixture
def _patch_base_dir(monkeypatch, tmp_path):
    """Point both routes' file resolution at *tmp_path*."""
    from backend.app.api.routes import library as library_routes
    from backend.app.core.config import settings

    monkeypatch.setattr(settings, "base_dir", tmp_path)
    monkeypatch.setattr(library_routes.app_settings, "base_dir", tmp_path)
    return tmp_path


async def _make_library_file(db_session, tmp_path: Path, data: bytes) -> int:
    from backend.app.models.library import LibraryFile

    dest = tmp_path / "preview.gcode.3mf"
    dest.write_bytes(data)
    lib_file = LibraryFile(
        filename="preview.gcode.3mf",
        file_path="preview.gcode.3mf",
        file_type="gcode",
        file_size=len(data),
    )
    db_session.add(lib_file)
    await db_session.commit()
    await db_session.refresh(lib_file)
    return lib_file.id


class TestLibraryPlateObjects:
    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_lists_every_instance_even_when_skipping_is_forbidden(
        self, async_client: AsyncClient, db_session, _patch_base_dir
    ):
        data = _build_3mf(glo=True, eo=False, top=True, gcode_ids=[941, 942, 943, 944, 945])
        file_id = await _make_library_file(db_session, _patch_base_dir, data)

        resp = await async_client.get(f"/api/v1/library/files/{file_id}/plate-objects")
        assert resp.status_code == 200
        body = resp.json()
        assert [o["id"] for o in body["objects"]] == [941, 942, 943, 944, 945]
        assert body["skip_objects_supported"] is False
        assert body["has_top_view"] is True

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_reports_skip_support_when_both_flags_are_on(
        self, async_client: AsyncClient, db_session, _patch_base_dir
    ):
        data = _build_3mf(glo=True, eo=True, top=True)
        file_id = await _make_library_file(db_session, _patch_base_dir, data)

        resp = await async_client.get(f"/api/v1/library/files/{file_id}/plate-objects")
        assert resp.json()["skip_objects_supported"] is True

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_missing_top_view_is_reported(self, async_client: AsyncClient, db_session, _patch_base_dir):
        data = _build_3mf(glo=True, eo=True, top=False)
        file_id = await _make_library_file(db_session, _patch_base_dir, data)

        resp = await async_client.get(f"/api/v1/library/files/{file_id}/plate-objects")
        assert resp.json()["has_top_view"] is False

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_unknown_file_404s(self, async_client: AsyncClient, _patch_base_dir):
        resp = await async_client.get("/api/v1/library/files/999999/plate-objects")
        assert resp.status_code == 404


class TestArchivePlateObjects:
    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_answers_for_the_printed_plate(
        self, async_client: AsyncClient, printer_factory, archive_factory, _patch_base_dir
    ):
        """plate_index picks the plate — there is no ?plate override."""
        data = _build_3mf(glo=True, eo=True, top=True, gcode_ids=[941, 942], plate_index=3)
        (_patch_base_dir / "archived.gcode.3mf").write_bytes(data)

        printer = await printer_factory()
        archive = await archive_factory(printer.id, file_path="archived.gcode.3mf", plate_index=3)

        resp = await async_client.get(f"/api/v1/archives/{archive.id}/plate-objects")
        assert resp.status_code == 200
        body = resp.json()
        assert body["plate_index"] == 3
        assert [o["id"] for o in body["objects"]] == [941, 942]

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_null_plate_index_reads_as_plate_one(
        self, async_client: AsyncClient, printer_factory, archive_factory, _patch_base_dir
    ):
        data = _build_3mf(glo=True, eo=True, top=True, gcode_ids=[941, 942])
        (_patch_base_dir / "archived.gcode.3mf").write_bytes(data)

        printer = await printer_factory()
        archive = await archive_factory(printer.id, file_path="archived.gcode.3mf", plate_index=None)

        resp = await async_client.get(f"/api/v1/archives/{archive.id}/plate-objects")
        assert resp.json()["plate_index"] == 1


class TestTopViewThumbnail:
    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_view_top_serves_the_top_render(self, async_client: AsyncClient, db_session, _patch_base_dir):
        data = _build_3mf(glo=True, eo=True, top=True)
        file_id = await _make_library_file(db_session, _patch_base_dir, data)

        resp = await async_client.get(f"/api/v1/library/files/{file_id}/plate-thumbnail/1?view=top")
        assert resp.status_code == 200
        assert resp.content.startswith(b"\x89PNG")
        assert b"threequarter" not in resp.content

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_missing_top_view_404s_instead_of_falling_back(
        self, async_client: AsyncClient, db_session, _patch_base_dir
    ):
        """The whole point: a ¾ render under top-down markers is worse than none."""
        data = _build_3mf(glo=True, eo=True, top=False)
        file_id = await _make_library_file(db_session, _patch_base_dir, data)

        resp = await async_client.get(f"/api/v1/library/files/{file_id}/plate-thumbnail/1?view=top")
        assert resp.status_code == 404

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_default_view_is_unchanged(self, async_client: AsyncClient, db_session, _patch_base_dir):
        data = _build_3mf(glo=True, eo=True, top=True)
        file_id = await _make_library_file(db_session, _patch_base_dir, data)

        resp = await async_client.get(f"/api/v1/library/files/{file_id}/plate-thumbnail/1")
        assert resp.status_code == 200
        assert b"threequarter" in resp.content
