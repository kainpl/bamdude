"""Every path that creates a queue item records who created it.

``PrintQueueItem.created_by_id`` is what ``queue:read_own`` / ``queue:update_own``
/ ``queue:delete_own`` filter on. A path that leaves it NULL does not merely lose
a label — it makes the item **invisible** to the person who queued it, if their
permissions are scoped to their own work. Upstream found exactly that on its
bulk "Add to queue", the one path built for adding many files at once, which is
where it was hardest to notice (9beb001a).

Ours is spread across services rather than inlined in the routes, so the guard
is over the construction sites themselves: adding a new one without the field is
the mistake worth catching, and it is not the kind of thing a feature test for
that path would ever look at.

⚠️ **One documented exception: the virtual printer.** A ``VirtualPrinter``
carries no owner and the obvious substitute is wrong rather than incomplete —
one admin configures the VP while everyone slices through it, so crediting those
items to the admin would make the "added by" column lie and put other people's
jobs in the admin's own queue. Upstream leaves it ownerless for the same reason.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

_BACKEND = pathlib.Path(__file__).resolve().parents[2] / "app"

# Path (relative to backend/app) → why this site does not set the field.
_ALLOWED_OWNERLESS: dict[str, str] = {
    "services/virtual_printer/manager.py": "a VirtualPrinter has no owner; see the comment at the call",
}


def _construction_sites() -> list[tuple[str, int, ast.Call]]:
    """Every ``PrintQueueItem(...)`` built anywhere under ``backend/app``."""
    found: list[tuple[str, int, ast.Call]] = []
    for path in _BACKEND.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "PrintQueueItem"
                # The model's own class statement is not a construction.
                and node.keywords
            ):
                found.append((path.relative_to(_BACKEND).as_posix(), node.lineno, node))
    return found


def test_there_are_construction_sites_to_check():
    """A guard that found nothing would pass forever after a rename."""
    assert len(_construction_sites()) >= 5


@pytest.mark.parametrize(
    ("where", "line", "call"),
    [(w, ln, c) for w, ln, c in _construction_sites()],
    ids=lambda value: f"{value}" if isinstance(value, str) else "",
)
def test_every_site_records_who_queued_it(where, line, call):
    names = {kw.arg for kw in call.keywords}

    if "created_by_id" in names:
        return

    reason = _ALLOWED_OWNERLESS.get(where)
    assert reason is not None, (
        f"{where}:{line} builds a PrintQueueItem without created_by_id. "
        "queue:read_own filters on it, so the item would be invisible to whoever "
        "queued it. Either pass it, or add this path to _ALLOWED_OWNERLESS with "
        "the reason it genuinely has no owner."
    )
