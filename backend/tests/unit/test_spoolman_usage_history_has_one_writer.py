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


#: Who may READ the rows, and how each is fenced to Spoolman mode: the routes refuse
#: while Spoolman is off (``_require_spoolman_mode``), the forecast engine asks
#: ``spoolman_is_inventory`` before it picks this source. A new reader is a decision
#: — the built-in inventory must never show a Spoolman spool's prints, nor the reverse.
READERS = {
    APP / "services" / "spoolman_usage_history.py",
    APP / "services" / "spoolman_usage_service.py",
    APP / "services" / "forecast_engine.py",
    APP / "models" / "spoolman_usage_history.py",
    APP / "models" / "__init__.py",
}


def test_the_readers_of_the_spoolman_usage_history_are_a_closed_list():
    offenders = [
        str(path.relative_to(APP))
        for path in APP.rglob("*.py")
        if path not in READERS and "migrations" not in path.parts and MODEL in path.read_text(encoding="utf-8")
    ]
    assert offenders == []


def test_the_git_backup_reaches_the_rows_only_through_the_writers_two_doors():
    """The backup exports and the restore writes through ``export_rows`` /
    ``restore_rows`` of the one writer — neither names the table (the reader
    list above stays closed), and nothing else calls the two doors. They are
    deliberately NOT fenced to Spoolman mode: a backup keeps the rows whatever
    the inventory in use, and shows them to nobody."""
    callers = {"export_rows": [], "restore_rows": []}
    for path in APP.rglob("*.py"):
        if path == APP / "services" / "spoolman_usage_history.py":
            continue
        source = path.read_text(encoding="utf-8")
        for door, found in callers.items():
            found.extend([path.relative_to(APP).as_posix()] * source.count(f"spoolman_usage_history.{door}("))
    assert callers == {"export_rows": ["services/git_backup.py"], "restore_rows": ["services/git_restore.py"]}


def test_every_reader_is_fenced_to_spoolman_mode():
    routes = (APP / "api" / "routes" / "spoolman_usage.py").read_text(encoding="utf-8")
    handlers = routes.count("@router.")
    assert handlers == routes.count("await _require_spoolman_mode(db)") and handlers >= 8

    engine = (APP / "services" / "forecast_engine.py").read_text(encoding="utf-8")
    # The Spoolman source is entered only behind the mode check, in both entry points.
    assert engine.count("if await spoolman_is_inventory(db):") == 2
    # ...and the built-in source never names the Spoolman table.
    internal = engine[engine.index("async def _internal_groups(") : engine.index("class ForecastSourceUnavailable")]
    assert MODEL not in internal and "spoolman" not in internal.lower()
