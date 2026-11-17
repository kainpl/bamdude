"""``save_3mf_bytes_to_library(staged=…)``: a file a model import streamed to disk is adopted, not copied through memory."""

from pathlib import Path

import pytest

from backend.app.api.routes.library import save_3mf_bytes_to_library, to_absolute_path
from backend.app.core.config import settings as app_settings

STL = b"solid part\nendsolid part\n" + b" " * 300


@pytest.fixture
def isolated_archive_dir(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(app_settings, "archive_dir", str(tmp_path / "data"), raising=False)
    return tmp_path


@pytest.mark.asyncio
@pytest.mark.integration
async def test_a_staged_file_becomes_the_library_file_and_the_staging_file_is_gone(db_session, isolated_archive_dir):
    staged = isolated_archive_dir / "incoming.part"
    staged.write_bytes(STL)
    result = await save_3mf_bytes_to_library(
        db_session,
        content=None,
        staged=staged,
        filename="part.stl",
        source_type="manyfold",
        source_url="http://manyfold.lan:3214/models/x7q3k2pdbm4z#file-f1c2d3g4h5j6",
    )
    assert result.outcome == "created"
    row = result.file
    assert (row.filename, row.file_type, row.file_size, row.source_type) == ("part.stl", "stl", len(STL), "manyfold")
    assert to_absolute_path(row.file_path).read_bytes() == STL
    assert not staged.exists()


@pytest.mark.asyncio
@pytest.mark.integration
async def test_the_same_bytes_staged_again_are_deduplicated_like_any_other(db_session, isolated_archive_dir):
    first = isolated_archive_dir / "a.part"
    first.write_bytes(STL)
    created = await save_3mf_bytes_to_library(db_session, content=None, staged=first, filename="part.stl")
    second = isolated_archive_dir / "b.part"
    second.write_bytes(STL)
    again = await save_3mf_bytes_to_library(db_session, content=None, staged=second, filename="copy.stl")
    assert again.outcome == "deduped" and again.file.id == created.file.id


def test_a_copy_that_fails_half_way_leaves_no_partial_library_file(tmp_path, monkeypatch):
    """Review MB-I4: staging on another filesystem is copied — a full destination must leave nothing."""
    import errno
    import os
    import shutil

    from backend.app.api.routes import library

    staged, target = tmp_path / "in.part", tmp_path / "library" / "uuid.stl"
    target.parent.mkdir()
    staged.write_bytes(STL)

    def cross_device(_source, _destination):
        raise OSError(errno.EXDEV, "Invalid cross-device link")

    def fills(source, destination):
        Path(destination).write_bytes(Path(source).read_bytes()[:10])
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(os, "replace", cross_device)
    monkeypatch.setattr(shutil, "copyfile", fills)
    with pytest.raises(OSError) as caught:
        library._adopt_staged(target, staged)
    assert caught.value.errno == errno.ENOSPC
    assert not target.exists()
