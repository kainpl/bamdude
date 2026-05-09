"""Integration tests for /archives/{id}/gcode + /archives/{id}/plates.

Covers the two archive-side endpoints that the in-modal G-code viewer +
ModelViewerModal rely on:

1. ``/archives/{id}/gcode?plate=N`` — extracts the requested plate's
   ``Metadata/plate_<N>.gcode`` from the archive zip. Zero-padded names
   (``plate_01.gcode``) must resolve as plate 1 to match what the
   ``/plates`` endpoint reports. Negative or zero ``?plate=`` values
   must 400, and a missing plate index must 404.
2. ``/archives/{id}/plates`` ``has_gcode`` flag — gates the modal's
   "G-code" tab so source-only 3MFs (PNG/JSON only, no actual gcode)
   surface a "no gcode" state instead of opening a tab that 404s on
   every plate select.
"""

import zipfile
from pathlib import Path

import pytest
from httpx import AsyncClient


def _write_3mf(
    path: Path,
    plate_gcode: dict[int, str] | None = None,
    plate_filenames: dict[int, str] | None = None,
    include_png_for: list[int] | None = None,
) -> None:
    """Write a synthetic Bambu-style 3MF zip at *path*."""
    plate_gcode = plate_gcode or {}
    plate_filenames = plate_filenames or {}
    include_png_for = include_png_for or []
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        for idx, text in plate_gcode.items():
            zf.writestr(f"Metadata/plate_{idx}.gcode", text)
        for idx, filename in plate_filenames.items():
            zf.writestr(f"Metadata/{filename}", f"; stub for plate {idx}\n")
        for idx in include_png_for:
            zf.writestr(f"Metadata/plate_{idx}.png", b"\x89PNG\r\n\x1a\n")
            zf.writestr(f"Metadata/plate_{idx}.json", b'{"bbox_objects": []}')


@pytest.fixture
def _patch_archive_base_dir(monkeypatch, tmp_path):
    """Point archive file_path resolution at *tmp_path* for this test."""
    from backend.app.core.config import settings

    monkeypatch.setattr(settings, "base_dir", tmp_path)
    return tmp_path


class TestArchiveGcodePlateParam:
    """The viewer passes ``?plate=N`` for multi-plate archives."""

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_plate_param_returns_that_plate(
        self,
        async_client: AsyncClient,
        archive_factory,
        printer_factory,
        _patch_archive_base_dir,
    ):
        """GET /archives/{id}/gcode?plate=2 returns Metadata/plate_2.gcode."""
        tmp = _patch_archive_base_dir
        threemf = tmp / "multi.3mf"
        _write_3mf(
            threemf,
            plate_gcode={1: "G0 ; plate 1\n", 2: "G1 X0 Y0 ; plate 2\n"},
        )
        printer = await printer_factory()
        archive = await archive_factory(printer.id, filename="multi.3mf", file_path="multi.3mf")

        response = await async_client.get(f"/api/v1/archives/{archive.id}/gcode?plate=2")

        assert response.status_code == 200
        assert "plate 2" in response.text

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_plate_param_zero_padded_filename_resolves(
        self,
        async_client: AsyncClient,
        archive_factory,
        printer_factory,
        _patch_archive_base_dir,
    ):
        """plate_01.gcode reports as plate 1 from /plates — /gcode?plate=1 must find it.

        Regression: an exact-string match on ``Metadata/plate_1.gcode`` missed
        zero-padded filenames exported by some slicers, so the picker showed
        plate 1 as selectable but the viewer 404'd on selection.
        """
        tmp = _patch_archive_base_dir
        threemf = tmp / "padded.3mf"
        with zipfile.ZipFile(threemf, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("Metadata/plate_01.gcode", "G0 ; padded plate\n")
        printer = await printer_factory()
        archive = await archive_factory(printer.id, filename="padded.3mf", file_path="padded.3mf")

        response = await async_client.get(f"/api/v1/archives/{archive.id}/gcode?plate=1")

        assert response.status_code == 200
        assert "padded plate" in response.text

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_missing_plate_returns_404(
        self,
        async_client: AsyncClient,
        archive_factory,
        printer_factory,
        _patch_archive_base_dir,
    ):
        """Requesting a plate index the archive doesn't contain returns 404."""
        tmp = _patch_archive_base_dir
        threemf = tmp / "only_plate_2.3mf"
        _write_3mf(threemf, plate_gcode={2: "G0\n"})
        printer = await printer_factory()
        archive = await archive_factory(printer.id, filename="only_plate_2.3mf", file_path="only_plate_2.3mf")

        response = await async_client.get(f"/api/v1/archives/{archive.id}/gcode?plate=1")

        assert response.status_code == 404

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_no_plate_param_returns_first_plate(
        self,
        async_client: AsyncClient,
        archive_factory,
        printer_factory,
        _patch_archive_base_dir,
    ):
        """Omitting ?plate falls back to the first gcode in the archive."""
        tmp = _patch_archive_base_dir
        threemf = tmp / "single.3mf"
        _write_3mf(threemf, plate_gcode={1: "G0 ; only plate\n"})
        printer = await printer_factory()
        archive = await archive_factory(printer.id, filename="single.3mf", file_path="single.3mf")

        response = await async_client.get(f"/api/v1/archives/{archive.id}/gcode")

        assert response.status_code == 200
        assert "only plate" in response.text

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_plate_param_rejects_zero_and_negative(
        self,
        async_client: AsyncClient,
        archive_factory,
        printer_factory,
        _patch_archive_base_dir,
    ):
        """``?plate=0`` or negative must 400 — not silently fall through."""
        tmp = _patch_archive_base_dir
        threemf = tmp / "any.3mf"
        _write_3mf(threemf, plate_gcode={1: "G0\n"})
        printer = await printer_factory()
        archive = await archive_factory(printer.id, filename="any.3mf", file_path="any.3mf")

        response = await async_client.get(f"/api/v1/archives/{archive.id}/gcode?plate=0")

        assert response.status_code == 400


class TestArchivePlatesHasGcode:
    """The ``has_gcode`` flag on /plates gates the modal's G-code tab."""

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_has_gcode_true_when_gcode_files_present(
        self,
        async_client: AsyncClient,
        archive_factory,
        printer_factory,
        _patch_archive_base_dir,
    ):
        """Sliced multi-plate 3MF → has_gcode=true."""
        tmp = _patch_archive_base_dir
        threemf = tmp / "sliced.3mf"
        _write_3mf(threemf, plate_gcode={1: "G0\n", 2: "G1\n"})
        printer = await printer_factory()
        archive = await archive_factory(printer.id, filename="sliced.3mf", file_path="sliced.3mf")

        response = await async_client.get(f"/api/v1/archives/{archive.id}/plates")

        assert response.status_code == 200
        data = response.json()
        assert data["has_gcode"] is True

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_has_gcode_false_for_source_only_archive(
        self,
        async_client: AsyncClient,
        archive_factory,
        printer_factory,
        _patch_archive_base_dir,
    ):
        """Source-only 3MF (PNG/JSON only, no gcode) → has_gcode=false.

        The PNG/JSON fallback path on /plates reports plate indices that the
        gcode endpoint can't actually serve, so without ``has_gcode`` the
        modal would surface "G-code" tabs that 404 on every plate select.
        """
        tmp = _patch_archive_base_dir
        threemf = tmp / "project.3mf"
        _write_3mf(threemf, include_png_for=[1, 2, 3])  # no .gcode at all
        printer = await printer_factory()
        archive = await archive_factory(printer.id, filename="project.3mf", file_path="project.3mf")

        response = await async_client.get(f"/api/v1/archives/{archive.id}/plates")

        assert response.status_code == 200
        data = response.json()
        assert data["has_gcode"] is False
        # The endpoint still reports plates (from JSON/PNG) — the flag is what
        # the frontend keys on, not an empty plate list.
        assert len(data["plates"]) == 3
