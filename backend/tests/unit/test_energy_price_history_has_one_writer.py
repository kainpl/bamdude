"""Only services/energy_price.py writes energy_price_history (m245).

Energy is costed at the price of its own time, so a row written, changed or removed anywhere else
would re-cost the past -- or leave a change of the price unrecorded.
"""

import ast
from pathlib import Path

APP = Path(__file__).resolve().parents[2] / "app"
ALLOWED = {APP / "services" / "energy_price.py", APP / "models" / "energy_price_history.py"}
MODEL = "EnergyPriceHistory"
TABLE = "energy_price_history"


def _writes(tree: ast.AST) -> list[int]:
    hits = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            name = func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else None
            if name == MODEL:
                hits.append(node.lineno)  # a row built to be added
            elif name in {"insert", "update", "delete"} and node.args:
                first = node.args[0]
                if isinstance(first, ast.Name) and first.id == MODEL:
                    hits.append(node.lineno)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            # Raw SQL that writes the table by name.
            text = " ".join(node.value.lower().split())
            if any(f"{verb} {TABLE}" in text for verb in ("insert into", "update", "delete from")):
                hits.append(node.lineno)
    return hits


def test_nothing_but_the_price_service_writes_the_price_history():
    offenders = []
    for path in APP.rglob("*.py"):
        if path in ALLOWED or "migrations" in path.parts:
            continue
        for line in _writes(ast.parse(path.read_text(encoding="utf-8"))):
            offenders.append(f"{path.relative_to(APP)}:{line}")
    assert offenders == []


def test_the_scan_sees_a_writer_when_there_is_one():
    # A guard that could never fail proves nothing.
    tree = ast.parse(
        "db.add(EnergyPriceHistory(price_per_kwh=1))\n"
        "await db.execute(delete(EnergyPriceHistory))\n"
        "await db.execute(update(EnergyPriceHistory).values(price_per_kwh=2))\n"
        "await db.execute(text('INSERT INTO energy_price_history (price_per_kwh) VALUES (1)'))\n"
        "await db.execute(select(EnergyPriceHistory))\n"
    )
    assert sorted(_writes(tree)) == [1, 2, 3, 4]


def test_the_price_setting_is_written_through_the_seam():
    """``energy_cost_per_kwh`` reaches its row through ``set_setting`` / ``set_price`` -- both record the
    change first. A module that upserts the key by hand would change the price behind the history."""
    offenders = []
    for path in APP.rglob("*.py"):
        if path in ALLOWED or "migrations" in path.parts:
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else None
            literals = [a.value for a in node.args if isinstance(a, ast.Constant) and isinstance(a.value, str)]
            literals += [
                k.value.value
                for k in node.keywords
                if isinstance(k.value, ast.Constant) and isinstance(k.value.value, str)
            ]
            if name in {"upsert_setting", "Settings"} and "energy_cost_per_kwh" in literals:
                offenders.append(f"{path.relative_to(APP)}:{node.lineno}")
    assert offenders == []
