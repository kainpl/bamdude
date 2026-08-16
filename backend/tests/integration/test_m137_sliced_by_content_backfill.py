"""m137 re-decides "is this 3MF sliced" by opening the existing files.

The tag that gates every Print affordance was derived from the **filename**.
This migration corrects the rows that predate the content check — and, more
importantly, must not invent an answer for the files it cannot read.
"""

from __future__ import annotations

import json
import zipfile

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.app.migrations.m137_sliced_by_content_backfill import seed
from backend.app.models.library import LibraryFile
from backend.app.services.library_helpers import SLICED_GCODE_META_KEY

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


def _write_3mf(path, *, sliced: bool):
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("Metadata/slice_info.config", "<config/>")
        zf.writestr("Metadata/plate_1.gcode" if sliced else "Metadata/plate_1.png", "x")
    return path


async def _row(db_session, *, filename, rel_path, file_type, tags):
    f = LibraryFile(
        filename=filename,
        file_path=rel_path,
        file_type=file_type,
        file_size=1,
        file_tags=tags,
    )
    db_session.add(f)
    await db_session.commit()
    await db_session.refresh(f)
    return f.id


async def _run(test_engine, tmp_path, monkeypatch):
    from backend.app.core.config import settings as app_settings

    monkeypatch.setattr(app_settings, "base_dir", tmp_path)
    await seed(async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False))


async def test_a_model_named_like_a_sliced_file_loses_its_print_tag(db_session, test_engine, tmp_path, monkeypatch):
    """⚠️ The row this migration exists for: named ``.gcode.3mf``, holding no
    G-code at all. It offered a Print button the printer answers thirty
    seconds later with "unable to parse the 3mf file"."""
    _write_3mf(tmp_path / "lib" / "a.gcode.3mf", sliced=False)
    fid = await _row(
        db_session, filename="a.gcode.3mf", rel_path="lib/a.gcode.3mf", file_type="gcode", tags=["gcode", "3mf"]
    )

    await _run(test_engine, tmp_path, monkeypatch)

    db_session.expire_all()
    f = await db_session.scalar(select(LibraryFile).where(LibraryFile.id == fid))
    assert f.file_metadata[SLICED_GCODE_META_KEY] is False
    assert "gcode" not in f.file_tags
    assert "project" in f.file_tags


async def test_a_genuinely_sliced_file_keeps_its_print_tag(db_session, test_engine, tmp_path, monkeypatch):
    _write_3mf(tmp_path / "lib" / "b.gcode.3mf", sliced=True)
    fid = await _row(
        db_session, filename="b.gcode.3mf", rel_path="lib/b.gcode.3mf", file_type="gcode", tags=["gcode", "3mf"]
    )

    await _run(test_engine, tmp_path, monkeypatch)

    db_session.expire_all()
    f = await db_session.scalar(select(LibraryFile).where(LibraryFile.id == fid))
    assert f.file_metadata[SLICED_GCODE_META_KEY] is True
    assert {"gcode", "3mf"} <= set(f.file_tags)


async def test_a_plain_3mf_that_is_actually_sliced_gains_the_print_tag(db_session, test_engine, tmp_path, monkeypatch):
    """The mirror case — sliced output saved without the compound suffix."""
    _write_3mf(tmp_path / "lib" / "c.3mf", sliced=True)
    fid = await _row(db_session, filename="c.3mf", rel_path="lib/c.3mf", file_type="3mf", tags=["3mf", "project"])

    await _run(test_engine, tmp_path, monkeypatch)

    db_session.expire_all()
    f = await db_session.scalar(select(LibraryFile).where(LibraryFile.id == fid))
    assert "gcode" in f.file_tags
    assert "project" not in f.file_tags


async def test_a_missing_file_is_left_exactly_as_it_was(db_session, test_engine, tmp_path, monkeypatch):
    """⚠️ Not evidence of anything. An external mount that is not attached, or
    a file deleted off disk, must not be recorded as "contains no G-code" —
    that would strip a Print button from a file nobody has looked at."""
    fid = await _row(
        db_session, filename="gone.gcode.3mf", rel_path="lib/gone.gcode.3mf", file_type="gcode", tags=["gcode", "3mf"]
    )

    await _run(test_engine, tmp_path, monkeypatch)

    db_session.expire_all()
    f = await db_session.scalar(select(LibraryFile).where(LibraryFile.id == fid))
    assert (f.file_metadata or {}).get(SLICED_GCODE_META_KEY) is None
    assert {"gcode", "3mf"} <= set(f.file_tags)


async def test_a_corrupt_archive_is_left_alone_too(db_session, test_engine, tmp_path, monkeypatch):
    broken = tmp_path / "lib" / "broken.gcode.3mf"
    broken.parent.mkdir(parents=True, exist_ok=True)
    broken.write_bytes(b"not a zip")
    fid = await _row(
        db_session,
        filename="broken.gcode.3mf",
        rel_path="lib/broken.gcode.3mf",
        file_type="gcode",
        tags=["gcode", "3mf"],
    )

    await _run(test_engine, tmp_path, monkeypatch)

    db_session.expire_all()
    f = await db_session.scalar(select(LibraryFile).where(LibraryFile.id == fid))
    assert (f.file_metadata or {}).get(SLICED_GCODE_META_KEY) is None


async def test_existing_metadata_survives(db_session, test_engine, tmp_path, monkeypatch):
    """The flag is added to what is there, not written over it."""
    _write_3mf(tmp_path / "lib" / "d.gcode.3mf", sliced=True)
    f = LibraryFile(
        filename="d.gcode.3mf",
        file_path="lib/d.gcode.3mf",
        file_type="gcode",
        file_size=1,
        file_tags=["gcode", "3mf"],
        file_metadata={"print_time_seconds": 1234, "is_multi_plate": True},
    )
    db_session.add(f)
    await db_session.commit()
    await db_session.refresh(f)
    fid = f.id

    await _run(test_engine, tmp_path, monkeypatch)

    db_session.expire_all()
    got = await db_session.scalar(select(LibraryFile).where(LibraryFile.id == fid))
    assert got.file_metadata["print_time_seconds"] == 1234
    assert got.file_metadata["is_multi_plate"] is True
    assert got.file_metadata[SLICED_GCODE_META_KEY] is True


async def test_non_3mf_rows_are_not_touched(db_session, test_engine, tmp_path, monkeypatch):
    fid = await _row(db_session, filename="part.stl", rel_path="lib/part.stl", file_type="stl", tags=["stl"])

    await _run(test_engine, tmp_path, monkeypatch)

    db_session.expire_all()
    f = await db_session.scalar(select(LibraryFile).where(LibraryFile.id == fid))
    assert f.file_metadata is None
    assert f.file_tags == ["stl"]


async def test_the_json_column_reads_back_as_a_dict_not_a_string(db_session, test_engine, tmp_path, monkeypatch):
    """⚠️ The migration writes with raw SQL and json.dumps. If the column were
    handed a string on a backend that stores JSON natively, every later reader
    would get a str and ``.get`` would explode far from here."""
    _write_3mf(tmp_path / "lib" / "e.gcode.3mf", sliced=True)
    fid = await _row(db_session, filename="e.gcode.3mf", rel_path="lib/e.gcode.3mf", file_type="gcode", tags=["gcode"])

    await _run(test_engine, tmp_path, monkeypatch)

    db_session.expire_all()
    f = await db_session.scalar(select(LibraryFile).where(LibraryFile.id == fid))
    assert isinstance(f.file_metadata, dict), f"got {type(f.file_metadata).__name__}"
    assert isinstance(f.file_tags, list)
    assert json.dumps(f.file_metadata)  # serialisable, i.e. not nested strings
