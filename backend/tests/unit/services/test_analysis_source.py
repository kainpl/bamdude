import shutil
import zipfile

import pytest

from backend.app.services.analysis_source import (
    AnalysisSource,
    AnalysisSourceError,
    open_verified_archive,
    resolve_source,
)


def _fixture(tmp_path):
    root = tmp_path / "archive"
    folder = root / "Ноги Пташка"
    folder.mkdir(parents=True)
    path = folder / "part.gcode.3mf"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("Metadata/slice_info.config", "<config/>")
    return root, path


def test_local_source_roundtrip_and_single_verified_handle(tmp_path):
    root, path = _fixture(tmp_path)
    source = resolve_source(
        base_dir=tmp_path,
        archive_dir=root,
        archive_file_path=str(path.relative_to(tmp_path)),
        plate_id=1,
        token="context:1",
    )
    assert AnalysisSource.from_payload(source.to_payload()) == source
    with open_verified_archive(source) as archive:
        assert archive.read("Metadata/slice_info.config") == b"<config/>"


def test_source_descriptor_rejects_non_object_payload():
    with pytest.raises(AnalysisSourceError, match="invalid local source descriptor"):
        AnalysisSource.from_payload([])


def test_local_source_rejects_outside_root_and_replacement(tmp_path):
    root, path = _fixture(tmp_path)
    outside = tmp_path / "outside.3mf"
    outside.write_bytes(path.read_bytes())
    with pytest.raises(AnalysisSourceError, match="outside storage"):
        resolve_source(base_dir=tmp_path, archive_dir=root, archive_file_path="outside.3mf", plate_id=None, token="a")
    source = resolve_source(
        base_dir=tmp_path,
        archive_dir=root,
        archive_file_path=str(path.relative_to(tmp_path)),
        plate_id=None,
        token="a",
    )
    with path.open("ab") as handle:
        handle.write(b"changed")
    with pytest.raises(AnalysisSourceError, match="changed before parsing"), open_verified_archive(source):
        pass


def test_readonly_handle_allows_unlink_and_directory_cleanup_after_close(tmp_path):
    root, path = _fixture(tmp_path)
    source = resolve_source(
        base_dir=tmp_path,
        archive_dir=root,
        archive_file_path=str(path.relative_to(tmp_path)),
        plate_id=1,
        token="context:1",
    )
    with open_verified_archive(source) as archive:
        path.unlink()  # Windows opener must include FILE_SHARE_DELETE.
        assert archive.read("Metadata/slice_info.config") == b"<config/>"
        # Windows delete-pending behaviour may keep the parent directory until
        # handle close; do not pretend unlink alone proves retention success.
        try:
            shutil.rmtree(path.parent)
        except OSError:
            pass
    if path.parent.exists():
        shutil.rmtree(path.parent)
    assert not path.parent.exists()
