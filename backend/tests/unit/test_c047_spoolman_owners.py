"""GUARD (C047 part 2): who may write the Spoolman source, delete a Spoolman spool, charge it.

Spec ``60-specs/c047-spool-id-never-reissued-spec.md`` (vault), §C1–§C5. A new
caller is a decision — each list below says why its members are there.
"""

from __future__ import annotations

import ast
import inspect
import re
from pathlib import Path

from backend.app.services import spoolman_tracking, spoolman_usage_history

APP = Path(__file__).resolve().parents[2] / "app"


def _calls(name: str) -> dict[str, int]:
    """``{relative path: count}`` of calls of ``<anything>.name(`` or ``name(`` under backend/app."""
    found: dict[str, int] = {}
    for path in APP.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        count = 0
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                called = (
                    func.attr if isinstance(func, ast.Attribute) else func.id if isinstance(func, ast.Name) else None
                )
                if called == name:
                    count += 1
        if count:
            found[path.relative_to(APP).as_posix()] = count
    return found


def test_only_owners_persist_the_history_source():
    """The JSON record is written by owners under the source guard (S39): never by a reader."""
    assert set(_calls("persist_source")) == {
        # keep_previous, rotate and the owner-only ``source_token`` helper
        "services/spoolman_usage_history.py",
        # a print's accounting tail, admitted before its first write (S40)
        "services/spoolman_charge.py",
        # Git's SPOOLS category, inside its own transaction (S29, S30)
        "services/git_restore.py",
    }


def test_no_production_reader_names_the_source_by_writing():
    """``source_token`` persists: a reader asks ``current_token`` (S39)."""
    assert set(_calls("source_token")) <= {"services/spoolman_usage_history.py"}


def test_the_history_writer_takes_the_charge_token():
    """``record_print`` never derives its source from the Settings of now (S33)."""
    source = inspect.getsource(spoolman_usage_history.record_print)
    assert "source_token(" not in source and "current_token(" not in source
    assert "source_token=charges.source_token" in inspect.getsource(spoolman_tracking._record_usage_history)


def test_one_door_deletes_a_spoolman_spool():
    """Single and bulk delete go through ``services/spoolman_delete`` (§C): only it sends the DELETE."""
    senders = {
        path.relative_to(APP).as_posix()
        for path in APP.rglob("*.py")
        if re.search(r"client\.delete_spool\(", path.read_text(encoding="utf-8"))
    }
    assert senders == {"services/spoolman_delete.py"}
    routes = (APP / "api" / "routes" / "spoolman_inventory.py").read_text(encoding="utf-8")
    assert routes.count("spoolman_delete.delete_spool(") == 1 and routes.count("spoolman_delete.delete_many(") == 1


def test_the_accounting_tail_charges_inside_its_ownership():
    """Every charge path takes its client from the charge (S33, S36)."""
    for fn in (
        spoolman_tracking._report_usage,
        spoolman_tracking._report_partial_usage,
        spoolman_tracking._cleanup_tracking,
    ):
        source = inspect.getsource(fn)
        assert "_get_spoolman_client_with_fallback(charge)" in source, fn.__name__
        assert "get_spoolman_client(" not in source and "init_spoolman_client(" not in source, fn.__name__
