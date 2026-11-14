"""What main takes from the packing child's directory: nothing the child says is trusted (the child parses
untrusted meshes, so its result file and its outputs are treated as hostile)."""

import hashlib
import re
from pathlib import Path

import pytest

from backend.app.schemas.assembly import AssemblyLayout, AssemblyPrinter
from backend.app.services.assembly import job
from backend.app.services.assembly.job import Plan, Row, Source, _counts, _read_member, _stage, _take_packed

REF = {"source": "local", "id": "1"}
TARGET = AssemblyPrinter(printer_preset=REF, process_preset=REF, filament_preset=REF)


def _plan(split=False, sources=()) -> Plan:
    return Plan(
        mode="new",
        name="jar",
        parts=({"name": "a.stl", "quantity": 1, "fill": False, "orientation": "as_is"},) * 2,
        sources=tuple(sources),
        printers=(TARGET,),
        layout=AssemblyLayout(split_plates=split),
        folder_id=None,
        authority=None,
        owner_id=None,
        cloud_user_id=None,
    )


def _row() -> Row:
    return Row(key="p0", request=TARGET, label="X1")


@pytest.fixture
def root(tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    for name in ("p0.3mf", "p0_1.3mf", "p0_2.3mf", "p0_plate_1.png", "p1.3mf"):
        (out / name).write_bytes(b"PK" + name.encode())
    (tmp_path / "secret.3mf").write_bytes(b"secret")
    return tmp_path


def _packed(files, **extra) -> dict:
    return {"status": "ok", "files": files, "plates": [], **extra}


def test_a_good_row_names_its_files_without_reading_them(root):
    row = _row()
    taken = _take_packed(_plan(), row, _packed([{"member": "out/p0.3mf", "plate": None}]), root, [0])
    assert taken == [("out/p0.3mf", None)] and row.status == "ok"
    split = _take_packed(
        _plan(split=True),
        _row(),
        _packed([{"member": "out/p0_1.3mf", "plate": 0}, {"member": "out/p0_2.3mf", "plate": 1}]),
        root,
        [0],
    )
    assert split == [("out/p0_1.3mf", 0), ("out/p0_2.3mf", 1)]


@pytest.mark.parametrize(
    "files",
    [
        [],
        "out/p0.3mf",
        [{"member": "out/p0.3mf", "plate": None}] * 2,  # the same file twice
        [{"member": "out/p0.3mf", "plate": None}, {"member": "out/p0_1.3mf", "plate": None}],  # two for one file
        [{"member": "out/p1.3mf", "plate": None}],  # another row's file
        [{"member": "secret.3mf", "plate": None}],
        [{"member": "out/../secret.3mf", "plate": None}],
        [{"member": "out/p0_9.3mf", "plate": None}],  # not there
        [{"member": "out/p0.3mf", "plate": -1}],
        [{"member": "out/p0.3mf", "plate": "0"}],
        [{"member": 7, "plate": None}],
        ["out/p0.3mf"],
    ],
)
def test_anything_else_fails_the_row_and_takes_nothing(root, files):
    row = _row()
    assert _take_packed(_plan(), row, _packed(files), root, [0]) == []
    assert (row.status, row.reason) == ("failed", "bad_result")


def test_a_split_row_cannot_name_more_files_than_plates_exist(root, monkeypatch):
    monkeypatch.setattr(job, "MAX_ROW_FILES", 1)
    row = _row()
    files = [{"member": "out/p0_1.3mf", "plate": 0}, {"member": "out/p0_2.3mf", "plate": 1}]
    assert _take_packed(_plan(split=True), row, _packed(files), root, [0]) == []
    assert row.reason == "bad_result"


@pytest.mark.parametrize("packed", [None, [], {"status": "published"}, {"status": "failed", "reason": "../x"}])
def test_a_result_of_another_shape_is_a_bad_result(root, packed):
    row = _row()
    assert _take_packed(_plan(), row, packed, root, [0]) == []
    assert row.status == "failed" and row.reason == "bad_result"


def test_a_failed_row_keeps_only_closed_words_and_our_own_part_names(root):
    row = _row()
    packed = {
        "status": "failed",
        "reason": "part_rejected",
        "parts": [
            {"index": 1, "name": "C:/secret/path.stl", "reason": "too_large"},
            {"index": 9, "name": "x", "reason": "too_large"},
            {"index": 0, "name": "x", "reason": "a sentence with a path"},
            "nonsense",
        ],
    }
    assert _take_packed(_plan(), row, packed, root, [0]) == []
    assert row.reason == "part_rejected"
    assert row.parts == [
        {"index": 1, "name": "a.stl", "reason": "too_large"},
        {"index": 0, "name": "a.stl", "reason": ""},
    ]


def test_counts_survive_keys_that_only_look_like_numbers():
    assert _counts({"0": 2, "1": 3, "\u00b2": 4, "7": 1, "x": 1, "1 ": 1, "-1": 1}, 2) == {"0": 2, "1": 3}
    assert _counts({"0": True, "1": 0}, 2) == {}
    assert _counts(["0"], 2) == {}


def test_a_member_is_read_only_under_its_cap_and_inside_the_directory(root):
    pattern = re.compile(r"out/p0(_\d+)?\.3mf")
    assert _read_member(root, "out/p0.3mf", pattern, 100) == b"PKp0.3mf"
    assert _read_member(root, "out/p0.3mf", pattern, 3) is None
    assert _read_member(root, "out/../secret.3mf", pattern, 100) is None
    assert _read_member(root, "out/p0_5.3mf", pattern, 100) is None
    assert _read_member(root, None, pattern, 100) is None


def _source(tmp_path: Path, data: bytes, claimed: bytes | None = None) -> Source:
    path = tmp_path / "model.stl"
    path.write_bytes(data)
    digest = hashlib.sha256(claimed if claimed is not None else data).hexdigest()
    return Source((1, "model.stl", digest, None), path, "stl", "model.stl")


def test_staging_refuses_bytes_that_are_not_the_ones_the_caller_chose(tmp_path):
    root = tmp_path / "stage"
    with pytest.raises(job.SourceChanged):
        _stage(root, _plan(sources=[_source(tmp_path, b"new bytes", claimed=b"old bytes")]), [])


def test_staging_copies_a_source_whose_hash_matches_or_is_unknown(tmp_path):
    _stage(tmp_path / "a", _plan(sources=[_source(tmp_path, b"bytes")]), [])
    assert (tmp_path / "a" / "src" / "0.stl").read_bytes() == b"bytes"
    unknown = Source((1, "model.stl", None, None), tmp_path / "model.stl", "stl", "model.stl")
    _stage(tmp_path / "b", _plan(sources=[unknown]), [])
    assert (tmp_path / "b" / "src" / "0.stl").is_file()


def test_staging_refuses_a_source_larger_than_the_request_allowed(tmp_path, monkeypatch):
    monkeypatch.setattr(job, "MAX_SOURCE_BYTES", 4)
    with pytest.raises(ValueError):
        _stage(tmp_path / "a", _plan(sources=[_source(tmp_path, b"too many bytes")]), [])
