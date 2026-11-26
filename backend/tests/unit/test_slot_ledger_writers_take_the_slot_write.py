"""Every row of a slot ledger is written under a slot-ledger write (C047 acceptance A08).

``spool_assignments`` and ``spoolman_slot_assignments`` hold the active mode only: a change
of the mode empties the ledger it leaves (``spoolman_source_changes.clear_left_ledger``) once
the writers in flight finished — the doors that hold ``spoolman_activity.slot_write`` from
before their mode check to their commit. A row inserted outside one can come back after
the clear, in the other mode's table, where every reader takes it for the active mode's.

The scan finds every INSERT of either ledger under ``backend/app`` and pins it, through the
functions that reach it, to a door that takes the record. A new writer, or a new caller of
one, fails here until it is listed with its door.
"""

from __future__ import annotations

import ast
from pathlib import Path

APP = Path(__file__).resolve().parents[2] / "app"
MODELS = {"SpoolAssignment", "SpoolmanSlotAssignment"}
LEDGER_SQL = ("INSERT INTO spoolman_slot_assignments", "INSERT INTO spool_assignments")

# Where a ledger row is inserted: (file, enclosing function). The ledger module's statements are
# module constants, used by ``write_sync_results`` alone.
INSERTS = {
    ("api/routes/inventory.py", "_assign_spool"),
    ("api/routes/spoolman.py", "link_spool"),
    ("api/routes/spoolman.py", "_create_spool_from_slot"),
    ("api/routes/spoolman_inventory.py", "assign_spoolman_slot"),
    ("services/auto_stock_spool.py", "claim_on_insertion"),
    ("services/spool_tag_matcher.py", "auto_assign_spool"),
    ("services/spoolman_slot_ledger.py", "<module>"),
}

# Every caller of a function that inserts (or leads to one), by name.
CALLERS = {
    "_assign_spool": {("api/routes/inventory.py", "assign_spool")},
    "auto_assign_spool": {("api/routes/inventory.py", "create_spool_from_slot"), ("main.py", "_on_ams_change")},
    "_on_ams_change": {("main.py", "on_ams_change")},
    "claim_on_insertion": {
        ("main.py", "on_stock_spool_inserted"),
        ("services/auto_stock_spool.py", "claim_on_external_resume"),
    },
    "claim_on_external_resume": {("main.py", "_handle_resume_edge")},
    "write_sync_results": {("api/routes/spoolman.py", "_write_and_announce_sync")},
    "_create_spool_from_slot": {("api/routes/spoolman.py", "create_spool_from_slot")},
}

# The doors: each takes the slot-ledger write itself (``admitted_for`` takes it for the manual
# Spoolman assignment).
DOORS = {
    ("api/routes/inventory.py", "assign_spool"),
    ("api/routes/inventory.py", "create_spool_from_slot"),
    ("main.py", "on_ams_change"),
    ("main.py", "on_stock_spool_inserted"),
    ("main.py", "_handle_resume_edge"),
    ("api/routes/spoolman.py", "link_spool"),
    ("api/routes/spoolman.py", "_create_spool_from_slot"),
    ("api/routes/spoolman.py", "_write_and_announce_sync"),
    ("api/routes/spoolman_inventory.py", "assign_spoolman_slot"),
}


def _modules():
    for path in sorted(APP.rglob("*.py")):
        rel = path.relative_to(APP).as_posix()
        yield rel, path.read_text(encoding="utf-8")


def _scoped(tree):
    """Every node with the dotted name of the function or class it sits in."""
    out = []

    def walk(node, stack):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                out.append((child, ".".join(stack) or "<module>"))
                walk(child, [*stack, child.name])
            else:
                out.append((child, ".".join(stack) or "<module>"))
                walk(child, stack)

    walk(tree, [])
    return out


def _called_name(node) -> str | None:
    fn = node.func
    if isinstance(fn, ast.Name):
        return fn.id
    if isinstance(fn, ast.Attribute):
        return fn.attr
    return None


def test_every_insert_of_a_slot_ledger_is_known():
    found = set()
    for rel, source in _modules():
        if rel.startswith(("migrations/", "models/")):
            continue
        for node, scope in _scoped(ast.parse(source)):
            if isinstance(node, ast.Call):
                name = _called_name(node)
                inserted = name in MODELS or (
                    name == "insert" and node.args and isinstance(node.args[0], ast.Name) and node.args[0].id in MODELS
                )
                if inserted:
                    found.add((rel, scope))
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if any(sql in node.value for sql in LEDGER_SQL):
                    found.add((rel, scope))
    assert found == INSERTS, "a new writer of a slot ledger: list it with the door that takes its slot write"


def test_every_writer_is_reached_only_through_its_callers():
    found: dict[str, set] = {name: set() for name in CALLERS}
    for rel, source in _modules():
        for node, scope in _scoped(ast.parse(source)):
            # Direct calls by name: an attribute call of the same name (the printer manager's
            # registered AMS callback) is another function.
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in CALLERS:
                found[node.func.id].add((rel, scope))
    assert found == CALLERS


def test_the_ledger_statements_stay_in_their_module():
    for rel, source in _modules():
        if rel == "services/spoolman_slot_ledger.py":
            continue
        names = {n.id for n in ast.walk(ast.parse(source)) if isinstance(n, ast.Name)}
        assert not names & {"_UPSERT", "_INSERT_IF_STILL_EMPTY"}, rel


def _function_source(rel: str, qualname: str) -> str:
    source = (APP / rel).read_text(encoding="utf-8")
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == qualname.split(".")[-1]:
            return ast.get_source_segment(source, node) or ""
    raise AssertionError(f"{rel}: no function {qualname}")


def test_every_door_takes_the_slot_ledger_write():
    for rel, qualname in sorted(DOORS):
        body = _function_source(rel, qualname)
        assert "slot_write(" in body or "admitted_for(" in body, f"{rel}:{qualname} takes no slot-ledger write"
    assert "slot_write(" in _function_source("services/spoolman_source_changes.py", "admitted_for")
