"""The AutoQueue printer order has ONE reader (C01, spec upstream-c01-printer-pick-order §1).

The router, the wake step, the rebalancer's receivers and its replay must pick printers in the
same order; a second reader of the setting or of a printer's priority would let one of them
drift. Only ``services/printer_pick_order.py`` reads them for routing; the schemas, the migration and Git
restore's key list name them for their own reasons (the model's column is a declaration, which the scan
does not count; the settings and printer routes write through the schemas and name neither).
"""

import ast
from pathlib import Path

APP = Path(__file__).resolve().parents[2] / "app"
NAMES = {"model_dispatch_order", "dispatch_priority"}

ALLOWED: dict[str, str] = {
    "services/printer_pick_order.py": "THE reader",
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
    """Every exemption still names the setting or the column (acceptance r1 A1-03): an entry that names neither
    would let a later routing reader in that file pass unseen."""
    for rel in ALLOWED:
        path = APP / rel
        assert path.is_file(), rel
        assert _mentions(ast.parse(path.read_text(encoding="utf-8"))), f"{rel} no longer names the order: drop it"
