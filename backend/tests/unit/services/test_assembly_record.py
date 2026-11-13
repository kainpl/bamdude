"""The input record an assembled 3MF carries."""

import json

import pytest

from backend.app.services.assembly.record import (
    RECORD_MEMBER,
    AssemblyRecord,
    LayoutInput,
    PartInput,
    PrinterInput,
    dump,
    load,
    rotations,
)

REF = {"source": "standard", "id": "X"}


def _record(**changes) -> AssemblyRecord:
    base = {
        "set_id": "a" * 32,
        "base_name": "jar",
        "parts": (PartInput("jar.stl", 6, False, "as_is"), PartInput("lid.stl", 1, True, "x+90")),
        "printer": PrinterInput(REF, REF, REF, 2, "H2C"),
        "layout": LayoutInput(2.0, 90, True, False),
        "plates": ({"plate": 0, "counts": {"0": 6}, "fill": False}, {"plate": 1, "counts": {"1": 14}, "fill": True}),
    }
    base.update(changes)
    return AssemblyRecord(**base)


def _raw(**changes) -> bytes:
    raw = json.loads(dump(_record()))
    raw.update(changes)
    return json.dumps(raw).encode()


def test_the_member_name_is_fixed():
    assert RECORD_MEMBER == "Metadata/bamdude_assembly.json"


def test_round_trip():
    assert load(dump(_record())) == _record()


def test_rotations_per_step():
    assert rotations(0) == (0.0,) and rotations(180) == (0.0, 180.0)
    assert rotations(90) == (0.0, 90.0, 180.0, 270.0) and len(rotations(45)) == 8
    with pytest.raises(ValueError):
        rotations(30)


@pytest.mark.parametrize(
    "data",
    [
        b"",
        b"{",
        b"[1]",
        pytest.param(b"[" * 100_000, id="deep-nesting"),
        pytest.param(b" " * (256 * 1024 + 1), id="too-large"),
        _raw(version=2),
        _raw(set_id=""),
        _raw(parts=[]),
        _raw(parts="x"),
        _raw(parts=[{"name": "a", "quantity": 0, "fill": False, "orientation": "as_is"}]),
        _raw(parts=[{"name": "a", "quantity": True, "fill": False, "orientation": "as_is"}]),
        _raw(parts=[{"name": "a", "quantity": 1, "fill": False, "orientation": "sideways"}]),
        _raw(parts=[{"name": 5, "quantity": 1, "fill": False, "orientation": "as_is"}]),
        _raw(printer={"printer": REF, "process": REF, "filament": "x", "nozzle": 1, "model": "m"}),
        _raw(printer={"printer": REF, "process": REF, "filament": REF, "nozzle": 0, "model": "m"}),
        _raw(layout={"spacing": -1, "rotation_step": 90, "centre": True, "split_plates": False}),
        _raw(layout={"spacing": 2, "rotation_step": 30, "centre": True, "split_plates": False}),
        _raw(layout={"spacing": 2, "rotation_step": 90, "centre": "yes", "split_plates": False}),
        _raw(plates=[{"plate": 0, "counts": {"7": 1}, "fill": False}]),
        _raw(plates=[{"plate": -1, "counts": {"0": 1}, "fill": False}]),
        _raw(plates=[{"plate": 0, "counts": {"0": 0}, "fill": False}]),
    ],
)
def test_anything_else_is_not_a_record(data):
    with pytest.raises(ValueError, match="not an assembly record"):
        load(data)


def test_a_missing_key_is_named():
    raw = json.loads(dump(_record()))
    del raw["layout"]
    with pytest.raises(ValueError, match="layout is missing"):
        load(json.dumps(raw).encode())
