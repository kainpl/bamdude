"""Only services/spoolman_usage_history.py writes spoolman_usage_history (m228).

The rows carry a source token and the spool's registration stamp; a second
writer would have to get both right, and a row without them attaches history
to whichever spool later holds the id.
"""

import ast
from pathlib import Path

APP = Path(__file__).resolve().parents[2] / "app"
ALLOWED = {APP / "services" / "spoolman_usage_history.py", APP / "models" / "spoolman_usage_history.py"}
MODEL = "SpoolmanUsageHistory"


def _writes(tree: ast.AST) -> list[int]:
    hits = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else None
        if name == MODEL:
            hits.append(node.lineno)
        elif name in {"insert", "update", "delete"} and node.args:
            first = node.args[0]
            if isinstance(first, ast.Name) and first.id == MODEL:
                hits.append(node.lineno)
    return hits


def test_nothing_but_the_writer_writes_the_spoolman_usage_history():
    offenders = []
    for path in APP.rglob("*.py"):
        if path in ALLOWED or "migrations" in path.parts:
            continue
        for line in _writes(ast.parse(path.read_text(encoding="utf-8"))):
            offenders.append(f"{path.relative_to(APP)}:{line}")
    assert offenders == []


def test_the_scan_sees_a_writer_when_there_is_one():
    # A guard that could never fail proves nothing.
    tree = ast.parse(f"db.add({MODEL}(source_token='x'))\nawait db.execute(delete({MODEL}))\n")
    assert _writes(tree) == [1, 2]


def test_the_charge_writer_observes_the_charge_in_one_place():
    """``_PrintCost.add`` is the one observation point; ``_settle_charges`` the one flush."""
    source = (APP / "services" / "spoolman_tracking.py").read_text(encoding="utf-8")
    assert source.count("spoolman_usage_history.record_print(") == 1
    assert source.count("await _record_usage_history(") == 1
    assert source.count("self.observed.append(") == 1
