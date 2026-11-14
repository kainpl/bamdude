"""``prepare_file`` marks a project assembled here, and only one whose record really is a record."""

import io
import zipfile

import numpy as np
from platenest.bambu3mf import Instance, MeshObject, write_project

from backend.app.services import library_file_preparation
from backend.app.services.assembly import record as rec
from backend.app.services.library_file_preparation import prepare_file

REF = {"source": "standard", "id": "X"}


def _record() -> bytes:
    return rec.dump(
        rec.AssemblyRecord(
            set_id="c" * 32,
            base_name="jar",
            parts=(rec.PartInput("jar.stl", 1, False, "as_is"),),
            printer=rec.PrinterInput(REF, REF, REF, 1, "X1"),
            layout=rec.LayoutInput(2.0, 90, True, False),
            plates=({"plate": 0, "counts": {"0": 1}, "fill": False},),
        )
    )


def _assembled(extra: dict) -> bytes:
    vertices = np.array([[0, 0, 0], [10, 0, 0], [0, 10, 0], [0, 0, 10]], dtype=float)
    triangles = np.array([[0, 2, 1], [0, 1, 3], [1, 2, 3], [0, 3, 2]])
    return write_project(
        [MeshObject("jar.stl", vertices, triangles)],
        [Instance(0, 0, 20.0, 20.0, 0.0)],
        bed_size=(256.0, 256.0),
        project_settings=b"{}",
        title="jar",
        extra_files=extra,
    )


def _prepare(tmp_path, data: bytes) -> dict:
    path = tmp_path / "file.3mf"
    path.write_bytes(data)
    return prepare_file(path, root=tmp_path).metadata


def _without(data: bytes, member: str) -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(data)) as source, zipfile.ZipFile(out, "w") as target:
        for info in source.infolist():
            if info.filename != member:
                target.writestr(info, source.read(info.filename))
    return out.getvalue()


def test_prepare_marks_an_assembled_project(tmp_path):
    metadata = _prepare(tmp_path, _assembled({rec.RECORD_MEMBER: _record()}))
    assert metadata["assembled"] is True and metadata["assembly_set"] == "c" * 32


def test_prepare_ignores_a_fake_record(tmp_path):
    for fake in (b"{}", b"not json", b'{"version": 1}', b" " * (rec.MAX_RECORD_BYTES + 1)):
        metadata = _prepare(tmp_path, _assembled({rec.RECORD_MEMBER: fake}))
        assert "assembled" not in metadata and "assembly_set" not in metadata


def test_a_record_without_the_library_marker_is_not_an_assembly(tmp_path):
    data = _without(_assembled({rec.RECORD_MEMBER: _record()}), "Metadata/platenest.json")
    assert "assembled" not in _prepare(tmp_path, data)


def test_a_project_written_without_a_record_is_not_marked(tmp_path):
    assert "assembled" not in _prepare(tmp_path, _assembled({}))


def test_the_extraction_version_did_not_move():
    assert library_file_preparation.EXTRACTION_VERSION == 1
