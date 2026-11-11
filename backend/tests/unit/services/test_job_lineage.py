"""A job's lineage is written in three places only, and never synthesised (host spec r2.2 §11 S2).

- the AutoQueue promotion writes ``print_queue.source_auto_identity`` from the AutoQueue row it promotes;
- the dispatcher stamps an execution archive from the queue row it launches, in both branches, through
  ``job_lineage.stamp_execution`` -- every place that binds an archive to a queue row calls it;
- nothing else assigns these fields: external prints, adoptions, imports, library archives, clones and the transfer
  back into the AutoQueue never invent or copy a lineage.
"""

from __future__ import annotations

import ast
from pathlib import Path

from backend.app.models.archive import PrintArchive
from backend.app.models.print_queue import PrintQueueItem
from backend.app.services import job_lineage

APP = Path(__file__).resolve().parents[3] / "app"
LINEAGE_FIELDS = {"queue_item_identity", "source_auto_identity"}
# Module -> what may write lineage there. Models declare the columns; the helper stamps archives; the scheduler's
# promotion passes source_auto_identity to the queue row it creates.
ALLOWED = {
    "services/job_lineage.py",
    "services/auto_queue_scheduler.py",
}


def _sources():
    for path in sorted(APP.rglob("*.py")):
        rel = path.relative_to(APP).as_posix()
        if rel.startswith(("migrations/", "models/")):
            continue
        yield rel, ast.parse(path.read_text(encoding="utf-8"))


def test_stamp_execution_copies_the_launched_rows_identities():
    row = PrintQueueItem(recovery_identity="q-1", source_auto_identity="u-1")
    archive = PrintArchive()

    job_lineage.stamp_execution(archive, row)

    assert (archive.queue_item_identity, archive.source_auto_identity) == ("q-1", "u-1")


def test_a_direct_job_stamps_no_auto_identity():
    archive = PrintArchive()

    job_lineage.stamp_execution(archive, PrintQueueItem(recovery_identity="q-2"))

    assert (archive.queue_item_identity, archive.source_auto_identity) == ("q-2", None)


def test_only_the_allowed_places_write_lineage():
    writers = set()
    for rel, tree in _sources():
        for node in ast.walk(tree):
            targets = []
            if isinstance(node, ast.Assign):
                targets = node.targets
            elif isinstance(node, ast.AugAssign | ast.AnnAssign):
                targets = [node.target]
            for target in targets:
                if isinstance(target, ast.Attribute) and target.attr in LINEAGE_FIELDS:
                    writers.add(rel)
            if isinstance(node, ast.keyword) and node.arg in LINEAGE_FIELDS:
                writers.add(rel)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "setattr":
                if len(node.args) >= 2 and isinstance(node.args[1], ast.Constant):
                    if node.args[1].value in LINEAGE_FIELDS:
                        writers.add(rel)
    assert writers <= ALLOWED, f"lineage written outside its writers: {sorted(writers - ALLOWED)}"
    assert writers == ALLOWED, f"a lineage writer stopped writing: {sorted(ALLOWED - writers)}"


def test_every_archive_bound_to_a_queue_row_is_stamped():
    """Each ``<row>.archive_id = archive.id`` in the dispatcher sits beside a ``stamp_execution`` call."""
    tree = ast.parse((APP / "services" / "background_dispatch.py").read_text(encoding="utf-8"))
    bindings = 0
    stamps = 0
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Attribute) and t.attr == "archive_id" for t in node.targets
        ):
            if isinstance(node.value, ast.Attribute) and node.value.attr == "id":
                bindings += 1
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "stamp_execution":
            stamps += 1
    assert bindings == 2, "the dispatcher has two archive-creating branches (reprint, library)"
    assert stamps == bindings
