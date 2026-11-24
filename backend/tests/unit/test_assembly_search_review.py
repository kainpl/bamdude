"""Exercise real PlateNest recovery through the assembly worker and 3MF writer."""

import json
import math
from types import SimpleNamespace

import numpy as np
import spyrrow
from platenest import Options, PackResult, Part, Placement, Zone
from platenest.bambu3mf import read_project
from platenest.optimization import _geometry, _valid

from backend.app.assembly_child import run
from backend.tests.unit.test_assembly_child import SHAPES, _box, _printer, stage


def _validate_project(project, width):
    parts = [
        Part(np.asarray(o.vertices), np.asarray(o.triangles), quantity=sum(i.object == n for i in project.instances))
        for n, o in enumerate(project.objects)
    ]
    options = Options(spacing=2)
    outlines, region = _geometry(parts, Zone(width, width), 2)
    placements = tuple(Placement(i.object, i.plate, i.x, i.y, i.angle) for i in project.instances)
    assert _valid(PackResult(1, placements), parts, outlines, region, options, True)


def test_between_seed_angle_publishes_readable_spaced_project(tmp_path, monkeypatch):
    angle = math.radians(7.5)

    def rotate(v):
        x, y, z = v
        return x * math.cos(angle) - y * math.sin(angle), x * math.sin(angle) + y * math.cos(angle), z

    monkeypatch.setitem(SHAPES, "bar", [tuple(rotate(v) for v in tri) for tri in _box(100, 3, 2)])
    # Limit only the soft search budget; the real worker, optimizer, validator
    # and writer run unchanged, with the original 2 mm clearance.
    import platenest

    original = platenest.optimize
    monkeypatch.setattr(platenest, "optimize", lambda *a, **kw: original(*a, **{**kw, "seconds": 2}))
    result = run(stage(tmp_path, [("bar", 1)], [_printer("p0", 77)], layout={"long_search": True, "rotation_step": -1}))
    assert result["outcome"] == "ok" and result["printers"][0]["status"] == "ok"
    project = read_project((tmp_path / result["printers"][0]["files"][0]["member"]).read_bytes())
    assert len(project.instances) == 1
    _validate_project(project, 77)


def test_optional_native_failure_publishes_valid_best_and_diagnostic(tmp_path, monkeypatch):
    def fail(config):
        (tmp_path / "finish").touch()
        raise ValueError("injected recoverable native failure")

    monkeypatch.setattr(spyrrow, "StripPackingInstance", lambda *a: SimpleNamespace(solve=fail))
    result = run(
        stage(tmp_path, [("lid", "fill")], [_printer("p0", 55)], layout={"long_search": True, "rotation_step": -1})
    )
    assert result["outcome"] == "ok" and result["printers"][0]["status"] == "ok"
    project = read_project((tmp_path / result["printers"][0]["files"][0]["member"]).read_bytes())
    assert len(project.instances) == 4
    _validate_project(project, 55)
    assert "injected recoverable" in json.loads((tmp_path / "progress.json").read_text())["search_diagnostic"]
