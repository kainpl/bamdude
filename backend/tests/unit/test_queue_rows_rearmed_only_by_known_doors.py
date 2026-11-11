"""Every writer that puts an existing row back to ``pending`` is known (spec plate-gate-owner-requeue A5).

A row its printer's plate gate waits on may leave its finished state only through the
plate answer: Repeat print re-arms it and releases the gate in one transaction. A Retry
that re-armed it left the gate pointing at an unfinished row and refused every answer
(incident 2026-11-11). A new writer of ``status = "pending"`` fails here until it states
how it relates to the gate.
"""

import ast
from pathlib import Path

from backend.app import main

APP_DIR = Path(main.__file__).resolve().parent

KNOWN = {
    # The plate answer itself: re-arms its own row and releases the gate.
    ("services/plate_hold.py", "_rearm"),
    # Retry doors: refuse a row an armed plate gate waits on (plate_hold.plate_gate_owner_ids).
    ("api/routes/print_queue.py", "retry_failed_item"),
    ("services/queue_bulk_retry.py", "retry_block"),
    # A skipped row never ran: it is not a finished run a gate can wait on.
    ("api/routes/print_queue.py", "unskip_item"),
    # A printing row whose dispatch was never accepted goes back; it is not finished either.
    ("services/interrupted_dispatch.py", "_settle"),
    ("services/print_reconciliation.py", "release_interrupted_dispatch_claims"),
    # An AutoQueue row (``AutoQueueItem``), not a printer queue row.
    ("services/queue_auto_transfer.py", "apply"),
}


class _PendingWriters(ast.NodeVisitor):
    """Collects ``(file, enclosing function)`` for every ``<x>.status = "pending"``."""

    def __init__(self, relative: str, found: set[tuple[str, str]]):
        self.relative, self.found, self.stack = relative, found, []

    def visit_FunctionDef(self, node):
        self.stack.append(node.name)
        self.generic_visit(node)
        self.stack.pop()

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_Assign(self, node):
        if isinstance(node.value, ast.Constant) and node.value.value == "pending":
            for target in node.targets:
                if isinstance(target, ast.Attribute) and target.attr == "status":
                    self.found.add((self.relative, self.stack[-1] if self.stack else "<module>"))
        self.generic_visit(node)


def _writers() -> set[tuple[str, str]]:
    found: set[tuple[str, str]] = set()
    for path in APP_DIR.rglob("*.py"):
        _PendingWriters(path.relative_to(APP_DIR).as_posix(), found).visit(ast.parse(path.read_text(encoding="utf-8")))
    return found


def test_every_writer_that_rearms_a_row_is_known():
    assert _writers() == KNOWN
