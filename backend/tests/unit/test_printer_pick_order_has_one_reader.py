"""The AutoQueue printer order has ONE reader (C01, spec upstream-c01-printer-pick-order §1).

The router, the wake step, the rebalancer's receivers and its replay must pick printers in the
same order; a second reader of the setting or of a printer's priority would let one of them
drift. Only ``services/printer_pick_order.py`` reads them for routing; the column, the schemas,
the migration, the two CRUD routes and Git restore's key list name them for their own reasons.
"""

import ast
from pathlib import Path

APP = Path(__file__).resolve().parents[2] / "app"
NAMES = {"model_dispatch_order", "dispatch_priority"}

ALLOWED: dict[str, str] = {
    "services/printer_pick_order.py": "THE reader",
    "models/printer.py": "the column itself",
    "api/routes/settings.py": "the settings CRUD route",
    "api/routes/printers.py": "the printer CRUD route",
    "services/git_restore.py": "the restorable settings key list",
}


def _mentions(tree: ast.AST) -> list[int]:
    return sorted(
        node.lineno
        for node in ast.walk(tree)
        if (isinstance(node, ast.Attribute) and node.attr in NAMES)
        or (isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value in NAMES)
    )


def test_nobody_else_reads_the_pick_order():
    offenders = []
    for path in APP.rglob("*.py"):
        rel = path.relative_to(APP).as_posix()
        if rel in ALLOWED or rel.startswith("migrations/") or rel.startswith("schemas/"):
            continue
        lines = _mentions(ast.parse(path.read_text(encoding="utf-8")))
        if lines:
            offenders.append(f"{rel}:{lines}")
    assert offenders == [], "read the printer order through services/printer_pick_order.py: " + ", ".join(offenders)


def test_the_allowlist_is_live():
    for rel in ALLOWED:
        assert (APP / rel).is_file(), rel
