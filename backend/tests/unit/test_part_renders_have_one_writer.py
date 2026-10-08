"""Nothing but services/part_renders.py writes plate_renders / plate_render_objects (spec §15)."""

import ast
from pathlib import Path

APP = Path(__file__).resolve().parents[2] / "app"
ALLOWED = {APP / "services" / "part_renders.py", APP / "models" / "plate_render.py"}
MODELS = {"PlateRender", "PlateRenderObject"}


def _writes(tree: ast.AST) -> list[int]:
    hits = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else None
        if name in MODELS:
            hits.append(node.lineno)
        elif name in {"insert", "update", "delete"} and node.args:
            first = node.args[0]
            target = first.value if isinstance(first, ast.Attribute) and first.attr == "__table__" else first
            if isinstance(target, ast.Name) and target.id in MODELS:
                hits.append(node.lineno)
    return hits


def test_nothing_but_the_writer_writes_the_render_tables():
    offenders = []
    for path in APP.rglob("*.py"):
        if path in ALLOWED or "migrations" in path.parts:
            continue
        for line in _writes(ast.parse(path.read_text(encoding="utf-8"))):
            offenders.append(f"{path.relative_to(APP)}:{line}")
    assert offenders == []


def test_the_scan_sees_a_writer_when_there_is_one():
    tree = ast.parse(
        "db.add(PlateRender(file_sha256='a'))\n"
        "await db.execute(delete(PlateRenderObject))\n"
        "await db.execute(update(PlateRender.__table__))\n"
    )
    assert _writes(tree) == [1, 2, 3]
