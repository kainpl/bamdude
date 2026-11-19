"""Only services/wear_cost.py writes print_archives.wear_cost / wear_rate.

The figure is a fact of the run, stamped once from a measured end. A second writer is how a
close without a measured end (reconciliation, an operator's close) would start charging wear.
"""

import ast
from pathlib import Path

APP = Path(__file__).resolve().parents[2] / "app"
ALLOWED = {APP / "services" / "wear_cost.py"}
COLUMNS = {"wear_cost", "wear_rate"}
#: Calls whose keyword arguments are column writes: the ORM constructor and Core ``.values()``.
_ROW_WRITERS = {"PrintArchive", "values"}


def _call_name(node: ast.Call) -> str | None:
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _writes(tree: ast.AST) -> list[int]:
    hits = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, (ast.AugAssign, ast.AnnAssign)):
            targets = [node.target]
        else:
            targets = []
        for target in targets:
            for leaf in ast.walk(target):
                if isinstance(leaf, ast.Attribute) and leaf.attr in COLUMNS:
                    hits.append(node.lineno)
        if not isinstance(node, ast.Call):
            continue
        name = _call_name(node)
        if name == "setattr" and len(node.args) >= 2:
            key = node.args[1]
            if isinstance(key, ast.Constant) and key.value in COLUMNS:
                hits.append(node.lineno)
        elif name in _ROW_WRITERS:
            if any(kw.arg in COLUMNS for kw in node.keywords):
                hits.append(node.lineno)
            for arg in node.args:
                if isinstance(arg, ast.Dict) and any(
                    (isinstance(k, ast.Constant) and k.value in COLUMNS)
                    or (isinstance(k, ast.Attribute) and k.attr in COLUMNS)
                    for k in arg.keys
                ):
                    hits.append(node.lineno)
    return hits


def test_nothing_but_the_wear_service_writes_the_figure():
    offenders = []
    for path in APP.rglob("*.py"):
        if path in ALLOWED or "migrations" in path.parts:
            continue
        for line in _writes(ast.parse(path.read_text(encoding="utf-8"))):
            offenders.append(f"{path.relative_to(APP)}:{line}")
    assert offenders == []


def test_the_wear_service_is_seen_as_a_writer():
    # The allowlisted file must really be where the writes are, or the allowlist guards nothing.
    (path,) = ALLOWED
    assert _writes(ast.parse(path.read_text(encoding="utf-8")))


def test_the_scan_sees_a_writer_when_there_is_one():
    # A guard that could never fail proves nothing.
    source = "\n".join(
        [
            "archive.wear_cost = 1.0",
            "archive.wear_rate += 1",
            "setattr(archive, 'wear_cost', 2)",
            "db.add(PrintArchive(filename='f', wear_cost=1.0))",
            "await db.execute(update(PrintArchive).values(wear_rate=None))",
            "await db.execute(update(PrintArchive).values({PrintArchive.wear_cost: None}))",
            "row = {'wear_cost': archive.wear_cost}",  # a read into a response: not a write
            "wear_cost: float | None = None",  # a schema or model field: not a write
            "Response(wear_cost=archive.wear_cost)",  # a reader's keyword: not a write
        ]
    )
    assert _writes(ast.parse(source)) == [1, 2, 3, 4, 5, 6]


def test_only_the_live_completion_reaches_the_stamp():
    """Every other closer -- reconciliation, the operator's close, the terminal auto-repair, the
    dispatcher's «cancelled before start» -- gets no figure because it never calls the writer."""
    stampers, wear_callers = set(), set()
    for path in APP.rglob("*.py"):
        if path.name == "wear_cost.py":
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.Call):
                continue
            name = getattr(node.func, "attr", getattr(node.func, "id", None))
            if name == "stamp_measured_end":
                stampers.add(path.relative_to(APP).as_posix())
            if any(kw.arg in ("wear_printer_id", "wear_record_holds") for kw in node.keywords):
                wear_callers.add(path.relative_to(APP).as_posix())
    assert stampers == {"main.py", "services/archive.py"}
    assert wear_callers == {"main.py"}
