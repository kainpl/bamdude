"""The packing child of "Assemble project", run in-process on a staged directory."""

import json
import struct
from pathlib import Path

import pytest
from platenest.bambu3mf import read_project

from backend.app.assembly_child import run
from backend.app.services.assembly import record
from backend.app.services.assembly.record import RECORD_MEMBER

REF = {"source": "standard", "id": "X"}


def _box(w, d, h, hole=0.0):
    """Triangles of a box; with ``hole`` it becomes a frame with walls ``hole`` thick."""

    def brick(x0, y0, x1, y1):
        c = [(x0, y0, 0), (x1, y0, 0), (x1, y1, 0), (x0, y1, 0), (x0, y0, h), (x1, y0, h), (x1, y1, h), (x0, y1, h)]
        f = [(0, 1, 2), (0, 2, 3), (4, 5, 6), (4, 6, 7), (0, 1, 5), (0, 5, 4), (1, 2, 6), (1, 6, 5), (2, 3, 7),
             (2, 7, 6), (3, 0, 4), (3, 4, 7)]  # fmt: skip
        return [tuple(c[i] for i in tri) for tri in f]

    if not hole:
        return brick(0, 0, w, d)
    return (brick(0, 0, w, hole) + brick(0, d - hole, w, d) + brick(0, hole, hole, d - hole)
            + brick(w - hole, hole, w, d - hole))  # fmt: skip


def _stl(triangles) -> bytes:
    data = b"\0" * 80 + struct.pack("<I", len(triangles))
    for tri in triangles:
        data += struct.pack("<3f", 0, 0, 0) + b"".join(struct.pack("<3f", *v) for v in tri) + b"\0\0"
    return data


def _obj(triangles) -> bytes:
    lines, n = [], 0
    for tri in triangles:
        lines += [f"v {x} {y} {z}" for x, y, z in tri] + [f"f {n + 1} {n + 2} {n + 3}"]
        n += 3
    return ("\n".join(lines) + "\n").encode()


SHAPES = {
    "frame": _box(60, 60, 4, hole=5),
    "lid": _box(20, 20, 3),
    "big200": _box(200, 200, 5),
    "tall": _box(10, 10, 100),
}


def _printer(key, size, max_height=250.0):
    return {
        "key": key,
        "label": f"P{size}",
        "bed_size": [size, size],
        "zone": {"width": size, "depth": size, "allowed": [], "excluded": [], "max_height": max_height},
        "settings": f"{key}.settings.json",
        "record_printer": {"printer": REF, "process": REF, "filament": REF, "nozzle": 1, "model": f"P{size}"},
    }


def stage(root: Path, parts, printers, *, fmt="stl", layout=None, raw=None) -> Path:
    """``parts``: [(shape name, quantity or "fill", orientation?)]"""
    (root / "src").mkdir(parents=True, exist_ok=True)
    sources, specs = [], []
    for index, part in enumerate(parts):
        name, quantity, orientation = (*part, "as_is")[:3]
        member = f"src/{index}.{fmt}"
        payload = raw if raw is not None else (_stl if fmt == "stl" else _obj)(SHAPES[name])
        (root / member).write_bytes(payload)
        sources.append({"member": member, "format": fmt})
        fill = quantity == "fill"
        specs.append({"name": name, "quantity": 1 if fill else quantity, "fill": fill, "orientation": orientation})
    for printer in printers:
        (root / printer["settings"]).write_bytes(json.dumps({"printer": printer["key"]}).encode())
    job = {
        "mode": "new",
        "sources": sources,
        "parts": specs,
        "layout": {"spacing": 2.0, "rotation_step": 90, "centre": True, "split_plates": False, **(layout or {})},
        "base_name": "demo",
        "printers": printers,
    }
    (root / "job.json").write_text(json.dumps(job), encoding="utf-8")
    return root


def _project(root: Path, member: str):
    return read_project((root / member).read_bytes())


def test_two_parts_one_printer_one_file(tmp_path):
    result = run(stage(tmp_path, [("frame", 1), ("lid", 3)], [_printer("p0", 256)]))
    printer = result["printers"][0]
    assert result["outcome"] == "ok" and printer["status"] == "ok"
    assert [f["plate"] for f in printer["files"]] == [None] and len(printer["plates"]) == 1
    project = _project(tmp_path, printer["files"][0]["member"])
    assert len(project.instances) == 4 and [o.name for o in project.objects] == ["frame", "lid"]
    saved = record.load(project.extra_files[RECORD_MEMBER])
    assert saved.parts[1].quantity == 3 and saved.base_name == "demo" and saved.plates[0]["counts"] == {"0": 1, "1": 3}
    assert project.project_settings == (tmp_path / "p0.settings.json").read_bytes()


def test_small_parts_go_inside_the_frame(tmp_path):
    result = run(stage(tmp_path, [("frame", 1), ("lid", 1)], [_printer("p0", 64)]))
    assert result["printers"][0]["status"] == "ok" and len(result["printers"][0]["plates"]) == 1


def test_split_plates_writes_one_file_per_plate_with_one_set_id(tmp_path):
    result = run(stage(tmp_path, [("big200", 2)], [_printer("p0", 256)], layout={"split_plates": True}))
    files = result["printers"][0]["files"]
    assert [f["plate"] for f in files] == [0, 1] and [f["member"] for f in files] == ["out/p0_1.3mf", "out/p0_2.3mf"]
    projects = [_project(tmp_path, f["member"]) for f in files]
    assert all(len(p.instances) == 1 and p.instances[0].plate == 0 for p in projects)
    records = [record.load(p.extra_files[RECORD_MEMBER]) for p in projects]
    assert records[0].set_id == records[1].set_id and len(records[0].plates) == 2 and records[0].layout.split_plates


def test_fill_part_gets_its_own_plate_and_reports_the_count(tmp_path):
    result = run(stage(tmp_path, [("frame", 1), ("lid", "fill")], [_printer("p0", 100)]))
    printer = result["printers"][0]
    assert printer["fill_counts"] == {"1": 16} and printer["fill_limited"] == []
    assert [p["fill"] for p in printer["plates"]] == [False, True] and printer["plates"][1]["counts"] == {"1": 16}


def test_finish_search_keeps_real_projects_for_every_printer(tmp_path):
    stage(
        tmp_path,
        [("lid", 3)],
        [_printer("p0", 100), _printer("p1", 120)],
        layout={"long_search": True, "rotation_step": -1},
    )
    (tmp_path / "finish").touch()
    result = run(tmp_path)
    assert result["outcome"] == "ok"
    assert len(result["printers"]) == 2
    for printer in result["printers"]:
        assert printer["status"] == "ok"
        project = _project(tmp_path, printer["files"][0]["member"])
        assert len(project.instances) == 3
        saved = record.load(project.extra_files[RECORD_MEMBER])
        assert saved.layout.long_search and saved.layout.rotation_step == -1


def test_one_printer_too_small_fails_alone(tmp_path):
    result = run(stage(tmp_path, [("big200", 1)], [_printer("p0", 180), _printer("p1", 256)]))
    assert result["outcome"] == "ok" and [p["status"] for p in result["printers"]] == ["failed", "ok"]
    assert result["printers"][0]["reason"] == "part_rejected"
    assert result["printers"][0]["parts"] == [{"index": 0, "name": "big200", "reason": "too_large"}]
    assert not list((tmp_path / "out").glob("p0*")) and (tmp_path / "out" / "p1.3mf").is_file()


def test_orientation_lays_a_tall_part_down(tmp_path):
    upright = run(stage(tmp_path / "a", [("tall", 1)], [_printer("p0", 256, max_height=50)]))
    assert upright["printers"][0]["parts"][0]["reason"] == "too_tall"
    laid = run(stage(tmp_path / "b", [("tall", 1, "x+90")], [_printer("p0", 256, max_height=50)]))
    assert laid["printers"][0]["status"] == "ok"
    project = _project(tmp_path / "b", laid["printers"][0]["files"][0]["member"])
    zs = [v[2] for v in project.objects[0].vertices]
    assert min(zs) == 0 and max(zs) == pytest.approx(10)
    assert record.load(project.extra_files[RECORD_MEMBER]).parts[0].orientation == "as_is"  # the mesh is already turned


def test_obj_and_stl_load_alike(tmp_path):
    a = run(stage(tmp_path / "a", [("lid", 2)], [_printer("p0", 100)], fmt="stl"))
    b = run(stage(tmp_path / "b", [("lid", 2)], [_printer("p0", 100)], fmt="obj"))
    pa = _project(tmp_path / "a", a["printers"][0]["files"][0]["member"])
    pb = _project(tmp_path / "b", b["printers"][0]["files"][0]["member"])
    assert pa.instances == pb.instances and len(pa.objects[0].vertices) == len(pb.objects[0].vertices) == 8


def test_unreadable_mesh_fails_with_its_name(tmp_path):
    result = run(stage(tmp_path, [("lid", 1)], [_printer("p0", 100)], raw=b"this is not a mesh at all"))
    assert result["outcome"] == "failed" and result["why"] == "unreadable_mesh" and result["printers"] == []
    assert result["parts"] == [{"index": 0, "name": "lid", "reason": "unreadable_mesh"}]


def test_cancel_file_stops_the_run_and_leaves_no_output(tmp_path):
    stage(tmp_path, [("lid", 5)], [_printer("p0", 100)])
    (tmp_path / "cancel").write_bytes(b"")
    result = run(tmp_path)
    assert result["outcome"] == "canceled" and result["printers"] == []
    assert not list((tmp_path / "out").glob("*")) if (tmp_path / "out").exists() else True


def test_a_passed_deadline_is_a_timeout(tmp_path):
    assert run(stage(tmp_path, [("lid", 5)], [_printer("p0", 100)]), deadline_ns=1)["outcome"] == "timeout"


@pytest.mark.parametrize("job", [b"{", b"[]", json.dumps({"mode": "new", "printers": [], "parts": []}).encode()])
def test_a_bad_job_is_failed_not_a_crash(tmp_path, job):
    (tmp_path / "job.json").write_bytes(job)
    assert run(tmp_path) == {"outcome": "failed", "why": "bad_job", "parts": [], "printers": []}


def test_a_source_outside_the_root_is_refused(tmp_path):
    stage(tmp_path / "job", [("lid", 1)], [_printer("p0", 100)])
    job = json.loads((tmp_path / "job" / "job.json").read_text())
    job["sources"][0]["member"] = "../outside.stl"
    (tmp_path / "outside.stl").write_bytes(_stl(SHAPES["lid"]))
    (tmp_path / "job" / "job.json").write_text(json.dumps(job))
    assert run(tmp_path / "job")["outcome"] == "failed"


def test_relayout_reads_meshes_from_the_previous_files(tmp_path):
    first = run(stage(tmp_path / "a", [("frame", 1), ("lid", 3)], [_printer("p0", 256)], layout={"spacing": 2.0}))
    old = tmp_path / "a" / first["printers"][0]["files"][0]["member"]
    root = tmp_path / "b"
    (root / "src").mkdir(parents=True)
    (root / "src" / "0.3mf").write_bytes(old.read_bytes())
    printer = _printer("p0", 256)
    (root / printer["settings"]).write_bytes(b"{}")
    job = {
        "mode": "relayout",
        "sources": [{"member": "src/0.3mf", "format": "3mf"}],
        "parts": [
            {"name": "frame", "quantity": 1, "fill": False, "orientation": "as_is"},
            {"name": "lid", "quantity": 5, "fill": False, "orientation": "as_is"},
        ],
        "layout": {"spacing": 6.0, "rotation_step": 0, "centre": False, "split_plates": False},
        "base_name": "demo",
        "printers": [printer],
    }
    (root / "job.json").write_text(json.dumps(job))
    result = run(root)
    project = _project(root, result["printers"][0]["files"][0]["member"])
    assert result["outcome"] == "ok" and len(project.instances) == 6
    saved = record.load(project.extra_files[RECORD_MEMBER])
    assert saved.layout.spacing == 6.0 and saved.layout.rotation_step == 0 and saved.parts[1].quantity == 5


def test_relayout_with_a_part_missing_from_the_files_names_it(tmp_path):
    first = run(stage(tmp_path / "a", [("big200", 2)], [_printer("p0", 256)], layout={"split_plates": True}))
    # only plate 1's file of a two-part job where part 1 lives nowhere
    root = tmp_path / "b"
    (root / "src").mkdir(parents=True)
    (root / "src" / "0.3mf").write_bytes((tmp_path / "a" / first["printers"][0]["files"][0]["member"]).read_bytes())
    printer = _printer("p0", 256)
    (root / printer["settings"]).write_bytes(b"{}")
    job = {
        "mode": "relayout",
        "sources": [{"member": "src/0.3mf", "format": "3mf"}],
        "parts": [
            {"name": "big200", "quantity": 1, "fill": False, "orientation": "as_is"},
            {"name": "ghost", "quantity": 1, "fill": False, "orientation": "as_is"},
        ],
        "layout": {"spacing": 2.0, "rotation_step": 90, "centre": True, "split_plates": False},
        "base_name": "demo",
        "printers": [printer],
    }
    (root / "job.json").write_text(json.dumps(job))
    result = run(root)
    assert result["why"] == "mesh_missing" and result["parts"] == [
        {"index": 1, "name": "ghost", "reason": "mesh_missing"}
    ]


def test_pictures_are_png_one_per_plate(tmp_path):
    result = run(stage(tmp_path, [("big200", 2)], [_printer("p0", 256)]))
    plates = result["printers"][0]["plates"]
    assert len(plates) == 2
    for plate in plates:
        assert (tmp_path / plate["picture"]).read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"


def test_progress_is_written_while_packing(tmp_path, monkeypatch):
    monkeypatch.setattr("backend.app.assembly_child._PROGRESS_SECONDS", 0.0)
    run(stage(tmp_path, [("lid", 6)], [_printer("p0", 256)]))
    progress = json.loads((tmp_path / "progress.json").read_text())
    assert progress["printer"] == "p0" and progress["printer_count"] == 1 and progress["total"] == 6


def test_a_progress_file_that_cannot_be_replaced_does_not_fail_the_job(tmp_path, monkeypatch):
    """On Windows the service may hold progress.json open for a read at the very moment it is replaced."""
    from backend.app import assembly_child

    real, refused = assembly_child._write_json, []

    def write(path, payload):
        if path.name == "progress.json":
            refused.append(path)
            raise PermissionError("in use by another process")
        real(path, payload)

    monkeypatch.setattr(assembly_child, "_write_json", write)
    monkeypatch.setattr(assembly_child, "_PROGRESS_SECONDS", 0.0)
    result = run(stage(tmp_path, [("frame", 1), ("lid", 3)], [_printer("p0", 256)]))
    assert refused and result["outcome"] == "ok" and result["printers"][0]["status"] == "ok"


def test_two_plates_that_hold_the_same_parts_are_still_two_different_files(tmp_path):
    """The library reuses a row for bytes it already holds: two full plates of one part, written alike, would
    become ONE file, and the set would then look incomplete for good."""
    import hashlib

    root = stage(tmp_path, [("big200", 2)], [_printer("p0", 256)], layout={"split_plates": True})
    result = run(root)
    files = result["printers"][0]["files"]
    assert len(files) == 2
    digests = {hashlib.sha256((root / entry["member"]).read_bytes()).hexdigest() for entry in files}
    assert len(digests) == 2
