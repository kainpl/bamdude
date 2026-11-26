"""Who may touch a source's swap declaration (swap-source-lifecycle spec r4 §3; plan T1, closed in T8).

The archive column ``swap_compatible`` is a projection of the record ``extra_data["swap_source"]``; no macro or
plate-clear gate may read it, and only ``services/swap_compatibility`` writes either. A library row's column is
the projection of its hash-bound carrier, and every ingest door writes both through the same module
(``declare_ingest`` / ``scan_declaration``, spec r3 §8, plan T9.3); decisions go through ``library_fact``.

The scan finds every touch of the fact in ``backend/app`` -- an attribute (ORM read, assignment, SELECT of the
column), a keyword (constructor, response), a string (``getattr`` / ``setattr`` / a dict key) -- per function.
Each one is either ALLOWED, in one of the plan's categories with its reason, or PENDING, naming the task that
replaces it. A new touch fails; a pending one that is gone fails until it leaves the list, so the list only
shrinks. T3-T7 emptied it; a touch that appears now belongs in ALLOWED with its category, or nowhere.
"""

from __future__ import annotations

import ast
import functools
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
APP = ROOT / "backend" / "app"
OWNER = "backend/app/services/swap_compatibility.py"

#: Permanent touches, each in one of the plan's categories (the model and the frozen migrations are not scanned):
#: this module and a declared response transport. The ingest doors write through the module since T9.3. Each entry
#: names its category.
ALLOWED: dict[tuple[str, str, str], str] = {
    ("backend/app/services/library_helpers.py", "sync_system_tags", "kw"): (
        "declared response transport: the row's declaration, through library_fact, shown as its 'swap' tag"
    ),
}

#: Touches that predate the policy module. Each names the task that replaces it with the reader/writer, or moves
#: it to ALLOWED in one of the categories above.
PENDING: dict[tuple[str, str, str], str] = {}


class _Touches(ast.NodeVisitor):
    def __init__(self, rel: str):
        self.rel = rel
        self.stack: list[str] = []
        self.found: set[tuple[str, str, str]] = set()
        self.carrier: list[str] = []

    def _scope(self, node):
        self.stack.append(node.name)
        self.generic_visit(node)
        self.stack.pop()

    visit_FunctionDef = visit_AsyncFunctionDef = visit_ClassDef = _scope

    def _add(self, kind: str) -> None:
        self.found.add((self.rel, ".".join(self.stack) or "<module>", kind))

    def visit_Attribute(self, node):
        if node.attr == "swap_compatible":
            self._add("attr")
        self.generic_visit(node)

    def visit_keyword(self, node):
        if node.arg == "swap_compatible":
            self._add("kw")
        self.generic_visit(node)

    def visit_Constant(self, node):
        if node.value == "swap_compatible":
            self._add("str")
        elif node.value == "swap_source":
            self.carrier.append(f"{self.rel}:{node.lineno}")


@functools.cache
def _touches() -> tuple[frozenset[tuple[str, str, str]], tuple[str, ...]]:
    """One scan per process for the four tests below (the tree does not change during a run);
    immutable, so no test can edit another's view."""
    found: set[tuple[str, str, str]] = set()
    carrier: list[str] = []
    for path in sorted(APP.rglob("*.py")):
        rel = path.relative_to(ROOT).as_posix()
        if "/migrations/" in rel or rel == OWNER:
            continue
        visitor = _Touches(rel)
        visitor.visit(ast.parse(path.read_text(encoding="utf-8")))
        found |= visitor.found
        carrier += visitor.carrier
    return frozenset(found), tuple(carrier)


def test_every_touch_of_the_fact_is_allowed_or_pending():
    found, _ = _touches()
    unexpected = sorted(found - set(ALLOWED) - set(PENDING))
    assert not unexpected, f"read or write the swap fact through services/swap_compatibility: {unexpected}"


def test_a_pending_touch_that_is_gone_leaves_the_list():
    found, _ = _touches()
    stale = sorted(set(PENDING) - found)
    assert not stale, f"replaced: remove from PENDING: {stale}"


def test_an_allowed_touch_that_is_gone_leaves_the_list():
    found, _ = _touches()
    stale = sorted(set(ALLOWED) - found)
    assert not stale, f"no longer used: remove from ALLOWED: {stale}"


def test_the_reserved_record_key_is_only_the_writers():
    _, carrier = _touches()
    assert not carrier, f"extra_data['swap_source'] belongs to services/swap_compatibility: {carrier}"


def test_nothing_is_left_pending():
    """Emptied by T3-T7: every touch is now the policy module's, or ALLOWED in a category. A new one goes there."""
    assert not PENDING


# --- T9: a source's declaration and a job's incarnation (swap-source-declaration spec r3 §3) -----------------------

#: The new columns, touched -- attribute, keyword, string -- only by the policy module (and the models / migrations
#: that declare them). A later step that needs one names it here with its category.
_DECLARATION_NAMES = ("swap_source_declaration", "swap_declaration_identity")
_DECLARING_MODELS = {
    "backend/app/models/archive.py",
    "backend/app/models/library.py",
    "backend/app/models/print_queue.py",
    "backend/app/models/auto_queue.py",
}
ALLOWED_DECLARATION: dict[tuple[str, str, str], str] = {}


class _DeclarationTouches(ast.NodeVisitor):
    def __init__(self, rel: str):
        self.rel = rel
        self.stack: list[str] = []
        self.found: set[tuple[str, str, str]] = set()

    def _scope(self, node):
        self.stack.append(node.name)
        self.generic_visit(node)
        self.stack.pop()

    visit_FunctionDef = visit_AsyncFunctionDef = visit_ClassDef = _scope

    def _add(self, kind: str) -> None:
        self.found.add((self.rel, ".".join(self.stack) or "<module>", kind))

    def visit_Attribute(self, node):
        if node.attr in _DECLARATION_NAMES:
            self._add("attr")
        self.generic_visit(node)

    def visit_keyword(self, node):
        if node.arg in _DECLARATION_NAMES:
            self._add("kw")
        self.generic_visit(node)

    def visit_Constant(self, node):
        if isinstance(node.value, str) and node.value in _DECLARATION_NAMES:
            self._add("str")


def test_a_sources_declaration_and_a_jobs_incarnation_have_one_owner():
    found: set[tuple[str, str, str]] = set()
    for path in sorted(APP.rglob("*.py")):
        rel = path.relative_to(ROOT).as_posix()
        if "/migrations/" in rel or rel == OWNER or rel in _DECLARING_MODELS:
            continue
        visitor = _DeclarationTouches(rel)
        visitor.visit(ast.parse(path.read_text(encoding="utf-8")))
        found |= visitor.found
    unexpected = sorted(found - set(ALLOWED_DECLARATION))
    assert not unexpected, f"a source's declaration goes through services/swap_compatibility: {unexpected}"
    stale = sorted(set(ALLOWED_DECLARATION) - found)
    assert not stale, f"no longer used: remove from ALLOWED_DECLARATION: {stale}"
