"""Unit tests for :mod:`backend.app.services.library_3mf_preview`.

Covers the pure-bytes helpers (preview detection + injection) and the
async ``inject_source_stl_preview`` dispatcher across the supported
branches:

- non-STL source falls through unchanged.
- sliced 3MF that already carries a preview falls through unchanged.
- STL source with an existing on-disk thumbnail reuses it.
- STL source without a thumbnail triggers on-the-fly render and the
  generated PNG is persisted on the source row.
- on-disk STL missing → no-op.
- generate_stl_thumbnail returning ``None`` → no-op.
"""

from __future__ import annotations

import io
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from backend.app.services.library_3mf_preview import (
    _has_3mf_preview,
    _inject_preview,
    inject_source_stl_preview,
)


def _build_3mf(extra_files: dict[str, bytes] | None = None) -> bytes:
    """Return a minimal valid 3MF zip with optional extra entries."""
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", b"<Types/>")
        zf.writestr("3D/3dmodel.model", b"<model/>")
        for name, data in (extra_files or {}).items():
            zf.writestr(name, data)
    return out.getvalue()


def _names(zip_bytes: bytes) -> set[str]:
    with zipfile.ZipFile(io.BytesIO(zip_bytes), "r") as zf:
        return set(zf.namelist())


def _entry(zip_bytes: bytes, name: str) -> bytes:
    with zipfile.ZipFile(io.BytesIO(zip_bytes), "r") as zf:
        return zf.read(name)


class TestHas3mfPreview:
    def test_returns_true_when_plate_png_present(self) -> None:
        content = _build_3mf({"Metadata/plate_1.png": b"PNG"})
        assert _has_3mf_preview(content) is True

    def test_returns_true_when_any_preview_slot_present(self) -> None:
        for slot in ("Metadata/plate_1.png", "Metadata/top_1.png", "Metadata/pick_1.png"):
            content = _build_3mf({slot: b"PNG"})
            assert _has_3mf_preview(content) is True, f"slot {slot} not detected"

    def test_returns_false_when_no_preview(self) -> None:
        content = _build_3mf()
        assert _has_3mf_preview(content) is False

    def test_returns_false_on_corrupt_zip(self) -> None:
        assert _has_3mf_preview(b"not a zip") is False


class TestInjectPreview:
    def test_adds_all_three_preview_slots(self) -> None:
        content = _build_3mf()
        png = b"\x89PNG\r\n\x1a\nfake-image-bytes"

        rewritten = _inject_preview(content, png)

        names = _names(rewritten)
        for slot in ("Metadata/plate_1.png", "Metadata/top_1.png", "Metadata/pick_1.png"):
            assert slot in names, f"missing {slot}"
            assert _entry(rewritten, slot) == png

    def test_preserves_existing_entries(self) -> None:
        content = _build_3mf({"Metadata/project_settings.config": b"{}"})
        rewritten = _inject_preview(content, b"PNG")
        assert _entry(rewritten, "Metadata/project_settings.config") == b"{}"
        assert _entry(rewritten, "3D/3dmodel.model") == b"<model/>"

    def test_overwrites_existing_preview(self) -> None:
        content = _build_3mf({"Metadata/plate_1.png": b"OLD"})
        rewritten = _inject_preview(content, b"NEW")
        assert _entry(rewritten, "Metadata/plate_1.png") == b"NEW"

    def test_corrupt_input_returns_original(self) -> None:
        garbage = b"not a zip"
        assert _inject_preview(garbage, b"PNG") is garbage


class TestInjectSourceStlPreview:
    """End-to-end async dispatcher behaviour."""

    @pytest.mark.asyncio
    async def test_non_stl_source_passes_through(self) -> None:
        sliced = _build_3mf()
        src = SimpleNamespace(id=1, filename="model.3mf", thumbnail_path=None, file_path="model.3mf")
        db = AsyncMock()

        out = await inject_source_stl_preview(sliced_3mf_bytes=sliced, source_library_file=src, db=db)

        assert out is sliced
        db.flush.assert_not_called()

    @pytest.mark.asyncio
    async def test_already_has_preview_passes_through(self) -> None:
        sliced = _build_3mf({"Metadata/plate_1.png": b"existing"})
        src = SimpleNamespace(id=1, filename="model.stl", thumbnail_path=None, file_path="model.stl")
        db = AsyncMock()

        out = await inject_source_stl_preview(sliced_3mf_bytes=sliced, source_library_file=src, db=db)

        assert out is sliced
        db.flush.assert_not_called()

    @pytest.mark.asyncio
    async def test_uses_existing_source_thumbnail(self, tmp_path: Path) -> None:
        thumb = tmp_path / "thumb.png"
        thumb.write_bytes(b"PNG-from-source")

        sliced = _build_3mf()
        src = SimpleNamespace(
            id=1,
            filename="model.stl",
            thumbnail_path=str(thumb),  # absolute → to_absolute_path returns as-is
            file_path="ignored",
        )
        db = AsyncMock()

        out = await inject_source_stl_preview(sliced_3mf_bytes=sliced, source_library_file=src, db=db)

        assert out != sliced
        assert _entry(out, "Metadata/plate_1.png") == b"PNG-from-source"
        # No render needed → no flush + thumbnail_path unchanged
        db.flush.assert_not_called()

    @pytest.mark.asyncio
    async def test_generates_when_source_missing_thumbnail(self, tmp_path: Path) -> None:
        stl = tmp_path / "model.stl"
        stl.write_bytes(b"solid mock\nendsolid mock\n")
        rendered = tmp_path / "rendered.png"
        rendered.write_bytes(b"PNG-rendered")

        sliced = _build_3mf()
        src = SimpleNamespace(
            id=42,
            filename="model.stl",
            thumbnail_path=None,
            file_path=str(stl),
        )
        db = AsyncMock()

        with patch(
            "backend.app.services.stl_thumbnail.generate_stl_thumbnail",
            return_value=str(rendered),
        ):
            out = await inject_source_stl_preview(sliced_3mf_bytes=sliced, source_library_file=src, db=db)

        assert out != sliced
        assert _entry(out, "Metadata/plate_1.png") == b"PNG-rendered"
        # Source row was updated and flushed so the STL itself lights up
        # in the listing and subsequent slices reuse the same PNG.
        assert src.thumbnail_path  # populated
        db.flush.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_missing_stl_on_disk_is_noop(self, tmp_path: Path) -> None:
        sliced = _build_3mf()
        src = SimpleNamespace(
            id=42,
            filename="model.stl",
            thumbnail_path=None,
            file_path=str(tmp_path / "does_not_exist.stl"),
        )
        db = AsyncMock()

        out = await inject_source_stl_preview(sliced_3mf_bytes=sliced, source_library_file=src, db=db)

        assert out is sliced
        db.flush.assert_not_called()

    @pytest.mark.asyncio
    async def test_render_failure_is_noop(self, tmp_path: Path) -> None:
        stl = tmp_path / "model.stl"
        stl.write_bytes(b"junk")

        sliced = _build_3mf()
        src = SimpleNamespace(id=42, filename="model.stl", thumbnail_path=None, file_path=str(stl))
        db = AsyncMock()

        with patch("backend.app.services.stl_thumbnail.generate_stl_thumbnail", return_value=None):
            out = await inject_source_stl_preview(sliced_3mf_bytes=sliced, source_library_file=src, db=db)

        assert out is sliced
        db.flush.assert_not_called()
