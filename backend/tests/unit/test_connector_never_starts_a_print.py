"""The connector only files work; the ordinary scheduler decides when it runs (OrcaBamDude plan r2 AC44).

A connector entry goes through the two queue writers and nothing else. It never dispatches, never clears the plate,
startup or maintenance gates, and touches the printer manager only to read a status for an observation. This test
reads the connector's modules (AST, so an alias or a local import is seen too) and fails on anything else.
"""

from __future__ import annotations

import ast
from pathlib import Path

APP = Path(__file__).resolve().parents[2] / "app"
MODULES = (
    APP / "api" / "routes" / "connector.py",
    APP / "services" / "connector_submissions.py",
    APP / "services" / "connector_runtime.py",
)
# The layer that starts, gates or controls a print: none of it may be reached from the connector.
FORBIDDEN = (
    "backend.app.services.background_dispatch",
    "backend.app.services.print_scheduler",
    "backend.app.services.farm_startup_guard",
    "backend.app.services.auto_queue_scheduler",
    "backend.app.services.preheat",
    "backend.app.services.macro_executor",
    "backend.app.services.print_state_recovery",
    "backend.app.services.print_state_recovery_store",
    "backend.app.services.queue_ops",
)
# What an observation may ask the printer manager: a status and the client generation, nothing that sends.
PRINTER_MANAGER_READS = {"get_status", "current_client_generation"}


def _tree(path: Path) -> ast.AST:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _imports(tree: ast.AST) -> set[str]:
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
            names.update(f"{node.module}.{alias.name}" for alias in node.names)
    return names


def test_the_connector_imports_nothing_that_starts_or_gates_a_print():
    for path in MODULES:
        imported = _imports(_tree(path))
        reached = sorted(name for name in imported for forbidden in FORBIDDEN if name.startswith(forbidden))
        assert reached == [], f"{path.name} reaches {reached}"


def test_the_connector_only_reads_the_printer_manager():
    for path in MODULES:
        used = {
            node.attr
            for node in ast.walk(_tree(path))
            if isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == "printer_manager"
        }
        assert used <= PRINTER_MANAGER_READS, f"{path.name} uses printer_manager.{sorted(used - PRINTER_MANAGER_READS)}"


def test_the_guard_sees_a_forbidden_import_and_a_command():
    # The guard itself: a module that dispatches or publishes through the manager is caught.
    source = (
        "from backend.app.services.background_dispatch import background_dispatch\n"
        "def go():\n    printer_manager.start_print(1)\n"
    )
    tree = ast.parse(source)
    assert any(name.startswith(FORBIDDEN[0]) for name in _imports(tree))
    used = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name)}
    assert used - PRINTER_MANAGER_READS == {"start_print"}
