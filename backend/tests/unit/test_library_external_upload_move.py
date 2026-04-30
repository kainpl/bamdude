"""Unit tests for the external-folder upload + cross-boundary move helpers.

Covers upstream #1112 / #1112-follow-up: ``_resolve_upload_destination``
must write through to writable external mounts (and reject path traversal /
collisions / read-only / missing paths), while ``_move_file_bytes`` must
physically copy the file when the move straddles the managed↔external
boundary.
"""

import os
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

from backend.app.api.routes.library import (
    _move_file_bytes,
    _MoveSkip,
    _resolve_upload_destination,
    _stored_file_path,
)


def _make_folder(*, is_external: bool, external_path: str | None, readonly: bool = False) -> MagicMock:
    folder = MagicMock()
    folder.is_external = is_external
    folder.external_readonly = readonly
    folder.external_path = external_path
    return folder


def _make_file(*, filename: str, file_path: str, is_external: bool = False) -> MagicMock:
    f = MagicMock()
    f.filename = filename
    f.file_path = file_path
    f.is_external = is_external
    f.folder = None
    return f


# ---------- _resolve_upload_destination ----------


def test_managed_target_returns_uuid_path_in_library_dir(tmp_path, monkeypatch):
    monkeypatch.setattr("backend.app.api.routes.library.get_library_files_dir", lambda: tmp_path)
    dest, is_ext = _resolve_upload_destination(None, "model.3mf")
    assert is_ext is False
    assert dest.parent == tmp_path
    assert dest.suffix == ".3mf"
    assert dest.stem != "model"  # uuid-scoped


def test_external_writable_writes_through_to_mount(tmp_path):
    folder = _make_folder(is_external=True, external_path=str(tmp_path))
    dest, is_ext = _resolve_upload_destination(folder, "model.3mf")
    assert is_ext is True
    assert dest == (tmp_path / "model.3mf").resolve()


def test_external_readonly_returns_403():
    folder = _make_folder(is_external=True, external_path="/some/path", readonly=True)
    with pytest.raises(HTTPException) as exc:
        _resolve_upload_destination(folder, "model.3mf")
    assert exc.value.status_code == 403


def test_external_missing_path_returns_400():
    folder = _make_folder(is_external=True, external_path=None)
    with pytest.raises(HTTPException) as exc:
        _resolve_upload_destination(folder, "model.3mf")
    assert exc.value.status_code == 400


def test_external_inaccessible_path_returns_400(tmp_path):
    bogus = tmp_path / "does-not-exist"
    folder = _make_folder(is_external=True, external_path=str(bogus))
    with pytest.raises(HTTPException) as exc:
        _resolve_upload_destination(folder, "model.3mf")
    assert exc.value.status_code == 400


def test_external_collision_returns_409(tmp_path):
    (tmp_path / "model.3mf").write_bytes(b"existing")
    folder = _make_folder(is_external=True, external_path=str(tmp_path))
    with pytest.raises(HTTPException) as exc:
        _resolve_upload_destination(folder, "model.3mf")
    assert exc.value.status_code == 409


def test_external_path_traversal_returns_400(tmp_path):
    folder = _make_folder(is_external=True, external_path=str(tmp_path))
    with pytest.raises(HTTPException) as exc:
        _resolve_upload_destination(folder, "../escape.3mf")
    assert exc.value.status_code == 400


# ---------- _stored_file_path ----------


def test_stored_file_path_external_returns_absolute(tmp_path):
    abs_path = tmp_path / "x.3mf"
    assert _stored_file_path(abs_path, is_external=True) == str(abs_path)


# ---------- _move_file_bytes ----------


def test_move_managed_to_external_copies_bytes_and_unlinks(tmp_path, monkeypatch):
    src_dir = tmp_path / "library_files"
    src_dir.mkdir()
    src = src_dir / "abc.3mf"
    src.write_bytes(b"payload")

    monkeypatch.setattr("backend.app.api.routes.library.get_library_files_dir", lambda: src_dir)
    monkeypatch.setattr(
        "backend.app.api.routes.library.to_absolute_path",
        lambda p: src_dir / Path(p).name if p else None,
    )

    file = _make_file(filename="model.3mf", file_path="library_files/abc.3mf", is_external=False)
    target = _make_folder(is_external=True, external_path=str(tmp_path / "ext"))
    (tmp_path / "ext").mkdir()

    new_path = _move_file_bytes(file, target)
    assert new_path == str((tmp_path / "ext" / "model.3mf").resolve())
    assert (tmp_path / "ext" / "model.3mf").read_bytes() == b"payload"
    assert not src.exists()


def test_move_external_to_managed_computes_uuid_path(tmp_path, monkeypatch):
    src = tmp_path / "ext" / "real.3mf"
    src.parent.mkdir()
    src.write_bytes(b"data")

    library_dir = tmp_path / "library_files"
    library_dir.mkdir()
    monkeypatch.setattr("backend.app.api.routes.library.get_library_files_dir", lambda: library_dir)
    monkeypatch.setattr("backend.app.api.routes.library.to_relative_path", lambda p: f"library_files/{Path(p).name}")

    file = _make_file(filename="real.3mf", file_path=str(src), is_external=True)
    new_path = _move_file_bytes(file, target_folder=None)
    assert new_path.startswith("library_files/")
    assert new_path.endswith(".3mf")
    # Original gone, dest exists with bytes
    assert not src.exists()
    moved_name = Path(new_path).name
    assert (library_dir / moved_name).read_bytes() == b"data"


def test_move_source_missing_raises_skip(monkeypatch):
    monkeypatch.setattr("backend.app.api.routes.library.to_absolute_path", lambda p: Path("/nope"))
    file = _make_file(filename="x.3mf", file_path="missing", is_external=False)
    with pytest.raises(_MoveSkip) as exc:
        _move_file_bytes(file, target_folder=None)
    assert exc.value.code == "source_missing"


def test_move_to_external_collision_raises_skip(tmp_path, monkeypatch):
    src_dir = tmp_path / "library_files"
    src_dir.mkdir()
    src = src_dir / "abc.3mf"
    src.write_bytes(b"src")
    monkeypatch.setattr(
        "backend.app.api.routes.library.to_absolute_path",
        lambda p: src,
    )
    ext_dir = tmp_path / "ext"
    ext_dir.mkdir()
    (ext_dir / "model.3mf").write_bytes(b"existing")
    file = _make_file(filename="model.3mf", file_path="library_files/abc.3mf", is_external=False)
    target = _make_folder(is_external=True, external_path=str(ext_dir))
    with pytest.raises(_MoveSkip) as exc:
        _move_file_bytes(file, target)
    assert exc.value.code == "name_collision"


def test_move_to_external_readonly_raises_skip(tmp_path, monkeypatch):
    src_dir = tmp_path / "library_files"
    src_dir.mkdir()
    src = src_dir / "abc.3mf"
    src.write_bytes(b"src")
    monkeypatch.setattr("backend.app.api.routes.library.to_absolute_path", lambda p: src)
    target = _make_folder(is_external=True, external_path=str(tmp_path), readonly=True)
    file = _make_file(filename="x.3mf", file_path="library_files/abc.3mf", is_external=False)
    with pytest.raises(_MoveSkip) as exc:
        _move_file_bytes(file, target)
    assert exc.value.code == "target_readonly"


@pytest.mark.skipif(os.name == "nt", reason="POSIX permissions")
def test_move_to_unwritable_external_raises_skip(tmp_path, monkeypatch):
    src_dir = tmp_path / "library_files"
    src_dir.mkdir()
    src = src_dir / "abc.3mf"
    src.write_bytes(b"src")
    monkeypatch.setattr("backend.app.api.routes.library.to_absolute_path", lambda p: src)
    ext_dir = tmp_path / "ext"
    ext_dir.mkdir(mode=0o555)
    try:
        target = _make_folder(is_external=True, external_path=str(ext_dir))
        file = _make_file(filename="x.3mf", file_path="library_files/abc.3mf", is_external=False)
        with pytest.raises(_MoveSkip) as exc:
            _move_file_bytes(file, target)
        assert exc.value.code == "target_unwritable"
    finally:
        ext_dir.chmod(0o755)
