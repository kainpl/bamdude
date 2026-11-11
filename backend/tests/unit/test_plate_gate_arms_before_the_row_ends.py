"""A run-bound plate gate is armed before its queue row turns terminal (spec plate-gate-owner-requeue A3, T12).

The Retry doors read the gate without locking the printer: a row they see finished must
already have its gate in the database. That holds only while every place that arms a
run-bound gate does so before the same run's row is written terminal.
"""

import ast
import inspect
from pathlib import Path

from backend.app import main
from backend.app.services import print_reconciliation

APP_DIR = Path(main.__file__).resolve().parent


def _code_only(src: str) -> str:
    return "\n".join(line.split("#", 1)[0] for line in src.splitlines())


def test_only_the_two_finishers_arm_a_run_bound_gate():
    callers = set()
    for path in APP_DIR.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (
                isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name != "arm_awaiting_plate_clear"
                and any(
                    isinstance(call, ast.Call)
                    and isinstance(call.func, ast.Attribute)
                    and call.func.attr == "arm_awaiting_plate_clear"
                    for call in ast.walk(node)
                )
            ):
                callers.add((path.relative_to(APP_DIR).as_posix(), node.name))
    # A new caller must show here that it, too, arms before its row turns terminal.
    assert callers == {
        ("main.py", "_on_print_complete_impl"),
        ("services/print_reconciliation.py", "_reconcile_complete_archive"),
    }


def test_the_live_finisher_arms_before_the_row_is_written_terminal():
    source = _code_only(inspect.getsource(main._on_print_complete_impl))
    assert source.index("arm_awaiting_plate_clear(") < source.index("queue_item.status = queue_status")


def test_the_reconciliation_arms_before_the_row_is_written_terminal():
    source = _code_only(inspect.getsource(print_reconciliation._reconcile_complete_archive))
    assert source.index("arm_awaiting_plate_clear(") < source.index("item.status = status")
