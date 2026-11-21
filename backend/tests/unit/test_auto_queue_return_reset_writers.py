"""New reset writers must explicitly preserve irreversible return facts."""

import ast
import re
from pathlib import Path

FIELDS = {"assigned_at", "assigned_to_item_id", "failed_return_count", "failed_return_proof"}


def resets(source):
    found = set()

    class Scan(ast.NodeVisitor):
        def __init__(self):
            self.functions = []

        def visit_FunctionDef(self, node):
            self.functions.append(node.name)
            self.generic_visit(node)
            self.functions.pop()

        visit_AsyncFunctionDef = visit_FunctionDef

        def record(self, name, value):
            if name in FIELDS and isinstance(value, ast.Constant) and value.value in (None, 0):
                found.add((".".join(self.functions), name))

        def visit_Assign(self, node):
            for target in node.targets:
                if isinstance(target, ast.Attribute):
                    self.record(target.attr, node.value)
            self.generic_visit(node)

        def visit_AnnAssign(self, node):
            if isinstance(node.target, ast.Attribute):
                self.record(node.target.attr, node.value)
            self.generic_visit(node)

        def visit_Call(self, node):
            if isinstance(node.func, ast.Attribute) and node.func.attr == "values":
                for keyword in node.keywords:
                    self.record(keyword.arg, keyword.value)
            if isinstance(node.func, ast.Name) and node.func.id == "setattr" and len(node.args) == 3:
                if isinstance(node.args[1], ast.Constant):
                    self.record(node.args[1].value, node.args[2])
            self.generic_visit(node)

        def visit_Constant(self, node):
            if not isinstance(node.value, str):
                return
            sql = node.value.replace('"', "").replace("`", "")
            if not re.search(r"\bUPDATE\s+auto_queue_items\b.*?\bSET\b", sql, re.I | re.S):
                return
            for field in FIELDS:
                if re.search(rf"\b{field}\s*=\s*(NULL|0)\b", sql, re.I):
                    found.add((".".join(self.functions), field))

    Scan().visit(ast.parse(source))
    return found


def test_auto_assignment_reset_writers_are_reviewed():
    app = Path(__file__).resolve().parents[2] / "app"
    observed = {
        (path.relative_to(app).as_posix(), function, field)
        for path in app.rglob("*.py")
        for function, field in resets(path.read_text(encoding="utf-8-sig"))
    }
    assert observed == {
        ("services/queue_auto_transfer.py", "apply", "assigned_at"),
        ("services/queue_auto_transfer.py", "apply", "assigned_to_item_id"),
        ("services/auto_queue_return.py", "return_ready", "assigned_at"),
        ("services/auto_queue_return.py", "return_ready", "assigned_to_item_id"),
        # A proven unsent return resets placement and advances its separate unsent count.
        ("services/auto_queue_unsent_return.py", "_build_return", "assigned_at"),
        ("services/auto_queue_unsent_return.py", "_build_return", "assigned_to_item_id"),
        # Same as FK SET NULL on PG: navigation only. Assignment time and
        # irreversible return facts/count survive; routing still requires pending.
        ("services/queue_counters.py", "detach_print_queue_refs", "assigned_to_item_id"),
    }, "Review a new reset writer: dispatched-return proof and count must survive"


def test_scan_catches_direct_setattr_and_sql_value_resets():
    assert resets(
        "async def unknown(row, stmt):\n"
        " row.failed_return_count = 0\n"
        " setattr(row, 'failed_return_proof', None)\n"
        " stmt.values(assigned_at=None, assigned_to_item_id=None)\n"
    ) == {
        ("unknown", "failed_return_count"),
        ("unknown", "failed_return_proof"),
        ("unknown", "assigned_at"),
        ("unknown", "assigned_to_item_id"),
    }


def test_scan_catches_literal_bulk_sql_resets():
    assert resets(
        'def unknown(db):\n db.execute("UPDATE auto_queue_items SET failed_return_proof=NULL, assigned_at=NULL")\n'
    ) == {("unknown", "failed_return_proof"), ("unknown", "assigned_at")}
