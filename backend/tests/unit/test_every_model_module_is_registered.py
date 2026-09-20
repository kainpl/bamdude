"""Every model module is named by ``import_all_models``.

``create_all`` emits DDL only for tables the mapper has seen, and it sees a
table only once the module defining it has been imported. So a model module
nobody imports is a table that does not exist — in the application and, until
this list became one list, in the test harness alone, which is worse: the
statement is fine in production and fails under test with "no such table".

This is a directory listing against a parsed import statement rather than a
runtime check, because the failure has to name the module the author forgot.
"""

import ast
from pathlib import Path

import pytest

MODELS_DIR = Path(__file__).resolve().parents[3] / "backend" / "app" / "models"
DATABASE_PY = Path(__file__).resolve().parents[3] / "backend" / "app" / "core" / "database.py"


def _modules_on_disk() -> set[str]:
    """Every model module a developer could have added. ``__init__`` and private
    modules are excluded — the first is the package, the second is not a table."""
    return {p.stem for p in MODELS_DIR.glob("*.py") if not p.stem.startswith("_")}


def _modules_named_by_import_all_models() -> set[str]:
    """The names in the ``from backend.app.models import (...)`` statement inside
    ``import_all_models`` — read with ``ast`` so importing nothing is required."""
    tree = ast.parse(DATABASE_PY.read_text(encoding="utf-8"))
    functions = [
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name == "import_all_models"
    ]
    assert len(functions) == 1, "core/database.py must define exactly one import_all_models"

    imports = [
        node
        for node in ast.walk(functions[0])
        if isinstance(node, ast.ImportFrom) and node.module == "backend.app.models"
    ]
    assert len(imports) == 1, "import_all_models must carry exactly one `from backend.app.models import (...)`"
    return {alias.name for alias in imports[0].names}


def test_import_all_models_names_every_model_module():
    on_disk = _modules_on_disk()
    imported = _modules_named_by_import_all_models()

    missing = sorted(on_disk - imported)
    stale = sorted(imported - on_disk)
    assert not missing, (
        f"model module(s) {missing} exist under backend/app/models/ but are not imported by "
        "core/database.py::import_all_models — their tables reach neither create_all nor the "
        "test database. Add each name to that import."
    )
    assert not stale, (
        f"core/database.py::import_all_models imports {stale}, which no longer exist under "
        "backend/app/models/ — drop the name."
    )


def test_calling_import_all_models_puts_the_tables_on_the_metadata():
    """The three that motivated the single list: ``printer_tags`` reached the test
    metadata only through a runtime import inside ``models/printer.py``,
    ``hms_muted_entries`` (m163) was missing from the test copy altogether, and
    ``printer_queues`` is what the queue tests fail on when a copy drifts."""
    from backend.app.core.database import Base, import_all_models

    import_all_models()

    for table in ("printer_tags", "hms_muted_entries", "printer_queues"):
        assert table in Base.metadata.tables, f"{table} is not registered after import_all_models()"


@pytest.mark.parametrize("path", [DATABASE_PY, MODELS_DIR])
def test_the_paths_this_guard_reads_exist(path: Path):
    """A guard that silently reads nothing passes forever. Pin the paths."""
    assert path.exists(), f"{path} moved — this drift guard is reading the wrong place"
