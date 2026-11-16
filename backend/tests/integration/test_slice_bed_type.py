"""The bed plate of a slice: one list, checked at every door.

- A saved set of slice settings (a pipeline) takes only a bed plate the slice
  request itself accepts.
- ``Textured Cool Plate`` exists in OrcaSlicer's ``curr_bed_type`` enum and not
  in Bambu Studio's, so it is accepted for the one and refused for the other
  before any work is done (owner's decision, G12 point 13).
- Every slice call the route makes carries the bed plate, the per-plate
  "slice all plates with arrange" loop included.
"""

from __future__ import annotations

import io
import json
import zipfile

import httpx
import pytest
from fastapi import HTTPException
from httpx import AsyncClient
from pydantic import ValidationError

from backend.app.models.library import LibraryFile
from backend.app.schemas.slicer import BED_TYPES, ORCA_ONLY_BED_TYPES, SliceRequest
from backend.app.schemas.slicer_pipeline import SlicerPipelineCreate, SlicerPipelineResponse, SlicerPipelineUpdate
from backend.app.services.slicer_routing import refuse_bed_type_for_slicer
from backend.tests.integration.test_slice_api import (  # noqa: F401 -- slice_test_setup is a fixture
    _install_mock_sidecar,
    _make_3mf_with_settings,
    _wait_for_job,
    slice_test_setup,
)

TEXTURED_COOL = "Textured Cool Plate"


def _refs() -> dict:
    return {
        "printer_preset": {"source": "local", "id": "1"},
        "process_preset": {"source": "local", "id": "2"},
        "filament_presets": [{"source": "local", "id": "3"}],
    }


class TestTheOneList:
    def test_the_slice_request_accepts_every_listed_plate(self):
        for bed in BED_TYPES:
            assert SliceRequest(**_refs(), bed_type=bed).bed_type == bed

    def test_textured_cool_plate_is_listed_and_orca_only(self):
        assert TEXTURED_COOL in BED_TYPES
        assert frozenset({TEXTURED_COOL}) == ORCA_ONLY_BED_TYPES

    @pytest.mark.parametrize(
        "bed", ["Default Plate", "Smooth PEI Plate", "Cool Plate (SuperTack)", "textured pei plate"]
    )
    def test_the_slice_request_refuses_anything_else(self, bed):
        with pytest.raises(ValidationError):
            SliceRequest(**_refs(), bed_type=bed)


class TestASavedSetNamesAKnownPlate:
    @pytest.mark.parametrize("bed", [*sorted(BED_TYPES), None])
    def test_create_and_update_take_what_a_slice_takes(self, bed):
        assert SlicerPipelineCreate(name="x", **_refs(), bed_type=bed).bed_type == bed
        assert SlicerPipelineUpdate(bed_type=bed).bed_type == bed

    @pytest.mark.parametrize("bed", ["Smooth PEI Plate", "Cool Plate (SuperTack)", "anything"])
    def test_create_and_update_refuse_a_plate_no_slice_would_take(self, bed):
        with pytest.raises(ValidationError):
            SlicerPipelineCreate(name="x", **_refs(), bed_type=bed)
        with pytest.raises(ValidationError):
            SlicerPipelineUpdate(bed_type=bed)

    def test_a_row_saved_before_the_check_can_still_be_listed(self):
        """The response stays a free string: one old row must not take the
        whole list down. The dialog says the plate was ignored instead."""
        from datetime import datetime

        row = SlicerPipelineResponse(
            id=1,
            name="old",
            **_refs(),
            bed_type="Smooth PEI Plate",
            created_by=None,
            created_at=datetime(2026, 1, 1),
            updated_at=datetime(2026, 1, 1),
        )
        assert row.bed_type == "Smooth PEI Plate"

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_the_api_answers_422_on_create_and_update(self, async_client: AsyncClient):
        payload = {"name": "Set", **_refs(), "bed_type": "Smooth PEI Plate"}
        assert (await async_client.post("/api/v1/slicer-pipelines/", json=payload)).status_code == 422

        created = await async_client.post("/api/v1/slicer-pipelines/", json={**payload, "bed_type": TEXTURED_COOL})
        assert created.status_code == 201, created.text
        assert created.json()["bed_type"] == TEXTURED_COOL

        bad = await async_client.put(
            f"/api/v1/slicer-pipelines/{created.json()['id']}", json={"bed_type": "Cool Plate (SuperTack)"}
        )
        assert bad.status_code == 422
        kept = await async_client.get(f"/api/v1/slicer-pipelines/{created.json()['id']}")
        assert kept.json()["bed_type"] == TEXTURED_COOL


class TestTexturedCoolPlateIsOrcaSlicers:
    def test_refused_for_bambu_studio(self):
        with pytest.raises(HTTPException) as refusal:
            refuse_bed_type_for_slicer(TEXTURED_COOL, "bambu_studio")
        assert refusal.value.status_code == 422
        assert "OrcaSlicer" in refusal.value.detail

    @pytest.mark.parametrize(
        ("bed", "slicer"),
        [
            (TEXTURED_COOL, "orcaslicer"),
            ("Supertack Plate", "bambu_studio"),
            (None, "bambu_studio"),
            (TEXTURED_COOL, None),
        ],
    )
    def test_everything_else_passes(self, bed, slicer):
        refuse_bed_type_for_slicer(bed, slicer)

    def _body(self, setup: dict, **extra) -> dict:
        return {
            "printer_preset_id": setup["printer_id"],
            "process_preset_id": setup["process_id"],
            "filament_preset_id": setup["filament_id"],
            **extra,
        }

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_the_slice_is_refused_before_any_work_when_bambu_studio_is_asked_for(
        self,
        async_client: AsyncClient,
        slice_test_setup,  # noqa: F811
    ):
        calls: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            return httpx.Response(200, content=b"")

        _install_mock_sidecar(handler)
        response = await async_client.post(
            f"/api/v1/library/files/{slice_test_setup['src_file_id']}/slice",
            json=self._body(slice_test_setup, bed_type=TEXTURED_COOL, slicer="bambu_studio"),
        )
        assert response.status_code == 422, response.text
        assert "OrcaSlicer" in response.json()["detail"]
        assert calls == []

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_orcaslicer_is_sent_the_plate_by_its_own_name(
        self,
        async_client: AsyncClient,
        slice_test_setup,  # noqa: F811
    ):
        bodies: list[bytes] = []

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.startswith("/slice/progress/"):
                return httpx.Response(404)
            bodies.append(request.content)
            return httpx.Response(
                200,
                content=_make_3mf_with_settings(),
                headers={"x-print-time-seconds": "1", "x-filament-used-g": "1", "x-filament-used-mm": "1"},
            )

        _install_mock_sidecar(handler)
        # The fixture's preferred slicer is OrcaSlicer.
        response = await async_client.post(
            f"/api/v1/library/files/{slice_test_setup['src_file_id']}/slice",
            json=self._body(slice_test_setup, bed_type=TEXTURED_COOL),
        )
        assert response.status_code == 202, response.text
        terminal = await _wait_for_job(async_client, response.json()["job_id"])
        assert terminal["status"] == "completed", terminal
        assert len(bodies) == 1
        assert b'name="bedType"\r\n\r\nTextured Cool Plate' in bodies[0]
        # ...and in the process profile, the path that lands before the CLI
        # reads its config.
        assert b'"curr_bed_type": "Textured Cool Plate"' in bodies[0]


def _two_plate_3mf() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("3D/3dmodel.model", "<model/>")
        zf.writestr("Metadata/project_settings.config", json.dumps({"printer_model": "Bambu Lab A1"}))
        zf.writestr(
            "Metadata/model_settings.config",
            "<config>"
            '<plate><metadata key="plater_id" value="1"/></plate>'
            '<plate><metadata key="plater_id" value="2"/></plate>'
            "</config>",
        )
    return buf.getvalue()


class TestEverySliceCallCarriesThePlate:
    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_slice_all_plates_with_arrange_sends_it_for_each_plate(
        self,
        async_client: AsyncClient,
        db_session,
        slice_test_setup,  # noqa: F811
    ):
        """The per-plate loop is its own call site; it used to leave the
        ``bedType`` field out, so only the process-profile patch carried it."""
        src_path = slice_test_setup["tmp_path"] / "library" / "files" / "Two.3mf"
        src_path.write_bytes(_two_plate_3mf())
        source = LibraryFile(
            filename="Two.3mf",
            file_path=str(src_path.relative_to(slice_test_setup["tmp_path"])),
            file_type="3mf",
            file_size=src_path.stat().st_size,
        )
        db_session.add(source)
        await db_session.commit()
        await db_session.refresh(source)

        bodies: list[bytes] = []

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.startswith("/slice/progress/"):
                return httpx.Response(404)
            bodies.append(request.content)
            return httpx.Response(
                200,
                content=_make_3mf_with_settings(),
                headers={"x-print-time-seconds": "1", "x-filament-used-g": "1", "x-filament-used-mm": "1"},
            )

        _install_mock_sidecar(handler)
        response = await async_client.post(
            f"/api/v1/library/files/{source.id}/slice",
            json={
                "printer_preset_id": slice_test_setup["printer_id"],
                "process_preset_id": slice_test_setup["process_id"],
                "filament_preset_id": slice_test_setup["filament_id"],
                "plate": 0,
                "auto_arrange": True,
                "bed_type": "Engineering Plate",
            },
        )
        assert response.status_code == 202, response.text
        # Whether the merge of these stub outputs succeeds is not this test's
        # question; the two per-plate calls are made before it.
        await _wait_for_job(async_client, response.json()["job_id"])

        per_plate = [b for b in bodies if b'name="arrange"' in b]
        assert len(per_plate) == 2, [len(b) for b in bodies]
        for body in per_plate:
            assert b'name="bedType"\r\n\r\nEngineering Plate' in body
